"""
Database Features - Declares capabilities of the Polars + Iceberg backend.
"""

from django.db.backends.base.features import BaseDatabaseFeatures
from django.utils.functional import cached_property


class DatabaseFeatures(BaseDatabaseFeatures):
    """
    Database features for the Polars + Iceberg backend.

    This declares what SQL features and capabilities our backend supports.
    """

    # Basic capabilities
    minimum_database_version = None
    test_db_allows_multiple_connections = True
    supports_unspecified_pk = True
    supports_timezones = False  # Store as UTC
    supports_transactions = True  # Iceberg has ACID
    atomic_transactions = True
    can_rollback_ddl = False  # Schema changes are immediate
    can_create_inline_fk = False  # FKs at application level
    requires_literal_defaults = True
    can_clone_databases = False
    supports_temporal_subtraction = True
    ignores_table_name_case = False
    supports_cast_with_precision = False
    time_cast_precision = 3
    can_release_savepoints = False  # No savepoints in Iceberg
    has_case_insensitive_like = False
    can_alter_table_drop_column = False  # Iceberg doesn't drop columns
    supports_parentheses_in_compound = True

    # Constraint support (handled at application level)
    can_defer_constraint_checks = False
    supports_foreign_keys = False  # Validated at application level
    supports_column_check_constraints = False
    supports_table_check_constraints = False

    # Window functions and aggregates
    supports_over_clause = True
    supports_frame_range_fixed_distance = True
    supports_frame_exclusion = True
    supports_aggregate_filter_clause = True
    supports_aggregate_order_by_clause = True
    supports_aggregate_distinct_multiple_argument = True
    supports_any_value = True

    # Ordering
    order_by_nulls_first = False

    # JSON support
    supports_json_field = True
    supports_json_field_contains = False
    can_introspect_json_field = True
    has_json_object_function = True

    # Upsert/conflict handling
    supports_update_conflicts = False
    supports_update_conflicts_with_target = False

    # Generated columns
    supports_stored_generated_columns = False
    supports_virtual_generated_columns = False

    # Insert handling
    supports_default_keyword_in_insert = True
    can_return_columns_from_insert = True
    can_return_rows_from_bulk_insert = False
    can_return_rows_from_update = False

    # Character fields
    supports_unlimited_charfield = True

    # Expressions
    supports_boolean_expr_in_select_clause = True
    supports_expression_indexes = False
    supports_partial_indexes = False

    # Explain
    supports_explaining_query_execution = False

    # Sequences
    supports_sequence_reset = False

    # Collation
    supports_collation_on_charfield = False
    supports_collation_on_textfield = False

    # Comments
    supports_comments = False

    # Covering indexes
    supports_covering_indexes = False

    @cached_property
    def introspected_field_types(self):
        """Map introspected types to Django field types."""
        return {
            **super().introspected_field_types,
            "BigAutoField": "AutoField",
            "DurationField": "BigIntegerField",
            "GenericIPAddressField": "CharField",
            "SmallAutoField": "AutoField",
        }

    @cached_property
    def django_test_skips(self):
        """Tests to skip for this backend."""
        return {
            "Polars/Iceberg backend doesn't support this feature": {
                # Skip tests that require features we don't support
                "schema.tests.SchemaTests.test_add_foreign_key_with_index",
                "schema.tests.SchemaTests.test_foreign_key_index_long_db_column",
            },
        }
