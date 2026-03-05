# Right-Sizing Engine - Testing & Validation Guide

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [How the Right-Sizing Engine Works](#2-how-the-right-sizing-engine-works)
3. [Data Pipeline: Bronze, Silver, Gold](#3-data-pipeline-bronze-silver-gold)
4. [Database Tables Reference](#4-database-tables-reference)
5. [Extending via JSON Config Files](#5-extending-via-json-config-files)
6. [Creating Recommendation Templates](#6-creating-recommendation-templates)
7. [Hooking Up Metric Data](#7-hooking-up-metric-data)
8. [Testing & Validation Checklist](#8-testing--validation-checklist)
9. [Cloud-Specific Notes](#9-cloud-specific-notes)

---

## 1. Architecture Overview

The right-sizing engine is a multi-cloud recommendation framework that analyzes resource utilization and generates cost-optimization recommendations. It runs as part of the **Silver layer** in our Bronze-Silver-Gold medallion architecture.

### High-Level Data Flow

```
Bronze Layer                    Silver Layer                    Gold Layer
(Data Ingestion)               (Recommendation Generation)     (RDS Persistence)

Cloud APIs ──> Bronze Tables ──> Silver Custom Reco Job ──────> custom_recommendations (RDS)
  - Billing       (Iceberg)      - Reads bronze tables             - Upsert via SQL/ORM
  - Metrics                      - Evaluates thresholds            - SHA256 dedup key
  - Tags                         - Calls right-sizing engine       - Status tracking
  - Native Recos                 - Calculates savings
                                 - Writes silver table (Iceberg)
```

### Package Structure

```
src/s3/scripts/common/
  rightsizing_engine/               # Main engine package (deployed to S3 as zip)
    __init__.py
    engine.py                       # Main entry point: RightSizingEngine class
    models.py                       # Data models: RightSizingResult, SKUInfo, etc.
    config_loader_s3.py             # S3 JSON config loader
    constants.py                    # Enums, default mappings, load_mappings()
    iceberg_store.py                # Iceberg table read/write operations
    pricing_resolver.py             # 4-step pricing resolution chain
    sku_catalog.py                  # SKU catalog lookups (family, tier, ordinal)
    sizing_strategies.py            # 6 sizing strategy implementations
    service_config_loader.py        # Service config from Iceberg into memory
    pipeline_helpers.py             # Integration helpers for silver jobs
    db_utils.py                     # Database utility functions
    providers/                      # Cloud-specific pricing API adapters
      aws_pricing.py
      azure_pricing.py
      gcp_pricing.py
      oci_pricing.py
      alibaba_pricing.py
    catalog_scripts/                # Catalog refresh and seeding scripts
      refresh_catalog.py            # Orchestrator for full catalog refresh
      seed_sku_catalog_aws.py       # AWS SKU seeding
      seed_sku_catalog_azure.py     # Azure SKU seeding
      seed_sku_catalog_gcp.py       # GCP SKU seeding
      seed_sku_catalog_oci.py       # OCI SKU seeding
      seed_sku_catalog_alibaba.py   # Alibaba SKU seeding
      seed_rightsizing_rules.py     # Default rules seeding
      seed_service_config.py        # Service config from Excel
      update_pricing_from_cur.py    # CUR pricing extraction
      export_config_to_s3.py        # Export defaults to S3 JSON
      migrations/
        001_create_rightsizing_tables.sql

  config/rightsizing_engine/        # External JSON config files
    provider_config.json            # Instance types, pricing, regions (all clouds)
    cur_mappings.json               # CUR service codes, SKU field mappings
    sizing_defaults.json            # Ordinals, thresholds, rules, migration maps
```

---

## 2. How the Right-Sizing Engine Works

### 2.1 Initialization

When a Silver job starts, it initializes the engine once:

```python
from rightsizing_engine.engine import RightSizingEngine

engine = RightSizingEngine(
    spark=spark,
    iceberg_db="my_iceberg_db",
    cloud_provider="aws",      # or "azure", "gcp", "oci", "alibaba"
    bucket="finomics-data-pod",
    prefix="warehouse"
)
```

During initialization, the engine loads **everything into memory**:

1. **S3 JSON Config**: Reads `provider_config.json`, `cur_mappings.json`, `sizing_defaults.json` from `s3://{bucket}/{prefix}/config/rightsizing_engine/`
2. **Iceberg Tables**: Loads into Pandas DataFrames:
   - `rs_cloud_sku_catalog` -> SKU catalog (instance types, specs, pricing)
   - `rs_cloud_sku_pricing_history` -> CUR-derived effective prices
   - `rs_finops_service_config` -> Service configuration (1,048 services)
   - `rs_rightsizing_rules` -> Sizing rules (19 generic + 26 Azure-specific = 45+ rules)
3. **In-Memory Caches**: Builds pricing cache, config indexes, rule priority lists

### 2.2 Recommendation Generation

For each resource, call `get_recommendation()`:

```python
result = engine.get_recommendation(
    resource_data={
        "resource_id": "i-abc123",
        "service_name": "Amazon EC2",
        "service_category": "Compute",
        "current_sku": "m5.xlarge",
        "region": "us-east-1",
        "account_id": "123456789012",
    },
    metrics={
        "cpu_avg": 15.2,
        "memory_avg": 45.0,
    },
    current_cost=140.16   # monthly cost from CUR (optional)
)
```

The engine follows these steps:

1. **Service Config Lookup**: Finds the service configuration for "Amazon EC2" to determine the sizing strategy and dimension
2. **Rule Matching**: Finds all active rules for the service category (e.g., "Compute"), sorted by priority
3. **Strategy Execution**: For each rule, runs the appropriate sizing strategy:
   - **StepDown**: Move N steps down in the same SKU family (e.g., m5.xlarge -> m5.large)
   - **MatchUtilization**: Pick the smallest SKU that fits actual utilization + headroom
   - **TierDowngrade**: Move to a lower service tier (e.g., Premium -> Standard)
   - **Terminate**: Recommend termination for idle resources
   - **MigrateType**: Migrate to a different type (e.g., gp3 -> gp2 storage)
   - **Consolidate**: Consolidate underutilized workloads
4. **Pricing Resolution**: Looks up prices for both current and recommended SKUs using the 4-step chain:
   - Step 1: CUR effective price (customer's actual negotiated price)
   - Step 2: SKU catalog list price (from cloud pricing APIs)
   - Step 3: Config fallback (from `provider_config.json` pricing sections)
   - Step 4: Returns 0.0 with source='unknown'
5. **Savings Calculation**: Computes monthly/annual savings and savings percentage
6. **Result Assembly**: Returns a `RightSizingResult` with all fields populated

### 2.3 RightSizingResult Output

The engine returns a `RightSizingResult` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `resource_id` | str | Resource identifier |
| `cloud_provider` | str | aws, azure, gcp, oci, alibaba |
| `service_name` | str | e.g., "Amazon EC2" |
| `service_category` | str | e.g., "Compute" |
| `region` | str | Cloud region |
| `current_sku` | str | Current instance type |
| `current_hourly_price` | float | Current hourly rate |
| `current_monthly_cost` | float | Current monthly cost |
| **`recommended_sku`** | **str** | **Target instance type (the key output)** |
| `recommended_action` | str | downsize, terminate, migrate, optimize, consolidate |
| `recommended_hourly_price` | float | Target hourly rate |
| `estimated_monthly_savings` | float | Monthly savings |
| `estimated_annual_savings` | float | Annual savings |
| `savings_percentage` | float | Savings as percentage |
| `confidence` | str | high, medium, low |
| `pricing_source` | str | cur_effective, sku_catalog, config_fallback, unknown |
| `sizing_rationale` | str | Human-readable explanation |
| `details` | dict | JSON with rule_code, sizing_method, metrics_used |

### 2.4 Simple Price Lookup API

The engine also provides a drop-in replacement for hardcoded pricing:

```python
# Instead of: price = EC2_PRICING_MAP.get("m5.large", 0.0)
# Use:
price = engine.get_price("Amazon EC2", "m5.large", "us-east-1")
```

This uses the same 4-step pricing resolution chain.

---

## 3. Data Pipeline: Bronze, Silver, Gold

All three layers must be run for custom recommendations. Here is what each layer does per cloud:

### 3.1 Bronze Layer (Data Ingestion)

| Cloud | Bronze Job | What It Ingests | Tables Created |
|-------|-----------|-----------------|----------------|
| **AWS** | `aws_bronze_job.py` | CUR billing from Athena | `bronze_aws_tbl` |
| | `aws_bronze_custom_recom_job.py` | CloudWatch metrics, EC2/EBS inventory, Compute Optimizer | `bronze_aws_cloudwatch_metrics`, `bronze_aws_ec2_instances`, `bronze_aws_ebs_volumes`, `bronze_aws_compute_optimizer_ec2`, `bronze_aws_compute_optimizer_ebs` |
| **Azure** | `azure_bronze_job.py` | Billing data | `bronze_azure_tbl` |
| | `azure_bronze_custom_recom_job.py` | Azure Monitor metrics + Management API resources (17 services) | `bronze_azure_vm_metrics`, `bronze_azure_vms`, `bronze_azure_cosmos_metrics`, etc. |
| **GCP** | `gcp_bronze_job.py` | BigQuery billing | `bronze_gcp_tbl` |
| | `gcp_bronze_custom_recom_job.py` | Cloud Monitoring metrics + resource inventory | `bronze_gcp_compute_engine_metrics`, `bronze_gcp_compute_engine`, etc. |
| **OCI** | `oci_bronze_job.py` | Billing + Tags + OCI Optimizer recommendations + Resource Actions | `bronze_oci_billing_tbl`, `bronze_oci_tags_tbl`, `bronze_oci_recommendations_tbl` |
| **Alibaba** | `alibaba_bronze_job.py` | Billing only (no metrics API) | `bronze_alibaba_tbl` |

**Key Differences:**
- AWS, Azure, GCP have **separate** bronze custom recommendation jobs that collect metrics from monitoring APIs
- OCI collects everything (billing + tags + native recommendations) in a **single** bronze job
- Alibaba has **billing data only** - no CloudWatch-equivalent metrics

### 3.2 Silver Layer (Recommendation Generation)

| Cloud | Silver Job | Recommendation Types |
|-------|-----------|---------------------|
| **AWS** | `aws_silver_custom_recommendation_job.py` | EC2 idle, EBS idle, EC2 rightsizing, EBS migration, cost anomaly, untagged |
| **Azure** | `azure_unified_recommendations.py` | Per-service: VM, Cosmos DB, Redis, AKS, Blob, SQL, EventHub, etc. (15 services). **Engine-first**: all services try the right-sizing engine for savings/SKU calculation before falling back to per-service hardcoded logic. |
| **GCP** | `silver_gcp_custom_recommendations.py` | Per-service: Compute Engine, Cloud SQL, GCS, BigQuery, etc. |
| **OCI** | `oci_silver_custom_recommendation_job.py` | OCI Optimizer enrichment, idle resources, cost anomalies, untagged |
| **Alibaba** | `alibaba_silver_custom_recommendation_job.py` | Idle resources, cost anomalies, discount optimization, untagged |

Each silver job:
1. Reads bronze tables for resource data and metrics
2. Evaluates thresholds to identify underutilized resources
3. Calls the right-sizing engine for savings calculation and SKU recommendations (engine-first for Azure; optional for other clouds)
4. Falls back to per-service hardcoded logic if the engine returns no result
5. Writes results to a silver Iceberg table (e.g., `silver_aws_custom_recommendations`)

### 3.3 Gold Layer (RDS Persistence)

| Cloud | Gold Job | Target RDS Table |
|-------|---------|-----------------|
| **AWS** | `aws_gold_job.py` | `custom_recommendations` |
| **Azure** | `azure_gold_cost_recomm_job.py` | `custom_recommendations` |
| **GCP** | `gcp_gold_job.py` | `custom_recommendations` |
| **OCI** | `oci_gold_job.py` | `custom_recommendations` |
| **Alibaba** | `alibaba_gold_job.py` | `custom_recommendations` |

Each gold job:
1. Reads the silver Iceberg table
2. Transforms each row into the standard recommendation schema
3. Upserts to the `custom_recommendations` RDS table using `ON CONFLICT (id) DO UPDATE`
4. The `id` is a SHA256 hash of `{cloud}|{account}|{resource}|{rec_type}`

---

## 4. Database Tables Reference

### 4.1 RDS Tables

#### `custom_recommendations` (Generated Recommendations)

This is the **primary table where generated recommendations are stored** and surfaced to users.

| Column | Type | Description |
|--------|------|-------------|
| `id` | VARCHAR(128) PK | SHA256 hash dedup key |
| `company_id` | TEXT NOT NULL | Company identifier |
| `client_id` | VARCHAR, INDEXED | Client identifier |
| `cloud_name` | TEXT NOT NULL | "AWS", "Azure", "GCP", "OCI", "Alibaba" |
| `account_id` | VARCHAR, INDEXED | Cloud account ID |
| `resource_arn` | TEXT | AWS ARN (NULL for other clouds) |
| `resource_id` | TEXT NOT NULL, INDEXED | Resource identifier |
| `recommendation` | TEXT | Recommendation text/title |
| `rec_type` | TEXT NOT NULL, INDEXED | "custom_rightsize", "terminate", "rightsize", etc. |
| `issue` | TEXT NOT NULL, INDEXED | What is wrong with the resource |
| `status` | VARCHAR DEFAULT "PENDING" | PENDING, APPLIED, CLEANED, RESOLVED |
| `details` | JSON | **Full context as JSON (includes recommended SKU, metrics, etc.)** |
| `category` | VARCHAR, INDEXED | Service category (Compute, Storage, etc.) |
| `total_cost` | DOUBLE | Annual cost of resource |
| `savings` | DOUBLE | Annual savings amount |
| `region` | VARCHAR | Cloud region |
| `resource_type` | TEXT | Resource type (VirtualMachine, EC2Instance, etc.) |
| `severity` | VARCHAR | Low, Medium, High, Critical |
| `first_seen_at` | DATETIME | When first detected |
| `last_seen_at` | DATETIME, INDEXED | Last time recommendation was active |
| `ttl_expire_at` | DATETIME | TTL for cleanup |

**How the recommended SKU shows up**: The recommended SKU appears in:
- The `recommendation` text field (e.g., "Downsize m5.xlarge to m5.large")
- The `details` JSON field under keys like `recommended_sku`, `target_sku`, or embedded in `description`

**Engine metadata in `details` JSON**: When the right-sizing engine produces a recommendation, the following keys are added to the `details` JSON:
- `engine_recommended_sku` - Target SKU from the engine
- `engine_action` - downsize, terminate, migrate, optimize, consolidate
- `engine_confidence` - high, medium, low
- `engine_pricing_source` - cur_effective, sku_catalog, config_fallback, unknown
- `engine_sizing_rationale` - Human-readable explanation
- `engine_savings_pct` - Savings as percentage
- `engine_sizing_method` - step_down, match_util, tier_downgrade, etc.
- `engine_rule_code` - Which engine rule matched (e.g., AZURE_VM_STEP_DOWN)

#### `finops_threshold_rules` (Recommendation Rules/Templates)

This table stores the **rules that drive recommendation generation**. Each rule defines thresholds, evaluation logic, and recommendation templates.

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PK | Auto-increment ID |
| `rule_code` | VARCHAR | Unique rule ID (e.g., "VM_LOW_CPU") |
| `service_name` | VARCHAR | Service name (e.g., "Virtual Machines") |
| `cloud_provider` | VARCHAR | aws, azure, gcp, oci, alibaba |
| `rule_type` | VARCHAR | Rule type classification |
| `evaluation_logic` | JSONB | Metric conditions: `{"conditions": [{"metric":"cpu","operator":"lt","threshold":40}]}` |
| `cost_model` | JSONB | Savings config: `{"savings_pct": 30.0}` |
| `category` | VARCHAR | Service category |
| `severity` | VARCHAR | Low, Medium, High, Critical |
| `title` | VARCHAR | Rule title |
| `description` | TEXT | Rule description |
| `recommendation_template` | TEXT | Template for recommendation text |
| `estimated_savings_formula` | TEXT | Formula for savings |
| `client_id` | VARCHAR NULL | Client-specific override (NULL = global) |
| `is_active` | BOOLEAN | Active flag |
| `priority` | INTEGER | Priority (lower = higher priority) |
| `created_at` | TIMESTAMP | Creation time |
| `updated_at` | TIMESTAMP | Last update |

#### `base_finops_threshold_rules` (Global Rule Templates)

Same schema as `finops_threshold_rules` but without `client_id`/`subscription_id`. These are **global defaults** that get synced to client-specific `finops_threshold_rules` entries via `sync_base_rules_to_client_glue.py`.

#### `service_configuration` (Service Metadata)

Maps services to their bronze table names and configuration. **Used primarily by Azure** for dynamic service discovery.

| Column | Type | Description |
|--------|------|-------------|
| `service_code` | VARCHAR | Service code (e.g., "VM", "COSMOS") |
| `service_name` | VARCHAR | Human-readable name |
| `cloud_provider` | VARCHAR | Cloud provider |
| `is_active` | BOOLEAN | Active flag |
| `metrics_table_suffix` | VARCHAR | Bronze metrics table name (e.g., `bronze_azure_vm_metrics`) |
| `resources_table_suffix` | VARCHAR | Bronze resources table name (e.g., `bronze_azure_vms`) |
| `service_name_filter` | VARCHAR | Filter for metrics table queries |
| `primary_metric` | VARCHAR | Main evaluation metric |
| `threshold_config` | JSONB | Structured thresholds |
| `sizing_strategy` | VARCHAR | downsize, terminate, migrate, etc. |
| `sizing_dimension` | VARCHAR | vcpu, memory, storage, iops, etc. |

### 4.2 Iceberg Tables (S3)

These tables are managed by the right-sizing engine and stored in the Iceberg warehouse on S3.

#### `rs_cloud_sku_catalog` (SKU Catalog)

All cloud instance types, specs, and list prices. Refreshed by `refresh_catalog.py`.

| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | VARCHAR | aws, azure, gcp, oci, alibaba |
| `service_category` | VARCHAR | Compute, Storage, Databases |
| `service_name` | VARCHAR | Service name |
| `sku_code` | VARCHAR | Instance type (e.g., "m5.large") |
| `sku_family` | VARCHAR | Family (e.g., "m5") |
| `sku_size_ordinal` | INTEGER | Order within family (1=nano, 5=large, 6=xlarge) |
| `tier` | VARCHAR | Basic, Standard, Premium |
| `region` | VARCHAR | Region |
| `vcpus` | DOUBLE | vCPU count |
| `memory_gb` | DOUBLE | Memory in GB |
| `hourly_price` | DOUBLE | On-demand hourly rate |
| `specs` | STRING (JSON) | Service-specific specs |
| `is_active` | BOOLEAN | Active flag |

**Unique Key**: (cloud_provider, service_name, sku_code, region, pricing_model)

#### `rs_cloud_sku_pricing_history` (CUR Pricing)

Effective prices extracted from CUR/billing data.

| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | VARCHAR | Cloud provider |
| `service_name` | VARCHAR | Service name |
| `sku_code` | VARCHAR | Instance type |
| `region` | VARCHAR | Region |
| `effective_price` | DOUBLE | Actual price (cost/usage) |
| `account_id` | VARCHAR | Which account saw this price |
| `usage_date` | DATE | Date of usage |

#### `rs_finops_service_config` (Service Config)

1,048 services from the Combined_Finops_Metrics_Recos.xlsx, defining per-service sizing strategies.

| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | VARCHAR | Cloud provider |
| `service_name` | VARCHAR | Service name |
| `service_category` | VARCHAR | Category |
| `primary_metric` | VARCHAR | Main metric for evaluation |
| `sizing_strategy` | VARCHAR | downsize, terminate, migrate, optimize, consolidate |
| `sizing_dimension` | VARCHAR | vcpu, memory, storage, iops, throughput, capacity, tier |
| `cur_service_code` | VARCHAR | CUR product code mapping |
| `cur_sku_field` | VARCHAR | CUR column holding the SKU |

#### `rs_rightsizing_rules` (Sizing Rules)

45+ rules that map service categories to sizing logic (19 generic cross-cloud + 26 Azure-specific).

| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | VARCHAR | Cloud provider |
| `service_category` | VARCHAR | Compute, Storage, Databases, etc. |
| `service_name` | VARCHAR NULL | NULL/empty = applies to all in category. Azure-specific rules set this to the exact service name (e.g., "Virtual Machines", "Azure Cache for Redis") |
| `rule_code` | VARCHAR | Unique ID (e.g., "COMPUTE_RIGHTSIZE_STEP_DOWN", "AZURE_VM_STEP_DOWN") |
| `action_type` | VARCHAR | downsize_vcpu, downsize_tier, terminate, migrate_type |
| `sizing_method` | VARCHAR | step_down, match_util, tier_downgrade, terminate, migrate_type |
| `sizing_params` | STRING (JSON) | `{"steps":1, "min_vcpu":1}` |
| `default_savings_pct` | DOUBLE | Fallback savings percentage |
| `priority` | INTEGER | Rule priority |

---

## 5. Extending via JSON Config Files

The engine's reference data is externalized into 3 JSON config files on S3. These can be updated **without redeploying the engine code**.

### Config File Location

```
s3://{bucket}/{prefix}/config/rightsizing_engine/
  provider_config.json       # 52KB - Instance types, pricing, regions
  cur_mappings.json          # CUR service codes, SKU field maps
  sizing_defaults.json       # Ordinals, thresholds, rules, migration maps
```

Local development path:
```
src/s3/scripts/common/config/rightsizing_engine/
```

### 5.1 provider_config.json

Contains per-cloud reference data: instance types, pricing, regions, disk pricing, API configurations.

**Structure:**

```json
{
  "aws": {
    "regions": ["us-east-1", "us-west-2", ...],
    "ebs_volume_types": ["gp3", "gp2", "io2", "io1", "st1", "sc1"],
    "ebs_pricing": {"gp3": 0.08, "gp2": 0.10, ...},
    "ebs_iops_pricing": {"io2": 0.065, "io1": 0.065},
    "ec2_instance_pricing": {"t3.micro": 0.0104, "m5.large": 0.096, ...},
    "burstable_families": ["t2", "t3", "t3a", "t4g"],
    "previous_gen_families": ["t2", "m4", "c4", "r4", "i2", "d2"],
    "engine_service_mapping": {
      "ec2": {"service_name": "Amazon EC2", "service_category": "Compute"},
      "ebs": {"service_name": "Amazon EBS", "service_category": "Storage"},
      ...
    }
  },
  "azure": {
    "regions": ["eastus", "westus2", ...],
    "memory_ratio_estimates": {"Standard_B": 4.0, "Standard_D": 4.0, ...},
    "service_filter_templates": {
      "VirtualMachines": "serviceName eq 'Virtual Machines' and ...",
      ...
    },
    "engine_service_mapping": {
      "VM":     {"service_name": "Virtual Machines",       "service_category": "Compute"},
      "REDIS":  {"service_name": "Azure Cache for Redis",  "service_category": "Cache"},
      "COSMOS": {"service_name": "Azure Cosmos DB",        "service_category": "Database"},
      "...":    "// 23 total aliases covering all 15 Azure services"
    }
  },
  "gcp": {
    "regions": ["us-central1", ...],
    "machine_types": {
      "e2-micro": {"vcpus": 0.25, "memory_gb": 1.0, "family": "e2"},
      "n1-standard-1": {"vcpus": 1, "memory_gb": 3.75, "family": "n1"},
      ...
    },
    "vm_pricing": {"e2-micro": 0.00838, ...},
    "disk_pricing": {"pd-standard": 0.040, ...},
    "sql_tiers": {"db-f1-micro": {"vcpus": 0.6, "memory_gb": 0.6}, ...},
    "sql_pricing": {"db-f1-micro": 0.0150, ...}
  },
  "oci": {
    "regions": ["us-ashburn-1", ...],
    "compute_shapes": {
      "VM.Standard.E4.Flex": {"base_vcpus": 1, "max_vcpus": 64, ...},
      ...
    },
    "block_storage_pricing": {"balanced": 0.0255, ...}
  },
  "alibaba": {
    "regions": ["cn-hangzhou", ...],
    "ecs_types": {
      "ecs.t6-c1m1.large": {"vcpus": 2, "memory_gb": 2.0, ...},
      ...
    },
    "disk_pricing": {"cloud_efficiency": 0.035, ...}
  }
}
```

**To add new instance types or update pricing:**

```bash
# Download current config
aws s3 cp s3://finomics-data-pod/warehouse/config/rightsizing_engine/provider_config.json .

# Edit: add new instance types under the appropriate cloud section
# Example: add a new AWS instance type
# In "aws" > "ec2_instance_pricing", add: "m7i.large": 0.1008

# Upload back
aws s3 cp provider_config.json s3://finomics-data-pod/warehouse/config/rightsizing_engine/

# Next Glue job run picks up the new config automatically
```

### 5.2 cur_mappings.json

Maps service names to CUR product codes and defines which CUR column holds the SKU.

```json
{
  "service_code_map": {
    "aws": {
      "Amazon EC2": "AmazonEC2",
      "Amazon EBS": "AmazonEC2",
      "Amazon RDS": "AmazonRDS",
      ...
    },
    "azure": { ... },
    "gcp": { ... },
    "oci": { ... },
    "alibaba": { ... }
  },
  "sku_field_map": {
    "aws": "product_instance_type",
    "azure": "armSkuName",
    "gcp": "sku_description",
    "oci": "product_description",
    "alibaba": "configuration"
  },
  "reverse_service_code_map": {
    "aws": {
      "AmazonEC2": ["Amazon EC2", "Amazon EBS"],
      ...
    }
  },
  "cur_billing_columns": {
    "aws": {
      "cost_column": "line_item_unblended_cost",
      "usage_column": "line_item_usage_amount",
      ...
    },
    "azure": { ... },
    ...
  }
}
```

**To add a new service mapping:**
Add the service name and CUR product code under the appropriate cloud in `service_code_map`.

### 5.3 sizing_defaults.json

Contains ordinals, thresholds, default rules, and migration maps.

```json
{
  "size_ordinal": {
    "nano": 1, "micro": 2, "small": 3, "medium": 4,
    "large": 5, "xlarge": 6, "2xlarge": 7, "4xlarge": 8,
    "8xlarge": 9, "9xlarge": 10, "12xlarge": 11,
    "13xlarge": 15, "16xlarge": 16, "18xlarge": 17,
    "24xlarge": 18, "48xlarge": 19, "52xlarge": 21,
    "96xlarge": 24, "metal": 25
  },
  "azure_tier_ordinal": {
    "free": 1, "shared": 2, "basic": 3, "standard": 4,
    "premium": 5, "enterprise": 6, "ultra": 8
  },
  "recommendation_thresholds": {
    "cpu_idle_threshold": 5.0,
    "cpu_low_threshold": 40.0,
    "memory_idle_threshold": 10.0,
    "memory_low_threshold": 50.0,
    "iops_idle_threshold": 1.0,
    "min_metric_datapoints": 24,
    "min_observation_days": 7,
    "min_savings_threshold": 10.0,
    "cost_anomaly_multiplier": 1.5,
    ...
  },
  "default_rightsizing_rules": [
    {
      "service_category": "Compute",
      "rule_code": "compute_downsize_vcpu",
      "action_type": "downsize_vcpu",
      "sizing_method": "step_down",
      "sizing_params": {"steps": 1, "min_vcpu": 1, "min_memory_gb": 0.5},
      "default_savings_pct": 25.0,
      "priority": 1
    },
    ...
  ],
  "storage_migration_maps": {
    "aws": {"io1": "gp3", "gp2": "gp3", "standard": "gp3"},
    "azure": {"Premium_LRS": "StandardSSD_LRS", ...},
    "gcp": {"pd-ssd": "pd-balanced", ...},
    "oci": {"Higher Performance": "Balanced", ...},
    "alibaba": {"cloud_ssd": "cloud_essd_entry", ...}
  }
}
```

**To add new size tiers:** Add them to `size_ordinal` with the correct numeric ordering. The code automatically picks up new ordinals at runtime without any code changes.

**To adjust thresholds:** Change values in `recommendation_thresholds`. For example, to make idle detection more aggressive, lower `cpu_idle_threshold` from 5.0 to 3.0.

### 5.4 Config Update Workflow

```
1. Download:  aws s3 cp s3://{bucket}/{prefix}/config/rightsizing_engine/{file}.json .
2. Edit:      Modify the JSON (add instance types, update pricing, change thresholds)
3. Upload:    aws s3 cp {file}.json s3://{bucket}/{prefix}/config/rightsizing_engine/
4. Run:       Next Glue job run picks up the new config (loaded at engine init)
```

No code changes, no zip rebuild, no redeployment required.

---

## 6. Creating Recommendation Templates

### 6.1 How Recommendations Are Structured

Each recommendation in the `custom_recommendations` RDS table has these key fields:

| Field | Example Value |
|-------|--------------|
| `recommendation` | "Downsize m5.xlarge to m5.large - Low CPU utilization (15.2%)" |
| `issue` | "EC2 instance underutilized: avg CPU 15.2%, avg memory 45.0%" |
| `rec_type` | "custom_rightsize" |
| `category` | "Compute" |
| `severity` | "Medium" |
| `savings` | 421.92 (annual) |
| `total_cost` | 1023.27 (annual) |
| `details` | `{"recommended_sku": "m5.large", "current_sku": "m5.xlarge", ...}` |

### 6.2 Making the Recommended SKU Show Up Correctly

The recommended SKU appears in **two places**:

#### A) In the `recommendation` text field

The silver job constructs the recommendation text. For right-sizing engine results:

```python
# In silver job code:
result = engine.get_recommendation(resource_data, metrics, current_cost)

# Build recommendation text using engine output:
recommendation_text = (
    f"Downsize {result.current_sku} to {result.recommended_sku} - "
    f"Estimated annual savings: ${result.estimated_annual_savings:,.2f}"
)
```

#### B) In the `details` JSON field

The `details` field should include the recommended SKU as a structured field:

```python
details = {
    "recommended_sku": result.recommended_sku,
    "current_sku": result.current_sku,
    "recommended_action": result.recommended_action,
    "sizing_rationale": result.sizing_rationale,
    "confidence": result.confidence,
    "pricing_source": result.pricing_source,
    "current_hourly_price": result.current_hourly_price,
    "recommended_hourly_price": result.recommended_hourly_price,
    "savings_percentage": result.savings_percentage,
    "metrics_used": result.details.get("metrics_used", {}),
    "rule_code": result.details.get("rule_code", ""),
}
```

### 6.3 Creating Rules in `finops_threshold_rules`

To create a recommendation template that triggers for a specific condition:

```sql
INSERT INTO finops_threshold_rules (
    rule_code, service_name, cloud_provider, rule_type,
    evaluation_logic, cost_model,
    category, severity, title, description,
    recommendation_template, is_active, priority
) VALUES (
    'VM_LOW_CPU',                        -- Unique rule code
    'Virtual Machines',                   -- Azure service name
    'azure',                             -- Cloud provider
    'utilization',                       -- Rule type
    '{"conditions": [
        {"metric": "cpu", "operator": "lt", "threshold": 40, "weight": 1.0},
        {"metric": "memory", "operator": "lt", "threshold": 60, "weight": 0.5}
    ]}',                                 -- When to trigger
    '{"savings_pct": 25}',              -- Expected savings percentage
    'Compute',                           -- Category
    'Medium',                            -- Severity
    'Underutilized VM',                  -- Title
    'VM has low CPU and memory utilization', -- Description
    'Consider downsizing {current_sku} to a smaller VM size. '
    'Current avg CPU: {cpu_avg}%, avg Memory: {memory_avg}%.',  -- Template
    TRUE,                                -- Active
    5                                    -- Priority
);
```

#### Template Variables Available

| Variable | Source | Description |
|----------|--------|-------------|
| `{resource_name}` | resource_data | Resource name/ID |
| `{current_sku}` | resource_data | Current instance type |
| `{cpu_avg}` | metrics | Average CPU utilization |
| `{memory_avg}` | metrics | Average memory utilization |
| `{total_cost}` | billing | Annual cost |
| `{savings}` | calculation | Estimated savings |
| `{threshold_value}` | evaluation_logic | The threshold that was exceeded |

### 6.4 Creating Rules in `rs_rightsizing_rules` (Iceberg)

For the right-sizing engine specifically, rules go into the `rs_rightsizing_rules` Iceberg table:

```sql
-- Via Spark SQL in a Glue job:
INSERT INTO {iceberg_db}.rs_rightsizing_rules
VALUES (
    'aws',              -- cloud_provider
    'Compute',          -- service_category
    'Amazon EC2',       -- service_name (NULL for category-wide)
    'ec2_step_down',    -- rule_code
    'downsize_vcpu',    -- action_type
    'step_down',        -- sizing_method
    '{"steps": 1, "min_vcpu": 1, "min_memory_gb": 0.5}',  -- sizing_params
    25.0,               -- default_savings_pct
    1,                  -- priority
    TRUE                -- is_active
);
```

### 6.5 Sizing Methods and Their Parameters

| Sizing Method | What It Does | sizing_params |
|--------------|-------------|---------------|
| `step_down` | Move N steps down in the same SKU family | `{"steps": 1, "min_vcpu": 1, "min_memory_gb": 0.5}` |
| `match_util` | Find smallest SKU matching utilization + headroom | `{"target_utilization": 0.7, "headroom": 0.2}` |
| `tier_downgrade` | Move to a lower service tier | `{"skip_tiers": 0}` |
| `terminate` | Recommend termination for idle resources | `{}` |
| `migrate_type` | Migrate to different storage/service type | `{"target_type_map": {"gp2": "gp3"}}` (auto-injected from JSON config) |
| `consolidate` | Consolidate underutilized workloads | `{}` |

---

## 7. Hooking Up Metric Data

Different clouds have different patterns for connecting metric data to the recommendation pipeline.

### 7.1 AWS: Direct Metric Table Joins

AWS reads metrics directly from bronze Iceberg tables using SQL joins:

```python
# Bronze tables:
bronze_metrics = f"{catalog}.{db}.bronze_aws_cloudwatch_metrics"
bronze_ec2 = f"{catalog}.{db}.bronze_aws_ec2_instances"

# SQL query pattern:
query = f"""
    WITH cpu_metrics AS (
        SELECT m.account_id, m.resource_id,
               AVG(m.average) AS avg_cpu,
               COUNT(*) AS metric_count
        FROM {bronze_metrics} m
        INNER JOIN {bronze_ec2} i
            ON m.resource_id = i.instance_id
            AND m.account_id = i.account_id
        WHERE m.metric_name = 'CPUUtilization'
          AND m.client_id = '{client_id}'
          AND CAST(m.timestamp AS DATE) >= DATE('{start_date}')
        GROUP BY m.account_id, m.resource_id
    )
    SELECT * FROM cpu_metrics WHERE avg_cpu < {threshold}
"""
```

**To add a new AWS service:**
1. Ensure the bronze custom recommendation job collects metrics for the service into `bronze_aws_cloudwatch_metrics`
2. In the silver job, add a new function that queries the bronze table with the appropriate `metric_name` filter
3. Call `engine.get_recommendation()` with the gathered metrics

### 7.2 Azure: Database-Driven Service Configuration

Azure uses the `service_configuration` RDS table to dynamically discover bronze tables per service:

```python
# Load service config from RDS
service_config = load_service_configuration(service_code="VM", cloud_provider="azure")
# Returns: {metrics_table_suffix: "bronze_azure_vm_metrics",
#           resources_table_suffix: "bronze_azure_vms",
#           service_name_filter: "Virtual Machines"}

# Build table names
metrics_table = f"{catalog}.{db}.{service_config['metrics_table_suffix']}"
resources_table = f"{catalog}.{db}.{service_config['resources_table_suffix']}"

# Query metrics
metrics_df = spark.sql(f"""
    SELECT * FROM {metrics_table}
    WHERE client_id = '{client_id}'
      AND service_name = '{service_config['service_name_filter']}'
""")
```

**To add a new Azure service:**
1. Create a bronze ingestion class in `azure_bronze_custom_recom_job.py` that:
   - Calls the Azure Management API to list resources
   - Calls the Azure Monitor API to collect metrics
   - Writes to `bronze_azure_{service}_metrics` and `bronze_azure_{service}s` Iceberg tables
2. Insert a row into the `service_configuration` RDS table:
   ```sql
   INSERT INTO service_configuration (
       service_code, service_name, cloud_provider, is_active,
       metrics_table_suffix, resources_table_suffix, service_name_filter,
       primary_metric, sizing_strategy, sizing_dimension
   ) VALUES (
       'NEWSERVICE', 'Azure New Service', 'azure', TRUE,
       'bronze_azure_newservice_metrics', 'bronze_azure_newservices',
       'New Service', 'cpu', 'downsize', 'vcpu'
   );
   ```
3. Add the service to `finops_threshold_rules` for evaluation:
   ```sql
   INSERT INTO finops_threshold_rules (
       rule_code, service_name, cloud_provider, evaluation_logic,
       cost_model, category, severity, is_active, priority
   ) VALUES (
       'NEWSERVICE_LOW_UTIL', 'Azure New Service', 'azure',
       '{"conditions": [{"metric":"cpu","operator":"lt","threshold":40}]}',
       '{"savings_pct": 25}', 'Compute', 'Medium', TRUE, 5
   );
   ```
4. Add to `provider_config.json` if API filter templates are needed

### 7.3 GCP: Service Class Pattern

GCP uses per-service Python classes, each knowing its own bronze table names:

```python
class ComputeEngineRecommendations:
    def __init__(self, spark, bronze_instance_table, bronze_metrics_table):
        self.bronze_metrics_table = bronze_metrics_table

    def get_recommendations(self, client_id, ...):
        metrics = spark.sql(f"""
            SELECT instance_name, metric_name,
                   AVG(metric_value) as metric_avg
            FROM {self.bronze_metrics_table}
            WHERE instance_name IN (...)
              AND metric_name IN ('cpu_utilization', 'guest_os_memory_percent')
            GROUP BY instance_name, metric_name
        """)
```

**To add a new GCP service:**
1. Create bronze tables via the GCP bronze custom recommendation job
2. Create a new service class (e.g., `CloudRunRecommendations`) with metric fetching methods
3. Instantiate it in the silver job's main loop
4. Add service mapping to `provider_config.json` under `gcp.engine_service_mapping`

### 7.4 OCI: Cost-Based Metrics (No Cloud Metrics)

OCI does not have a CloudWatch-equivalent. Recommendations come from:
- **Billing patterns**: Aggregate daily costs from `bronze_oci_billing_tbl`
- **Native Optimizer**: Read from `bronze_oci_recommendations_tbl`

```python
# OCI idle detection via billing
idle_query = f"""
    WITH daily_costs AS (
        SELECT resource_id, service,
               AVG(cost) AS avg_daily_cost,
               COUNT(DISTINCT date) AS days_present
        FROM {bronze_oci_billing}
        WHERE client_id = '{client_id}'
          AND date >= '{lookback_start}'
        GROUP BY resource_id, service
    )
    SELECT * FROM daily_costs
    WHERE avg_daily_cost < 1.0 AND days_present >= 7
"""
```

**To add a new OCI service:**
1. The billing data is already ingested generically - no bronze change needed
2. In the silver job, add a new recommendation function that filters the billing table by service name
3. Use cost-based thresholds since OCI has no raw metrics

### 7.5 Alibaba: Billing-Only (No Metrics API)

Alibaba has billing data only. Unique feature: discount analysis using `pretax_gross_amount` vs `after_discount_amount`.

```python
# Alibaba discount optimization
discount_query = f"""
    SELECT instance_id, service,
           SUM(pretax_gross_amount) as gross_total,
           SUM(after_discount_amount) as discount_total,
           1.0 - (SUM(after_discount_amount) / NULLIF(SUM(pretax_gross_amount), 0)) as discount_ratio
    FROM {bronze_alibaba}
    WHERE client_id = '{client_id}'
    GROUP BY instance_id, service
    HAVING discount_ratio < 0.10  -- Less than 10% discount
"""
```

**To add a new Alibaba service:**
1. Billing data is already ingested generically
2. Add recommendation functions in the silver job filtering by service name

### 7.6 Connecting Athena Tables

For AWS, CUR data is typically in Athena. To connect Athena metric data:

1. The bronze job already reads from Athena via Spark:
   ```python
   df = spark.read.format("jdbc") \
       .option("url", f"jdbc:awsathena://athena.{region}.amazonaws.com:443") \
       .option("query", "SELECT * FROM cur_database.cur_table WHERE ...") \
       .load()
   ```

2. The data lands in Iceberg bronze tables, which the silver job reads:
   ```python
   billing_df = spark.sql(f"SELECT * FROM {catalog}.{db}.bronze_aws_tbl WHERE ...")
   ```

3. For CloudWatch metrics specifically, the bronze custom recommendation job calls the CloudWatch API directly and writes to `bronze_aws_cloudwatch_metrics`

---

## 8. Testing & Validation Checklist

### 8.1 Pre-Requisites

- [ ] Ensure S3 config files exist at `s3://{bucket}/{prefix}/config/rightsizing_engine/`
- [ ] Ensure Iceberg tables are created (run `refresh_catalog.py` at least once)
- [ ] Ensure `service_configuration` RDS table has entries for the target cloud/service
- [ ] Ensure `finops_threshold_rules` has rules for the services being tested

### 8.2 Per-Cloud Validation

#### AWS

| Step | What to Verify |
|------|---------------|
| 1. Run bronze job | `bronze_aws_tbl` has billing data |
| 2. Run bronze custom reco job | `bronze_aws_cloudwatch_metrics` has CPU/memory data, `bronze_aws_ec2_instances` has inventory |
| 3. Run silver custom reco job | `silver_aws_custom_recommendations` has recommendations |
| 4. Verify recommendations | Check `recommendation` text includes current and target SKU |
| 5. Verify savings | Check `savings` > 0 and `total_cost` is populated |
| 6. Verify details JSON | `details` field includes `recommended_sku`, `metrics_used` |
| 7. Run gold job | `custom_recommendations` RDS table has the recommendations |

#### Azure

| Step | What to Verify |
|------|---------------|
| 1. Run bronze job | `bronze_azure_tbl` has billing data |
| 2. Run bronze custom reco job | Per-service bronze tables populated (e.g., `bronze_azure_vm_metrics`) |
| 3. Verify service_configuration | RDS table has `metrics_table_suffix` for each active service |
| 4. Verify finops_threshold_rules | Rules exist for each service (e.g., `VM_LOW_CPU`) |
| 5. Run silver custom reco job | Recommendations generated per service |
| 6. Run gold job | Recommendations in RDS |

#### GCP

| Step | What to Verify |
|------|---------------|
| 1. Run bronze job | `bronze_gcp_tbl` has billing data |
| 2. Run bronze custom reco job | `bronze_gcp_compute_engine_metrics`, `bronze_gcp_compute_engine` populated |
| 3. Run silver custom reco job | Recommendations generated |
| 4. Run gold job | Recommendations in RDS |

#### OCI

| Step | What to Verify |
|------|---------------|
| 1. Run bronze job | `bronze_oci_billing_tbl`, `bronze_oci_recommendations_tbl`, `bronze_oci_tags_tbl` populated |
| 2. Run silver custom reco job | 4 recommendation types: optimizer enrichment, idle, anomaly, untagged |
| 3. Verify OCI Optimizer | Native recommendations enriched with cost data |
| 4. Run gold job | Recommendations in RDS |

#### Alibaba

| Step | What to Verify |
|------|---------------|
| 1. Run bronze job | `bronze_alibaba_tbl` has billing data with `pretax_gross_amount` and `after_discount_amount` |
| 2. Run silver custom reco job | 4 recommendation types: idle, anomaly, discount optimization, untagged |
| 3. Verify discount optimization | Resources with < 10% discount flagged |
| 4. Run gold job | Recommendations in RDS |

### 8.3 Right-Sizing Engine Validation

| Test | How to Verify |
|------|--------------|
| **Pricing resolution** | Call `engine.get_price("Amazon EC2", "m5.large", "us-east-1")` - should return > 0 |
| **Config loading** | Check engine init logs: "Loaded config from s3://..." messages |
| **SKU catalog** | Verify `engine.sku_catalog` has entries for the cloud's services |
| **Rule matching** | Verify `engine._rules` has rules for Compute, Storage, etc. |
| **Recommendation output** | Call `engine.get_recommendation(resource_data, metrics)` - verify `recommended_sku` is populated |
| **JSON config override** | Update a price in `provider_config.json`, restart engine, verify new price is used |
| **Ordinal override** | Add a new size ordinal to `sizing_defaults.json`, verify step-down uses it |

### 8.4 Expected Output Schemas

**Silver table output** (standard 15-column schema):

| Column | Type | Example |
|--------|------|---------|
| `client_id` | STRING | "client_123" |
| `account_id` | STRING | "123456789012" |
| `resource_id` | STRING | "i-abc123" |
| `recommendation` | STRING | "Downsize m5.xlarge to m5.large" |
| `rec_type` | STRING | "custom_rightsize" |
| `issue` | STRING | "Low CPU utilization (15.2%)" |
| `details` | STRING (JSON) | `{"recommended_sku": "m5.large", ...}` |
| `category` | STRING | "Compute" |
| `total_cost` | DOUBLE | 1023.27 |
| `savings` | DOUBLE | 421.92 |
| `region` | STRING | "us-east-1" |
| `resource_type` | STRING | "EC2Instance" |
| `severity` | STRING | "Medium" |
| `recommendation_date` | STRING | "2025-01-15" |

---

## 9. Cloud-Specific Notes

### AWS
- Uses Compute Optimizer for EC2/EBS rightsizing recommendations (reads from `bronze_aws_compute_optimizer_ec2/ebs`)
- CloudWatch metrics provide CPU, memory, disk, network data
- CUR billing is the primary cost source
- EBS idle detection uses VolumeReadOps + VolumeWriteOps

### Azure
- Most mature implementation with 17 service types in bronze custom reco job
- Uses `service_configuration` RDS table for dynamic service discovery
- Azure Monitor API provides metrics, Management API provides resource inventory
- Uses `finops_threshold_rules` table for rule-driven evaluation (decides WHEN to recommend)
- Right-sizing engine handles savings/SKU calculation for all 15 services (decides HOW MUCH savings)
- `sync_base_rules_to_client_glue.py` copies base rules to client-specific entries

#### Azure Engine Integration Architecture

`azure_unified_recommendations.py` uses the right-sizing engine as the **primary** savings calculator for all services, with per-service hardcoded logic as fallback:

```
finops_threshold_rules (RDS)          Right-Sizing Engine (Iceberg)
  - Evaluates metrics                   - Picks recommended SKU
  - Decides IF to recommend             - Calculates pricing-aware savings
  - Provides rule_code, severity        - Returns confidence, rationale
           │                                        │
           ▼                                        ▼
    ThresholdRule.evaluate()         engine.get_recommendation()
           │                                        │
           └──────────── SavingsCalculator ─────────┘
                               │
                    1. Try engine (all services)
                    2. If engine returns savings > 0 → use it
                    3. Else fall back to per-service logic
                               │
                               ▼
                    create_recommendation()
                      - details JSON includes engine metadata
                      - engine_confidence, engine_pricing_source
                      - engine_sizing_method, engine_rule_code
```

**Key design decisions:**
- Engine is called **once** per resource in `calculate_savings()` — no redundant calls at save time
- `_resolve_engine_service()` uses `engine_service_mapping` from `provider_config.json` for data-driven service name resolution (no hardcoded if/elif chain)
- Full `RightSizingResult` is stored in `resource_data["_engine_result"]` and propagated into the recommendation `details` JSON
- `AzureSkuNavigator` (live Azure API) remains as a fallback for VM SKU navigation when the engine's SKU catalog lacks data

#### Azure-Specific Engine Rules (26 rules)

All 15 Azure services have dedicated rules in `rs_rightsizing_rules` with `cloud_provider="azure"` and explicit `service_name`:

| Service | Rules | Strategies |
|---------|-------|------------|
| Virtual Machines | AZURE_VM_IDLE_TERMINATE, AZURE_VM_STEP_DOWN, AZURE_VM_MATCH_UTIL | terminate, step_down, match_util |
| Azure Cache for Redis | AZURE_REDIS_TIER_DOWNGRADE | tier_downgrade |
| Azure Cosmos DB | AZURE_COSMOS_CONSOLIDATE, AZURE_COSMOS_RESERVED | consolidate |
| Managed Disks | AZURE_DISK_IDLE_TERMINATE, AZURE_DISK_MIGRATE_TYPE | terminate, migrate_type |
| Azure Kubernetes Service | AZURE_AKS_STEP_DOWN | step_down |
| Container Instances | AZURE_ACI_IDLE_TERMINATE, AZURE_ACI_STEP_DOWN | terminate, step_down |
| Event Hubs | AZURE_EVENTHUB_TIER_DOWNGRADE | tier_downgrade |
| Blob Storage | AZURE_BLOB_TIER_DOWNGRADE | tier_downgrade |
| Load Balancer | AZURE_LB_UNUSED_TERMINATE, AZURE_LB_CONSOLIDATE | terminate, consolidate |
| Azure Private Link | AZURE_PL_IDLE_TERMINATE, AZURE_PL_CONSOLIDATE | terminate, consolidate |
| App Service | AZURE_WEBAPP_IDLE_TERMINATE, AZURE_WEBAPP_STEP_DOWN | terminate, step_down |
| Azure Bot Service | AZURE_BOT_IDLE_TERMINATE, AZURE_BOT_TIER_DOWNGRADE | terminate, tier_downgrade |
| Azure Synapse Analytics | AZURE_SYNAPSE_IDLE_TERMINATE, AZURE_SYNAPSE_TIER_DOWNGRADE, AZURE_SYNAPSE_CONSOLIDATE | terminate, tier_downgrade, consolidate |
| Azure Virtual Network | AZURE_VNET_IDLE_TERMINATE, AZURE_VNET_CONSOLIDATE | terminate, consolidate |

Services with real SKU catalog data (VM, Redis, Disk, AKS, App Service) use pricing-aware strategies (`step_down`, `match_util`, `tier_downgrade`, `migrate_type`). Services without meaningful SKU data (CosmosDB, EventHubs, PrivateLink, VNet, etc.) use `consolidate` with `default_savings_pct` matching the existing per-service percentages as fallback.

### GCP
- Class-based architecture: each service has its own recommender class
- Cloud Monitoring provides metrics (different naming convention from AWS CloudWatch)
- Large codebase (>256KB) - service classes include: ComputeEngine, CloudSQL, GCS, BigQuery, Bigtable, Memorystore, VertexAI, ArtifactRegistry, CloudFunctions, Firestore

### OCI
- No separate metrics API - uses billing patterns for cost-based recommendations
- OCI Optimizer recommendations come from the native OCI API (already in bronze)
- Silver job enriches Optimizer recommendations with cost data from billing table
- Tags analysis from `bronze_oci_tags_tbl` for untagged resource detection

### Alibaba
- Billing-only cloud with no metrics equivalent
- Unique discount optimization comparing gross vs discounted amounts
- RI/Savings Plan opportunities identified by discount ratio < 10%
- Estimated 25% savings from Reserved Instances for eligible resources

---

## Appendix A: Quick Reference - Config Access API

The engine exposes these methods for silver jobs to access JSON config:

```python
# Get a recommendation threshold
threshold = engine.get_threshold("cpu_idle_threshold", default=5.0)

# Get cloud-specific provider config
aws_config = engine.get_provider_config("aws")

# Get service mapping for a cloud
mapping = engine.get_engine_service_mapping("aws")
# Returns: {"ec2": {"service_name": "Amazon EC2", "service_category": "Compute"}, ...}

# Get storage migration map
migration_map = engine.get_storage_migration_map("aws")
# Returns: {"io1": "gp3", "gp2": "gp3", ...}

# Get all loaded mappings
all_mappings = engine.get_mappings()
```

## Appendix B: Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `recommended_sku` is None | SKU not in catalog, or no matching rule | Run `refresh_catalog.py` to populate SKU catalog |
| Price is 0.0 | No CUR data, no catalog price, no config fallback | Add pricing to `provider_config.json` |
| "unknown" pricing_source | Pricing resolution chain exhausted | Check all 4 sources: CUR history, SKU catalog, config fallback |
| No rules matched | Service category mismatch or no active rules | Verify `rs_rightsizing_rules` has rules for the service category. For Azure, check that `service_name` in the rule matches the `engine_service_mapping` value in `provider_config.json` and that `service_category` matches the category used in `azure_pricing.py` (e.g., Redis uses "Cache", not "Database") |
| Azure engine returns no savings but fallback works | Engine rules missing or SKU catalog empty for service | Run `refresh_catalog.py` to seed SKU data. Check `seed_rightsizing_rules.py` has Azure-specific rules for the service. Engine falls back gracefully to per-service hardcoded percentages |
| Engine metadata missing from details JSON | Engine returned no result, or `_engine_result` not propagated | Check logs for "Engine call failed" or "falling back to rules". Verify `create_recommendation()` includes the `_engine_result` extraction block |
| Config not loading | S3 path mismatch or permissions | Check path: `s3://{bucket}/{prefix}/config/rightsizing_engine/` |
| New size tier not recognized | Ordinal not in `sizing_defaults.json` | Add the tier to `size_ordinal` with correct numeric value |
| Template variables not replaced | Variable name mismatch | Check `{variable_name}` matches the metrics/resource_data keys |
| Gold layer not updating | ON CONFLICT with RESOLVED status | Resolved recommendations are preserved; new ones go through |
