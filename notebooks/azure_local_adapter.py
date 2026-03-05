"""
Azure Unified Recommendations — Local Adapter
===============================================
Replaces Glue/Spark-specific code so `azure_unified_recommendations.py`
classes can run inside a Jupyter notebook.

Provides:
    - LocalDatabaseConfig   : drop-in for SimplifiedDatabaseConfig (uses psycopg2 not Spark JDBC)
    - LocalIcebergLoader    : reads Iceberg tables via Athena (replaces spark.sql)
    - GlueContext stub      : no-op so imports don't fail
    - build_local_args()    : builds the args dict the Orchestrator expects
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from local_helpers import (
    athena_query,
    get_rds_engine,
    rds_query,
    get_rds_creds,
    read_s3_json,
    LocalIcebergReader,
    LocalConfigLoader,
    _env,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Glue stubs — enough to let the module import without error
# ---------------------------------------------------------------------------


class _StubGlueContext:
    """No-op GlueContext so the orchestrator can be instantiated."""
    pass


class _StubJob:
    def init(self, *a, **kw):
        pass

    def commit(self):
        pass


# ---------------------------------------------------------------------------
# LocalDatabaseConfig — replaces SimplifiedDatabaseConfig
# ---------------------------------------------------------------------------


class LocalDatabaseConfig:
    """
    Drop-in replacement for ``SimplifiedDatabaseConfig``.

    Instead of Spark JDBC it uses ``psycopg2`` / ``sqlalchemy`` via
    ``local_helpers.rds_query``.

    Exposes the same public interface:
        - load_service_rules(service_name, cloud_provider) -> List[ThresholdRule]
        - get_service_config(service_code, cloud_provider) -> dict | None
        - evaluate_all_rules(service_name, metrics) -> List[ThresholdRule]
        - get_rule_by_code(service_name, rule_code) -> ThresholdRule | None
        - get_threshold(service_name, rule_code, metric_key) -> float | None

    You must import ``ThresholdRule`` from the Azure unified module *before*
    calling any method that returns rules.
    """

    def __init__(
        self,
        client_id: str = None,
        subscription_id: str = None,
        ThresholdRuleClass=None,
    ):
        self.client_id = client_id
        self.subscription_id = subscription_id
        self.rules_cache: Dict[str, Any] = {}
        self._ThresholdRule = ThresholdRuleClass  # injected after import

    # ---- service config ---------------------------------------------------

    def get_service_config(
        self, service_code: str, cloud_provider: str = "azure"
    ) -> Optional[Dict]:
        cache_key = f"service_config_{service_code}_{cloud_provider}"
        if cache_key in self.rules_cache:
            return self.rules_cache[cache_key]

        df = rds_query(
            """
            SELECT service_code, service_name, service_display_name,
                   category, resource_type,
                   metrics_table_suffix, resources_table_suffix,
                   service_name_filter, recommender_class,
                   vmui_service_name, retail_api_service_name,
                   config_options::text, priority
            FROM service_configuration
            WHERE UPPER(service_code) = UPPER(:code)
              AND cloud_provider = :cloud
              AND is_active = TRUE
            LIMIT 1
            """,
            {"code": service_code, "cloud": cloud_provider},
        )
        if df.empty:
            return None

        row = df.iloc[0]
        config = {
            "service_code": row["service_code"],
            "service_name": row["service_name"],
            "service_display_name": row.get("service_display_name"),
            "category": row.get("category"),
            "resource_type": row.get("resource_type"),
            "metrics_table_suffix": row.get("metrics_table_suffix"),
            "resources_table_suffix": row.get("resources_table_suffix"),
            "service_name_filter": row.get("service_name_filter"),
            "recommender_class": row.get("recommender_class"),
            "vmui_service_name": row.get("vmui_service_name"),
            "retail_api_service_name": row.get("retail_api_service_name"),
            "config_options": (
                json.loads(row["config_options"]) if row.get("config_options") else {}
            ),
            "priority": row.get("priority"),
        }
        self.rules_cache[cache_key] = config
        return config

    # ---- threshold rules --------------------------------------------------

    def load_service_rules(
        self,
        service_name: str,
        cloud_provider: str = "azure",
        client_id: str = None,
        subscription_id: str = None,
    ) -> list:
        eff_cid = client_id or self.client_id or ""
        eff_sid = subscription_id or self.subscription_id or ""
        cache_key = f"{cloud_provider}_{service_name}_{eff_cid or 'global'}_{eff_sid or 'all'}"

        if cache_key in self.rules_cache:
            return self.rules_cache[cache_key]

        df = rds_query(
            """
            SELECT id, client_id, subscription_id, rule_code, rule_type,
                   cloud_provider, service_name, severity, priority,
                   evaluation_logic::text, cost_model,
                   estimated_savings_formula,
                   threshold_config::text, threshold_config_mapping::text,
                   title, description,
                   recommendation_template, advisor_explanation_template,
                   category, tags::text, is_active
            FROM finops_threshold_rules
            WHERE cloud_provider = :cloud
              AND service_name = :svc
              AND is_active = TRUE
              AND (client_id IS NULL OR client_id = :cid)
              AND (subscription_id IS NULL OR subscription_id = :sid)
            ORDER BY priority, rule_code
            """,
            {"cloud": cloud_provider, "svc": service_name, "cid": eff_cid, "sid": eff_sid},
        )

        if self._ThresholdRule is None:
            raise RuntimeError(
                "ThresholdRule class not injected. "
                "Call adapter.set_threshold_rule_class(ThresholdRule) first."
            )

        rules = [self._ThresholdRule(row.to_dict()) for _, row in df.iterrows()]
        self.rules_cache[cache_key] = rules
        logger.info(f"Loaded {len(rules)} rules for {service_name}")
        return rules

    def get_rule_by_code(
        self, service_name: str, rule_code: str
    ) -> Optional[Any]:
        eff_cid = self.client_id or "global"
        eff_sid = self.subscription_id or "all"
        cache_key = f"azure_{service_name}_{eff_cid}_{eff_sid}"
        for rule in self.rules_cache.get(cache_key, []):
            if rule.rule_code == rule_code:
                return rule
        return None

    def get_threshold(
        self, service_name: str, rule_code: str, metric_key: str
    ) -> Optional[float]:
        rule = self.get_rule_by_code(service_name, rule_code)
        if not rule:
            return None
        for c in rule.evaluation_logic.get("conditions", []):
            if c.get("metric") == metric_key:
                return float(c.get("threshold", 0))
        return None

    def evaluate_all_rules(
        self, service_name: str, metrics: Dict[str, float], tags=None
    ) -> list:
        eff_cid = self.client_id or "global"
        eff_sid = self.subscription_id or "all"
        cache_key = f"azure_{service_name}_{eff_cid}_{eff_sid}"
        rules = self.rules_cache.get(cache_key)
        if rules is None:
            rules = self.load_service_rules(service_name)
        return [r for r in rules if r.evaluate(metrics)]

    def set_threshold_rule_class(self, cls):
        """Inject the ThresholdRule class after it's been imported."""
        self._ThresholdRule = cls


