# Adding a New Azure Service: Developer Guide

This guide walks through the complete end-to-end process for adding support for a new Azure service to the recommendation pipeline. It covers bronze data collection, silver recommendation generation, right-sizing engine integration, and all configuration needed.

---

## Table of Contents

1. [Overview: What You're Building](#1-overview-what-youre-building)
2. [Prerequisites](#2-prerequisites)
3. [Step 1: Identify Metrics and Billing Data](#step-1-identify-metrics-and-billing-data)
4. [Step 2: Bronze Layer - Data Collection](#step-2-bronze-layer---data-collection)
5. [Step 3: Register the Service in RDS](#step-3-register-the-service-in-rds)
6. [Step 4: Create Threshold Rules](#step-4-create-threshold-rules)
7. [Step 5: Silver Layer - Pricing and Recommendation Generation](#step-5-silver-layer---pricing-and-recommendation-generation)
8. [Step 6: Right-Sizing Engine Integration](#step-6-right-sizing-engine-integration)
9. [Step 7: Testing and Validation](#step-7-testing-and-validation)
10. [Files Modified Checklist](#files-modified-checklist)
11. [Worked Example: Adding Azure SignalR Service](#worked-example-adding-azure-signalr-service)

---

## 1. Overview: What You're Building

Adding a new service means building these three pieces:

```
Bronze Layer                   Silver Layer                    RDS
(Data Collection)              (Recommendations)               (Frontend)

Azure Monitor API ──> bronze_azure_metrics      ──> finops_threshold_rules evaluate
Azure Mgmt API   ──> bronze_azure_{service}         ──> Right-sizing engine calculates savings
                                                        ──> create_recommendation()
                                                            ──> silver_azure_custom_recommendations (Iceberg)
                                                            ──> custom_recommendations (RDS)
```

The system is designed so that **most of the recommendation logic is database-driven**. You write code for data collection (bronze) and pricing (silver), but rule evaluation, savings calculation, and recommendation formatting are handled by the existing framework.

---

## 2. Prerequisites

Before starting, you need:

- Access to the Azure portal to identify the service's Management API and Monitor API endpoints
- An understanding of what metrics indicate underutilization for this service
- The Azure SDK package name for the service (e.g., `azure-mgmt-signalr`)
- Knowledge of how the service is billed (what SKU tiers exist, pricing model)

---

## Step 1: Identify Metrics and Billing Data

### 1.1 Find the Right Metrics

Go to the Azure portal, open a resource of the target service, and check **Monitoring > Metrics**. Identify metrics that indicate:

| Signal | Example Metrics | What They Tell You |
|--------|-----------------|-------------------|
| **Idle/Unused** | Connection count = 0, request count = 0 | Resource can be terminated |
| **Underutilized** | CPU < 30%, memory < 40%, throughput < 20% of capacity | Resource can be downsized |
| **Overprovisioned** | Provisioned capacity >> actual usage | Tier can be downgraded |
| **Cost-inefficient** | Premium features unused, reserved capacity available | Pricing model can be optimized |

Document the metric names exactly as Azure Monitor returns them (e.g., `"ServerLoad"`, `"UsedMemoryPercentage"`, `"ConnectionCount"`).

### 1.2 Identify the Azure Resource Provider

Find the resource provider namespace (e.g., `Microsoft.SignalRService/SignalR`). This is needed for:
- Listing resources via the Management API
- Constructing resource IDs for Monitor API queries

### 1.3 Identify SKU/Tier Structure

Document the service's SKU tiers (e.g., Free, Standard, Premium) and what differentiates them (units, capacity, features). This drives:
- What downsizing recommendations are possible
- How savings are calculated
- What rules to create

### 1.4 Identify Billing Data

Check how the service appears in the Azure FOCUS/billing data:
- What `MeterCategory` value does it use?
- What `armSkuName` values appear?
- Is it billed hourly, monthly, per-unit, or per-transaction?

---

## Step 2: Bronze Layer - Data Collection

**File:** `src/glue/azure/azure_bronze_custom_recom_job.py`

You need to add 4 things to this file.

### 2.1 Create the Service Class

Add a new class following the existing pattern. Every service class has:

```python
class AzureNewService:
    """Collects metrics and resource inventory for Azure New Service."""

    def __init__(self, config, window_days: int = 7):
        self.config = config
        self.window_days = window_days
        self.subscription_id = config.subscription_id

        # Azure SDK credentials
        self.credential = ClientSecretCredential(
            tenant_id=config.tenant_id,
            client_id=config.azure_client_id,
            client_secret=config.client_secret
        )

        # Service-specific management client
        self.service_client = NewServiceManagementClient(
            credential=self.credential,
            subscription_id=self.subscription_id
        )

        # Monitor client (same for all services)
        self.monitor_client = MonitorManagementClient(
            credential=self.credential,
            subscription_id=self.subscription_id
        )
```

### 2.2 Add Metric Fetching

Add a `_get_metric()` method and a `collect_metrics_only()` method:

```python
    def _get_metric(self, resource_id: str, metric_name: str, aggregation: str) -> float:
        """Fetch a single metric from Azure Monitor, averaged over the window."""
        try:
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=self.window_days)
            timespan = f"{start_time.isoformat()}Z/{end_time.isoformat()}Z"

            response = self.monitor_client.metrics.list(
                resource_uri=resource_id,
                timespan=timespan,
                interval="PT1H",
                metricnames=metric_name,
                aggregation=aggregation
            )

            values = []
            for metric in response.value:
                for ts in metric.timeseries:
                    for dp in ts.data:
                        val = getattr(dp, aggregation.lower(), None)
                        if val is not None:
                            values.append(float(val))

            return sum(values) / len(values) if values else 0.0
        except Exception as e:
            logger.warning(f"Failed to get metric {metric_name} for {resource_id}: {e}")
            return 0.0

    async def collect_metrics_only(self) -> tuple:
        """
        Collect resource inventory and metrics.
        Returns (resource_rows, metric_rows) where:
          - resource_rows: list of dicts for the service-specific resource table
          - metric_rows: list of dicts for the shared metrics table
        """
        resource_rows = []
        metric_rows = []
        now = datetime.utcnow()
        year_month = now.strftime("%Y-%m")

        try:
            # List all resources of this type
            resources = self.service_client.resources.list()

            for resource in resources:
                resource_id = resource.id
                resource_name = resource.name
                region = resource.location
                rg_name = resource_id.split("/resourceGroups/")[1].split("/")[0]

                # --- Resource row (service-specific table) ---
                resource_rows.append({
                    "client_id": self.config.client_id,
                    "account_id": self.subscription_id,
                    "resource_id": resource_id,
                    "resource_name": resource_name,
                    "service_name": "Azure New Service",
                    "region": region,
                    "cloud_name": "Azure",
                    "resource_group": rg_name,
                    "sku_name": resource.sku.name if resource.sku else "",
                    "sku_tier": resource.sku.tier if resource.sku else "",
                    # Add service-specific fields here
                    "year_month": year_month,
                    "ingestion_timestamp": now,
                    "job_runtime_utc": now,
                })

                # --- Metric rows (shared metrics table) ---
                metrics_to_collect = {
                    "ConnectionCount": "Average",
                    "MessageCount": "Total",
                    "ServerLoad": "Average",
                }

                for metric_name, aggregation in metrics_to_collect.items():
                    value = self._get_metric(resource_id, metric_name, aggregation)
                    metric_rows.append({
                        "client_id": self.config.client_id,
                        "account_id": self.subscription_id,
                        "resource_id": resource_id,
                        "resource_name": resource_name,
                        "service_name": "Azure New Service",
                        "namespace": "Microsoft.NewService/resources",
                        "date": now.strftime("%Y-%m-%d"),
                        "metric_name": metric_name,
                        "metric_value": value,
                        "unit": "Count",
                        "region": region,
                        "cloud_name": "Azure",
                        "year_month": year_month,
                        "ingestion_timestamp": now,
                        "job_runtime_utc": now,
                    })

        except Exception as e:
            logger.error(f"Failed to collect New Service data: {e}")

        return resource_rows, metric_rows
```

### 2.3 Add Table Configuration

Add entries to the `tables_config` dict (around line 12556):

```python
"newservice": {
    "table_name": f"glue_catalog.{config.iceberg_db}.bronze_azure_newservice",
    "key_columns": ["client_id", "account_id", "resource_id"],
    "partition_cols": ["client_id", "account_id", "year_month"],
    "numeric_cols": ["unit_count"],  # service-specific numeric fields
    "string_cols": [
        "client_id", "account_id", "resource_id", "resource_name",
        "service_name", "region", "cloud_name", "resource_group",
        "sku_name", "sku_tier", "year_month"
    ],
    "date_cols": ["ingestion_timestamp", "job_runtime_utc"]
}
```

Note: The shared `"metrics"` table config already exists and is reused by all services. You do not need to add another one.

### 2.4 Add Dispatch Entry

Add to the service dispatch section (around line 12930):

```python
if 'NEWSERVICE' in active_services:
    asyncio.run(process_newservice(spark, config, tables_config["metrics"], tables_config["newservice"]))
```

And create the `process_newservice()` function following the existing pattern:

```python
async def process_newservice(spark, config, metrics_table_config, resource_table_config):
    """Collect and save Azure New Service data."""
    svc = AzureNewService(config, window_days=config.vmui_window_days)
    resource_rows, metric_rows = await svc.collect_metrics_only()

    if metric_rows:
        save_to_iceberg(spark, metric_rows, metrics_table_config)
    if resource_rows:
        save_to_iceberg(spark, resource_rows, resource_table_config)

    logger.info(f"New Service: {len(resource_rows)} resources, {len(metric_rows)} metric rows")
```

---

## Step 3: Register the Service in RDS

### 3.1 `service_configuration` Table

Insert a row that tells the silver layer where to find the bronze data:

```sql
INSERT INTO service_configuration (
    service_code,
    service_name,
    service_display_name,
    cloud_provider,
    is_active,
    category,
    resource_type,
    metrics_table_suffix,
    resources_table_suffix,
    service_name_filter,
    retail_api_service_name,
    primary_metric,
    sizing_strategy,
    sizing_dimension,
    priority
) VALUES (
    'NEWSERVICE',                          -- service_code (uppercase, used in dispatch)
    'Azure New Service',                   -- service_name (must match bronze service_name)
    'Azure New Service',                   -- display name
    'azure',                               -- cloud_provider
    TRUE,                                  -- is_active
    'Messaging',                           -- category (Compute/Storage/Databases/Networking/Messaging/etc.)
    'NewServiceResource',                  -- resource_type
    'bronze_azure_metrics',                -- metrics_table_suffix (shared metrics table)
    'bronze_azure_newservice',             -- resources_table_suffix (service-specific table)
    'Azure New Service',                   -- service_name_filter (filters metrics table)
    'Azure New Service',                   -- retail_api_service_name (for pricing API)
    'ConnectionCount',                     -- primary_metric
    'downsize',                            -- sizing_strategy
    'tier',                                -- sizing_dimension
    10                                     -- priority (lower = processed first)
);
```

**Key fields explained:**
- `service_name_filter`: Used in `WHERE service_name = '{filter}'` when querying the shared `bronze_azure_metrics` table. Must exactly match the `service_name` value you wrote in the bronze metric rows.
- `metrics_table_suffix`: Always `bronze_azure_metrics` (the shared table).
- `resources_table_suffix`: Your service-specific resource table name (without the catalog/db prefix).

---

## Step 4: Create Threshold Rules

### 4.1 `base_finops_threshold_rules` Table

These rules define **when** a recommendation should fire. Each rule specifies metric conditions and recommendation templates.

```sql
INSERT INTO base_finops_threshold_rules (
    rule_code, service_name, cloud_provider, rule_type,
    evaluation_logic, threshold_config, threshold_config_mapping,
    cost_model, category, severity,
    title, description, recommendation_template,
    estimated_savings_formula, advisor_explanation_template,
    is_active, priority
) VALUES
-- Rule 1: Idle detection (zero connections)
(
    'NEWSERVICE_IDLE',
    'Azure New Service',
    'azure',
    'utilization',
    '{"logic": "AND", "conditions": [
        {"metric": "ConnectionCount", "operator": "lt", "threshold": 1}
    ]}',
    '{"connection_threshold": 1}',
    '{"ConnectionCount": "connection_threshold"}',
    '{"savings_pct": 100}',
    'Messaging',
    'High',
    'Idle {resource_name} - No connections detected',
    'Azure New Service has zero connections over the observation period',
    'Consider terminating idle Azure New Service instance {resource_name}. '
    'No connections detected in the last {observation_days} days. '
    'Estimated annual savings: ${estimated_annual_savings:,.2f}.',
    'total_cost * 1.0',
    'Azure New Service {resource_name} ({current_sku}) has had no connections '
    'for {observation_days} days. Terminating would save ${estimated_annual_savings:,.2f}/year.',
    TRUE,
    1
),
-- Rule 2: Low utilization (underutilized)
(
    'NEWSERVICE_LOW_UTIL',
    'Azure New Service',
    'azure',
    'utilization',
    '{"logic": "AND", "conditions": [
        {"metric": "ServerLoad", "operator": "lt", "threshold": 20},
        {"metric": "ConnectionCount", "operator": "lt", "threshold": 100}
    ]}',
    '{"load_threshold": 20, "connection_threshold": 100}',
    '{"ServerLoad": "load_threshold", "ConnectionCount": "connection_threshold"}',
    '{"savings_pct": 30}',
    'Messaging',
    'Medium',
    'Underutilized {resource_name} - Low server load ({ServerLoad:.1f}%)',
    'Azure New Service instance is underutilized with low server load and connection count',
    'Consider downsizing {resource_name} from {current_sku} to a smaller tier. '
    'Server load avg: {ServerLoad:.1f}%, connections avg: {ConnectionCount:.0f}. '
    'Estimated annual savings: ${estimated_annual_savings:,.2f}.',
    'total_cost * 0.30',
    'Azure New Service {resource_name} ({current_sku}) has avg server load of {ServerLoad:.1f}% '
    'and {ConnectionCount:.0f} connections. Downsizing is recommended.',
    TRUE,
    5
);
```

### 4.2 Template Variables

The `recommendation_template` and `advisor_explanation_template` support these variables:

| Variable | Source | Description |
|----------|--------|-------------|
| `{resource_name}` | resource_data | Resource name |
| `{current_sku}` | resource_data | Current SKU/tier |
| `{target_sku}` | savings_calculator | Recommended SKU (set by engine or fallback) |
| `{observation_days}` | config | Monitoring window |
| `{estimated_annual_savings}` | calculation | Dollar savings |
| `{total_cost}` | billing | Annual resource cost |
| `{MetricName}` | metrics dict | Any metric name used in conditions |
| `{engine_confidence}` | engine result | Engine confidence level (if engine was used) |
| `{engine_rationale}` | engine result | Engine sizing rationale (if engine was used) |

### 4.3 Sync Rules to Clients

After inserting base rules, run the sync job to propagate them:

```bash
aws glue start-job-run \
  --job-name sync-base-rules-to-client \
  --arguments '{"--CLIENT_ID":"your_client_id","--SUBSCRIPTION_ID":"your_sub_id"}'
```

This copies rules from `base_finops_threshold_rules` to `finops_threshold_rules` for each client/subscription pair.

---

## Step 5: Silver Layer - Pricing and Recommendation Generation

**File:** `src/glue/azure/azure_unified_recommendations.py`

### 5.1 Add Pricing kwargs (Required)

In `_process_single_service()`, find the pricing kwargs chain (around line 5225) and add a block for your service:

```python
elif service_code in ("NEWSERVICE",):
    kwargs["sku_name"] = resource_data.get("sku_name", "Standard_S1")
    kwargs["sku_tier"] = resource_data.get("sku_tier", "Standard")
    kwargs["unit_count"] = resource_data.get("unit_count", 1)
```

These kwargs are passed to `GenericPricing.get_annual_cost()` to look up the resource's cost from the billing cache or retail API.

### 5.2 Add Metric Aliases (If Needed)

If the metric names from Azure Monitor differ from the metric names used in your `finops_threshold_rules` conditions, add aliases to the `_METRIC_ALIASES` dict (around line 5451):

```python
_METRIC_ALIASES = {
    # ... existing aliases ...
    "ServerLoad": "server_load",          # Only if your rules use "server_load"
    "ConnectionCount": "connection_count", # Only if your rules use "connection_count"
}
```

If your rule conditions use the exact Azure metric names (recommended), no aliases are needed.

### 5.3 Add a Per-Service Savings Fallback (Optional but Recommended)

In `SavingsCalculator`, add a fallback method that executes when the right-sizing engine returns no result:

```python
def _calculate_newservice_savings(
    self, rule_code: str, total_cost: float,
    resource_data: Dict[str, Any], metrics: Dict[str, float]
) -> float:
    """New Service savings fallback when engine returns no result."""
    rule_upper = rule_code.upper()

    # Idle - full savings
    if "IDLE" in rule_upper:
        resource_data["target_sku"] = "Terminate"
        return total_cost * 1.0

    # Low utilization - downsize
    if "LOW" in rule_upper:
        current_tier = resource_data.get("sku_tier", "Standard")
        if current_tier == "Premium":
            resource_data["target_sku"] = "Standard"
            return total_cost * 0.40
        elif current_tier == "Standard":
            resource_data["target_sku"] = "Free"
            return total_cost * 0.30

    return total_cost * 0.25  # Default fallback
```

Then add the dispatch entry in the per-service fallback chain (around line 1870):

```python
if "NEWSERVICE" in svc:
    return self._calculate_newservice_savings(
        rule_code, total_cost, resource_data, metrics
    )
```

**Important:** This fallback only executes when the right-sizing engine returns no result. With proper engine rules and SKU catalog data, the engine handles most cases.

### 5.4 How the Flow Works (No Other Code Needed)

With the above changes, the existing framework handles everything else automatically:

1. `_process_single_service("NEWSERVICE")` reads `service_configuration` to find table names
2. Loads resources from `bronze_azure_newservice` and metrics from `bronze_azure_metrics`
3. Calls `GenericPricing.get_annual_cost()` with your kwargs to get the resource cost
4. Creates a `BaseAzureServiceRecommender` (no subclass needed)
5. For each resource, builds the combined metrics dict
6. Calls `evaluate_all_rules("Azure New Service", metrics)` to find triggered rules
7. For each triggered rule, calls `calculate_savings()`:
   - **First**: tries the right-sizing engine via the unified path
   - **Fallback**: calls your `_calculate_newservice_savings()` method
8. Calls `create_recommendation()` with the rule template and savings
9. Saves to Iceberg and RDS

---

## Step 6: Right-Sizing Engine Integration

The engine provides pricing-aware savings calculation. To get the best results, configure it for your service.

### 6.1 Add to `provider_config.json`

**File:** `src/s3/scripts/common/config/rightsizing_engine/provider_config.json`

Add your service to the Azure `engine_service_mapping`:

```json
"engine_service_mapping": {
    ...existing entries...
    "NEWSERVICE": {"service_name": "Azure New Service", "service_category": "Messaging"}
}
```

The `service_category` must match one of the categories used in `rs_rightsizing_rules`. Common categories: `Compute`, `Storage`, `Databases`, `Networking`, `Messaging`, `Analytics`, `AI`, `Cache`, `Serverless`, `Security`.

(Optional) If you want the SKU catalog to include pricing for this service, add a `service_filter_templates` entry:

```json
"service_filter_templates": {
    ...existing entries...
    "Azure New Service": {
        "service_name": "Azure New Service",
        "service_category": "Messaging",
        "filter_template": "serviceName eq 'Azure New Service' and armRegionName eq '{region}' and priceType eq 'Consumption'"
    }
}
```

### 6.2 Add Engine Rules

**File:** `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_rightsizing_rules.py`

Add Azure-specific rules to the `DEFAULT_RULES` list. The `service_name` must exactly match the value in `engine_service_mapping`:

```python
# ---------- Azure New Service ----------
{
    "cloud_provider": "azure",
    "service_category": "Messaging",
    "service_name": "Azure New Service",
    "rule_code": "AZURE_NEWSERVICE_IDLE_TERMINATE",
    "action_type": "terminate",
    "sizing_method": "terminate",
    "sizing_params": {},
    "default_savings_pct": 100.0,
    "priority": 1,
},
{
    "cloud_provider": "azure",
    "service_category": "Messaging",
    "service_name": "Azure New Service",
    "rule_code": "AZURE_NEWSERVICE_TIER_DOWNGRADE",
    "action_type": "downsize_tier",
    "sizing_method": "tier_downgrade",
    "sizing_params": {},
    "default_savings_pct": 30.0,
    "priority": 2,
},
```

**Choosing the right `sizing_method`:**

| Your Service Has... | Use `sizing_method` | What It Does |
|---------------------|---------------------|-------------|
| SKU families with sizes (like VMs) | `step_down` | Moves to next smaller SKU in same family |
| Utilization-based sizing | `match_util` | Picks smallest SKU matching actual utilization |
| Tier levels (Free/Standard/Premium) | `tier_downgrade` | Moves to next lower tier |
| Storage type migration | `migrate_type` | Changes storage type (e.g., Premium to Standard) |
| Idle resources | `terminate` | Recommends termination |
| General optimization (no SKU catalog) | `consolidate` | Uses `default_savings_pct` as the savings estimate |

**When to use `consolidate`**: If your service does not have meaningful SKU entries in the `rs_cloud_sku_catalog` table (e.g., the service is billed per-unit or per-transaction rather than per-instance-type), use `consolidate`. The engine will return `current_cost * default_savings_pct / 100` as the savings amount. This matches the behavior of the per-service hardcoded fallback percentages.

### 6.3 Run the Catalog Refresh

After modifying `seed_rightsizing_rules.py` and `provider_config.json`, run the SKU catalog refresh to seed the new rules and (optionally) populate the SKU catalog with pricing data:

```bash
aws glue start-job-run \
  --job-name refresh-sku-catalog \
  --arguments '{"--CLOUD_PROVIDERS":"azure","--ICEBERG_DB":"your_db"}'
```

### 6.4 How the Engine Integration Works at Runtime

When `calculate_savings()` is called for your service:

1. `_resolve_engine_service("NEWSERVICE")` looks up the `engine_service_mapping` in `provider_config.json` and returns `("Azure New Service", "Messaging")`
2. `engine.get_recommendation()` is called with `service_name="Azure New Service"`, `service_category="Messaging"`, and the resource's current SKU, region, and metrics
3. The engine finds applicable rules from `rs_rightsizing_rules` where `cloud_provider="azure"` and either `service_name="Azure New Service"` or `service_category="Messaging"`
4. The engine tries each rule in priority order:
   - For `terminate`: returns 100% savings
   - For `tier_downgrade`: looks up the current SKU in `rs_cloud_sku_catalog`, finds the next lower tier, and calculates price-based savings
   - For `consolidate`: returns `current_cost * default_savings_pct / 100`
5. If the engine returns `estimated_annual_savings > 0`, the unified path stores the full result in `resource_data["_engine_result"]` and returns the savings
6. If the engine returns nothing, the fallback method (`_calculate_newservice_savings`) executes

---

## Step 7: Testing and Validation

### 7.1 Bronze Layer Validation

| Check | How to Verify |
|-------|--------------|
| Resources collected | Query `bronze_azure_newservice`: `SELECT COUNT(*) ...` |
| Metrics collected | Query `bronze_azure_metrics WHERE service_name = 'Azure New Service'` |
| Metric values populated | Verify `metric_value` is non-null and reasonable |
| All expected metrics present | Check all metric names from your `metrics_to_collect` dict appear |

### 7.2 Service Configuration Validation

```sql
-- Verify service is registered
SELECT * FROM service_configuration
WHERE service_code = 'NEWSERVICE' AND cloud_provider = 'azure';

-- Verify threshold rules exist for the client
SELECT rule_code, is_active, evaluation_logic
FROM finops_threshold_rules
WHERE service_name = 'Azure New Service' AND cloud_provider = 'azure';
```

### 7.3 Silver Layer Validation

| Check | How to Verify |
|-------|--------------|
| Rules trigger | Check logs for `"[Azure New Service] Rule NEWSERVICE_IDLE triggered"` |
| Engine called | Check logs for `"[Azure New Service Savings] Engine:"` |
| Fallback used | Check logs for `"falling back to rules"` (means engine had no result) |
| Savings calculated | Verify `savings > 0` in the output |
| Details JSON | Check `details` field contains `engine_*` keys if engine was used |

### 7.4 Engine Validation

```python
# Quick test in a Glue job or notebook
from rightsizing_engine.engine import RightSizingEngine

engine = RightSizingEngine(spark, iceberg_db, cloud_provider='azure')

result = engine.get_recommendation(
    resource_data={
        "resource_id": "test-resource-123",
        "service_name": "Azure New Service",
        "service_category": "Messaging",
        "current_sku": "Standard_S1",
        "region": "eastus",
        "cloud_provider": "azure",
    },
    metrics={"ConnectionCount": 0, "ServerLoad": 5.0},
    current_cost=100.0
)

print(f"Action: {result.recommended_action}")
print(f"Savings: ${result.estimated_annual_savings:,.2f}/yr")
print(f"Method: {result.details.get('sizing_method')}")
print(f"Rule: {result.details.get('rule_code')}")
```

### 7.5 RDS Validation

```sql
-- Verify recommendations landed in RDS
SELECT resource_id, recommendation, savings, rule_code,
       details::jsonb->'engine_confidence' as engine_confidence,
       details::jsonb->'engine_sizing_method' as engine_method
FROM custom_recommendations
WHERE cloud_name = 'Azure'
  AND category = 'Messaging'
  AND rule_code LIKE 'NEWSERVICE%'
ORDER BY savings DESC;
```

---

## Files Modified Checklist

| # | File | What to Add | Required? |
|---|------|-------------|-----------|
| 1 | `src/glue/azure/azure_bronze_custom_recom_job.py` | `AzureNewService` class, `process_newservice()` function, `tables_config` entry, dispatch entry | Yes |
| 2 | `src/glue/azure/azure_unified_recommendations.py` | Pricing kwargs block in `_process_single_service()` | Yes |
| 3 | `src/glue/azure/azure_unified_recommendations.py` | `_calculate_newservice_savings()` method + dispatch entry in `SavingsCalculator` | Recommended |
| 4 | `src/glue/azure/azure_unified_recommendations.py` | Metric aliases in `_METRIC_ALIASES` | Only if metric names differ |
| 5 | `src/s3/scripts/common/config/rightsizing_engine/provider_config.json` | `engine_service_mapping` entry (+ optional `service_filter_templates`) | Yes |
| 6 | `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_rightsizing_rules.py` | Azure-specific engine rules | Yes |
| 7 | RDS: `service_configuration` | New row | Yes |
| 8 | RDS: `base_finops_threshold_rules` | New threshold rules | Yes |
| 9 | Run: `sync_base_rules_to_client_glue.py` | Propagate rules to clients | Yes (run, not modify) |
| 10 | Run: `refresh_sku_catalog_job.py` | Seed engine rules + SKU catalog | Yes (run, not modify) |

---

## Worked Example: Adding Azure SignalR Service

Here is a condensed real-world example showing all the pieces together.

### Metrics Identified

| Metric | Aggregation | Signal |
|--------|-------------|--------|
| `ConnectionCount` | Average | Idle if 0, underutilized if < 100 |
| `MessageCount` | Total | Usage volume |
| `ServerLoad` | Average | CPU-like utilization |

### SKU Tiers

| Tier | Units | Price |
|------|-------|-------|
| Free | 1 | $0/mo |
| Standard_S1 | 1-100 | ~$50/unit/mo |
| Premium_P1 | 1-100 | ~$100/unit/mo |

### Bronze Class (Key Parts)

```python
class AzureSignalRService:
    def __init__(self, config, window_days=7):
        self.signalr_client = SignalRManagementClient(credential=self.credential, subscription_id=self.subscription_id)
        self.monitor_client = MonitorManagementClient(credential=self.credential, subscription_id=self.subscription_id)

    async def collect_metrics_only(self):
        resources = self.signalr_client.signal_r.list_by_subscription()
        for resource in resources:
            resource_rows.append({
                "sku_name": resource.sku.name,   # "Standard_S1"
                "sku_tier": resource.sku.tier,    # "Standard"
                "unit_count": resource.sku.capacity,
                ...
            })
            for metric_name in ["ConnectionCount", "MessageCount", "ServerLoad"]:
                metric_rows.append({
                    "service_name": "Azure SignalR",
                    "metric_name": metric_name,
                    "metric_value": self._get_metric(resource.id, metric_name, "Average"),
                    ...
                })
        return resource_rows, metric_rows
```

### service_configuration Row

```sql
INSERT INTO service_configuration VALUES (
    'SIGNALR', 'Azure SignalR', 'Azure SignalR Service', 'azure', TRUE,
    'Messaging', 'SignalRResource',
    'bronze_azure_metrics', 'bronze_azure_signalr',
    'Azure SignalR', 'Azure SignalR',
    'ConnectionCount', 'downsize', 'tier', 10
);
```

### Threshold Rules

```sql
INSERT INTO base_finops_threshold_rules (...) VALUES
('SIGNALR_IDLE', 'Azure SignalR', 'azure', 'utilization',
 '{"logic":"AND","conditions":[{"metric":"ConnectionCount","operator":"lt","threshold":1}]}',
 ...),
('SIGNALR_LOW_UTIL', 'Azure SignalR', 'azure', 'utilization',
 '{"logic":"AND","conditions":[
     {"metric":"ServerLoad","operator":"lt","threshold":20},
     {"metric":"ConnectionCount","operator":"lt","threshold":100}
 ]}',
 ...);
```

### provider_config.json

```json
"SIGNALR": {"service_name": "Azure SignalR", "service_category": "Messaging"}
```

### Engine Rules

```python
{"cloud_provider": "azure", "service_category": "Messaging", "service_name": "Azure SignalR",
 "rule_code": "AZURE_SIGNALR_IDLE_TERMINATE", "sizing_method": "terminate", "default_savings_pct": 100.0, "priority": 1},
{"cloud_provider": "azure", "service_category": "Messaging", "service_name": "Azure SignalR",
 "rule_code": "AZURE_SIGNALR_TIER_DOWNGRADE", "sizing_method": "tier_downgrade", "default_savings_pct": 30.0, "priority": 2},
```

### Pricing kwargs

```python
elif service_code in ("SIGNALR",):
    kwargs["sku_name"] = resource_data.get("sku_name", "Standard_S1")
    kwargs["sku_tier"] = resource_data.get("sku_tier", "Standard")
    kwargs["unit_count"] = resource_data.get("unit_count", 1)
```

### Savings Fallback

```python
def _calculate_signalr_savings(self, rule_code, total_cost, resource_data, metrics):
    rule_upper = rule_code.upper()
    if "IDLE" in rule_upper:
        resource_data["target_sku"] = "Terminate"
        return total_cost * 1.0
    if "LOW" in rule_upper:
        resource_data["target_sku"] = "Standard_S1 (reduced units)"
        return total_cost * 0.30
    return total_cost * 0.25
```

With all of this in place, the pipeline will automatically collect SignalR metrics in bronze, evaluate rules in silver, call the right-sizing engine for savings, and persist recommendations to both Iceberg and RDS.
