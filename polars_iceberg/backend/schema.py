"""
Schema Editor - Handles Django migrations for Iceberg tables.

This module translates Django migration operations into Iceberg
schema changes (create table, add column, etc.).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from pyiceberg.schema import Schema, NestedField
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
    UUIDType,
)

from .constraints import register_model_constraints

if TYPE_CHECKING:
    from django.db.models import Model, Field


# Django field type to Iceberg type mapping
FIELD_TYPE_MAP = {
    "AutoField": LongType(),
    "BigAutoField": LongType(),
    "SmallAutoField": IntegerType(),
    "IntegerField": IntegerType(),
    "BigIntegerField": LongType(),
    "SmallIntegerField": IntegerType(),
    "PositiveIntegerField": IntegerType(),
    "PositiveBigIntegerField": LongType(),
    "PositiveSmallIntegerField": IntegerType(),
    "FloatField": DoubleType(),
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
    "TimeField": StringType(),
    "DurationField": LongType(),
    "BinaryField": BinaryType(),
    "UUIDField": StringType(),  # Store as string for simplicity
    "JSONField": StringType(),
    "ForeignKey": LongType(),  # Foreign keys are stored as integers
    "OneToOneField": LongType(),  # Same as ForeignKey
}


class DatabaseSchemaEditor(BaseDatabaseSchemaEditor):
    """
    Schema editor for Polars + Iceberg backend.

    Translates Django migrations into Iceberg schema operations.
    """

    # SQL templates (some won't be used directly)
    sql_create_table = "CREATE TABLE %(table)s (%(definition)s)"
    sql_delete_table = "DROP TABLE %(table)s"
    sql_create_sequence = None
    sql_delete_sequence = None
    sql_create_fk = None  # FKs handled at application level
    sql_delete_fk = None
    sql_create_unique = None  # Unique constraints at application level
    sql_delete_unique = None

    def __init__(self, connection, collect_sql=False, atomic=True):
        super().__init__(connection, collect_sql, atomic)
        self._deferred_sql = []

    @property
    def iceberg(self):
        """Get the Iceberg manager from the connection."""
        return self.connection.iceberg

    def quote_value(self, value):
        """Quote a value for SQL."""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        elif isinstance(value, (Decimal, float, int)):
            return str(value)
        elif isinstance(value, str):
            return "'%s'" % value.replace("'", "''")
        elif value is None:
            return "NULL"
        elif isinstance(value, (bytes, bytearray, memoryview)):
            return "X'%s'" % value.hex()
        else:
            raise ValueError(f"Cannot quote value {value!r} of type {type(value)}")

    def prepare_default(self, value):
        """Prepare a default value for SQL."""
        return self.quote_value(value)

    def _field_to_iceberg_type(self, field: Field) -> Any:
        """Convert a Django field to an Iceberg type."""
        internal_type = field.get_internal_type()

        if internal_type == "DecimalField":
            return DecimalType(
                precision=field.max_digits or 38,
                scale=field.decimal_places or 9,
            )

        return FIELD_TYPE_MAP.get(internal_type, StringType())

    def _model_to_iceberg_schema(self, model: type[Model]) -> Schema:
        """Convert a Django model to an Iceberg schema."""
        fields = []
        field_id = 1

        for django_field in model._meta.get_fields():
            # Skip fields without a column (reverse relations, etc.)
            if not hasattr(django_field, "column"):
                continue

            # Skip fields with None column (virtual fields)
            if django_field.column is None:
                continue

            iceberg_type = self._field_to_iceberg_type(django_field)

            # Determine if field is required (non-nullable)
            # Note: Primary keys are always required
            is_required = not getattr(django_field, "null", True)
            if getattr(django_field, "primary_key", False):
                is_required = True

            fields.append(
                NestedField(
                    field_id=field_id,
                    name=django_field.column,
                    field_type=iceberg_type,
                    required=is_required,
                )
            )
            field_id += 1

        return Schema(*fields)

    def create_model(self, model: type[Model]) -> None:
        """
        Create an Iceberg table for a Django model.

        Args:
            model: Django model class
        """
        table_name = model._meta.db_table

        # Skip if table already exists
        if self.iceberg.table_exists(table_name):
            return

        # Convert model to Iceberg schema
        schema = self._model_to_iceberg_schema(model)

        # Create the Iceberg table
        self.iceberg.create_table(table_name, schema)

        # Register constraints for application-level validation
        register_model_constraints(model)

    def delete_model(self, model: type[Model]) -> None:
        """
        Delete an Iceberg table for a Django model.

        Args:
            model: Django model class
        """
        table_name = model._meta.db_table

        try:
            self.iceberg.drop_table(table_name)
        except Exception:
            pass  # Table might not exist

    def add_field(self, model: type[Model], field: Field) -> None:
        """
        Add a field to an existing Iceberg table.

        Uses Iceberg's schema evolution to add the column.

        Args:
            model: Django model class
            field: Django field to add
        """
        table_name = model._meta.db_table

        if not self.iceberg.table_exists(table_name):
            return

        table = self.iceberg.load_table(table_name)
        iceberg_type = self._field_to_iceberg_type(field)

        with table.update_schema() as update:
            update.add_column(
                path=field.column,
                field_type=iceberg_type,
                doc=field.help_text if hasattr(field, "help_text") else None,
            )

    def remove_field(self, model: type[Model], field: Field) -> None:
        """
        Remove a field from an Iceberg table.

        Note: Iceberg doesn't support dropping columns by default for safety.
        Instead, we can make it optional or rename it.

        Args:
            model: Django model class
            field: Django field to remove
        """
        table_name = model._meta.db_table

        if not self.iceberg.table_exists(table_name):
            return

        table = self.iceberg.load_table(table_name)

        # Iceberg's safe approach: rename to mark as deprecated
        with table.update_schema() as update:
            try:
                update.rename_column(field.column, f"_deprecated_{field.column}")
            except Exception:
                pass  # Column might not exist

    def alter_field(
        self,
        model: type[Model],
        old_field: Field,
        new_field: Field,
        strict: bool = False,
    ) -> None:
        """
        Alter a field in an Iceberg table.

        Iceberg supports schema evolution for:
        - Renaming columns
        - Widening types (e.g., int to long)
        - Making columns optional

        Args:
            model: Django model class
            old_field: Original field
            new_field: New field definition
            strict: Whether to raise errors
        """
        table_name = model._meta.db_table

        if not self.iceberg.table_exists(table_name):
            return

        table = self.iceberg.load_table(table_name)

        with table.update_schema() as update:
            # Handle rename
            if old_field.column != new_field.column:
                try:
                    update.rename_column(old_field.column, new_field.column)
                except Exception:
                    pass

            # Handle type changes (limited support)
            old_type = self._field_to_iceberg_type(old_field)
            new_type = self._field_to_iceberg_type(new_field)

            if type(old_type) != type(new_type):
                # Type changes are limited in Iceberg
                # Only certain promotions are allowed (e.g., int -> long)
                pass

            # Handle nullability
            if old_field.null != new_field.null:
                if new_field.null:
                    # Making a column optional is allowed
                    try:
                        update.make_column_optional(new_field.column)
                    except Exception:
                        pass

    def alter_unique_together(
        self,
        model: type[Model],
        old_unique_together: set,
        new_unique_together: set,
    ) -> None:
        """
        Handle changes to unique_together.

        Since Iceberg doesn't support database-level unique constraints,
        we just update the constraint registry.

        Args:
            model: Django model class
            old_unique_together: Old unique_together value
            new_unique_together: New unique_together value
        """
        # Re-register model constraints to update the registry
        register_model_constraints(model)

    def alter_index_together(
        self,
        model: type[Model],
        old_index_together: set,
        new_index_together: set,
    ) -> None:
        """
        Handle changes to index_together.

        Iceberg doesn't have traditional indexes - it uses partition specs
        and sort orders for performance optimization.

        Args:
            model: Django model class
            old_index_together: Old index_together value
            new_index_together: New index_together value
        """
        pass  # No-op for Iceberg

    def add_index(self, model: type[Model], index) -> None:
        """
        Add an index.

        Iceberg uses partition specs instead of indexes.
        This is a no-op for now.
        """
        pass

    def remove_index(self, model: type[Model], index) -> None:
        """Remove an index (no-op for Iceberg)."""
        pass

    def add_constraint(self, model: type[Model], constraint) -> None:
        """
        Add a constraint.

        Constraints are handled at the application level.
        We just register them in the constraint registry.
        """
        register_model_constraints(model)

    def remove_constraint(self, model: type[Model], constraint) -> None:
        """
        Remove a constraint.

        Re-register model constraints to update the registry.
        """
        register_model_constraints(model)

    def alter_db_table(
        self,
        model: type[Model],
        old_db_table: str,
        new_db_table: str,
    ) -> None:
        """
        Rename a table.

        Iceberg supports table renaming through the catalog.
        """
        if old_db_table == new_db_table:
            return

        if not self.iceberg.table_exists(old_db_table):
            return

        # Iceberg table rename via catalog
        try:
            catalog = self.iceberg.catalog
            old_id = self.iceberg._get_table_identifier(old_db_table)
            new_id = self.iceberg._get_table_identifier(new_db_table)
            catalog.rename_table(old_id, new_id)
        except Exception:
            # Fallback: read data and recreate table
            pass

    def _create_unique_sql(self, model, fields, name=None, condition=None,
                          deferrable=None, include=None, opclasses=(),
                          expressions=(), nulls_distinct=None):
        """Create unique constraint SQL (handled at application level)."""
        return None

    def _delete_unique_sql(self, model, name, condition=None, deferrable=None,
                          include=None, opclasses=(), expressions=(),
                          nulls_distinct=None):
        """Delete unique constraint SQL (handled at application level)."""
        return None

    def execute(self, sql, params=()) -> None:
        """
        Execute a SQL statement.

        For DDL operations, we translate to Iceberg operations.
        """
        if self.collect_sql:
            self.collected_sql.append(sql % tuple(params) if params else sql)
            return

        # Most DDL is handled through create_model/delete_model etc.
        # This handles any raw SQL that might come through
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)

    def __enter__(self):
        self.deferred_sql = []
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            for sql in self.deferred_sql:
                self.execute(sql)
