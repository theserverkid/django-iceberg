"""Serverless storage engine for the Polars + Iceberg Django backend.

This module turns the Iceberg backend into a **serverless-native storage
engine** suitable for stateless compute (AWS Lambda) over object storage (S3) —
the compute/storage-separation model described in
``docs/serverless-storage-engine.md``.

It adds three things on top of :class:`IcebergManager`:

1. **A low-latency catalog tier.** A DynamoDB-backed Iceberg catalog (single
   digit millisecond metadata lookups) replaces the SQLite/REST catalog as the
   metadata pointer store. Glue and REST catalogs are also selectable.
2. **Warm-state reuse.** A process-global, TTL'd cache of catalog clients and
   resolved table handles that survives across warm Lambda invocations, so
   repeat requests skip the S3/catalog metadata walk. This is the cheapest
   latency win before a native Rust S3 client lands.
3. **An EFS hot tier.** An optional Amazon EFS mount hosting a shared POSIX
   catalog/warehouse for latency-sensitive tables, fronting S3 first-byte
   latency for hot data while S3 remains the durable system of record.

The heavy dependencies (PyIceberg, boto3, Polars) are imported lazily so that
:class:`ServerlessConfig` and :class:`WarmCache` — the pure configuration and
caching logic — can be imported and unit-tested without them.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Catalog backends we know how to build. "sql" covers SQLite (local dev) and is
# handled by the parent IcebergManager; the rest are serverless-oriented.
CATALOG_TYPES = ("sql", "dynamodb", "glue", "rest")

_LAMBDA_ENV_VARS = ("AWS_LAMBDA_FUNCTION_NAME", "AWS_LAMBDA_RUNTIME_API")


def detect_lambda() -> bool:
    """Return True when running inside an AWS Lambda execution environment."""
    return any(os.environ.get(var) for var in _LAMBDA_ENV_VARS)


def _infer_catalog_type(catalog_uri: Optional[str]) -> str:
    """Infer the catalog backend from a catalog URI scheme.

    ``dynamodb://<table>`` -> dynamodb, ``glue://`` -> glue,
    ``http(s)://`` -> rest, anything else (incl. ``sqlite://`` / ``None``) -> sql.
    """
    if not catalog_uri:
        return "sql"
    scheme = urlparse(catalog_uri).scheme.lower()
    if scheme == "dynamodb":
        return "dynamodb"
    if scheme == "glue":
        return "glue"
    if scheme in ("http", "https", "rest"):
        return "rest"
    return "sql"


@dataclass
class ServerlessConfig:
    """Resolved configuration for the serverless storage engine.

    Built from a Django ``DATABASES[...]`` settings dict via
    :meth:`from_settings`. Pure data — safe to import and test without any
    cloud SDK installed.
    """

    warehouse: str = "./data/warehouse"
    catalog_uri: Optional[str] = None
    namespace: str = "default"
    catalog_type: str = "sql"
    region: Optional[str] = None
    # DynamoDB catalog: the table holding Iceberg metadata pointers.
    dynamodb_table: Optional[str] = None
    # Optional EFS mount for a shared hot-tier catalog/warehouse.
    efs_mount: Optional[str] = None
    # TTL (seconds) for warm-cached table handles. 0 disables handle caching.
    cache_ttl: float = 30.0
    # Whether we are (or should behave as if) running serverless.
    serverless: bool = False
    # FileIO / cloud options forwarded to PyIceberg (s3.region, s3.endpoint, …).
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "ServerlessConfig":
        """Build a config from a Django database settings dict.

        Recognised keys (all optional except where the engine needs them):
        ``WAREHOUSE``, ``CATALOG_URI``, ``NAMESPACE``, ``CATALOG_TYPE``,
        ``DYNAMODB_TABLE``, ``EFS_MOUNT``, ``CACHE_TTL``, ``SERVERLESS`` and
        ``OPTIONS`` (a dict of PyIceberg FileIO options).
        """
        catalog_uri = settings.get("CATALOG_URI")
        options = dict(settings.get("OPTIONS") or {})

        catalog_type = (settings.get("CATALOG_TYPE") or _infer_catalog_type(catalog_uri)).lower()
        if catalog_type not in CATALOG_TYPES:
            raise ValueError(
                f"Unknown CATALOG_TYPE {catalog_type!r}; expected one of {CATALOG_TYPES}"
            )

        # A dynamodb:// URI carries the table name in its host component.
        dynamodb_table = settings.get("DYNAMODB_TABLE")
        if catalog_type == "dynamodb" and not dynamodb_table and catalog_uri:
            dynamodb_table = urlparse(catalog_uri).netloc or None

        region = (
            settings.get("REGION")
            or options.get("s3.region")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )

        # SERVERLESS may be True/False to force, or None/absent to auto-detect.
        serverless = settings.get("SERVERLESS")
        if serverless is None:
            serverless = detect_lambda()

        cache_ttl = settings.get("CACHE_TTL")
        cache_ttl = 30.0 if cache_ttl is None else float(cache_ttl)

        return cls(
            warehouse=settings.get("WAREHOUSE") or settings.get("NAME") or "./data/warehouse",
            catalog_uri=catalog_uri,
            namespace=settings.get("NAMESPACE", "default"),
            catalog_type=catalog_type,
            region=region,
            dynamodb_table=dynamodb_table,
            efs_mount=settings.get("EFS_MOUNT") or os.environ.get("EFS_WAREHOUSE"),
            cache_ttl=cache_ttl,
            serverless=bool(serverless),
            options=options,
        )

    @property
    def effective_warehouse(self) -> str:
        """Warehouse path the engine should use.

        When an EFS mount is configured and the catalog is a local SQL catalog,
        the warehouse is served from the EFS hot tier (shared POSIX filesystem,
        NFS latency) instead of the local ephemeral disk. S3/Glue/REST catalogs
        keep their object-storage warehouse as the system of record.
        """
        if self.efs_mount and self.catalog_type == "sql":
            return self.efs_mount
        return self.warehouse

    @property
    def cache_key(self) -> str:
        """Stable identity for warm-cache keying across invocations."""
        return f"{self.catalog_type}:{self.catalog_uri}:{self.namespace}:{self.effective_warehouse}"

    def describe(self) -> dict[str, Any]:
        """Human/ops-friendly summary (no secrets)."""
        return {
            "serverless": self.serverless,
            "lambda_runtime": detect_lambda(),
            "catalog_type": self.catalog_type,
            "warehouse": self.effective_warehouse,
            "namespace": self.namespace,
            "dynamodb_table": self.dynamodb_table,
            "region": self.region,
            "efs_hot_tier": bool(self.efs_mount),
            "cache_ttl": self.cache_ttl,
        }


# ---------------------------------------------------------------------------
# Warm-state cache (survives across warm Lambda invocations)
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    value: Any
    created: float


class WarmCache:
    """Process-global, thread-safe cache for catalog clients and table handles.

    Catalog clients are cached indefinitely (they are reusable connection-like
    objects). Table handles are cached with a TTL so that a warm invocation
    reuses resolved metadata for a short window but still picks up writes made
    elsewhere once the TTL lapses. Writers invalidate explicitly.
    """

    def __init__(self) -> None:
        self._catalogs: dict[str, Any] = {}
        self._tables: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    # -- catalog clients ---------------------------------------------------
    def get_catalog(self, key: str, factory: Callable[[], Any]) -> Any:
        with self._lock:
            cat = self._catalogs.get(key)
            if cat is not None:
                self.hits += 1
                return cat
            self.misses += 1
        # Build outside the lock; another thread racing is acceptable (idempotent).
        cat = factory()
        with self._lock:
            self._catalogs.setdefault(key, cat)
            return self._catalogs[key]

    # -- table handles (TTL'd) --------------------------------------------
    def get_table(
        self,
        key: tuple[str, str],
        ttl: float,
        factory: Callable[[], Any],
        now: Optional[float] = None,
    ) -> Any:
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._tables.get(key)
            if entry is not None and ttl > 0 and (now - entry.created) < ttl:
                self.hits += 1
                return entry.value
            self.misses += 1
        value = factory()
        with self._lock:
            self._tables[key] = _Entry(value=value, created=now)
        return value

    def invalidate_table(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._tables.pop(key, None)

    def invalidate_catalog(self, key: str) -> None:
        with self._lock:
            self._catalogs.pop(key, None)
            for tkey in [k for k in self._tables if k[0] == key]:
                self._tables.pop(tkey, None)

    def clear(self) -> None:
        with self._lock:
            self._catalogs.clear()
            self._tables.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "catalogs": len(self._catalogs),
                "tables": len(self._tables),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
            }


# Module-global singleton: shared by every manager in the process, which is what
# makes it survive across warm Lambda invocations of the same container.
WARM_CACHE = WarmCache()


# ---------------------------------------------------------------------------
# Serverless manager (lazily subclasses IcebergManager to avoid importing the
# heavy data stack until the engine is actually used).
# ---------------------------------------------------------------------------

_manager_class: Optional[type] = None


def get_serverless_manager_class() -> type:
    """Return the :class:`ServerlessIcebergManager` class, building it lazily.

    Importing :class:`IcebergManager` pulls in Polars/PyIceberg, so we defer it
    until the first time a serverless manager is actually constructed.
    """
    global _manager_class
    if _manager_class is not None:
        return _manager_class

    from .iceberg_manager import IcebergManager

    class ServerlessIcebergManager(IcebergManager):
        """IcebergManager wired for serverless object-storage deployments."""

        def __init__(self, config: ServerlessConfig):
            self.config = config
            super().__init__(
                catalog_uri=config.catalog_uri,
                warehouse_path=config.effective_warehouse,
                namespace=config.namespace,
                **config.options,
            )

        # -- catalog construction (DynamoDB / Glue / REST / SQL) ----------
        def _create_catalog(self):  # type: ignore[override]
            return WARM_CACHE.get_catalog(self.config.cache_key, self._build_catalog)

        def _build_catalog(self):
            from pyiceberg.catalog import load_catalog

            ct = self.config.catalog_type
            if ct == "dynamodb":
                props: dict[str, Any] = {
                    "type": "dynamodb",
                    "warehouse": self.config.warehouse,
                    "table-name": self.config.dynamodb_table or "iceberg",
                }
                if self.config.region:
                    props["dynamodb.region"] = self.config.region
                    props["s3.region"] = self.config.region
                props.update(self.config.options)
                catalog = load_catalog("default", **props)
            elif ct == "glue":
                props = {"type": "glue", "warehouse": self.config.warehouse}
                if self.config.region:
                    props["glue.region"] = self.config.region
                    props["s3.region"] = self.config.region
                props.update(self.config.options)
                catalog = load_catalog("default", **props)
            elif ct == "rest":
                props = {
                    "type": "rest",
                    "uri": self.config.catalog_uri,
                    "warehouse": self.config.warehouse,
                }
                props.update(self.config.options)
                catalog = load_catalog("default", **props)
            else:
                # Local SQL/SQLite catalog (dev, or EFS-hosted hot tier).
                return super()._create_catalog()

            try:
                catalog.create_namespace(self.config.namespace)
            except Exception:
                pass  # namespace already exists
            return catalog

        # -- warm table-handle reuse --------------------------------------
        def load_table(self, table_name: str):  # type: ignore[override]
            key = (self.config.cache_key, table_name)
            return WARM_CACHE.get_table(
                key,
                ttl=self.config.cache_ttl,
                factory=lambda: self.catalog.load_table(
                    self._get_table_identifier(table_name)
                ),
            )

        def _invalidate(self, table_name: str) -> None:
            WARM_CACHE.invalidate_table((self.config.cache_key, table_name))
            self._table_cache.pop(table_name, None)

        # Writers must drop the warm handle so the next read sees the new snapshot.
        def append(self, table_name, df):  # type: ignore[override]
            super().append(table_name, df)
            self._invalidate(table_name)

        def overwrite(self, table_name, df):  # type: ignore[override]
            super().overwrite(table_name, df)
            self._invalidate(table_name)

        def delete(self, table_name, filter_expr):  # type: ignore[override]
            result = super().delete(table_name, filter_expr)
            self._invalidate(table_name)
            return result

        def drop_table(self, table_name):  # type: ignore[override]
            super().drop_table(table_name)
            self._invalidate(table_name)

        # -- observability -------------------------------------------------
        def cache_stats(self) -> dict[str, Any]:
            return WARM_CACHE.stats()

    _manager_class = ServerlessIcebergManager
    return _manager_class


def create_manager(config: ServerlessConfig):
    """Instantiate a serverless Iceberg manager for the given config."""
    return get_serverless_manager_class()(config)
