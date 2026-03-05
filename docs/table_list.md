## RDS (PostgreSQL) Tables

| # | Table | Usage | Purpose |
|---|-------|-------|---------|
| 1 | `custom_recommendations` | Write (UPSERT) | Main output table — stores recommendations for the frontend |
| 2 | `finops_threshold_rules` | Read | Client-specific threshold rules with conditions, templates, severity. FK referenced by `custom_recommendations.rule_id` |
| 3 | `base_finops_threshold_rules` | Read/Write | Master template for default rules. Synced to `finops_threshold_rules` per client/subscription |
| 4 | `service_configuration` | Read | Defines which cloud services are active and their metadata (service_code, category, resource_type) |
| 5 | `cloud_accounts` | Read | Cloud provider credentials (encrypted via `pgp_sym_decrypt`) — tenant/client/secret |
| 6 | `vmui_credentials` | Read | VictoriaMetrics credentials (encrypted) for metrics DB access |
| 7 | `clients` | Read | Client master table (fallback for company_id lookup) |
| 8 | `flow_progress` | Write (UPSERT) | ETL pipeline progress/status tracking |

**Deprecated RDS tables** (migrated to Iceberg, SQL retained for reference only):
- `cloud_sku_catalog` → now `rs_cloud_sku_catalog`
- `cloud_sku_pricing_history` → now `rs_cloud_sku_pricing_history`
- `finops_service_config` → now `rs_finops_service_config`
- `rightsizing_rules` → now `rs_rightsizing_rules`

---

## Athena / Iceberg Tables (Glue Catalog)

### SKU Catalog (Right-Sizing Engine)

| # | Table | R/W | Partitioned By | Purpose |
|---|-------|-----|---------------|---------|
| 1 | `rs_cloud_sku_catalog` | R/W | `cloud_provider` | Multi-cloud SKU catalog with specs and list prices |
| 2 | `rs_cloud_sku_pricing_history` | R/W | `cloud_provider`, `year_month` | CUR-derived effective pricing time-series |
| 3 | `rs_rightsizing_rules` | R/W | None | Sizing rules (45 templates → 121 rows) |
| 4 | `rs_finops_service_config` | R/W | `cloud_provider` | Service config from Excel (metrics, thresholds, actions) |

### Custom Recommendations Output

| # | Table | R/W | Partitioned By | Purpose |
|---|-------|-----|---------------|---------|
| 5 | `silver_azure_custom_recommendations` | Write | `client_id`, `account_id`, `year_month` | Final Azure recommendations (Iceberg copy alongside RDS) |

### Bronze/Silver CUR Tables (Read by SKU Catalog Step 2)

| # | Table | Cloud | Purpose |
|---|-------|-------|---------|
| 6 | `bronze_azure_focus_report_tbl` | Azure | Raw FOCUS billing data (EffectiveCost, x_SkuMeterCategory) |
| 7 | `silver_aws_cur_report_tbl` | AWS | Processed AWS CUR data |
| 8 | `silver_gcp_billing_export` | GCP | GCP billing export |
| 9 | `silver_oci_cost_usage_merged_tbl` | OCI | OCI cost/usage merged data |
| 10 | `silver_alibaba_billing_tbl` | Alibaba | Alibaba billing data |

### Bronze Metric/Resource Tables (Read by Recommendation Engine)

| # | Table | Purpose |
|---|-------|---------|
| 11 | `bronze_azure_metrics` | Azure Monitor API metric snapshots (CPU, memory, serverLoad, etc.) |
| 12 | `bronze_azure_{service}` | Per-service resource detail tables (populated by bronze custom recom job) |

---

### How They Connect

```
RDS                                    Iceberg (S3 / Glue Catalog)
──────────────────                     ──────────────────────────────
cloud_accounts          ─credentials─→  bronze jobs
vmui_credentials        ─metrics──────→  bronze_azure_metrics
service_configuration   ─which svc──→   azure_unified_recommendations.py
finops_threshold_rules  ─WHEN─────────→  rule evaluation
                                        ↕
                                        rs_rightsizing_rules ─HOW────→ sizing
                                        rs_cloud_sku_catalog ─SKUs──→ target SKU
                                        rs_cloud_sku_pricing_history → pricing
                                        ↓
custom_recommendations  ←──RDS write──  silver_azure_custom_recommendations
                                        (Iceberg write, same data)
```

**In short**: RDS tables drive orchestration and store frontend-facing results. Iceberg tables store all bulk data — billing, metrics, SKU catalog, and recommendations.