# ---------------------------------------------------------------------------
# Local Iceberg loader — reads resources / metrics via Athena
# ---------------------------------------------------------------------------


class LocalIcebergLoader:
    """Read bronze / silver Iceberg tables via Athena instead of spark.sql.

    Usage::

        loader = LocalIcebergLoader()
        resources_pdf = loader.query_table(
            "bronze_azure_vms",
            where=f"client_id = '{client_id}'"
        )
    """

    def __init__(self, database: str = None):
        self.database = database or _env("ICEBERG_DB", "finomics_catalog_data")

    def query_table(
        self,
        table_name: str,
        where: str = "1=1",
        limit: int = None,
    ) -> pd.DataFrame:
        sql = f"SELECT * FROM {table_name} WHERE {where}"
        if limit:
            sql += f" LIMIT {limit}"
        return athena_query(sql, database=self.database)

    def table_exists(self, table_name: str) -> bool:
        try:
            df = athena_query(
                f"SHOW TABLES IN {self.database} LIKE '{table_name}'"
            )
            return len(df) > 0
        except Exception:
            return False

    def load_resources(
        self, resources_table_suffix: str, client_id: str
    ) -> pd.DataFrame:
        return self.query_table(
            resources_table_suffix,
            where=f"client_id = '{client_id}'",
        )

    def load_metrics(
        self,
        metrics_table_suffix: str,
        client_id: str,
        service_name_filter: str,
    ) -> pd.DataFrame:
        return self.query_table(
            metrics_table_suffix,
            where=(
                f"client_id = '{client_id}' "
                f"AND service_name = '{service_name_filter}'"
            ),
        )

    def load_billing_cost_cache(
        self, focus_table: str, client_id: str
    ) -> Dict[str, float]:
        """Replicate _load_billing_cost_cache from the orchestrator."""
        if not self.table_exists(focus_table):
            return {}
        df = athena_query(f"""
            SELECT
                LOWER(resourceid) AS resource_id_lower,
                SUM(CAST(effectivecost AS DECIMAL(18,6))) AS total_cost,
                COUNT(DISTINCT CAST(chargeperiodstart AS DATE)) AS days_present
            FROM {focus_table}
            WHERE client_id = '{client_id}'
              AND effectivecost > 0
              AND resourceid IS NOT NULL
              AND TRIM(resourceid) != ''
              AND chargeclass IS NULL
              AND CAST(chargeperiodstart AS DATE) >= DATE_ADD('day', -30, CURRENT_DATE)
            GROUP BY LOWER(resourceid)
        """, database=self.database)
        cache = {}
        for _, row in df.iterrows():
            total = float(row["total_cost"] or 0)
            days = int(row["days_present"] or 0)
            if total > 0 and days > 0:
                cache[row["resource_id_lower"]] = round((total / days) * 365, 2)
        return cache

    def load_sku_catalog_cache(self) -> Dict[tuple, float]:
        """Replicate _load_sku_catalog_cache from the orchestrator."""
        table = "rs_cloud_sku_catalog"
        if not self.table_exists(table):
            return {}
        df = athena_query(f"""
            SELECT
                LOWER(service_name) AS service_name_lower,
                LOWER(sku_code) AS sku_code_lower,
                LOWER(region) AS region_lower,
                hourly_price, monthly_price
            FROM {table}
            WHERE cloud_provider = 'azure'
              AND is_active = TRUE
              AND pricing_model = 'on_demand'
              AND (hourly_price > 0 OR monthly_price > 0)
        """, database=self.database)
        cache = {}
        for _, row in df.iterrows():
            hourly = float(row.get("hourly_price") or 0)
            monthly = float(row.get("monthly_price") or 0)
            annual = round(hourly * 8760, 2) if hourly > 0 else (round(monthly * 12, 2) if monthly > 0 else 0)
            if annual > 0:
                cache[(row["service_name_lower"], row["sku_code_lower"], row["region_lower"])] = annual
        return cache


