"""
Local development helpers for Jupyter notebooks.

Provides connection factories for S3, Athena, and RDS so the rightsizing
engine can be developed/debugged locally without deploying to AWS Glue.
"""

import json
import os
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import boto3
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()  # reads .env in cwd or parent directories


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# AWS Session
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_aws_session() -> boto3.Session:
    """Return a boto3 Session using the AWS_PROFILE from .env (SSO-friendly)."""
    profile = _env("AWS_PROFILE")
    region = _env("AWS_DEFAULT_REGION", "us-east-1")
    if profile:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def s3_client():
    return get_aws_session().client("s3")


def athena_client():
    return get_aws_session().client("athena")


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def read_s3_json(bucket: str, key: str) -> Dict[str, Any]:
    """Read a JSON file from S3 and return as dict."""
    obj = s3_client().get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def read_s3_csv(bucket: str, key: str, **kwargs) -> pd.DataFrame:
    """Read a CSV from S3 into a pandas DataFrame."""
    import io
    obj = s3_client().get_object(Bucket=bucket, Key=key)
    return pd.read_csv(io.BytesIO(obj["Body"].read()), **kwargs)


def list_s3_prefix(bucket: str, prefix: str, max_keys: int = 100):
    """List objects under an S3 prefix."""
    resp = s3_client().list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
    return [obj["Key"] for obj in resp.get("Contents", [])]


# ---------------------------------------------------------------------------
# Athena helpers (reads Iceberg tables without Spark)
# ---------------------------------------------------------------------------

def get_athena_engine():
    """Return a SQLAlchemy engine backed by PyAthena for Athena queries.

    Requires PyAthena[SQLAlchemy]:
        pip install 'PyAthena[SQLAlchemy]'

    Uses the AWS_PROFILE from .env for credentials.
    """
    from pyathena import connect as athena_connect
    from pyathena.pandas.cursor import PandasCursor

    region = _env("AWS_DEFAULT_REGION", "us-east-1")
    s3_output = _env("ATHENA_S3_OUTPUT", "s3://finomics-data-pod/athena-results/")
    workgroup = _env("ATHENA_WORKGROUP", "primary")
    profile = _env("AWS_PROFILE")

    conn_kwargs = dict(
        s3_staging_dir=s3_output,
        region_name=region,
        work_group=workgroup,
        cursor_class=PandasCursor,
    )
    if profile:
        conn_kwargs["profile_name"] = profile

    return athena_connect(**conn_kwargs)


def athena_query(sql: str, database: str = None) -> pd.DataFrame:
    """Run a SQL query against Athena and return a pandas DataFrame.

    Args:
        sql: SQL query string (can reference Iceberg tables via glue_catalog.db.table)
        database: Athena/Glue database name (defaults to ICEBERG_DB from .env)

    Returns:
        pandas DataFrame with query results
    """
    from pyathena import connect as athena_connect
    from pyathena.pandas.cursor import PandasCursor

    region = _env("AWS_DEFAULT_REGION", "us-east-1")
    s3_output = _env("ATHENA_S3_OUTPUT", "s3://finomics-data-pod/athena-results/")
    workgroup = _env("ATHENA_WORKGROUP", "primary")
    db = database or _env("ICEBERG_DB", "finomics_catalog_data")
    profile = _env("AWS_PROFILE")

    conn_kwargs = dict(
        s3_staging_dir=s3_output,
        region_name=region,
        work_group=workgroup,
        cursor_class=PandasCursor,
        schema_name=db,
    )
    if profile:
        conn_kwargs["profile_name"] = profile

    conn = athena_connect(**conn_kwargs)
    return conn.cursor().execute(sql).as_pandas()


# ---------------------------------------------------------------------------
# RDS (PostgreSQL) helpers
# ---------------------------------------------------------------------------

def _rds_creds_from_env() -> Dict[str, str]:
    """Build RDS creds from explicit env vars."""
    return {
        "host": _env("RDS_HOST"),
        "port": _env("RDS_PORT", "5432"),
        "dbname": _env("RDS_DBNAME"),
        "username": _env("RDS_USER"),
        "password": _env("RDS_PASSWORD"),
    }


def _rds_creds_from_secrets_manager() -> Dict[str, str]:
    """Fetch RDS creds from AWS Secrets Manager."""
    arn = _env("RDS_SECRET_ARN")
    if not arn:
        raise ValueError("RDS_SECRET_ARN not set in .env")
    client = get_aws_session().client("secretsmanager", region_name="us-east-1")
    secret = client.get_secret_value(SecretId=arn)
    return json.loads(secret["SecretString"])


def get_rds_creds() -> Dict[str, str]:
    """Get RDS credentials - prefers direct env vars, falls back to Secrets Manager."""
    if _env("RDS_HOST"):
        return _rds_creds_from_env()
    return _rds_creds_from_secrets_manager()


def get_rds_engine():
    """Return a SQLAlchemy engine connected to the RDS PostgreSQL database."""
    creds = get_rds_creds()
    url = (
        f"postgresql+psycopg2://{creds['username']}:{creds['password']}"
        f"@{creds['host']}:{creds['port']}/{creds['dbname']}"
    )
    return create_engine(url)


