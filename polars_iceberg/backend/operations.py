"""
Database Operations - SQL generation and value conversion for Polars + Iceberg.
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from django.conf import settings
from django.db.backends.base.operations import BaseDatabaseOperations
from django.utils import timezone


class DatabaseOperations(BaseDatabaseOperations):
    """
    Database operations for the Polars + Iceberg backend.

    Handles SQL generation and value conversion.
    """

    cast_char_field_without_max_length = "VARCHAR"
    cast_data_types = {
        "DateField": "DATE",
        "DateTimeField": "TIMESTAMP",
    }
    explain_prefix = "EXPLAIN"

    def quote_name(self, name: str) -> str:
        """Quote an identifier name."""
        if name.startswith('"') and name.endswith('"'):
            return name
        return '"%s"' % name

    def no_limit_value(self):
        """Return the value to use for no LIMIT clause."""
        return None

    def limit_offset_sql(self, low_mark: int, high_mark: int | None) -> str:
        """Generate LIMIT/OFFSET SQL."""
        fetch, offset = self._get_limit_offset_params(low_mark, high_mark)
        return " ".join(
            sql
            for sql in (
                ("LIMIT %d" % fetch) if fetch else None,
                ("OFFSET %d" % offset) if offset else None,
            )
            if sql
        )

    def _get_limit_offset_params(self, low_mark: int, high_mark: int | None):
        """Calculate LIMIT and OFFSET values."""
        offset = low_mark or 0
        if high_mark is not None:
            return (high_mark - offset), offset
        return None, offset

    def last_executed_query(self, cursor, sql: str, params) -> str:
        """Return the last executed query for debugging."""
        if params:
            if isinstance(params, (list, tuple)):
                params = tuple(self._quote_param(p) for p in params)
            else:
                params = {k: self._quote_param(v) for k, v in params.items()}
            return sql % params
        return sql

    def _quote_param(self, value) -> str:
        """Quote a parameter value for display."""
        if isinstance(value, str):
            return "'%s'" % value.replace("'", "''")
        elif isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        elif isinstance(value, (int, float, decimal.Decimal)):
            return str(value)
        elif value is None:
            return "NULL"
        elif isinstance(value, (bytes, bytearray, memoryview)):
            return "X'%s'" % value.hex()
        else:
            return "'%s'" % str(value)

    # Date/time extraction and truncation

    def date_extract_sql(self, lookup_type: str, sql: str, params):
        """Generate SQL for extracting a date part."""
        return f"EXTRACT({lookup_type} FROM {sql})", params

    def date_trunc_sql(self, lookup_type: str, sql: str, params, tzname=None):
        """Generate SQL for truncating a date."""
        return f"DATE_TRUNC('{lookup_type}', {sql})", params

    def datetime_cast_date_sql(self, sql: str, params, tzname):
        """Generate SQL for casting datetime to date."""
        return f"CAST({sql} AS DATE)", params

    def datetime_cast_time_sql(self, sql: str, params, tzname):
        """Generate SQL for casting datetime to time."""
        return f"CAST({sql} AS TIME)", params

    def datetime_extract_sql(self, lookup_type: str, sql: str, params, tzname):
        """Generate SQL for extracting a datetime part."""
        return f"EXTRACT({lookup_type} FROM {sql})", params

    def datetime_trunc_sql(self, lookup_type: str, sql: str, params, tzname):
        """Generate SQL for truncating a datetime."""
        return f"DATE_TRUNC('{lookup_type}', {sql})", params

    def time_trunc_sql(self, lookup_type: str, sql: str, params, tzname=None):
        """Generate SQL for truncating a time."""
        return f"DATE_TRUNC('{lookup_type}', {sql})", params

    def time_extract_sql(self, lookup_type: str, sql: str, params):
        """Generate SQL for extracting a time part."""
        return f"EXTRACT({lookup_type} FROM {sql})", params

    # Value adaptation

    def pk_default_value(self) -> str:
        """Return the default value for a primary key."""
        return "DEFAULT"

    def adapt_datetimefield_value(self, value):
        """Adapt a datetime value for storage."""
        if value is None:
            return None
        if timezone.is_aware(value):
            if settings.USE_TZ:
                value = timezone.make_naive(value, self.connection.timezone)
            else:
                raise ValueError(
                    "Backend does not support timezone-aware datetimes when USE_TZ is False."
                )
        return str(value)

    def adapt_timefield_value(self, value):
        """Adapt a time value for storage."""
        if value is None:
            return None
        if timezone.is_aware(value):
            raise ValueError("Backend does not support timezone-aware times.")
        return str(value)

    def adapt_datefield_value(self, value):
        """Adapt a date value for storage."""
        if value is None:
            return None
        return str(value)

    def adapt_decimalfield_value(self, value, max_digits=None, decimal_places=None):
        """Adapt a decimal value for storage."""
        if value is None:
            return None
        return str(value)

    # Value conversion

    def get_db_converters(self, expression):
        """Get converters for reading values from the database."""
        converters = super().get_db_converters(expression)
        internal_type = expression.output_field.get_internal_type()

        if internal_type == "UUIDField":
            converters.append(self.convert_uuidfield_value)
        elif internal_type == "BooleanField":
            converters.append(self.convert_booleanfield_value)
        elif internal_type == "DateTimeField":
            converters.append(self.convert_datetimefield_value)
        elif internal_type == "DateField":
            converters.append(self.convert_datefield_value)
        elif internal_type == "DecimalField":
            converters.append(self.convert_decimalfield_value)

        return converters

    def convert_uuidfield_value(self, value, expression, connection):
        """Convert a UUID value from the database."""
        if value is not None:
            if isinstance(value, str):
                value = uuid.UUID(value)
        return value

    def convert_booleanfield_value(self, value, expression, connection):
        """Convert a boolean value from the database."""
        if value in (1, 0):
            return bool(value)
        return value

    def convert_datetimefield_value(self, value, expression, connection):
        """Convert a datetime value from the database."""
        if value is not None and isinstance(value, str):
            value = datetime.datetime.fromisoformat(value)
        return value

    def convert_datefield_value(self, value, expression, connection):
        """Convert a date value from the database."""
        if value is not None and isinstance(value, str):
            value = datetime.date.fromisoformat(value)
        return value

    def convert_decimalfield_value(self, value, expression, connection):
        """Convert a decimal value from the database."""
        if value is not None and isinstance(value, (str, float)):
            value = decimal.Decimal(str(value))
        return value

    # Flush and reset

    def sql_flush(self, style, tables, *, reset_sequences=False, allow_cascade=False):
        """Generate SQL to flush (delete all data from) tables."""
        sql = []
        for table in tables:
            sql.append(
                "%s %s %s;"
                % (
                    style.SQL_KEYWORD("DELETE"),
                    style.SQL_KEYWORD("FROM"),
                    style.SQL_FIELD(self.quote_name(table)),
                )
            )
        return sql

    def sequence_reset_by_name_sql(self, style, sequences):
        """Generate SQL to reset sequences (not supported)."""
        return []

    def sequence_reset_sql(self, style, model_list):
        """Generate SQL to reset sequences (not supported)."""
        return []

    # Bulk operations

    def bulk_batch_size(self, fields, objs):
        """Return the batch size for bulk operations."""
        return len(objs)

    def bulk_insert_sql(self, fields, placeholder_rows):
        """Generate bulk insert SQL."""
        # placeholder_rows is a list of tuples containing SQL for each row
        if placeholder_rows and isinstance(placeholder_rows[0], tuple):
            # Convert tuple format to string format
            rows_sql = ", ".join(f"({', '.join(row)})" if isinstance(row, (list, tuple)) else str(row) for row in placeholder_rows)
        else:
            rows_sql = ", ".join(str(row) for row in placeholder_rows)
        return "VALUES " + rows_sql

    # Expressions

    def format_for_duration_arithmetic(self, sql: str) -> str:
        """Format a value for duration arithmetic."""
        return f"INTERVAL ({sql}) MICROSECOND"

    def combine_expression(self, connector: str, sub_expressions):
        """Combine expressions with an operator."""
        if connector == "^":
            return "POWER(%s, %s)" % tuple(sub_expressions)
        return super().combine_expression(connector, sub_expressions)

    # Limits

    def max_name_length(self) -> int:
        """Return the maximum length for identifiers."""
        return 128

    # Insert handling

    def last_insert_id(self, cursor, table_name: str, pk_name: str):
        """Get the last inserted ID."""
        return cursor.lastrowid

    def fetch_returned_insert_columns(self, cursor, returning_params):
        """Fetch columns returned from an INSERT."""
        return cursor.fetchone()

    def return_insert_columns(self, fields):
        """Generate RETURNING clause for INSERT."""
        if not fields:
            return "", ()
        columns = [
            "%s.%s"
            % (
                self.quote_name(field.model._meta.db_table),
                self.quote_name(field.column),
            )
            for field in fields
        ]
        return "RETURNING %s" % ", ".join(columns), ()

    # Regex

    def regex_lookup(self, lookup_type: str) -> str:
        """Generate regex lookup SQL."""
        if lookup_type == "regex":
            return "%s ~ %s"
        return "%s ~* %s"  # Case-insensitive

    # FOR UPDATE (not supported)

    def for_update_sql(self, nowait=False, skip_locked=False, of=(), no_key=False):
        """Generate FOR UPDATE SQL (not supported)."""
        return ""
