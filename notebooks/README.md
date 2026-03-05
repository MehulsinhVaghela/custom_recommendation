# Rightsizing Engine - Local Development Notebooks

Develop and debug the rightsizing engine and the Azure unified recommendations job on your laptop without deploying to AWS Glue. The notebooks connect to **S3**, **Athena**, and **RDS** on AWS to read the same data the Glue pipeline uses.

## Prerequisites

- Python 3.10+
- JupyterLab 4.5.5
- AWS CLI configured with SSO (`aws configure sso`)
- Network access to the RDS PostgreSQL instance (VPN or direct)

## Setup

```bash
# 1. Install dependencies
pip install -r requirements-dev.txt

# 2. Create your local env file
cp .env.example .env
# Edit .env with your AWS profile, RDS credentials, etc.

# 3. Authenticate AWS SSO
aws sso login --profile <your-profile>

# 4. Launch Jupyter
jupyter lab
```

Open `00_setup_and_connections.ipynb` first and run all cells to verify S3, Athena, and RDS connectivity.

## Notebooks

| Notebook | Purpose |
|----------|---------|
| `00_setup_and_connections` | Validate S3, Athena, and RDS connections. Run this first after setup. |
| `01_explore_iceberg_data` | Browse the 4 engine reference tables (SKU catalog, pricing history, service configs, rules) and bronze/silver pipeline tables via Athena. |
| `02_rightsizing_engine_dev` | **Main development notebook.** Loads engine components individually, step through `get_recommendation()`, test pricing lookups, and run batch recommendations against real bronze data. Supports hot-reload after code changes. |
| `03_rds_queries` | Query gold-layer RDS tables (`custom_recommendations`, `finops_threshold_rules`, etc.), analyze savings, and compare silver vs gold data. |
| `04_azure_unified_dev` | **Azure unified recommendations local dev.** Step through the 8k-line Azure Glue job locally — load service configs, threshold rules, resources/metrics from Iceberg, evaluate rules per-resource, test Azure Retail API pricing, and run batch processing. Uses `azure_local_adapter.py` to replace Spark JDBC and spark.sql calls. |

## How It Works

The notebooks bypass Spark/Glue entirely:

- **Iceberg tables** are read via **Athena SQL** (using PyAthena), returning pandas DataFrames
- **JSON configs** are loaded from local files (`src/s3/scripts/common/config/rightsizing_engine/`) with S3 fallback
- **RDS** is accessed directly via psycopg2/SQLAlchemy
- The engine source code (`src/s3/scripts/common/rightsizing_engine/`) is imported directly into the notebooks

This works because the engine's core logic (SKUCatalog, PricingResolver, SizingStrategies) is pure Python/pandas and does not depend on Spark.

For the **Azure unified recommendations job**, the same approach applies with an extra adapter layer (`azure_local_adapter.py`) that:
- Replaces `SimplifiedDatabaseConfig` (Spark JDBC) with `LocalDatabaseConfig` (psycopg2)
- Replaces `spark.sql()` Iceberg reads with `LocalIcebergLoader` (Athena)
- Stubs out `GlueContext` and `awsglue` imports
- Cherry-picks classes from the 8k-line module via `importlib` to avoid top-level side effects (SparkContext, pip installs)

## Development Workflow

1. **Edit** engine source code in `src/s3/scripts/common/rightsizing_engine/`
2. **Hot-reload** in the notebook:
   ```python
   import importlib
   import rightsizing_engine.sizing_strategies as ss
   importlib.reload(ss)
   ```
3. **Re-run** your test case (single resource or batch)
4. **Verify** pricing and savings output
5. **Commit** and let CI deploy to Glue

## Key Files

```
notebooks/
├── .env.example          # Credential template (copy to .env)
├── .gitignore            # Keeps .env out of version control
├── requirements-dev.txt  # Python dependencies
├── local_helpers.py      # Connection factories (S3, Athena, RDS)
├── azure_local_adapter.py # Azure-specific stubs (replaces Spark JDBC, spark.sql)
├── 00_setup_and_connections.ipynb
├── 01_explore_iceberg_data.ipynb
├── 02_rightsizing_engine_dev.ipynb
├── 03_rds_queries.ipynb
└── 04_azure_unified_dev.ipynb
```

