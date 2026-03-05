# Seeding the SKU Catalog

This guide explains how to seed, refresh, and maintain the right-sizing engine's SKU catalog. The catalog provides the pricing and sizing reference data that the recommendation engine uses to calculate savings and suggest target SKUs.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Architecture](#3-architecture)
4. [The 5-Step Seeding Process](#4-the-5-step-seeding-process)
5. [Iceberg Tables](#5-iceberg-tables)
6. [Configuration Files](#6-configuration-files)
7. [Running the Job](#7-running-the-job)
8. [Verification and Validation](#8-verification-and-validation)
9. [Operational Guide](#9-operational-guide)
10. [Troubleshooting](#10-troubleshooting)
11. [File Reference](#11-file-reference)

---

## 1. Overview

The SKU catalog is a set of 4 Apache Iceberg tables stored on S3, registered in the AWS Glue Data Catalog. They are populated by the `refresh_sku_catalog_job` Glue job, which orchestrates a 5-step pipeline:

| Step | What It Does | Target Table |
|------|-------------|--------------|
| 1 | Fetch live SKU data from cloud pricing APIs | `rs_cloud_sku_catalog` |
| 2 | Extract effective pricing from CUR/FOCUS billing data | `rs_cloud_sku_pricing_history` |
| 3 | Deactivate SKUs not updated in 90 days | `rs_cloud_sku_catalog` |
| 4 | Seed default right-sizing rules | `rs_rightsizing_rules` |
| 5 | Seed service config from Excel (optional) | `rs_finops_service_config` |

All 4 tables are stored at:
```
s3://{bucket}/{prefix}/reference/rightsizing/
```
Default: `s3://finomics-data-pod/warehouse/reference/rightsizing/`

Supports 5 cloud providers: **AWS, Azure, GCP, OCI, Alibaba**.

---

## 2. Prerequisites

Before running the seeding job, ensure the following are in place.

### 2.1 AWS Infrastructure

- **S3 bucket** exists (default: `finomics-data-pod`)
- **Iceberg database** exists in the AWS Glue Data Catalog:
  ```sql
  CREATE DATABASE IF NOT EXISTS finomics_iceberg_db
  LOCATION 's3://finomics-data-pod/warehouse/';
  ```

### 2.2 IAM Permissions

The Glue job execution role needs:

| Service | Permissions |
|---------|-------------|
| S3 | `GetObject`, `PutObject`, `ListBucket`, `DeleteObject` on the warehouse bucket |
| Glue Catalog | `GetDatabase`, `GetTable`, `CreateTable`, `UpdateTable`, `GetPartitions` |
| CloudWatch Logs | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` |

### 2.3 Bronze/Silver CUR Tables (for Step 2)

Step 2 extracts pricing from existing billing data tables. These must be populated by their respective ingestion pipelines before the CUR pricing extraction will produce results:

| Cloud | Table Name | Source |
|-------|-----------|--------|
| AWS | `silver_aws_cur_report_tbl` | AWS CUR pipeline |
| Azure | `bronze_azure_focus_report_tbl` | Azure FOCUS export pipeline |
| GCP | `silver_gcp_billing_export` | GCP billing export pipeline |
| OCI | `silver_oci_cost_usage_merged_tbl` | OCI cost/usage pipeline |
| Alibaba | `silver_alibaba_billing_tbl` | Alibaba billing pipeline |

> **Note**: Step 2 will gracefully skip any cloud whose CUR table does not exist. The job will not fail.

### 2.4 Cloud Credentials (for non-AWS clouds)

Step 1 fetches live pricing from cloud APIs. Credential requirements:

| Cloud | Credential Method | Environment Variables |
|-------|------------------|----------------------|
| AWS | Uses IAM role (no extra credentials) | — |
| Azure | No credentials needed (Retail Pricing API is public) | — |
| GCP | Service account JSON or ADC | `GCP_SERVICE_ACCOUNT_JSON` or `GOOGLE_APPLICATION_CREDENTIALS` |
| OCI | OCI config JSON or individual vars | `OCI_CONFIG_JSON` or `OCI_USER_OCID` + `OCI_TENANCY_OCID` + `OCI_FINGERPRINT` + `OCI_KEY_CONTENT` |
| Alibaba | Access key pair | `ALIBABA_ACCESS_KEY_ID` + `ALIBABA_ACCESS_KEY_SECRET` |

### 2.5 Configuration Files on S3

Upload the 3 JSON config files to S3 before the first run:
```bash
aws s3 cp src/s3/scripts/common/config/rightsizing_engine/provider_config.json \
  s3://finomics-data-pod/warehouse/config/rightsizing_engine/provider_config.json

aws s3 cp src/s3/scripts/common/config/rightsizing_engine/cur_mappings.json \
  s3://finomics-data-pod/warehouse/config/rightsizing_engine/cur_mappings.json

aws s3 cp src/s3/scripts/common/config/rightsizing_engine/sizing_defaults.json \
  s3://finomics-data-pod/warehouse/config/rightsizing_engine/sizing_defaults.json
```

### 2.6 Python Package on S3

The right-sizing engine code must be packaged and uploaded as a zip for the Glue job's `--extra-py-files`:
```bash
cd src/s3/scripts/common
zip -r rightsizing_engine.zip rightsizing_engine/
aws s3 cp rightsizing_engine.zip s3://finomics-data-pod/glue-scripts/common/rightsizing_engine.zip
```

### 2.7 Excel File (Optional, for Step 5)

If you want to seed service configs from the Excel spreadsheet:
```bash
aws s3 cp Combined_Finops_Metrics_Recos.xlsx \
  s3://finomics-data-pod/config/Combined_Finops_Metrics_Recos.xlsx
```

---

## 3. Architecture

### 3.1 Data Flow

```
                    ┌──────────────────────────────────┐
                    │  refresh_sku_catalog_job (Glue)   │
                    │  Entry point, parameter handling   │
                    └──────────────┬───────────────────┘
                                   │
                    ┌──────────────▼───────────────────┐
                    │  refresh_catalog() orchestrator    │
                    │  Loops per cloud_provider          │
                    └──────────────┬───────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
    ┌─────▼──────┐          ┌─────▼──────┐          ┌──────▼─────┐
    │   Step 1    │          │   Step 2    │          │   Step 3   │
    │ SKU Refresh │          │ CUR Pricing │          │ Stale Deact│
    │ (APIs)      │          │ (Billing)   │          │ (90 days)  │
    └─────┬──────┘          └─────┬──────┘          └──────┬─────┘
          │                        │                        │
          ▼                        ▼                        ▼
   rs_cloud_sku_catalog    rs_cloud_sku_            rs_cloud_sku_catalog
   (MERGE)                 pricing_history          (UPDATE is_active=FALSE)
                           (MERGE)

    ┌──────────┐          ┌────────────┐
    │  Step 4   │          │   Step 5    │
    │  Rules    │          │ Excel Config│
    │  (once)   │          │  (once)     │
    └─────┬────┘          └─────┬──────┘
          │                      │
          ▼                      ▼
   rs_rightsizing_rules     rs_finops_service_config
   (MERGE)                  (MERGE)
```

### 3.2 Key Design Decisions

- **Iceberg MERGE semantics**: All writes use `MERGE INTO` (upsert). Running the job multiple times is safe and idempotent.
- **Steps 4 and 5 run only once**: When processing multiple clouds, rules and service config are seeded only on the first cloud iteration because they are global (not cloud-specific).
- **Graceful failures**: Each step is wrapped in try/except. If Step 1 fails, Steps 2-5 still execute. The summary reports which steps succeeded.
- **Config overlay pattern**: JSON configs on S3 overlay hardcoded defaults. If a config file is missing, the engine falls back to embedded defaults in the code.

---

## 4. The 5-Step Seeding Process

### Step 1: Refresh SKU Catalog from Cloud Pricing APIs

**Purpose**: Fetch live SKU data (instance types, pricing, specs) from each cloud's pricing API and write to `rs_cloud_sku_catalog`.

**How it works**:

1. The orchestrator dispatches to a cloud-specific seeder:
   - `seed_sku_catalog_aws.py` → AWS Pricing API
   - `seed_sku_catalog_azure.py` → Azure Retail Pricing API
   - `seed_sku_catalog_gcp.py` → GCP Cloud Billing API
   - `seed_sku_catalog_oci.py` → OCI List Shapes API
   - `seed_sku_catalog_alibaba.py` → Alibaba ECS DescribeInstanceTypes

2. Each seeder creates a `PricingProvider` instance which:
   - Reads service filter templates from `provider_config.json`
   - Iterates over configured regions
   - Calls the cloud pricing API with pagination
   - Parses each price item into a normalized record

3. Records are collected into a Spark DataFrame and merged into Iceberg via `IcebergStore.write_sku_catalog()`.

**Azure example** (how the Retail API is called):

The Azure seeder iterates over 16 services, each with an API filter template:

| Service | API Filter |
|---------|-----------|
| Virtual Machines | `serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and priceType eq 'Consumption'` |
| Azure Cache for Redis | `serviceName eq 'Redis Cache' and armRegionName eq '{region}' and priceType eq 'Consumption'` |
| Managed Disks | `serviceName eq 'Storage' and ... and contains(productName, 'Managed Disks')` |
| ... | ... |

The API endpoint is `https://prices.azure.com/api/retail/prices` (public, no auth required). Responses are paginated via `NextPageLink`.

**Deduplication**: Before merging, `write_sku_catalog()` deduplicates on the merge keys `(cloud_provider, service_name, sku_code, region, pricing_model)`, keeping the row with the lowest `hourly_price` (to prefer base/Linux pricing over Windows/RHEL variants).

---

### Step 2: Extract CUR Pricing from Billing Data

**Purpose**: Compute actual effective prices (after discounts, RI/SP, credits) from CUR/FOCUS billing tables and write to `rs_cloud_sku_pricing_history`.

**How it works**:

1. For each cloud, a SQL query reads from the corresponding bronze/silver CUR table.
2. The query computes:
   ```
   effective_price = SUM(cost) / NULLIF(SUM(usage_amount), 0)
   ```
   grouped by `(service, sku, region, pricing_model, account_id, date)`.
3. Pricing models are classified: `on_demand`, `reserved_1yr`, `spot`, `savings_plan`.
4. Results are merged into `rs_cloud_sku_pricing_history` via Iceberg MERGE.

**Azure-specific details**:

Reads from `bronze_azure_focus_report_tbl` using these FOCUS columns:

| FOCUS Column | Maps To |
|-------------|---------|
| `x_SkuMeterCategory` | `service_name` |
| `COALESCE(x_SkuMeterSubcategory, x_SkuMeterName)` | `sku_code` |
| `RegionName` | `region` |
| `BilledCost` | cost amount |
| `PricingCategory` | pricing model |
| `ChargePeriodStart` | usage date |
| `SubAccountId` | subscription ID (extracted via regex) |

**Why this matters**: The `rs_cloud_sku_pricing_history` table gives the engine customer-specific pricing. When the recommendation engine calculates savings, it prefers CUR effective prices over list prices because they reflect the customer's actual negotiated rates.

---

### Step 3: Deactivate Stale SKUs

**Purpose**: Mark SKUs as inactive if they haven't been updated in 90 days, preventing recommendations to retired or deprecated instance types.

**How it works**:

```sql
UPDATE rs_cloud_sku_catalog
SET is_active = FALSE
WHERE cloud_provider = '{cloud}'
  AND is_active = TRUE
  AND last_updated < DATE_SUB(CURRENT_TIMESTAMP(), 90)
```

The `stale_days` parameter defaults to 90 but can be overridden in the `refresh_catalog()` function call.

**When SKUs become stale**:
- Cloud provider retires an instance type (e.g., AWS previous-gen c3/m3)
- A service is discontinued
- A region is deprecated

---

### Step 4: Seed Right-Sizing Rules

**Purpose**: Populate `rs_rightsizing_rules` with default sizing rules that tell the engine HOW to right-size a resource (which sizing method to use, what savings percentage to apply).

**How it works**:

1. `seed_rightsizing_rules.py` defines a `DEFAULT_RULES` list with 45 rule templates:
   - **19 generic rules** (cloud_provider=`"*"`) covering 9 service categories
   - **26 Azure-specific rules** with explicit `service_name` for higher-priority matching

2. Generic rules (cloud_provider=`"*"`) are expanded to all 5 clouds, producing **19 x 5 = 95** rows.

3. Azure-specific rules add **26** more rows.

4. Total: **121 rows** written via Iceberg MERGE.

**Rule structure**:

| Field | Description | Example |
|-------|------------|---------|
| `cloud_provider` | Target cloud | `"azure"` |
| `service_category` | Service category | `"Compute"` |
| `service_name` | Specific service (empty = all in category) | `"Virtual Machines"` |
| `rule_code` | Unique rule identifier | `"AZURE_VM_STEP_DOWN"` |
| `action_type` | What to recommend | `"downsize_vcpu"` |
| `sizing_method` | Algorithm to use | `"step_down"` |
| `sizing_params` | JSON parameters for the method | `{"steps": 1, "min_vcpu": 1}` |
| `default_savings_pct` | Fallback savings % | `30.0` |
| `priority` | Lower = higher priority | `2` |

**The 6 sizing methods**:

| Method | Description | Used For |
|--------|------------|----------|
| `terminate` | Remove the resource entirely | Idle resources (100% savings) |
| `step_down` | Move 1 size down in the SKU family | Underutilized VMs, AKS nodes |
| `match_util` | Find SKU matching actual utilization + headroom | VMs with known CPU/memory data |
| `tier_downgrade` | Drop to a lower tier | Premium → Standard Redis, SQL |
| `migrate_type` | Switch storage/resource type | Premium SSD → Standard SSD |
| `consolidate` | Merge underutilized resources | Cosmos DB, Load Balancers |

**Idempotency**: Uses Iceberg MERGE on keys `(cloud_provider, service_category, rule_code)`. Re-running the job updates existing rules without creating duplicates.

---

### Step 5: Seed Service Config from Excel (Optional)

**Purpose**: Parse the `Combined_Finops_Metrics_Recos.xlsx` spreadsheet to populate `rs_finops_service_config` with service-level thresholds, metrics, and recommended actions.

**When to use**: Only needed if you have the Excel file. The engine can function without this table, falling back to the rules in Step 4.

**Excel structure**:

The file has 5 sheets (one per cloud: Azure, AWS, GCP, OCI, Alibaba), each with these columns:

| Column | Description | Example |
|--------|------------|---------|
| Service Category | Category grouping | "Compute" |
| {Cloud} Service | Service name | "Virtual Machines" |
| Primary FinOps Metric | What to measure | "CPU Utilization %" |
| Unit of Measurement | Metric unit | "Percentage" |
| Underutilization Indicator | Free-text threshold | "CPU < 10% over 14 days" |
| Recommended Actions | What to do | "Downsize to smaller SKU" |
| Threshold Source/Reference | Where the threshold comes from | "AWS Well-Architected" |
| Additional Notes | Optional notes | — |

**Parsing**:

The seeder performs two types of inference:

1. **Threshold parsing**: Converts free-text indicators into structured JSON:
   ```
   "CPU < 10% over 14 days"
   →
   {"conditions": [{"metric": "cpu", "operator": "lt", "threshold": 10,
                     "unit": "percent", "window_days": 14}]}
   ```

2. **Sizing strategy inference**: Determines the right-sizing approach from the recommended actions text:
   ```
   "Terminate idle VM"         → strategy: "terminate"
   "Migrate gp2 to gp3"       → strategy: "migrate_type"
   "Downgrade to Standard"     → strategy: "tier_downgrade"
   "Downsize to smaller vCPU"  → strategy: "downsize"
   ```

---

## 5. Iceberg Tables

All tables are stored at `s3://{bucket}/{prefix}/reference/rightsizing/` and registered as `glue_catalog.{iceberg_db}.{table_name}`.

### 5.1 rs_cloud_sku_catalog

The main catalog of available cloud SKUs with pricing and specs.

**Schema**:
| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | STRING NOT NULL | aws, azure, gcp, oci, alibaba |
| `service_category` | STRING NOT NULL | Compute, Storage, Databases, etc. |
| `service_name` | STRING NOT NULL | Virtual Machines, Azure Cache for Redis, etc. |
| `sku_code` | STRING NOT NULL | Standard_D4s_v5, P1v3, etc. |
| `sku_family` | STRING | D, E, F (VM family letter) |
| `sku_generation` | STRING | current |
| `sku_size_ordinal` | INT | Ordinal for size ordering |
| `tier` | STRING | Basic, Standard, Premium |
| `region` | STRING NOT NULL | eastus, westus2, etc. |
| `vcpus` | DOUBLE | Number of vCPUs |
| `memory_gb` | DOUBLE | Memory in GB |
| `gpu_count` | INT | GPU count |
| `gpu_type` | STRING | GPU model |
| `storage_gb` | DOUBLE | Included storage |
| `storage_type` | STRING | SSD, HDD |
| `max_iops` | INT | Max IOPS |
| `max_throughput_mbps` | DOUBLE | Max throughput |
| `network_bandwidth_gbps` | DOUBLE | Network bandwidth |
| `hourly_price` | DOUBLE | Price per hour |
| `monthly_price` | DOUBLE | Price per month (hourly x 730) |
| `pricing_unit` | STRING | Hour, Month, GB-Month, etc. |
| `pricing_model` | STRING | on_demand, reserved_1yr, spot |
| `currency` | STRING | USD |
| `specs` | STRING | JSON with extra metadata |
| `is_active` | BOOLEAN | FALSE = stale/retired |
| `is_burstable` | BOOLEAN | TRUE for B-series VMs, T-series, etc. |
| `last_updated` | TIMESTAMP | When this row was last refreshed |
| `source` | STRING | "api" or "cur" |
| `job_runtime_utc` | TIMESTAMP | Job execution timestamp |

**Merge keys**: `(cloud_provider, service_name, sku_code, region, pricing_model)`
**Partitioned by**: `cloud_provider`

### 5.2 rs_cloud_sku_pricing_history

Time-series of actual customer pricing extracted from CUR/FOCUS billing data.

**Schema**:
| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | STRING NOT NULL | Cloud provider |
| `service_name` | STRING NOT NULL | Service name |
| `sku_code` | STRING NOT NULL | SKU identifier |
| `region` | STRING NOT NULL | Region |
| `pricing_model` | STRING | on_demand, reserved_1yr, spot, savings_plan |
| `effective_price` | DOUBLE NOT NULL | SUM(cost) / SUM(usage) |
| `list_price` | DOUBLE | Retail list price (if available) |
| `pricing_unit` | STRING | Hour, Usage, etc. |
| `currency` | STRING | USD |
| `account_id` | STRING | Cloud account/subscription ID |
| `usage_quantity` | DOUBLE | Total usage in the period |
| `usage_date` | DATE NOT NULL | Billing date |
| `record_count` | INT | Number of billing line items aggregated |
| `last_updated` | TIMESTAMP | Row update timestamp |
| `year_month` | STRING | Partition key (e.g., "2026-02") |
| `job_runtime_utc` | TIMESTAMP | Job execution timestamp |

**Merge keys**: `(cloud_provider, service_name, sku_code, region, pricing_model, account_id, usage_date)`
**Partitioned by**: `(cloud_provider, year_month)`

### 5.3 rs_rightsizing_rules

Sizing rules that define HOW to right-size each service category.

**Schema**:
| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | STRING NOT NULL | Cloud provider |
| `service_category` | STRING NOT NULL | Compute, Storage, etc. |
| `service_name` | STRING | Specific service (empty = all in category) |
| `rule_code` | STRING NOT NULL | Unique rule identifier |
| `action_type` | STRING NOT NULL | terminate, downsize_vcpu, migrate_type, etc. |
| `sizing_method` | STRING NOT NULL | step_down, match_util, tier_downgrade, etc. |
| `sizing_params` | STRING | JSON parameters for the sizing method |
| `default_savings_pct` | DOUBLE | Fallback savings percentage |
| `priority` | INT | Lower = higher priority |
| `is_active` | BOOLEAN | Whether the rule is active |
| `job_runtime_utc` | TIMESTAMP | Job execution timestamp |

**Merge keys**: `(cloud_provider, service_category, rule_code)`
**No partitioning** (small table, ~121 rows)

### 5.4 rs_finops_service_config

Service-level configuration parsed from the Excel spreadsheet.

**Schema**:
| Column | Type | Description |
|--------|------|-------------|
| `cloud_provider` | STRING NOT NULL | Cloud provider |
| `service_category` | STRING NOT NULL | Service category |
| `service_name` | STRING NOT NULL | Service name |
| `primary_metric` | STRING | Primary metric to track |
| `unit_of_measurement` | STRING | Metric unit |
| `underutilization_indicator` | STRING | Raw threshold text |
| `recommended_actions` | STRING | Raw action text |
| `threshold_source` | STRING | Reference source |
| `additional_notes` | STRING | Notes |
| `threshold_config` | STRING | Parsed JSON threshold conditions |
| `sizing_strategy` | STRING | Inferred strategy |
| `sizing_dimension` | STRING | Inferred dimension |
| `cur_service_code` | STRING | CUR product code mapping |
| `cur_sku_field` | STRING | CUR SKU column name |
| `is_active` | BOOLEAN | Whether active |
| `created_at` | TIMESTAMP | First creation time |
| `updated_at` | TIMESTAMP | Last update time |
| `job_runtime_utc` | TIMESTAMP | Job execution timestamp |

**Merge keys**: `(cloud_provider, service_name)`
**Partitioned by**: `cloud_provider`

---

## 6. Configuration Files

Three JSON config files are loaded from S3 at runtime. They allow updating reference data without redeploying the engine code.

**S3 location**: `s3://{bucket}/{prefix}/config/rightsizing_engine/`

### 6.1 provider_config.json

The largest and most important config file. Contains per-cloud configuration:

- **`regions`**: Default regions to seed SKUs for
- **`service_filter_templates`** (Azure): API query filters per service
- **`engine_service_mapping`**: Maps internal service codes to canonical names used by the engine
- **`memory_ratio_estimates`** (Azure): vCPU-to-memory ratios for VM families
- **`ec2_instance_pricing`** (AWS): Fallback hourly prices
- **`disk_type_mapping`** (Azure): Disk type name normalization
- **`redis_downgrade_map`** (Azure): Redis tier downgrade paths

### 6.2 cur_mappings.json

Maps between CUR/billing column names and engine service names:

- **`service_code_map`**: Excel service name → CUR product code per cloud
- **`sku_field_map`**: Which CUR column holds the SKU per cloud
- **`reverse_service_code_map`**: CUR code → canonical service name
- **`cur_billing_columns`**: Per-cloud cost/usage column names

### 6.3 sizing_defaults.json

Sizing constants and ordinals:

- **`size_ordinal`**: Instance size ordering (nano=1, micro=2, ..., metal=16)
- **`azure_tier_ordinal`**: Azure tier ordering (free=1, basic=2, ..., enterprise=5)
- **`hours_per_month`**: 730.0
- **`hours_per_year`**: 8760.0
- **`recommendation_thresholds`**: Default CPU/memory threshold values
- **`storage_migration_maps`**: Per-cloud storage type migration paths

### Config Loading

The `S3ConfigLoader` class reads these files from S3 using Spark's `wholeTextFiles()` (no boto3 dependency). Files are cached in-memory for the duration of the job. If a file is missing, the engine falls back to hardcoded defaults in `constants.py` and the provider modules.

---

## 7. Running the Job

### 7.1 Glue Job Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `--CLOUD_PROVIDER` | Yes | — | `aws`, `azure`, `gcp`, `oci`, `alibaba`, or `all` |
| `--ICEBERG_DB` | Yes | — | Iceberg database name (e.g., `finomics_iceberg_db`) |
| `--BUCKET` | No | `finomics-data-pod` | S3 bucket for Iceberg warehouse |
| `--PREFIX` | No | `warehouse` | S3 prefix for Iceberg warehouse |
| `--REGIONS` | No | Provider defaults | Comma-separated regions (e.g., `eastus,westus2`) |
| `--CUR_LOOKBACK_DAYS` | No | `30` | Days of billing data to process |
| `--SEED_RULES` | No | `true` | Whether to seed right-sizing rules |
| `--EXCEL_PATH` | No | — | S3/local path to the Excel file |

### 7.2 First-Time Full Seed (All Clouds)

This is the recommended initial seeding command. It runs all 5 steps for all 5 clouds:

```bash
aws glue start-job-run \
  --job-name refresh_sku_catalog_job \
  --arguments '{
    "--CLOUD_PROVIDER": "all",
    "--ICEBERG_DB": "finomics_iceberg_db",
    "--BUCKET": "finomics-data-pod",
    "--PREFIX": "warehouse",
    "--CUR_LOOKBACK_DAYS": "30",
    "--SEED_RULES": "true",
    "--EXCEL_PATH": "s3://finomics-data-pod/config/Combined_Finops_Metrics_Recos.xlsx"
  }'
```

**What happens**:
1. Iceberg tables are auto-created if they don't exist (`store.ensure_tables_exist()`)
2. SKU data is fetched from all 5 cloud pricing APIs
3. CUR pricing is extracted from all available billing tables
4. Stale SKUs are deactivated (none on first run)
5. 121 right-sizing rules are seeded
6. Service configs are parsed from the Excel file

### 7.3 Single-Cloud Refresh (e.g., Azure only)

For refreshing just one cloud, without re-seeding rules:

```bash
aws glue start-job-run \
  --job-name refresh_sku_catalog_job \
  --arguments '{
    "--CLOUD_PROVIDER": "azure",
    "--ICEBERG_DB": "finomics_iceberg_db",
    "--REGIONS": "eastus,westus2,centralus,westeurope",
    "--CUR_LOOKBACK_DAYS": "7",
    "--SEED_RULES": "false"
  }'
```

### 7.4 Rules-Only Re-Seed

To re-seed rules after modifying `seed_rightsizing_rules.py` (e.g., adding new Azure-specific rules), run for any single cloud with rules enabled. Step 1 will still fetch SKUs but that's harmless:

```bash
aws glue start-job-run \
  --job-name refresh_sku_catalog_job \
  --arguments '{
    "--CLOUD_PROVIDER": "azure",
    "--ICEBERG_DB": "finomics_iceberg_db",
    "--SEED_RULES": "true"
  }'
```

### 7.5 Scheduled Recurring Refresh

For production, schedule a daily or weekly refresh via EventBridge:

```json
{
  "ScheduleExpression": "cron(0 2 * * ? *)",
  "Description": "Daily SKU catalog refresh at 2 AM UTC",
  "Target": {
    "Arn": "arn:aws:glue:us-east-1:123456789012:job/refresh_sku_catalog_job",
    "RoleArn": "arn:aws:iam::123456789012:role/EventBridgeGlueRole",
    "Input": "{\"--CLOUD_PROVIDER\":\"all\",\"--ICEBERG_DB\":\"finomics_iceberg_db\",\"--CUR_LOOKBACK_DAYS\":\"1\",\"--SEED_RULES\":\"false\"}"
  }
}
```

> **Tip**: For daily scheduled runs, set `--SEED_RULES` to `false` (rules don't change daily) and `--CUR_LOOKBACK_DAYS` to `1` (only process yesterday's billing data).

### 7.6 Glue Job Properties

```python
{
    "Name": "refresh_sku_catalog_job",
    "Command": {
        "Name": "glueetl",
        "ScriptLocation": "s3://{bucket}/glue-scripts/common/refresh_sku_catalog_job.py",
        "PythonVersion": "3"
    },
    "DefaultArguments": {
        "--extra-py-files": "s3://{bucket}/glue-scripts/common/rightsizing_engine.zip",
        "--enable-glue-datacatalog": "true",
        "--enable-spark-ui": "true",
        "--spark-event-logs-path": "s3://{bucket}/glue-logs/",
        "--enable-metrics": "true",
        "--enable-continuous-cloudwatch-log": "true",
        "--job-language": "python",
        "--TempDir": "s3://{bucket}/glue-temp/"
    },
    "GlueVersion": "4.0",
    "NumberOfWorkers": 10,
    "WorkerType": "G.1X",
    "Timeout": 2880
}
```

---

## 8. Verification and Validation

After running the job, verify each table was populated correctly.

### 8.1 Check Tables Exist

```sql
SHOW TABLES IN glue_catalog.finomics_iceberg_db LIKE 'rs_%';
```

Expected: 4 tables (`rs_cloud_sku_catalog`, `rs_cloud_sku_pricing_history`, `rs_finops_service_config`, `rs_rightsizing_rules`).

### 8.2 Verify SKU Catalog

```sql
-- Overall counts per cloud and service
SELECT cloud_provider, service_name, COUNT(*) as sku_count
FROM glue_catalog.finomics_iceberg_db.rs_cloud_sku_catalog
WHERE is_active = TRUE
GROUP BY cloud_provider, service_name
ORDER BY cloud_provider, sku_count DESC;
```

For Azure, you should see entries for all 16 services (Virtual Machines, Redis, SQL DB, Cosmos DB, etc.).

```sql
-- Check a specific SKU
SELECT sku_code, region, hourly_price, monthly_price, vcpus, memory_gb, tier
FROM glue_catalog.finomics_iceberg_db.rs_cloud_sku_catalog
WHERE cloud_provider = 'azure'
  AND service_name = 'Virtual Machines'
  AND sku_code = 'Standard_D4s_v5'
  AND region = 'eastus'
  AND is_active = TRUE;
```

### 8.3 Verify Pricing History

```sql
-- Coverage by cloud
SELECT cloud_provider,
       COUNT(DISTINCT sku_code) as unique_skus,
       MIN(usage_date) as earliest_date,
       MAX(usage_date) as latest_date,
       COUNT(*) as total_records
FROM glue_catalog.finomics_iceberg_db.rs_cloud_sku_pricing_history
GROUP BY cloud_provider;
```

### 8.4 Verify Rules

```sql
-- Rules per cloud and category
SELECT cloud_provider, service_category, service_name, COUNT(*) as rule_count
FROM glue_catalog.finomics_iceberg_db.rs_rightsizing_rules
WHERE is_active = TRUE
GROUP BY cloud_provider, service_category, service_name
ORDER BY cloud_provider, service_category;
```

Expected: ~121 total rules. For Azure specifically, you should see Azure-specific rules with non-empty `service_name` (e.g., "Virtual Machines", "Azure Cache for Redis").

```sql
-- List all Azure-specific rules
SELECT rule_code, service_name, sizing_method, default_savings_pct, priority
FROM glue_catalog.finomics_iceberg_db.rs_rightsizing_rules
WHERE cloud_provider = 'azure'
  AND service_name != ''
  AND is_active = TRUE
ORDER BY service_name, priority;
```

### 8.5 Verify Service Config

```sql
-- Configs per cloud
SELECT cloud_provider, COUNT(*) as service_count
FROM glue_catalog.finomics_iceberg_db.rs_finops_service_config
WHERE is_active = TRUE
GROUP BY cloud_provider;
```

### 8.6 Check Job Logs

All job steps log with `[FLOW]` tags. Search CloudWatch Logs for the job run:

```
[FLOW] refresh_catalog: Step 1/5 complete - 12500 SKU records refreshed
[FLOW] refresh_catalog: Step 2/5 complete - 3400 CUR pricing records extracted
[FLOW] refresh_catalog: Step 3/5 complete - 0 stale SKUs deactivated
[FLOW] refresh_catalog: Step 4/5 complete - 121 rightsizing rules seeded
[FLOW] refresh_catalog: Step 5/5 complete - 210 service configs seeded
```

---

## 9. Operational Guide

### 9.1 Adding New Right-Sizing Rules

To add new rules, edit `seed_rightsizing_rules.py`:

1. Add rule templates to the `DEFAULT_RULES` list
2. For cloud-specific rules, set `cloud_provider` to the cloud string (e.g., `"azure"`)
3. For generic rules applicable to all clouds, set `cloud_provider: "*"`
4. Re-run the job with `--SEED_RULES true`

Example — adding a new Azure SQL rule:
```python
{
    "cloud_provider": "azure",
    "service_category": "Databases",
    "service_name": "Azure SQL Database",
    "rule_code": "AZURE_SQL_IDLE_TERMINATE",
    "action_type": "terminate",
    "sizing_method": "terminate",
    "sizing_params": {},
    "default_savings_pct": 100.0,
    "priority": 1,
},
```

### 9.2 Adding a New Cloud Service to the SKU Catalog

To add SKU fetching for a new Azure service:

1. Add a filter template entry in `provider_config.json` under `azure.service_filter_templates`:
   ```json
   "Azure SignalR Service": {
       "service_name": "Azure SignalR Service",
       "service_category": "Web",
       "filter_template": "serviceName eq 'SignalR' and armRegionName eq '{region}' and priceType eq 'Consumption'"
   }
   ```

2. Add an `engine_service_mapping` entry in `provider_config.json`:
   ```json
   "SIGNALR": {"service_name": "Azure SignalR Service", "service_category": "Web"}
   ```

3. Add right-sizing rules in `seed_rightsizing_rules.py`.

4. Upload updated `provider_config.json` to S3 and re-run the job.

### 9.3 Updating Regions

To change which regions are seeded, either:

- Pass `--REGIONS eastus,westus2,centralus` to the Glue job
- Or update the `regions` array in `provider_config.json` under the cloud's section

### 9.4 Adjusting Stale SKU Threshold

The default stale threshold is 90 days. To change it, modify the `stale_days` parameter in the `refresh_catalog()` call. Currently this is not exposed as a Glue job parameter, so you would need to modify `refresh_catalog.py` or add a new `--STALE_DAYS` parameter to the Glue job.

### 9.5 Repackaging After Code Changes

After modifying any engine code, repackage and upload:
```bash
cd src/s3/scripts/common
zip -r rightsizing_engine.zip rightsizing_engine/
aws s3 cp rightsizing_engine.zip s3://finomics-data-pod/glue-scripts/common/rightsizing_engine.zip
```

---

## 10. Troubleshooting

### Table Does Not Exist

**Symptom**: `Table not found: glue_catalog.finomics_iceberg_db.rs_cloud_sku_catalog`

**Fix**: The job auto-creates tables via `store.ensure_tables_exist()`. If this fails, run the migration script directly:
```python
from rightsizing_engine.catalog_scripts.migrations.create_iceberg_tables import create_all_tables
create_all_tables(spark, "finomics_iceberg_db", "finomics-data-pod", "warehouse")
```

### Zero SKUs Fetched for Azure

**Symptom**: Step 1 returns 0 SKU records for Azure.

**Possible causes**:
- Network connectivity issue (Glue job can't reach `https://prices.azure.com`)
- `provider_config.json` not found on S3 (check `[FLOW]` logs for "Provider config not available")
- Invalid service filter templates in `provider_config.json`

**Debug**: Check CloudWatch Logs for `ERROR fetching Azure` messages.

### Zero CUR Pricing Records

**Symptom**: Step 2 returns 0 records.

**Possible causes**:
- The bronze/silver CUR table doesn't exist for that cloud
- No billing data within the lookback period
- All billing rows have `BilledCost <= 0` or `chargeclass` is not null (credits/adjustments)

**Debug**: Run the diagnostic query that Step 2 executes:
```sql
SELECT COUNT(*) as total_rows,
       MIN(ChargePeriodStart) as min_date,
       MAX(ChargePeriodStart) as max_date,
       SUM(CASE WHEN BilledCost > 0 THEN 1 ELSE 0 END) as rows_with_cost
FROM glue_catalog.finomics_iceberg_db.bronze_azure_focus_report_tbl;
```

### Rules Not Taking Effect

**Symptom**: Engine doesn't use the seeded rules.

**Check**:
1. Verify rules are in the table:
   ```sql
   SELECT * FROM glue_catalog.finomics_iceberg_db.rs_rightsizing_rules
   WHERE cloud_provider = 'azure' AND is_active = TRUE;
   ```
2. Verify `service_name` in rules matches `engine_service_mapping` in `provider_config.json`
3. Verify `service_category` matches (e.g., Redis uses "Cache" not "Database")

### Duplicate SKU Records

**Symptom**: Same SKU appears multiple times.

**Cause**: Cloud APIs return multiple variants (Linux vs Windows pricing). The `write_sku_catalog()` method deduplicates by keeping the lowest `hourly_price` per merge key. If duplicates persist, check that the merge keys are correct.

### Spark/Iceberg Errors

**Common issues**:
- `AnalysisException: Table already exists`: This shouldn't happen with `CREATE TABLE IF NOT EXISTS`, but can occur with stale Glue catalog metadata. Try running `REFRESH TABLE` or dropping and recreating.
- `java.lang.OutOfMemoryError`: Increase Glue worker count or worker type (G.2X instead of G.1X).

---

## 11. File Reference

| File | Purpose |
|------|---------|
| `src/glue/common/refresh_sku_catalog_job.py` | Glue job entry point — parameter handling, Spark init, loop over clouds |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/refresh_catalog.py` | 5-step orchestrator — dispatches to each step, collects summary |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_sku_catalog_azure.py` | Azure SKU seeder — fetches from Retail Pricing API |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_sku_catalog_aws.py` | AWS SKU seeder |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_sku_catalog_gcp.py` | GCP SKU seeder |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_sku_catalog_oci.py` | OCI SKU seeder |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_sku_catalog_alibaba.py` | Alibaba SKU seeder |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/update_pricing_from_cur.py` | CUR pricing extraction — SQL queries per cloud |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_rightsizing_rules.py` | Default rules seeder — 45 rule templates |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/seed_service_config.py` | Excel parser — threshold and strategy inference |
| `src/s3/scripts/common/rightsizing_engine/catalog_scripts/migrations/create_iceberg_tables.py` | DDL — creates all 4 Iceberg tables |
| `src/s3/scripts/common/rightsizing_engine/iceberg_store.py` | Storage layer — read/write/merge for all 4 tables |
| `src/s3/scripts/common/rightsizing_engine/constants.py` | Enums, defaults, mappings, S3 defaults |
| `src/s3/scripts/common/rightsizing_engine/config_loader_s3.py` | S3 JSON config loader with caching |
| `src/s3/scripts/common/rightsizing_engine/providers/azure_pricing.py` | Azure Retail Pricing API client |
| `src/s3/scripts/common/config/rightsizing_engine/provider_config.json` | Per-cloud provider configuration |
| `src/s3/scripts/common/config/rightsizing_engine/cur_mappings.json` | CUR column and service code mappings |
| `src/s3/scripts/common/config/rightsizing_engine/sizing_defaults.json` | Sizing ordinals and thresholds |