# ---------------------------------------------------------------------------
# Helpers for aggregating metrics from a pandas DataFrame
# ---------------------------------------------------------------------------


def aggregate_resource_metrics(
    metrics_pdf: pd.DataFrame, resource_id: str
) -> Dict[str, float]:
    """Aggregate metric rows for a single resource into {metric_name: avg_value}.

    Mirrors the per-resource metric accumulation in _process_single_service.
    """
    subset = metrics_pdf[metrics_pdf["resource_id"] == resource_id]
    if subset.empty:
        return {}
    acc: Dict[str, list] = {}
    for _, row in subset.iterrows():
        name = row.get("metric_name", "")
        val = float(row.get("metric_value") or 0)
        acc.setdefault(name, []).append(val)
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


# ---------------------------------------------------------------------------
# build_local_args — construct the args dict expected by ConfigurationManager
# ---------------------------------------------------------------------------


def build_local_args(
    services: str = "VM",
    client_id: str = None,
    subscription_id: str = "",
    overrides: Dict[str, str] = None,
) -> Dict[str, str]:
    """Build the args dict that ``ConfigurationManager`` and
    ``AzureRecommendationOrchestrator`` expect.

    Reads defaults from ``.env`` and lets you override any key.
    """
    args = {
        "JOB_NAME": "local-notebook-dev",
        "SERVICES": services,
        "CLIENT_ID": client_id or _env("CLIENT_ID", "local-dev"),
        "ICEBERG_DB": _env("ICEBERG_DB", "finomics_catalog_data"),
        "BUCKET": _env("RS_ENGINE_BUCKET", "finomics-data-pod"),
        "PREFIX": _env("RS_ENGINE_PREFIX", "warehouse"),
        "RDS_SECRET_ARN": _env("RDS_SECRET_ARN", ""),
        "COMPANY_ID": _env("COMPANY_ID", "1"),
        "CLOUD_NAME": "Azure",
        "CATALOG": "glue_catalog",
        "SUBSCRIPTION_ID": subscription_id,
        # VMUI (optional — set in .env if available)
        "VMUI_URL": _env("VMUI_URL", ""),
        "VMUI_USERNAME": _env("VMUI_USERNAME", ""),
        "VMUI_PASSWORD": _env("VMUI_PASSWORD", ""),
        "VMUI_WINDOW_DAYS": _env("VMUI_WINDOW_DAYS", "90"),
        # Azure creds (optional — for SKU navigator)
        "TENANT_ID": _env("AZURE_TENANT_ID", ""),
        "AZURE_CLIENT_ID": _env("AZURE_CLIENT_ID", ""),
        "AZURE_CLIENT_SECRET": _env("AZURE_CLIENT_SECRET", ""),
        "FOCUS_TABLE_NAME": _env("FOCUS_TABLE_NAME", "bronze_azure_focus_report_tbl"),
    }
    if overrides:
        args.update(overrides)
    return args
