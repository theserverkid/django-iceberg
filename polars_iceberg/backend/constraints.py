"""
Constraint Validation - Application-level FK, Unique, and NOT NULL validation.

Since Iceberg doesn't support database-level constraints, we implement
validation at the application layer before writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import polars as pl
from django.db import IntegrityError

if TYPE_CHECKING:
    from .iceberg_manager import IcebergManager


@dataclass
class ForeignKeyConstraint:
    """Represents a foreign key constraint."""
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    on_delete: str = "CASCADE"  # CASCADE, SET NULL, PROTECT, DO NOTHING
    name: str | None = None


@dataclass
class UniqueConstraint:
    """Represents a unique constraint."""
    table: str
    columns: list[str]
    name: str | None = None


@dataclass
class NotNullConstraint:
    """Represents a NOT NULL constraint."""
    table: str
    column: str


class ConstraintRegistry:
    """
    Registry of all constraints in the database.

    This is populated from Django model metadata during migrations.
    """

    def __init__(self):
        self.foreign_keys: dict[str, list[ForeignKeyConstraint]] = {}  # table -> constraints
        self.unique_constraints: dict[str, list[UniqueConstraint]] = {}  # table -> constraints
        self.not_null_constraints: dict[str, list[NotNullConstraint]] = {}  # table -> constraints
        self.primary_keys: dict[str, str] = {}  # table -> pk_column

    def register_foreign_key(self, constraint: ForeignKeyConstraint) -> None:
        """Register a foreign key constraint."""
        if constraint.from_table not in self.foreign_keys:
            self.foreign_keys[constraint.from_table] = []
        self.foreign_keys[constraint.from_table].append(constraint)

    def register_unique(self, constraint: UniqueConstraint) -> None:
        """Register a unique constraint."""
        if constraint.table not in self.unique_constraints:
            self.unique_constraints[constraint.table] = []
        self.unique_constraints[constraint.table].append(constraint)

    def register_not_null(self, constraint: NotNullConstraint) -> None:
        """Register a NOT NULL constraint."""
        if constraint.table not in self.not_null_constraints:
            self.not_null_constraints[constraint.table] = []
        self.not_null_constraints[constraint.table].append(constraint)

    def register_primary_key(self, table: str, column: str) -> None:
        """Register a primary key."""
        self.primary_keys[table] = column

    def get_foreign_keys(self, table: str) -> list[ForeignKeyConstraint]:
        """Get all foreign key constraints for a table."""
        return self.foreign_keys.get(table, [])

    def get_unique_constraints(self, table: str) -> list[UniqueConstraint]:
        """Get all unique constraints for a table."""
        return self.unique_constraints.get(table, [])

    def get_not_null_constraints(self, table: str) -> list[NotNullConstraint]:
        """Get all NOT NULL constraints for a table."""
        return self.not_null_constraints.get(table, [])


class ConstraintValidator:
    """
    Validates constraints before write operations.

    Usage:
        validator = ConstraintValidator(iceberg_manager, registry)

        # Before INSERT
        validator.validate_insert("myapp_task", {"title": "Test", "user_id": 1})

        # Before UPDATE
        validator.validate_update("myapp_task", {"user_id": 2}, where_clause="id = 1")

        # Before DELETE
        validator.validate_delete("myapp_user", where_clause="id = 1")
    """

    def __init__(self, iceberg: IcebergManager, registry: ConstraintRegistry):
        self.iceberg = iceberg
        self.registry = registry

    def validate_insert(self, table: str, row_data: dict[str, Any]) -> None:
        """
        Validate constraints before an INSERT operation.

        Args:
            table: Table name
            row_data: Dictionary of column -> value for the new row

        Raises:
            IntegrityError: If any constraint is violated
        """
        self._validate_not_null(table, row_data)
        self._validate_foreign_keys(table, row_data)
        self._validate_unique(table, row_data)

    def validate_update(
        self,
        table: str,
        set_data: dict[str, Any],
        existing_rows: pl.DataFrame | None = None,
    ) -> None:
        """
        Validate constraints before an UPDATE operation.

        Args:
            table: Table name
            set_data: Dictionary of column -> new_value for updated columns
            existing_rows: DataFrame of rows being updated

        Raises:
            IntegrityError: If any constraint is violated
        """
        # For updates, we only validate the columns being changed
        self._validate_not_null(table, set_data, partial=True)
        self._validate_foreign_keys(table, set_data)

        # For unique validation on updates, we need to exclude the rows being updated
        if existing_rows is not None:
            self._validate_unique_update(table, set_data, existing_rows)

    def validate_delete(self, table: str, rows_to_delete: pl.DataFrame) -> None:
        """
        Validate constraints before a DELETE operation.

        Checks for referential integrity - ensures no other tables
        reference the rows being deleted.

        Args:
            table: Table name
            rows_to_delete: DataFrame of rows being deleted

        Raises:
            IntegrityError: If deletion would violate FK constraints
        """
        self._validate_delete_references(table, rows_to_delete)

    def _validate_not_null(
        self, table: str, row_data: dict[str, Any], partial: bool = False
    ) -> None:
        """Validate NOT NULL constraints."""
        constraints = self.registry.get_not_null_constraints(table)

        for constraint in constraints:
            col = constraint.column
            if col in row_data:
                if row_data[col] is None:
                    raise IntegrityError(
                        f"NOT NULL constraint failed: {table}.{col} cannot be NULL"
                    )
            elif not partial:
                # For INSERTs, missing NOT NULL columns are an error
                # (unless they have a default)
                pass  # Let the database handle defaults

    def _validate_foreign_keys(self, table: str, row_data: dict[str, Any]) -> None:
        """Validate foreign key constraints."""
        constraints = self.registry.get_foreign_keys(table)

        for constraint in constraints:
            fk_column = constraint.from_column
            if fk_column not in row_data:
                continue

            fk_value = row_data[fk_column]
            if fk_value is None:
                continue  # NULL FKs are allowed (unless NOT NULL constraint)

            # Check if the referenced row exists
            ref_table = constraint.to_table
            ref_column = constraint.to_column

            try:
                ref_df = self.iceberg.scan(ref_table, columns=[ref_column])
                if fk_value not in ref_df[ref_column].to_list():
                    raise IntegrityError(
                        f"Foreign key constraint failed: {table}.{fk_column} = {fk_value} "
                        f"references non-existent row in {ref_table}.{ref_column}"
                    )
            except Exception as e:
                if "NoSuchTableError" in str(type(e)):
                    raise IntegrityError(
                        f"Foreign key constraint failed: referenced table {ref_table} does not exist"
                    )
                # For other errors (like empty table), allow the operation

    def _validate_unique(
        self, table: str, row_data: dict[str, Any], exclude_ids: set | None = None
    ) -> None:
        """Validate unique constraints."""
        constraints = self.registry.get_unique_constraints(table)

        for constraint in constraints:
            # Check if any constrained columns are in the new data
            constraint_cols = constraint.columns
            has_relevant_data = any(col in row_data for col in constraint_cols)

            if not has_relevant_data:
                continue

            # Build the values tuple for the constraint columns
            values = tuple(row_data.get(col) for col in constraint_cols)

            # Check if this combination already exists
            try:
                existing_df = self.iceberg.scan(table, columns=constraint_cols)

                if len(existing_df) == 0:
                    continue

                # Build a filter for the exact combination
                filter_expr = None
                for col, val in zip(constraint_cols, values):
                    if val is None:
                        col_filter = pl.col(col).is_null()
                    else:
                        col_filter = pl.col(col) == val

                    if filter_expr is None:
                        filter_expr = col_filter
                    else:
                        filter_expr = filter_expr & col_filter

                if filter_expr is not None:
                    duplicates = existing_df.filter(filter_expr)

                    # Exclude rows being updated
                    if exclude_ids and "id" in duplicates.columns:
                        duplicates = duplicates.filter(~pl.col("id").is_in(list(exclude_ids)))

                    if len(duplicates) > 0:
                        raise IntegrityError(
                            f"UNIQUE constraint failed: {table} ({', '.join(constraint_cols)}) "
                            f"= {values} already exists"
                        )

            except Exception as e:
                if isinstance(e, IntegrityError):
                    raise
                # Table doesn't exist or is empty - no conflict possible

    def _validate_unique_update(
        self, table: str, set_data: dict[str, Any], existing_rows: pl.DataFrame
    ) -> None:
        """Validate unique constraints for UPDATE operations."""
        # Get IDs of rows being updated to exclude them from uniqueness check
        exclude_ids = set()
        if "id" in existing_rows.columns:
            exclude_ids = set(existing_rows["id"].to_list())

        self._validate_unique(table, set_data, exclude_ids)

    def _validate_delete_references(self, table: str, rows_to_delete: pl.DataFrame) -> None:
        """
        Check if any other tables reference the rows being deleted.

        This enforces referential integrity for ON DELETE RESTRICT/PROTECT.
        """
        # Find all FK constraints that reference this table
        for other_table, constraints in self.registry.foreign_keys.items():
            for constraint in constraints:
                if constraint.to_table != table:
                    continue

                # This constraint references our table
                ref_column = constraint.to_column
                fk_column = constraint.from_column

                if ref_column not in rows_to_delete.columns:
                    continue

                # Get the values being deleted
                deleting_values = set(rows_to_delete[ref_column].to_list())

                # Check if any rows in the referencing table use these values
                try:
                    referencing_df = self.iceberg.scan(other_table, columns=[fk_column])
                    referencing_values = set(referencing_df[fk_column].to_list())

                    conflicts = deleting_values & referencing_values
                    if conflicts:
                        if constraint.on_delete == "PROTECT":
                            raise IntegrityError(
                                f"Cannot delete from {table}: rows are referenced by "
                                f"{other_table}.{fk_column} (values: {conflicts})"
                            )
                        elif constraint.on_delete == "CASCADE":
                            # TODO: Implement cascading deletes
                            pass
                        elif constraint.on_delete == "SET NULL":
                            # TODO: Implement SET NULL
                            pass

                except Exception as e:
                    if isinstance(e, IntegrityError):
                        raise
                    # Referencing table doesn't exist - no conflict


# Global constraint registry (populated during migrations)
_constraint_registry: ConstraintRegistry | None = None


def get_constraint_registry() -> ConstraintRegistry:
    """Get the global constraint registry."""
    global _constraint_registry
    if _constraint_registry is None:
        _constraint_registry = ConstraintRegistry()
    return _constraint_registry


def register_model_constraints(model: Any) -> None:
    """
    Register all constraints from a Django model.

    Called during migrations to populate the constraint registry.
    """
    registry = get_constraint_registry()
    meta = model._meta

    # Register primary key
    if meta.pk:
        registry.register_primary_key(meta.db_table, meta.pk.column)

    # Register NOT NULL constraints
    for field in meta.get_fields():
        if not hasattr(field, "column"):
            continue
        if not field.null and not field.primary_key:
            registry.register_not_null(
                NotNullConstraint(table=meta.db_table, column=field.column)
            )

    # Register foreign key constraints
    for field in meta.get_fields():
        if not hasattr(field, "column"):
            continue
        if hasattr(field, "remote_field") and field.remote_field:
            related_model = field.remote_field.model
            if hasattr(related_model, "_meta"):
                on_delete = "CASCADE"
                if hasattr(field.remote_field, "on_delete"):
                    on_delete_action = field.remote_field.on_delete
                    if on_delete_action is not None:
                        action_name = getattr(on_delete_action, "__name__", str(on_delete_action))
                        if action_name == "CASCADE":
                            on_delete = "CASCADE"
                        elif action_name == "PROTECT":
                            on_delete = "PROTECT"
                        elif action_name == "SET_NULL":
                            on_delete = "SET NULL"
                        elif action_name == "DO_NOTHING":
                            on_delete = "DO NOTHING"

                registry.register_foreign_key(
                    ForeignKeyConstraint(
                        from_table=meta.db_table,
                        from_column=field.column,
                        to_table=related_model._meta.db_table,
                        to_column=related_model._meta.pk.column,
                        on_delete=on_delete,
                    )
                )

    # Register unique constraints
    # From unique_together
    for fields in meta.unique_together:
        columns = [meta.get_field(f).column for f in fields]
        registry.register_unique(
            UniqueConstraint(table=meta.db_table, columns=columns)
        )

    # From field unique=True
    for field in meta.get_fields():
        if not hasattr(field, "column"):
            continue
        if getattr(field, "unique", False) and not field.primary_key:
            registry.register_unique(
                UniqueConstraint(table=meta.db_table, columns=[field.column])
            )

    # From Meta.constraints
    for constraint in meta.constraints:
        if hasattr(constraint, "fields"):
            columns = [meta.get_field(f).column for f in constraint.fields]
            if hasattr(constraint, "condition") and constraint.condition is None:
                # UniqueConstraint without condition
                registry.register_unique(
                    UniqueConstraint(
                        table=meta.db_table, columns=columns, name=constraint.name
                    )
                )
