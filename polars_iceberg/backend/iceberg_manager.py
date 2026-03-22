"""
Iceberg Manager - Handles all Iceberg catalog and table operations.

This module provides a high-level interface for:
- Managing the Iceberg catalog (SQLite-based for local, REST for cloud)
- Creating/dropping tables with Iceberg schemas
- Reading data into Polars DataFrames
- Writing Polars DataFrames to Iceberg tables
- Time travel queries
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import polars as pl
import pyarrow as pa
from pyiceberg.catalog import load_catalog
from pyiceberg.catalog.sql import SqlCatalog


def _normalize_path_for_iceberg(path: str) -> str:
    """
    Normalize a path for use with PyIceberg.

    PyIceberg on Windows needs file:// URIs with forward slashes.
    """
    if path.startswith(("s3://", "gs://", "http://", "https://", "file://")):
        return path

    # Convert to absolute path
    abs_path = os.path.abspath(path)

    # Convert to forward slashes for consistency
    abs_path = abs_path.replace("\\", "/")

    return abs_path
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FloatType,
    IntegerType,
    LongType,
    StringType,
    TimestampType,
    TimestamptzType,
    UUIDType,
)
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.table import Table

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog


# Mapping from Django field types to Iceberg types
DJANGO_TO_ICEBERG_TYPES = {
    "AutoField": IntegerType(),
    "BigAutoField": LongType(),
    "SmallAutoField": IntegerType(),
    "IntegerField": IntegerType(),
    "BigIntegerField": LongType(),
    "SmallIntegerField": IntegerType(),
    "PositiveIntegerField": IntegerType(),
    "PositiveBigIntegerField": LongType(),
    "PositiveSmallIntegerField": IntegerType(),
    "FloatField": DoubleType(),
    "DecimalField": DecimalType(precision=38, scale=9),  # Default, will be overridden
    "BooleanField": BooleanType(),
    "CharField": StringType(),
    "TextField": StringType(),
    "SlugField": StringType(),
    "EmailField": StringType(),
    "URLField": StringType(),
    "FileField": StringType(),
    "FilePathField": StringType(),
    "IPAddressField": StringType(),
    "GenericIPAddressField": StringType(),
    "DateField": DateType(),
    "DateTimeField": TimestampType(),
    "TimeField": StringType(),  # Iceberg doesn't have a native time type
    "DurationField": LongType(),  # Store as microseconds
    "BinaryField": BinaryType(),
    "UUIDField": UUIDType(),
    "JSONField": StringType(),  # Store JSON as string
    "ForeignKey": LongType(),  # Foreign keys stored as integers
    "OneToOneField": LongType(),  # Same as ForeignKey
}


class IcebergManager:
    """
    Manages Iceberg catalog and table operations for the Django backend.

    Usage:
        manager = IcebergManager(
            catalog_uri="sqlite:///data/catalog.db",
            warehouse_path="./data/warehouse"
        )

        # Create a table
        manager.create_table("myapp_task", schema)

        # Write data
        manager.append("myapp_task", df)

        # Read data
        df = manager.scan("myapp_task")

        # Time travel
        df = manager.scan("myapp_task", snapshot_id=123456)
    """

    def __init__(
        self,
        catalog_uri: str | None = None,
        warehouse_path: str | None = None,
        namespace: str = "default",
        **options: Any,
    ):
        """
        Initialize the Iceberg manager.

        Args:
            catalog_uri: URI for the catalog (e.g., "sqlite:///data/catalog.db")
            warehouse_path: Path to the warehouse directory (local or S3)
            namespace: Iceberg namespace (default: "default")
            **options: Additional options (S3 credentials, etc.)
        """
        self.namespace = namespace
        self.warehouse_path = warehouse_path or "./data/warehouse"
        self.catalog_uri = catalog_uri or f"sqlite:///{self.warehouse_path}/catalog.db"
        self.options = options
        self._catalog: Catalog | None = None
        self._table_cache: dict[str, Table] = {}

        # Ensure warehouse directory exists for local storage
        if not self.warehouse_path.startswith("s3://"):
            Path(self.warehouse_path).mkdir(parents=True, exist_ok=True)

    @property
    def catalog(self) -> Catalog:
        """Lazily load the Iceberg catalog."""
        if self._catalog is None:
            self._catalog = self._create_catalog()
        return self._catalog

    def _create_catalog(self) -> Catalog:
        """Create and configure the Iceberg catalog."""
        # For SQLite catalog (local development)
        if self.catalog_uri.startswith("sqlite://"):
            db_path = self.catalog_uri.replace("sqlite:///", "")
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

            # Normalize warehouse path for PyIceberg
            warehouse_path = _normalize_path_for_iceberg(self.warehouse_path)

            # On Windows, PyIceberg has issues with local file paths
            # Use a workaround: create warehouse in a relative path
            if sys.platform == "win32":
                # Use relative path from current directory
                cwd = os.getcwd().replace("\\", "/")
                if warehouse_path.startswith(cwd):
                    # Make it relative
                    warehouse_path = warehouse_path[len(cwd):].lstrip("/")
                else:
                    # Use a simple relative path
                    warehouse_path = "data/warehouse"
                    Path(warehouse_path).mkdir(parents=True, exist_ok=True)

            catalog = SqlCatalog(
                "default",
                **{
                    "uri": self.catalog_uri,
                    "warehouse": warehouse_path,
                    **self.options,
                }
            )

            # Create namespace if it doesn't exist
            try:
                catalog.create_namespace(self.namespace)
            except Exception:
                pass  # Namespace may already exist

            return catalog

        # For REST catalog (production/cloud)
        return load_catalog(
            "default",
            **{
                "uri": self.catalog_uri,
                "warehouse": self.warehouse_path,
                **self.options,
            }
        )

    def _get_table_identifier(self, table_name: str) -> tuple[str, str]:
        """Get the full table identifier (namespace, table_name)."""
        if "." in table_name:
            namespace, name = table_name.rsplit(".", 1)
            return (namespace, name)
        return (self.namespace, table_name)

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the catalog."""
        try:
            self.catalog.load_table(self._get_table_identifier(table_name))
            return True
        except NoSuchTableError:
            return False

    def create_table(
        self,
        table_name: str,
        schema: Schema,
        partition_spec: PartitionSpec | None = None,
    ) -> Table:
        """
        Create a new Iceberg table.

        Args:
            table_name: Name of the table
            schema: Iceberg schema for the table
            partition_spec: Optional partition specification

        Returns:
            The created Table object
        """
        identifier = self._get_table_identifier(table_name)

        try:
            # Don't pass partition_spec if it's None
            create_kwargs = {
                "identifier": identifier,
                "schema": schema,
            }
            if partition_spec is not None:
                create_kwargs["partition_spec"] = partition_spec

            table = self.catalog.create_table(**create_kwargs)
            self._table_cache[table_name] = table
            return table
        except TableAlreadyExistsError:
            return self.load_table(table_name)

    def load_table(self, table_name: str) -> Table:
        """
        Load an existing Iceberg table.

        Args:
            table_name: Name of the table

        Returns:
            The Table object
        """
        if table_name not in self._table_cache:
            self._table_cache[table_name] = self.catalog.load_table(
                self._get_table_identifier(table_name)
            )
        return self._table_cache[table_name]

    def drop_table(self, table_name: str) -> None:
        """Drop an Iceberg table."""
        self.catalog.drop_table(self._get_table_identifier(table_name))
        self._table_cache.pop(table_name, None)

    def scan(
        self,
        table_name: str,
        columns: list[str] | None = None,
        row_filter: str | None = None,
        snapshot_id: int | None = None,
        timestamp: datetime | None = None,
    ) -> pl.DataFrame:
        """
        Scan an Iceberg table and return a Polars DataFrame.

        Args:
            table_name: Name of the table
            columns: List of columns to select (None = all)
            row_filter: Optional filter expression
            snapshot_id: Optional snapshot ID for time travel
            timestamp: Optional timestamp for time travel

        Returns:
            Polars DataFrame with the data
        """
        table = self.load_table(table_name)

        # Build the scan
        scan = table.scan()

        if columns:
            scan = scan.select(*columns)

        if row_filter:
            scan = scan.filter(row_filter)

        if snapshot_id:
            scan = scan.use_snapshot(snapshot_id)
        elif timestamp:
            # Find the snapshot at the given timestamp
            snapshot = self._find_snapshot_at_timestamp(table, timestamp)
            if snapshot:
                scan = scan.use_snapshot(snapshot.snapshot_id)

        # Convert to Arrow and then to Polars
        arrow_table = scan.to_arrow()
        return pl.from_arrow(arrow_table)

    def _find_snapshot_at_timestamp(
        self, table: Table, timestamp: datetime
    ) -> Any | None:
        """Find the snapshot that was current at the given timestamp."""
        target_ms = int(timestamp.timestamp() * 1000)

        # Get all snapshots and find the one valid at the timestamp
        snapshots = list(table.snapshots())
        valid_snapshot = None

        for snapshot in sorted(snapshots, key=lambda s: s.timestamp_ms):
            if snapshot.timestamp_ms <= target_ms:
                valid_snapshot = snapshot
            else:
                break

        return valid_snapshot

    def append(self, table_name: str, df: pl.DataFrame) -> None:
        """
        Append a Polars DataFrame to an Iceberg table.

        Args:
            table_name: Name of the table
            df: Polars DataFrame to append
        """
        table = self.load_table(table_name)

        # Convert DataFrame to Arrow with exact Iceberg schema
        arrow_table = self._align_dataframe_to_schema(df, table.schema())

        table.append(arrow_table)

        # Clear cache to ensure fresh reads
        self._table_cache.pop(table_name, None)

    def _align_dataframe_to_schema(self, df: pl.DataFrame, iceberg_schema: Schema) -> pa.Table:
        """
        Align a Polars DataFrame to match an Iceberg schema.

        Converts the DataFrame to Arrow with the exact schema Iceberg expects,
        including correct nullability.

        Returns:
            PyArrow Table with the exact schema Iceberg expects
        """
        # Get the Arrow schema that Iceberg expects
        target_arrow_schema = iceberg_schema.as_arrow()

        # Convert DataFrame to Arrow
        arrow_table = df.to_arrow()

        # Cast each column to match the target schema exactly
        new_columns = []
        for target_field in target_arrow_schema:
            col_name = target_field.name

            if col_name in arrow_table.column_names:
                col = arrow_table.column(col_name)
                # Cast to target type if different
                if col.type != target_field.type:
                    try:
                        col = col.cast(target_field.type)
                    except Exception:
                        # If cast fails, keep original
                        pass
                new_columns.append(col)
            else:
                # Create null column with correct type
                null_array = pa.nulls(len(arrow_table), type=target_field.type)
                new_columns.append(null_array)

        # Create new Arrow table with the EXACT target schema
        # This ensures nullability matches what Iceberg expects
        new_arrow_table = pa.table(
            {field.name: col for field, col in zip(target_arrow_schema, new_columns)},
            schema=target_arrow_schema
        )

        return new_arrow_table

    def overwrite(self, table_name: str, df: pl.DataFrame) -> None:
        """
        Overwrite an Iceberg table with a Polars DataFrame.

        Args:
            table_name: Name of the table
            df: Polars DataFrame to write
        """
        table = self.load_table(table_name)

        # Align DataFrame to match Iceberg schema
        arrow_table = self._align_dataframe_to_schema(df, table.schema())
        table.overwrite(arrow_table)

        # Clear cache to ensure fresh reads
        self._table_cache.pop(table_name, None)

    def delete(self, table_name: str, filter_expr: str) -> int:
        """
        Delete rows from an Iceberg table matching the filter.

        Args:
            table_name: Name of the table
            filter_expr: Filter expression for rows to delete

        Returns:
            Number of rows deleted
        """
        table = self.load_table(table_name)

        # Read current data
        current_df = self.scan(table_name)
        original_count = len(current_df)

        # Filter out rows to delete (keep rows that DON'T match)
        # Convert filter to Polars expression
        # Note: This is simplified - real implementation needs proper expression parsing
        remaining_df = current_df.filter(~pl.sql_expr(filter_expr))

        # Overwrite with remaining data
        self.overwrite(table_name, remaining_df)

        return original_count - len(remaining_df)

    def get_snapshots(self, table_name: str) -> list[dict[str, Any]]:
        """
        Get all snapshots for a table (for time travel).

        Args:
            table_name: Name of the table

        Returns:
            List of snapshot metadata dicts
        """
        table = self.load_table(table_name)
        return [
            {
                "snapshot_id": s.snapshot_id,
                "timestamp_ms": s.timestamp_ms,
                "timestamp": datetime.fromtimestamp(s.timestamp_ms / 1000),
                "summary": dict(s.summary) if s.summary else {},
            }
            for s in table.snapshots()
        ]

    def get_current_snapshot_id(self, table_name: str) -> int | None:
        """Get the current snapshot ID for a table."""
        table = self.load_table(table_name)
        return table.current_snapshot().snapshot_id if table.current_snapshot() else None

    def schema_from_django_model(self, model: Any) -> Schema:
        """
        Convert a Django model to an Iceberg schema.

        Args:
            model: Django model class

        Returns:
            Iceberg Schema
        """
        from pyiceberg.schema import NestedField

        fields = []
        field_id = 1

        for field in model._meta.get_fields():
            if not hasattr(field, "column"):
                continue

            internal_type = field.get_internal_type()
            iceberg_type = DJANGO_TO_ICEBERG_TYPES.get(internal_type, StringType())

            # Handle DecimalField specially
            if internal_type == "DecimalField":
                iceberg_type = DecimalType(
                    precision=field.max_digits or 38,
                    scale=field.decimal_places or 9,
                )

            fields.append(
                NestedField(
                    field_id=field_id,
                    name=field.column,
                    field_type=iceberg_type,
                    required=not field.null,
                )
            )
            field_id += 1

        return Schema(*fields)

    def evolve_schema(self, table_name: str, new_schema: Schema) -> None:
        """
        Evolve the schema of an existing table.

        Args:
            table_name: Name of the table
            new_schema: New Iceberg schema
        """
        table = self.load_table(table_name)

        with table.update_schema() as update:
            current_fields = {f.name for f in table.schema().fields}
            new_fields = {f.name for f in new_schema.fields}

            # Add new fields
            for field in new_schema.fields:
                if field.name not in current_fields:
                    update.add_column(field.name, field.field_type, field.doc)

            # Note: Iceberg doesn't support removing columns by default
            # for safety reasons. Columns can be renamed or made optional.

        # Clear cache
        self._table_cache.pop(table_name, None)
