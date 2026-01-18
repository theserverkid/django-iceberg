"""
Database Validation - Validates database configuration.
"""

from django.db.backends.base.validation import BaseDatabaseValidation


class DatabaseValidation(BaseDatabaseValidation):
    """
    Database validation for the Polars + Iceberg backend.

    Validates model definitions against backend capabilities.
    """

    def check(self, **kwargs):
        """
        Run validation checks.

        Returns:
            List of validation issues
        """
        issues = super().check(**kwargs)

        # Add any Iceberg-specific validation here

        return issues

    def check_field_type(self, field, field_type):
        """
        Check if a field type is supported.
        """
        errors = []

        # Check for unsupported field types
        unsupported_types = []

        if field_type in unsupported_types:
            errors.append(
                f"Field '{field.name}' uses unsupported type '{field_type}' "
                f"for Polars/Iceberg backend."
            )

        return errors
