"""
Polars + Iceberg Django Database Backend - Base Module

This is the main database wrapper that Django uses to interact with
the Polars + Iceberg storage layer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
from django.db.backends.base.base import BaseDatabaseWrapper
from django.utils.asyncio import async_unsafe

from .client import DatabaseClient
from .creation import DatabaseCreation
from .features import DatabaseFeatures
from .introspection import DatabaseIntrospection
from .operations import DatabaseOperations
from .schema import DatabaseSchemaEditor
from .validation import DatabaseValidation
from .iceberg_manager import IcebergManager
from .query_compiler import QueryCompiler, QueryType
from .constraints import ConstraintValidator, get_constraint_registry


class PolarsIcebergError(Exception):
    """Base exception for Polars/Iceberg backend."""
    pass


class DatabaseError(PolarsIcebergError):
    """Database error."""
    pass


class InterfaceError(PolarsIcebergError):
    """Interface error."""
    pass


class DataError(PolarsIcebergError):
    """Data error."""
    pass


class OperationalError(PolarsIcebergError):
    """Operational error."""
    pass


class IntegrityError(PolarsIcebergError):
    """Integrity constraint violation."""
    pass


class InternalError(PolarsIcebergError):
    """Internal error."""
    pass


class ProgrammingError(PolarsIcebergError):
    """Programming error."""
    pass


class NotSupportedError(PolarsIcebergError):
    """Feature not supported."""
    pass


class Database:
    """
    Mock database module to satisfy Django's expectations.

    Django expects connection.Database to be a DB-API 2.0 module.
    """
    Error = PolarsIcebergError
    DatabaseError = DatabaseError
    InterfaceError = InterfaceError
    DataError = DataError
    OperationalError = OperationalError
    IntegrityError = IntegrityError
    InternalError = InternalError
    ProgrammingError = ProgrammingError
    NotSupportedError = NotSupportedError


def _get_varchar_column(data):
    """Generate VARCHAR type with optional max_length."""
    if data["max_length"] is None:
        return "VARCHAR"
    return "VARCHAR(%(max_length)s)" % data


class DatabaseWrapper(BaseDatabaseWrapper):
    """
    Django database backend powered by Polars + Apache Iceberg.

    This backend uses:
    - Polars for blazing fast in-memory DataFrame operations
    - Apache Iceberg for persistent storage with time travel
    - Application-level constraint validation
    """

    vendor = "polars_iceberg"
    display_name = "Polars/Iceberg"

    # Database module (for error handling)
    Database = Database

    # Backend component classes
    SchemaEditorClass = DatabaseSchemaEditor
    client_class = DatabaseClient
    creation_class = DatabaseCreation
    features_class = DatabaseFeatures
    introspection_class = DatabaseIntrospection
    ops_class = DatabaseOperations
    validation_class = DatabaseValidation

    # Data type mapping for Django field types
    data_types = {
        "AutoField": "BIGINT",  # Will be auto-generated
        "BigAutoField": "BIGINT",
        "BinaryField": "BINARY",
        "BooleanField": "BOOLEAN",
        "CharField": _get_varchar_column,
        "DateField": "DATE",
        "DateTimeField": "TIMESTAMP",
        "DecimalField": "DECIMAL(%(max_digits)s, %(decimal_places)s)",
        "DurationField": "BIGINT",
        "FileField": "VARCHAR(%(max_length)s)",
        "FilePathField": "VARCHAR(%(max_length)s)",
        "FloatField": "DOUBLE",
        "IntegerField": "INTEGER",
        "BigIntegerField": "BIGINT",
        "IPAddressField": "VARCHAR(15)",
        "GenericIPAddressField": "VARCHAR(39)",
        "JSONField": "VARCHAR",  # Stored as JSON string
        "PositiveBigIntegerField": "BIGINT",
        "PositiveIntegerField": "INTEGER",
        "PositiveSmallIntegerField": "SMALLINT",
        "SlugField": "VARCHAR(%(max_length)s)",
        "SmallAutoField": "INTEGER",
        "SmallIntegerField": "SMALLINT",
        "TextField": "VARCHAR",
        "TimeField": "VARCHAR",  # Stored as string
        "UUIDField": "VARCHAR(36)",  # Stored as UUID string
    }

    data_types_suffix = {
        "AutoField": "",
        "BigAutoField": "",
        "SmallAutoField": "",
    }

    # SQL operators
    operators = {
        "exact": "= %s",
        "iexact": "= %s",  # Case-insensitive handled in Python
        "contains": "LIKE %s",
        "icontains": "LIKE %s",
        "regex": "~ %s",
        "iregex": "~* %s",
        "gt": "> %s",
        "gte": ">= %s",
        "lt": "< %s",
        "lte": "<= %s",
        "startswith": "LIKE %s",
        "endswith": "LIKE %s",
        "istartswith": "LIKE %s",
        "iendswith": "LIKE %s",
    }

    pattern_esc = r"REPLACE(REPLACE(REPLACE({}, '\', '\\'), '%%', '\%%'), '_', '\_')"
    pattern_ops = {
        "contains": "LIKE '%%' || {} || '%%'",
        "icontains": "LIKE '%%' || {} || '%%'",
        "startswith": "LIKE {} || '%%'",
        "istartswith": "LIKE {} || '%%'",
        "endswith": "LIKE '%%' || {}",
        "iendswith": "LIKE '%%' || {}",
    }

    def __init__(self, settings_dict, alias="default"):
        super().__init__(settings_dict, alias)
        self._iceberg_manager: IcebergManager | None = None
        self._query_compiler: QueryCompiler | None = None
        self._constraint_validator: ConstraintValidator | None = None

    def get_database_name(self) -> str:
        """Get the database/warehouse name."""
        name = self.settings_dict.get("NAME", "")
        if isinstance(name, Path):
            name = str(name)
        return name

    def get_connection_params(self) -> dict[str, Any]:
        """Get connection parameters for the Iceberg catalog."""
        settings = self.settings_dict

        params = {
            "warehouse_path": settings.get("WAREHOUSE") or settings.get("NAME") or "./data/warehouse",
            "catalog_uri": settings.get("CATALOG_URI"),
            "namespace": settings.get("NAMESPACE", "default"),
        }

        # Add S3/cloud options
        options = settings.get("OPTIONS", {})
        if options:
            for key in ["s3.access-key-id", "s3.secret-access-key", "s3.region",
                       "s3.endpoint", "aws_access_key_id", "aws_secret_access_key"]:
                if key in options:
                    params[key] = options[key]

        return params

    def get_new_connection(self, conn_params) -> IcebergManager:
        """Create a new Iceberg manager connection.

        When the database is configured for serverless operation — explicitly via
        ``"SERVERLESS": True`` / a DynamoDB|Glue|REST ``CATALOG_TYPE``, or
        implicitly by detecting an AWS Lambda runtime — the serverless storage
        engine is used (low-latency catalog tier, warm-state reuse, EFS hot
        tier). Otherwise the plain Iceberg manager is used unchanged.
        """
        from .serverless import ServerlessConfig, create_manager

        config = ServerlessConfig.from_settings(self.settings_dict)
        if config.serverless or config.catalog_type in ("dynamodb", "glue"):
            # Ensure a local EFS/disk warehouse exists; object stores need no mkdir.
            warehouse = config.effective_warehouse
            if not warehouse.startswith(("s3://", "gs://", "abfs://", "abfss://")):
                Path(warehouse).mkdir(parents=True, exist_ok=True)
            return create_manager(config)

        warehouse_path = conn_params.get("warehouse_path", "./data/warehouse")
        catalog_uri = conn_params.get("catalog_uri")
        namespace = conn_params.get("namespace", "default")

        # Ensure warehouse directory exists for local storage
        if not warehouse_path.startswith("s3://"):
            Path(warehouse_path).mkdir(parents=True, exist_ok=True)

        # Extract options
        options = {k: v for k, v in conn_params.items()
                   if k not in ("warehouse_path", "catalog_uri", "namespace")}

        return IcebergManager(
            catalog_uri=catalog_uri,
            warehouse_path=warehouse_path,
            namespace=namespace,
            **options,
        )

    @property
    def iceberg(self) -> IcebergManager:
        """Get the Iceberg manager."""
        if self._iceberg_manager is None:
            params = self.get_connection_params()
            self._iceberg_manager = self.get_new_connection(params)
        return self._iceberg_manager

    @property
    def query_compiler(self) -> QueryCompiler:
        """Get the query compiler."""
        if self._query_compiler is None:
            self._query_compiler = QueryCompiler(self.iceberg)
        return self._query_compiler

    @property
    def constraint_validator(self) -> ConstraintValidator:
        """Get the constraint validator."""
        if self._constraint_validator is None:
            self._constraint_validator = ConstraintValidator(
                self.iceberg, get_constraint_registry()
            )
        return self._constraint_validator

    def init_connection_state(self):
        """Initialize connection state."""
        pass

    @async_unsafe
    def ensure_connection(self):
        """Ensure we have a valid connection."""
        if self._iceberg_manager is None:
            with self.wrap_database_errors:
                self.connect()

    def connect(self):
        """Establish the database connection."""
        params = self.get_connection_params()
        self._iceberg_manager = self.get_new_connection(params)
        self._query_compiler = QueryCompiler(self._iceberg_manager)
        self._constraint_validator = ConstraintValidator(
            self._iceberg_manager, get_constraint_registry()
        )
        self.connection = self  # Set connection to self for compatibility

    def create_cursor(self, name=None):
        """Create a new cursor."""
        self.ensure_connection()
        return PolarsCursor(self)

    def _set_autocommit(self, autocommit):
        """Set autocommit mode (no-op for Iceberg)."""
        pass

    def is_usable(self):
        """Check if the connection is usable."""
        try:
            # Try to access the catalog
            _ = self.iceberg.catalog
            return True
        except Exception:
            return False

    def is_in_memory_db(self):
        """Check if this is an in-memory database."""
        return False  # Iceberg is always file-based

    @property
    def timezone(self):
        """Get the database timezone."""
        import datetime
        return datetime.timezone.utc

    @property
    def timezone_name(self):
        """Get the timezone name."""
        return "UTC"

    def close(self):
        """Close the connection."""
        self._iceberg_manager = None
        self._query_compiler = None
        self._constraint_validator = None
        self.connection = None


class PolarsCursor:
    """
    Cursor implementation that translates SQL to Polars operations.

    This cursor intercepts Django's SQL queries and executes them
    using Polars against Iceberg tables.
    """

    def __init__(self, db_wrapper: DatabaseWrapper):
        self.db = db_wrapper
        self._last_result: pl.DataFrame | None = None
        self._last_insert_id: Any = None
        self._rowcount: int = -1
        self._description: list | None = None
        self._result_iter: Any = None

    def execute(self, sql: str, params=None):
        """
        Execute a SQL statement.

        Args:
            sql: SQL statement with %s placeholders
            params: Query parameters

        Returns:
            self for chaining
        """
        if params is None:
            params = []

        # Parse the SQL
        query = self.db.query_compiler.parse(sql, params)

        # Execute based on query type
        if query.query_type == QueryType.SELECT:
            self._execute_select(query)
        elif query.query_type == QueryType.INSERT:
            self._execute_insert(query)
        elif query.query_type == QueryType.UPDATE:
            self._execute_update(query)
        elif query.query_type == QueryType.DELETE:
            self._execute_delete(query)
        elif query.query_type == QueryType.CREATE_TABLE:
            self._execute_create_table(query)
        elif query.query_type == QueryType.DROP_TABLE:
            self._execute_drop_table(query)
        else:
            # For other statements, do nothing
            self._last_result = pl.DataFrame()
            self._rowcount = 0

        return self

    def _execute_select(self, query):
        """Execute a SELECT query."""
        df = self.db.query_compiler.execute_select(query)
        self._last_result = df
        self._rowcount = len(df)
        self._update_description(df)
        self._result_iter = iter(df.iter_rows())

    def _execute_insert(self, query):
        """Execute an INSERT query."""
        # Validate constraints
        if query.columns and query.values:
            row_data = dict(zip(query.columns, query.values))
            self.db.constraint_validator.validate_insert(query.table_name, row_data)

        # Execute insert
        count, result = self.db.query_compiler.execute_insert(query)
        self._rowcount = count

        # For RETURNING clause - result is a tuple of values
        if query.returning and isinstance(result, tuple):
            self._last_insert_id = result[0] if result else None
            self._last_result = pl.DataFrame({
                col: [val] for col, val in zip(query.returning, result)
            })
            self._result_iter = iter([result])  # Make fetchone return the tuple
        else:
            self._last_insert_id = result
            self._last_result = pl.DataFrame()
            self._result_iter = None

    def _execute_update(self, query):
        """Execute an UPDATE query."""
        # Validate constraints
        if query.set_clauses:
            self.db.constraint_validator.validate_update(
                query.table_name, query.set_clauses
            )

        count = self.db.query_compiler.execute_update(query)
        self._rowcount = count
        self._last_result = pl.DataFrame()

    def _execute_delete(self, query):
        """Execute a DELETE query."""
        count = self.db.query_compiler.execute_delete(query)
        self._rowcount = count
        self._last_result = pl.DataFrame()

    def _execute_create_table(self, query):
        """Execute a CREATE TABLE query (handled by schema editor)."""
        self._rowcount = 0
        self._last_result = pl.DataFrame()

    def _execute_drop_table(self, query):
        """Execute a DROP TABLE query."""
        if query.table_name:
            try:
                self.db.iceberg.drop_table(query.table_name)
            except Exception:
                pass  # Table might not exist
        self._rowcount = 0
        self._last_result = pl.DataFrame()

    def _update_description(self, df: pl.DataFrame):
        """Update cursor description from DataFrame schema."""
        if df is None or len(df.columns) == 0:
            self._description = None
            return

        self._description = [
            (col, df[col].dtype, None, None, None, None, True)
            for col in df.columns
        ]

    def executemany(self, sql: str, param_list):
        """Execute a SQL statement with multiple parameter sets."""
        for params in param_list:
            self.execute(sql, params)

    def fetchone(self):
        """Fetch one row from the result set."""
        if self._result_iter is None:
            return None
        try:
            return next(self._result_iter)
        except StopIteration:
            return None

    def fetchmany(self, size=None):
        """Fetch multiple rows from the result set."""
        if self._last_result is None:
            return []
        if size is None:
            size = 1
        rows = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self):
        """Fetch all rows from the result set."""
        if self._last_result is None:
            return []
        return list(self._last_result.iter_rows())

    def close(self):
        """Close the cursor."""
        self._last_result = None
        self._result_iter = None

    @property
    def description(self):
        """Get cursor description (column info)."""
        return self._description

    @property
    def rowcount(self):
        """Get the number of affected rows."""
        return self._rowcount

    @property
    def lastrowid(self):
        """Get the last insert ID."""
        return self._last_insert_id

    def __iter__(self):
        """Iterate over result rows."""
        if self._last_result is None:
            return iter([])
        return self._last_result.iter_rows()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