### local_helpers.py

Provides drop-in replacements for Glue-specific code:

| Helper | Replaces |
|--------|----------|
| `athena_query(sql)` | Spark SQL on Iceberg tables |
| `LocalIcebergReader` | `IcebergStore` (read methods only) |
| `LocalConfigLoader` | `S3ConfigLoader` |
| `rds_query(sql)` / `get_rds_engine()` | `get_db_connection_string()` from `utils.py` |
| `read_s3_json(bucket, key)` | `boto3` S3 reads in Glue jobs |

### azure_local_adapter.py

Additional adapters for running the Azure unified recommendations job locally:

| Adapter | Replaces |
|---------|----------|
| `LocalDatabaseConfig` | `SimplifiedDatabaseConfig` (Spark JDBC → psycopg2 for threshold rules + service config) |
| `LocalIcebergLoader` | `spark.sql()` calls for resources, metrics, billing, and SKU catalog tables |
| `_StubGlueContext` | `GlueContext` (no-op so imports don't fail) |
| `build_local_args()` | `getResolvedOptions()` (constructs args dict from `.env`) |
| `aggregate_resource_metrics()` | Per-resource metric accumulation from pandas DataFrame |

## .env Configuration

| Variable | Description | Example |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS SSO profile name | `my-sso-profile` |
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |
| `RS_ENGINE_BUCKET` | S3 bucket for Iceberg warehouse | `finomics-data-pod` |
| `RS_ENGINE_PREFIX` | S3 prefix for warehouse root | `warehouse` |
| `ICEBERG_DB` | Glue Catalog database name | `finomics_catalog_data` |
| `ATHENA_S3_OUTPUT` | S3 path for Athena query results | `s3://finomics-data-pod/athena-results/` |
| `RDS_HOST` | RDS/PostgreSQL hostname | `mydb.postgres.database.azure.com` |
| `RDS_PORT` | Database port | `5432` |
| `RDS_DBNAME` | Database name | `finomics` |
| `RDS_USER` | Database username | `admin` |
| `RDS_PASSWORD` | Database password | `***` |

Alternatively, set `RDS_SECRET_ARN` to use the same Secrets Manager path as the Glue jobs.

### Azure-specific variables (for notebook 04)

| Variable | Description | Example |
|----------|-------------|---------|
| `AZURE_TENANT_ID` | Azure AD tenant ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `AZURE_CLIENT_ID` | Azure service principal client ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `AZURE_CLIENT_SECRET` | Azure service principal secret | `***` |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription to analyze | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| `FOCUS_TABLE_NAME` | Iceberg table for Azure billing data | `bronze_azure_focus_report_tbl` |
| `VMUI_URL` | VMUI endpoint (optional) | `https://vmui.example.com` |
| `VMUI_USERNAME` | VMUI credentials (optional) | `user` |
| `VMUI_PASSWORD` | VMUI credentials (optional) | `***` |

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `ExpiredTokenException` | Re-run `aws sso login --profile <profile>` |
| Athena query timeout | Check `ATHENA_WORKGROUP` and S3 output path in `.env` |
| RDS connection refused | Verify VPN is connected and `RDS_HOST`/`RDS_PORT` are correct |
| `ModuleNotFoundError: rightsizing_engine` | The notebook adds `src/s3/scripts/common` to `sys.path` automatically. Ensure you opened Jupyter from the `notebooks/` directory. |
| Stale code after edits | Use `importlib.reload()` on the changed module (see notebook 02, cell 8) |
| `ModuleNotFoundError: azure_local_adapter` | Ensure you launched Jupyter from the `notebooks/` directory |
| Azure Retail API errors | Verify `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` in `.env` |
| `ThresholdRule class not injected` | Call `db_config.set_threshold_rule_class(ThresholdRule)` after importing from the Azure module |
