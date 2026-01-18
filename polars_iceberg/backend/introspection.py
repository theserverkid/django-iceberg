"""
Database Introspection - Reads schema information from Iceberg tables.
"""

from __future__ import annotations

from collections import namedtuple
from typing import TYPE_CHECKING, Any

from django.db.backends.base.introspection import BaseDatabaseIntrospection
from django.db.backends.base.introspection import FieldInfo as BaseFieldInfo
from django.db.backends.base.introspection import TableInfo

if TYPE_CHECKING:
    from .base import PolarsCursor


FieldInfo = namedtuple("FieldInfo", [*BaseFieldInfo._fields, "is_autofield"])


class DatabaseIntrospection(BaseDatabaseIntrospection):
    """
    Database introspection for the Polars + Iceberg backend.

    Reads table and column information from the Iceberg catalog.
    """

    # Map Iceberg types to Django field types
    data_types_reverse = {
        "boolean": "BooleanField",
        "int": "IntegerField",
        "integer": "IntegerField",
        "long": "BigIntegerField",
        "float": "FloatField",
        "double": "FloatField",
        "decimal": "DecimalField",
        "string": "TextField",
        "binary": "BinaryField",
        "date": "DateField",
        "time": "TimeField",
        "timestamp": "DateTimeField",
        "timestamptz": "DateTimeField",
        "uuid": "UUIDField",
        # Arrow/Polars types
        "Utf8": "TextField",
        "LargeUtf8": "TextField",
        "Int32": "IntegerField",
        "Int64": "BigIntegerField",
        "Float32": "FloatField",
        "Float64": "FloatField",
        "Boolean": "BooleanField",
        "Date32": "DateField",
        "Timestamp": "DateTimeField",
    }

    @property
    def iceberg(self):
        """Get the Iceberg manager."""
        return self.connection.iceberg

    def get_field_type(self, data_type: str, description) -> str:
        """
        Convert an Iceberg/Polars type to a Django field type.

        Args:
            data_type: The Iceberg or Polars type name
            description: Field description tuple

        Returns:
            Django field type name
        """
        # Normalize the type name
        type_lower = data_type.lower()

        # Direct match
        if type_lower in self.data_types_reverse:
            return self.data_types_reverse[type_lower]

        # Handle parameterized types like decimal(10,2)
        base_type = type_lower.split("(")[0].strip()
        if base_type in self.data_types_reverse:
            return self.data_types_reverse[base_type]

        # Handle Polars types
        if data_type in self.data_types_reverse:
            return self.data_types_reverse[data_type]

        # Default to TextField
        return "TextField"

    def get_table_list(self, cursor: PolarsCursor) -> list[TableInfo]:
        """
        Return a list of table names in the current catalog/namespace.

        Args:
            cursor: Database cursor

        Returns:
            List of TableInfo namedtuples
        """
        tables = []

        try:
            # List tables from Iceberg catalog
            catalog = self.iceberg.catalog
            namespace = self.iceberg.namespace

            for table_id in catalog.list_tables(namespace):
                table_name = table_id[1] if isinstance(table_id, tuple) else str(table_id)
                tables.append(TableInfo(table_name, "t"))  # "t" for table

        except Exception:
            pass  # Catalog might be empty or not initialized

        return tables

    def get_table_description(
        self, cursor: PolarsCursor, table_name: str
    ) -> list[FieldInfo]:
        """
        Return a description of the table's columns.

        Args:
            cursor: Database cursor
            table_name: Name of the table

        Returns:
            List of FieldInfo namedtuples
        """
        fields = []

        try:
            table = self.iceberg.load_table(table_name)
            schema = table.schema()

            for iceberg_field in schema.fields:
                field_type = str(iceberg_field.field_type)

                fields.append(
                    FieldInfo(
                        name=iceberg_field.name,
                        type_code=field_type,
                        display_size=None,
                        internal_size=None,
                        precision=None,
                        scale=None,
                        null_ok=not iceberg_field.required,
                        default=None,
                        collation=None,
                        is_autofield=iceberg_field.name.lower() == "id",
                    )
                )

        except Exception:
            pass  # Table might not exist

        return fields

    def get_sequences(
        self, cursor: PolarsCursor, table_name: str, table_fields=()
    ) -> list[dict[str, Any]]:
        """
        Return sequences (not supported in Iceberg).
        """
        return []

    def get_relations(
        self, cursor: PolarsCursor, table_name: str
    ) -> dict[str, tuple[str, str]]:
        """
        Return foreign key relations (handled at application level).
        """
        return {}

    def get_primary_key_columns(
        self, cursor: PolarsCursor, table_name: str
    ) -> list[str]:
        """
        Return the primary key column(s) for a table.

        By convention, we assume 'id' is the primary key.
        """
        try:
            table = self.iceberg.load_table(table_name)
            schema = table.schema()

            # Look for 'id' column
            for field in schema.fields:
                if field.name.lower() == "id":
                    return [field.name]

            # Return first column if no 'id'
            if schema.fields:
                return [schema.fields[0].name]

        except Exception:
            pass

        return []

    def get_constraints(
        self, cursor: PolarsCursor, table_name: str
    ) -> dict[str, dict[str, Any]]:
        """
        Return constraints for a table.

        Constraints are handled at the application level, so we return
        an empty dict here. Django will use the constraint registry.
        """
        return {}

    def get_key_columns(
        self, cursor: PolarsCursor, table_name: str
    ) -> list[tuple[str, str, str]]:
        """
        Return foreign key columns (handled at application level).
        """
        return []

    def table_names(
        self, cursor: PolarsCursor = None, include_views: bool = False
    ) -> list[str]:
        """
        Return a list of table names.
        """
        return [t.name for t in self.get_table_list(cursor)]

    def get_storage_engine(
        self, cursor: PolarsCursor, table_name: str
    ) -> str:
        """
        Return the storage engine for a table.
        """
        return "Iceberg"
