"""Unit tests for the serverless storage engine's configuration and warm cache.

These cover the pure logic (config resolution, catalog-type inference, EFS hot
tier, TTL caching) and run without Polars / PyIceberg / boto3 installed, so they
work anywhere::

    python tests/test_serverless.py        # plain runner
    pytest tests/test_serverless.py        # or under pytest
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polars_iceberg.backend.serverless import (  # noqa: E402
    ServerlessConfig,
    WarmCache,
    _infer_catalog_type,
    detect_lambda,
)


def test_infer_catalog_type():
    assert _infer_catalog_type(None) == "sql"
    assert _infer_catalog_type("sqlite:///data/catalog.db") == "sql"
    assert _infer_catalog_type("dynamodb://iceberg-meta") == "dynamodb"
    assert _infer_catalog_type("glue://") == "glue"
    assert _infer_catalog_type("https://catalog.prod.internal") == "rest"


def test_dynamodb_table_from_uri():
    cfg = ServerlessConfig.from_settings(
        {
            "WAREHOUSE": "s3://bucket/warehouse",
            "CATALOG_URI": "dynamodb://iceberg-meta",
            "SERVERLESS": True,
        }
    )
    assert cfg.catalog_type == "dynamodb"
    assert cfg.dynamodb_table == "iceberg-meta"
    assert cfg.serverless is True
    assert cfg.effective_warehouse == "s3://bucket/warehouse"


def test_explicit_dynamodb_table_overrides_uri_host():
    cfg = ServerlessConfig.from_settings(
        {
            "CATALOG_TYPE": "dynamodb",
            "DYNAMODB_TABLE": "explicit-table",
            "CATALOG_URI": "dynamodb://ignored",
        }
    )
    assert cfg.dynamodb_table == "explicit-table"


def test_efs_hot_tier_only_for_sql_catalog():
    # EFS fronts a local SQL catalog/warehouse...
    sql_cfg = ServerlessConfig.from_settings(
        {"WAREHOUSE": "/var/data", "EFS_MOUNT": "/mnt/warehouse"}
    )
    assert sql_cfg.catalog_type == "sql"
    assert sql_cfg.effective_warehouse == "/mnt/warehouse"

    # ...but S3-backed object-store catalogs keep S3 as the system of record.
    s3_cfg = ServerlessConfig.from_settings(
        {
            "WAREHOUSE": "s3://bucket/warehouse",
            "CATALOG_URI": "dynamodb://meta",
            "EFS_MOUNT": "/mnt/warehouse",
        }
    )
    assert s3_cfg.effective_warehouse == "s3://bucket/warehouse"


def test_serverless_autodetect_from_lambda_env(monkeypatch=None):
    old = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    try:
        os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "my-func"
        assert detect_lambda() is True
        cfg = ServerlessConfig.from_settings({"WAREHOUSE": "s3://b/w"})
        assert cfg.serverless is True
    finally:
        if old is None:
            os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
        else:
            os.environ["AWS_LAMBDA_FUNCTION_NAME"] = old


def test_unknown_catalog_type_raises():
    try:
        ServerlessConfig.from_settings({"CATALOG_TYPE": "cassandra"})
    except ValueError as exc:
        assert "cassandra" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown catalog type")


def test_warm_cache_catalog_built_once():
    cache = WarmCache()
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return object()

    a = cache.get_catalog("k", factory)
    b = cache.get_catalog("k", factory)
    assert a is b
    assert calls["n"] == 1
    assert cache.stats()["hits"] == 1


def test_warm_cache_table_ttl():
    cache = WarmCache()
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return f"table-{calls['n']}"

    # Within TTL -> reused.
    v1 = cache.get_table(("c", "t"), ttl=10, factory=factory, now=100.0)
    v2 = cache.get_table(("c", "t"), ttl=10, factory=factory, now=105.0)
    assert v1 == v2 == "table-1"
    assert calls["n"] == 1

    # After TTL -> refreshed.
    v3 = cache.get_table(("c", "t"), ttl=10, factory=factory, now=120.0)
    assert v3 == "table-2"
    assert calls["n"] == 2


def test_warm_cache_invalidation():
    cache = WarmCache()
    cache.get_table(("c", "t"), ttl=60, factory=lambda: "v1", now=0.0)
    cache.invalidate_table(("c", "t"))
    v = cache.get_table(("c", "t"), ttl=60, factory=lambda: "v2", now=1.0)
    assert v == "v2"


def test_ttl_zero_disables_handle_cache():
    cache = WarmCache()
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return calls["n"]

    cache.get_table(("c", "t"), ttl=0, factory=factory, now=0.0)
    cache.get_table(("c", "t"), ttl=0, factory=factory, now=0.0)
    assert calls["n"] == 2  # never served from cache


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