def rds_query(sql: str, params: dict = None) -> pd.DataFrame:
    """Run a SQL query against RDS and return a pandas DataFrame."""
    engine = get_rds_engine()
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def rds_execute(sql: str, params: dict = None):
    """Execute a DML/DDL statement against RDS."""
    engine = get_rds_engine()
    with engine.begin() as conn:
        conn.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Iceberg table reader via Athena (replaces IcebergStore for local dev)
# ---------------------------------------------------------------------------

class LocalIcebergReader:
    """Drop-in reader for Iceberg tables via Athena (no Spark needed).

    Mirrors the IcebergStore read API so the engine can be initialized
    locally with minimal code changes.

    Usage:
        reader = LocalIcebergReader()
        catalog_pdf = reader.load_sku_catalog('aws')
        pricing_pdf = reader.load_pricing_history('aws')
    """

    def __init__(self, database: str = None):
        self.database = database or _env("ICEBERG_DB", "finomics_catalog_data")

    def _query(self, sql: str) -> pd.DataFrame:
        return athena_query(sql, database=self.database)

    def load_sku_catalog(self, cloud_provider: str) -> pd.DataFrame:
        return self._query(f"""
            SELECT * FROM rs_cloud_sku_catalog
            WHERE cloud_provider = '{cloud_provider}' AND is_active = TRUE
        """)

    def load_pricing_history(self, cloud_provider: str, lookback_days: int = 30) -> pd.DataFrame:
        return self._query(f"""
            SELECT * FROM rs_cloud_sku_pricing_history
            WHERE cloud_provider = '{cloud_provider}'
              AND usage_date >= DATE_ADD('day', -{lookback_days}, CURRENT_DATE)
        """)

    def load_service_configs(self, cloud_provider: str) -> pd.DataFrame:
        return self._query(f"""
            SELECT * FROM rs_finops_service_config
            WHERE cloud_provider = '{cloud_provider}' AND is_active = TRUE
            ORDER BY service_category, service_name
        """)

    def load_rules(self, cloud_provider: str) -> pd.DataFrame:
        return self._query(f"""
            SELECT * FROM rs_rightsizing_rules
            WHERE cloud_provider = '{cloud_provider}' AND is_active = TRUE
            ORDER BY priority ASC
        """)


# ---------------------------------------------------------------------------
# Config loader (replaces S3ConfigLoader for local dev)
# ---------------------------------------------------------------------------

class LocalConfigLoader:
    """Load rightsizing engine JSON configs - tries local files first, then S3.

    Usage:
        loader = LocalConfigLoader()
        provider_config = loader.load_json("provider_config.json")
    """

    def __init__(self, local_config_dir: str = None, bucket: str = None, prefix: str = None):
        import pathlib
        self.local_dir = pathlib.Path(
            local_config_dir
            or os.path.join(
                os.path.dirname(__file__), "..",
                "src", "s3", "scripts", "common", "config", "rightsizing_engine"
            )
        )
        self.bucket = bucket or _env("RS_ENGINE_BUCKET", "finomics-data-pod")
        self.prefix = prefix or _env("RS_ENGINE_PREFIX", "warehouse")
        self._cache: Dict[str, Any] = {}

    def load_json(self, filename: str, default: Optional[Dict] = None) -> Dict:
        if filename in self._cache:
            return self._cache[filename]

        result = self._try_local(filename) or self._try_s3(filename) or (default or {})
        self._cache[filename] = result
        return result

    def _try_local(self, filename: str) -> Optional[Dict]:
        path = self.local_dir / filename
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                print(f"  [config] Loaded {filename} from local: {path}")
                return data
        return None

    def _try_s3(self, filename: str) -> Optional[Dict]:
        try:
            key = f"{self.prefix}/scripts/common/apatel/{filename}"
            data = read_s3_json(self.bucket, key)
            print(f"  [config] Loaded {filename} from S3: s3://{self.bucket}/{key}")
            return data
        except Exception as e:
            print(f"  [config] Could not load {filename} from S3: {e}")
            return None


# ---------------------------------------------------------------------------
# Quick connection test
# ---------------------------------------------------------------------------

def test_connections():
    """Quick smoke test for all three connections."""
    print("=" * 60)
    print("Testing AWS / S3 / Athena / RDS connections")
    print("=" * 60)

    # S3
    try:
        bucket = _env("RS_ENGINE_BUCKET", "finomics-data-pod")
        keys = list_s3_prefix(bucket, "warehouse/", max_keys=5)
        print(f"\n[S3]     OK - found {len(keys)} objects under s3://{bucket}/warehouse/")
    except Exception as e:
        print(f"\n[S3]     FAIL - {e}")

    # Athena
    try:
        df = athena_query("SELECT 1 AS test_col")
        print(f"[Athena] OK - test query returned {len(df)} row(s)")
    except Exception as e:
        print(f"[Athena] FAIL - {e}")

    # RDS
    try:
        df = rds_query("SELECT 1 AS test_col")
        print(f"[RDS]    OK - test query returned {len(df)} row(s)")
    except Exception as e:
        print(f"[RDS]    FAIL - {e}")

    print("\n" + "=" * 60)
