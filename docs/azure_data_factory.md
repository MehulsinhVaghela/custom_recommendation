# Azure Data Factory (ADF) — Service Guide

This guide covers the Azure Data Factory integration: what it monitors, how rules are evaluated, what savings each rule targets, and how to operate it.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Data Flow](#2-data-flow)
3. [Metrics Collected](#3-metrics-collected)
4. [Threshold Rules](#4-threshold-rules)
5. [Cost Savings Breakdown](#5-cost-savings-breakdown)
6. [Deployment](#6-deployment)
7. [Verification](#7-verification)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Overview

Azure Data Factory (ADF) is registered as service code `ADF` with service name `Azure Data Factory v2`. The pipeline collects 10 Azure Monitor metrics from each factory resource, evaluates 9 threshold rules, and generates cost optimization recommendations.

**Key identifiers:**

| Field | Value |
|-------|-------|
| Service code | `ADF` |
| Service name | `Azure Data Factory v2` |
| Cloud provider | `azure` |
| Resource type | `Microsoft.DataFactory/factories` |
| Category | `Integration` |
| Recommender class | `BaseAzureServiceRecommender` |
| Metrics table | `bronze_azure_metrics` |
| Resources table | `bronze_azure_adf` |

---

## 2. Data Flow

```
Bronze Layer                          Silver Layer                         RDS (Frontend)
─────────────                         ─────────────                        ───────────────

Azure Monitor API                     azure_unified_recommendations.py     custom_recommendations
  │                                     │                                    │
  ├─ PipelineSucceededRuns              ├─ Loads metrics from Iceberg        ├─ metric_name
  ├─ PipelineFailedRuns                 ├─ Loads rules from                  ├─ recommendation
  ├─ ActivitySucceededRuns              │  finops_threshold_rules             ├─ savings
  ├─ ActivityFailedRuns                 ├─ ThresholdRule.evaluate()           ├─ rule_code
  ├─ TriggerSucceededRuns               ├─ SavingsCalculator                 └─ details
  ├─ TriggerFailedRuns                  └─ _upsert_to_rds()
  ├─ IntegrationRuntimeCpuPercentage
  ├─ IntegrationRuntimeQueueLength      base_finops_threshold_rules
  ├─ IntegrationRuntimeAvailableMemory    │
  └─ IntegrationRuntimeAvgTaskPickup      └─ sync_base_rules_to_client_glue.py
       Delay                                  │
         │                                    └─ finops_threshold_rules
         ▼                                       (per client/subscription)
  bronze_azure_metrics (Iceberg)
```

### Metric-to-Recommendation Flow

The `metric_name` stored in `custom_recommendations` is determined by:

```
evaluation_logic.conditions[0].metric  →  metric_key  →  metric_name
```

This means `conditions[0].metric` in each rule **must** use the exact Iceberg metric name from `bronze_azure_metrics`. If it doesn't match, the recommendation's `metric_name` will be wrong.

---

## 3. Metrics Collected

The bronze job collects these metrics from Azure Monitor for each Data Factory resource:

| Metric Name | Type | Description |
|-------------|------|-------------|
| `PipelineSucceededRuns` | Count | Successful pipeline executions |
| `PipelineFailedRuns` | Count | Failed pipeline executions |
| `ActivitySucceededRuns` | Count | Successful activity executions within pipelines |
| `ActivityFailedRuns` | Count | Failed activity executions within pipelines |
| `TriggerSucceededRuns` | Count | Successful trigger fires |
| `TriggerFailedRuns` | Count | Failed trigger fires |
| `IntegrationRuntimeCpuPercentage` | Percent | Self-hosted IR CPU utilization |
| `IntegrationRuntimeQueueLength` | Count | Tasks waiting in IR queue |
| `IntegrationRuntimeAvailableMemory` | Bytes | Available memory on IR nodes |
| `IntegrationRuntimeAverageTaskPickupDelay` | Seconds | Avg time before IR picks up a task |

### Azure Monitor Naming Variants

Azure emits two naming variants for some metrics:

| Variant with Data | Legacy Variant (Always 0) |
|-------------------|---------------------------|
| `PipelineSucceededRuns` | `PipelineRunsSucceeded` |
| `PipelineFailedRuns` | `PipelineRunsFailed` |

Rules use the variant that carries real data.

---

## 4. Threshold Rules

Nine rules are defined in `base_finops_threshold_rules`, ordered by priority (lower = evaluated first).

### Rule Summary

| Priority | Rule Code | Severity | Trigger Condition | Savings % |
|----------|-----------|----------|-------------------|-----------|
| 1 | `ADF_IDLE` | High | PipelineSucceededRuns < 1 AND PipelineFailedRuns < 1 | 90% |
| 3 | `ADF_HIGH_FAILURE_RATE` | High | PipelineFailedRuns >= 10 AND PipelineSucceededRuns >= 1 | 40% |
| 4 | `ADF_HIGH_ACTIVITY_FAILURE` | High | ActivityFailedRuns >= 100 AND ActivitySucceededRuns >= 1 | 30% |
| 5 | `ADF_LOW_ACTIVITY` | Medium | ActivitySucceededRuns < 10 AND PipelineSucceededRuns >= 1 | 30% |
| 7 | `ADF_IR_UNDERUTILIZED` | Medium | IntegrationRuntimeCpuPercentage < 10 AND PipelineSucceededRuns >= 1 | 40% |
| 8 | `ADF_IR_QUEUE_BOTTLENECK` | Medium | IntegrationRuntimeQueueLength > 15 AND PipelineSucceededRuns >= 1 | 20% |
| 9 | `ADF_IR_HIGH_PICKUP_DELAY` | Medium | IntegrationRuntimeAverageTaskPickupDelay > 5 AND PipelineSucceededRuns >= 1 | 20% |
| 10 | `ADF_TRIGGER_INEFFICIENCY` | Low | TriggerSucceededRuns > 20 AND PipelineSucceededRuns < 10 AND PipelineFailedRuns >= 5 | 25% |
| 11 | `ADF_TRIGGER_FAILURE` | Low | TriggerFailedRuns >= 5 AND TriggerSucceededRuns >= 1 | 15% |

### Rule Details

#### Rule 1: ADF_IDLE — Decommission Idle Factory

**When:** Zero pipeline runs (succeeded and failed) for the entire observation window.

**Why:** An idle factory still incurs base charges for Integration Runtimes, linked services, and managed VNet resources.

**Action:** Suspend or delete the factory.

**Savings:** 90% of total cost.

---

#### Rule 2: ADF_HIGH_FAILURE_RATE — Excessive Pipeline Failures

**When:** 10+ failed pipeline runs alongside at least 1 succeeded run.

**Why:** Failed pipeline runs still incur activity run charges and IR compute time. Each failed execution burns compute on retries.

**Action:** Investigate and fix failing pipelines.

**Savings:** 40% of total cost.

---

#### Rule 3: ADF_HIGH_ACTIVITY_FAILURE — High Activity-Level Failures

**When:** 100+ failed activity runs alongside at least 1 succeeded activity.

**Why:** Pipelines may "succeed" overall, but individual activities fail repeatedly — each consuming IR compute and per-execution charges. This is invisible at the pipeline level.

**Action:** Fix misconfigured data flows, connectivity errors, or timeout conditions.

**Savings:** 30% of total cost.

---

#### Rule 4: ADF_LOW_ACTIVITY — Underused Factory

**When:** Fewer than 10 activity runs despite pipelines running.

**Why:** Low activity volume indicates over-provisioned pipeline scheduling or lightweight pipelines that could be consolidated.

**Action:** Consolidate pipelines or reduce trigger frequency.

**Savings:** 30% of total cost.

---

#### Rule 5: ADF_IR_UNDERUTILIZED — Idle Integration Runtime

**When:** Self-hosted IR CPU utilization below 10% with pipelines running.

**Why:** Self-hosted IR nodes are VMs. Low CPU means you're paying for compute you're not using.

**Action:** Reduce IR node count or migrate to Azure-hosted IR (pay-per-use).

**Savings:** 40% of total cost.

---

#### Rule 6: ADF_IR_QUEUE_BOTTLENECK — IR Queue Congestion

**When:** IR queue length exceeds 15 tasks with pipelines running.

**Why:** Queued tasks extend total pipeline wall-clock time, increasing time-based IR charges.

**Action:** Stagger pipeline schedules, reduce concurrency, or right-size IR node count.

**Savings:** 20% of total cost.

---

#### Rule 7: ADF_IR_HIGH_PICKUP_DELAY — Slow Task Pickup

**When:** Average task pickup delay exceeds 5 seconds with pipelines running.

**Why:** Slow pickup extends execution duration, increasing per-hour IR compute charges.

**Action:** Scale IR nodes or stagger pipeline schedules.

**Savings:** 20% of total cost.

---

#### Rule 8: ADF_TRIGGER_INEFFICIENCY — Wasted Trigger Fires

**When:** 20+ trigger fires but fewer than 10 successful pipeline completions, with 5+ pipeline failures.

**Why:** Each trigger fire incurs an execution charge. Triggers firing when preconditions aren't met waste orchestration and IR spin-up costs.

**Action:** Review and optimize trigger schedules and conditions.

**Savings:** 25% of total cost.

---

#### Rule 9: ADF_TRIGGER_FAILURE — Failing Triggers

**When:** 5+ failed trigger runs alongside at least 1 succeeded trigger.

**Why:** Failed triggers consume monitoring and orchestration resources with zero productive output.

**Action:** Fix misconfigured event or schedule triggers.

**Savings:** 15% of total cost.

---

## 5. Cost Savings Breakdown

Each rule targets specific ADF billing dimensions:

| Billing Dimension | Rules That Target It |
|-------------------|---------------------|
| **IR VM compute** (self-hosted IR node costs) | ADF_IDLE, ADF_IR_UNDERUTILIZED |
| **Activity run charges** (per-execution fees) | ADF_HIGH_FAILURE_RATE, ADF_HIGH_ACTIVITY_FAILURE, ADF_LOW_ACTIVITY |
| **IR time-based charges** (hourly compute) | ADF_IR_QUEUE_BOTTLENECK, ADF_IR_HIGH_PICKUP_DELAY |
| **Trigger execution charges** | ADF_TRIGGER_INEFFICIENCY, ADF_TRIGGER_FAILURE |
| **Base factory charges** (linked services, managed VNet) | ADF_IDLE |

The `estimated_savings_formula` (e.g., `total_cost * 0.40`) is a fallback estimate used when the right-sizing engine has no more precise calculation. The percentages are intentionally conservative.

---

## 6. Deployment

### 6.1 Seed the RDS Tables

Run the seed script against the RDS instance:

```sql
-- Execute the full script
\i src/sql/seed_adf_rds.sql
```

This inserts/updates:
1. One row in `service_configuration` (registers ADF)
2. Nine rows in `base_finops_threshold_rules` (global rule templates)

Both use `ON CONFLICT ... DO UPDATE`, so the script is idempotent.

### 6.2 Propagate Rules to Clients

After seeding base rules, run the sync Glue job to copy them into per-client `finops_threshold_rules`:

```bash
aws glue start-job-run \
  --job-name sync-base-rules-to-client \
  --arguments '{"--CLIENT_ID":"your_client_id","--SUBSCRIPTION_ID":"your_sub_id"}'
```

This is handled by `sync_base_rules_to_client_glue.py`, which:
1. Reads active rules from `base_finops_threshold_rules`
2. Upserts each rule into `finops_threshold_rules` with `client_id` and `subscription_id`

### 6.3 Ensure Bronze Data is Flowing

Verify that the bronze job is collecting ADF metrics:

```sql
SELECT DISTINCT metric_name, COUNT(*) as row_count
FROM bronze_azure_metrics
WHERE service_name = 'Azure Data Factory v2'
GROUP BY metric_name
ORDER BY metric_name;
```

You should see all 10 metric names listed in [Section 3](#3-metrics-collected).

---

## 7. Verification

### 7.1 Verify Service Configuration

```sql
SELECT service_code, service_name, is_active, category,
       resource_type, metrics_table_suffix, resources_table_suffix,
       service_name_filter, recommender_class, retail_api_service_name
FROM service_configuration
WHERE service_code = 'ADF' AND cloud_provider = 'azure';
```

### 7.2 Verify Base Threshold Rules

```sql
SELECT rule_code, severity, priority, is_active,
       evaluation_logic->'conditions' AS conditions,
       cost_model
FROM base_finops_threshold_rules
WHERE service_name = 'Azure Data Factory v2' AND cloud_provider = 'azure'
ORDER BY priority;
```

Expected: 9 rows, priorities 1, 3, 4, 5, 7, 8, 9, 10, 11.

### 7.3 Verify Client-Level Rules (After Sync)

```sql
SELECT rule_code, client_id, subscription_id, is_active
FROM finops_threshold_rules
WHERE service_name = 'Azure Data Factory v2' AND cloud_provider = 'azure'
ORDER BY rule_code;
```

### 7.4 Verify Recommendations (After Silver Run)

```sql
SELECT resource_id, rule_code, metric_name, savings,
       recommendation, created_at
FROM custom_recommendations
WHERE cloud_name = 'Azure'
  AND rule_code LIKE 'ADF_%'
ORDER BY savings DESC;
```

---

## 8. Troubleshooting

### No Recommendations Generated

| Check | Query / Action |
|-------|---------------|
| Bronze data exists? | `SELECT COUNT(*) FROM bronze_azure_metrics WHERE service_name = 'Azure Data Factory v2'` |
| Rules synced to client? | `SELECT COUNT(*) FROM finops_threshold_rules WHERE service_name = 'Azure Data Factory v2' AND client_id = 'xxx'` |
| Rules active? | Verify `is_active = TRUE` in both base and client rules |
| Metric names match? | Compare `evaluation_logic->'conditions'->0->>'metric'` against actual `metric_name` values in `bronze_azure_metrics` |

### Wrong metric_name in custom_recommendations

The `metric_name` in `custom_recommendations` comes from `evaluation_logic.conditions[0].metric`. If it shows an unexpected value:

1. Check the rule's `evaluation_logic` JSON — the first condition's `metric` field is what gets stored
2. Verify it matches an actual metric name in `bronze_azure_metrics`
3. If you fix a metric name in `base_finops_threshold_rules`, re-run `sync_base_rules_to_client_glue.py` to propagate the change

### Legacy Metric Names (Always Zero)

If a rule uses `PipelineRunsSucceeded` instead of `PipelineSucceededRuns`, it will never trigger because the legacy variant always returns 0. Ensure rules use the correct variant listed in [Section 3](#3-metrics-collected).

---

## Files Reference

| File | Purpose |
|------|---------|
| `src/sql/seed_adf_rds.sql` | Seed script — service config + 9 base threshold rules |
| `src/glue/azure/sync_base_rules_to_client_glue.py` | Syncs base rules to per-client `finops_threshold_rules` |
| `src/glue/azure/azure_unified_recommendations.py` | Silver-layer recommendation engine (rule evaluation, savings calculation, RDS upsert) |
| `src/glue/azure/azure_bronze_custom_recom_job.py` | Bronze-layer data collection (Azure Monitor metrics) |
