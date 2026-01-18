"""
Query Compiler - Translates SQL to Polars operations.

This module parses Django-generated SQL and converts it to Polars
DataFrame operations that can be executed against Iceberg tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

import polars as pl
import sqlparse
from sqlparse.sql import (
    Comparison,
    Identifier,
    IdentifierList,
    Parenthesis,
    Token,
    Where,
)
from sqlparse.tokens import Keyword, DML, Punctuation

if TYPE_CHECKING:
    from .iceberg_manager import IcebergManager


class QueryType(Enum):
    """Types of SQL queries."""
    SELECT = auto()
    INSERT = auto()
    UPDATE = auto()
    DELETE = auto()
    CREATE_TABLE = auto()
    DROP_TABLE = auto()
    ALTER_TABLE = auto()
    OTHER = auto()


@dataclass
class ParsedQuery:
    """Represents a parsed SQL query."""
    query_type: QueryType
    table_name: str | None = None
    columns: list[str] = field(default_factory=list)
    values: list[Any] = field(default_factory=list)
    where_clause: str | None = None
    where_params: list[Any] = field(default_factory=list)
    order_by: list[tuple[str, str]] = field(default_factory=list)  # (column, direction)
    limit: int | None = None
    offset: int | None = None
    returning: list[str] = field(default_factory=list)
    set_clauses: dict[str, Any] = field(default_factory=dict)
    raw_sql: str = ""


class QueryCompiler:
    """
    Compiles SQL queries to Polars operations.

    This is the core translation layer between Django's SQL and Polars.
    """

    def __init__(self, iceberg_manager: IcebergManager):
        self.iceberg = iceberg_manager

    def parse(self, sql: str, params: tuple | list | dict | None = None) -> ParsedQuery:
        """
        Parse a SQL statement into a ParsedQuery object.

        Args:
            sql: SQL statement (with %s or ? placeholders)
            params: Query parameters

        Returns:
            ParsedQuery object
        """
        # Normalize placeholders to ?
        normalized_sql = sql.replace("%s", "?")
        params_list = self._normalize_params(params)

        # Parse with sqlparse
        parsed = sqlparse.parse(normalized_sql)
        if not parsed:
            return ParsedQuery(query_type=QueryType.OTHER, raw_sql=sql)

        stmt = parsed[0]
        query_type = self._get_query_type(stmt)

        result = ParsedQuery(query_type=query_type, raw_sql=sql)

        if query_type == QueryType.SELECT:
            self._parse_select(stmt, result, params_list)
        elif query_type == QueryType.INSERT:
            self._parse_insert(stmt, result, params_list)
        elif query_type == QueryType.UPDATE:
            self._parse_update(stmt, result, params_list)
        elif query_type == QueryType.DELETE:
            self._parse_delete(stmt, result, params_list)
        elif query_type == QueryType.CREATE_TABLE:
            self._parse_create_table(stmt, result)
        elif query_type == QueryType.DROP_TABLE:
            self._parse_drop_table(stmt, result)

        return result

    def _normalize_params(self, params: tuple | list | dict | None) -> list[Any]:
        """Convert params to a list."""
        if params is None:
            return []
        if isinstance(params, dict):
            return list(params.values())
        return list(params)

    def _get_query_type(self, stmt) -> QueryType:
        """Determine the type of SQL query."""
        first_token = stmt.token_first(skip_ws=True, skip_cm=True)
        if first_token is None:
            return QueryType.OTHER

        token_value = first_token.ttype
        token_text = first_token.value.upper()

        if token_value is DML:
            if token_text == "SELECT":
                return QueryType.SELECT
            elif token_text == "INSERT":
                return QueryType.INSERT
            elif token_text == "UPDATE":
                return QueryType.UPDATE
            elif token_text == "DELETE":
                return QueryType.DELETE

        if token_value is Keyword.DDL or token_text in ("CREATE", "DROP", "ALTER"):
            if "CREATE" in token_text:
                return QueryType.CREATE_TABLE
            elif "DROP" in token_text:
                return QueryType.DROP_TABLE
            elif "ALTER" in token_text:
                return QueryType.ALTER_TABLE

        return QueryType.OTHER

    def _parse_select(self, stmt, result: ParsedQuery, params: list) -> None:
        """Parse a SELECT statement."""
        tokens = list(stmt.flatten())
        param_idx = 0

        # State machine for parsing
        state = "start"
        current_columns = []

        for token in tokens:
            ttype = token.ttype
            value = token.value.upper().strip()
            original_value = token.value.strip()

            if value == "SELECT":
                state = "columns"
            elif value == "FROM":
                state = "table"
            elif value == "WHERE":
                state = "where"
            elif value == "ORDER":
                state = "order_by_pending"
            elif value == "BY" and state == "order_by_pending":
                state = "order_by"
            elif value == "LIMIT":
                state = "limit"
            elif value == "OFFSET":
                state = "offset"
            elif value == "ASC":
                if result.order_by:
                    col, _ = result.order_by[-1]
                    result.order_by[-1] = (col, "ASC")
            elif value == "DESC":
                if result.order_by:
                    col, _ = result.order_by[-1]
                    result.order_by[-1] = (col, "DESC")
            elif ttype is not None and not value:
                continue
            elif state == "columns" and original_value not in (",", ""):
                if original_value != "*":
                    result.columns.append(self._clean_identifier(original_value))
                else:
                    result.columns = ["*"]
            elif state == "table" and original_value not in (",", ""):
                result.table_name = self._clean_identifier(original_value)
                state = "after_table"
            elif state == "where":
                if original_value == "?":
                    if param_idx < len(params):
                        result.where_params.append(params[param_idx])
                        param_idx += 1
            elif state == "order_by" and original_value not in (",", ""):
                result.order_by.append((self._clean_identifier(original_value), "ASC"))
            elif state == "limit":
                try:
                    result.limit = int(original_value)
                except ValueError:
                    pass
            elif state == "offset":
                try:
                    result.offset = int(original_value)
                except ValueError:
                    pass

        # Extract WHERE clause as string
        result.where_clause = self._extract_where_clause(stmt)

    def _parse_insert(self, stmt, result: ParsedQuery, params: list) -> None:
        """Parse an INSERT statement."""
        sql_str = str(stmt)

        # Extract table name
        match = re.search(r'INSERT\s+INTO\s+"?(\w+)"?', sql_str, re.IGNORECASE)
        if match:
            result.table_name = match.group(1)

        # Extract column names
        col_match = re.search(r'\(([^)]+)\)\s*VALUES', sql_str, re.IGNORECASE)
        if col_match:
            cols = col_match.group(1)
            result.columns = [self._clean_identifier(c.strip()) for c in cols.split(",")]

        # Extract values (use params)
        result.values = params

        # Check for RETURNING clause
        returning_match = re.search(r'RETURNING\s+(.+?)(?:;|$)', sql_str, re.IGNORECASE)
        if returning_match:
            returning_cols = returning_match.group(1)
            result.returning = [
                self._clean_identifier(c.strip())
                for c in returning_cols.split(",")
            ]

    def _parse_update(self, stmt, result: ParsedQuery, params: list) -> None:
        """Parse an UPDATE statement."""
        sql_str = str(stmt)

        # Extract table name
        match = re.search(r'UPDATE\s+"?(\w+)"?', sql_str, re.IGNORECASE)
        if match:
            result.table_name = match.group(1)

        # Extract SET clauses
        set_match = re.search(r'SET\s+(.+?)(?:WHERE|$)', sql_str, re.IGNORECASE | re.DOTALL)
        if set_match:
            set_str = set_match.group(1)
            # Count placeholders in SET clause
            set_placeholders = set_str.count("?") + set_str.count("%s")

            # Parse SET assignments
            assignments = re.findall(r'"?(\w+)"?\s*=\s*(\?|%s|[^,]+)', set_str)
            param_idx = 0
            for col, val in assignments:
                col = self._clean_identifier(col)
                if val.strip() in ("?", "%s"):
                    if param_idx < len(params):
                        result.set_clauses[col] = params[param_idx]
                        param_idx += 1
                else:
                    # Parse literal value
                    val_stripped = val.strip()
                    if val_stripped.upper() == "NULL":
                        result.set_clauses[col] = None
                    elif val_stripped.upper() in ("TRUE", "FALSE"):
                        result.set_clauses[col] = val_stripped.upper() == "TRUE"
                    elif val_stripped.startswith("'") and val_stripped.endswith("'"):
                        result.set_clauses[col] = val_stripped[1:-1]
                    else:
                        # Try to parse as number
                        try:
                            if "." in val_stripped:
                                result.set_clauses[col] = float(val_stripped)
                            else:
                                result.set_clauses[col] = int(val_stripped)
                        except ValueError:
                            result.set_clauses[col] = val_stripped

            # Remaining params are for WHERE clause
            result.where_params = params[param_idx:]

        # Extract WHERE clause
        result.where_clause = self._extract_where_clause(stmt)

    def _parse_delete(self, stmt, result: ParsedQuery, params: list) -> None:
        """Parse a DELETE statement."""
        sql_str = str(stmt)

        # Extract table name
        match = re.search(r'DELETE\s+FROM\s+"?(\w+)"?', sql_str, re.IGNORECASE)
        if match:
            result.table_name = match.group(1)

        # WHERE params
        result.where_params = params
        result.where_clause = self._extract_where_clause(stmt)

    def _parse_create_table(self, stmt, result: ParsedQuery) -> None:
        """Parse a CREATE TABLE statement."""
        sql_str = str(stmt)
        match = re.search(r'CREATE\s+TABLE\s+"?(\w+)"?', sql_str, re.IGNORECASE)
        if match:
            result.table_name = match.group(1)

    def _parse_drop_table(self, stmt, result: ParsedQuery) -> None:
        """Parse a DROP TABLE statement."""
        sql_str = str(stmt)
        match = re.search(r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?"?(\w+)"?', sql_str, re.IGNORECASE)
        if match:
            result.table_name = match.group(1)

    def _extract_where_clause(self, stmt) -> str | None:
        """Extract the WHERE clause as a string."""
        sql_str = str(stmt)
        match = re.search(r'WHERE\s+(.+?)(?:ORDER|LIMIT|OFFSET|RETURNING|;|$)', sql_str, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return None

    def _clean_identifier(self, name: str) -> str:
        """Remove quotes and table prefixes from identifier names."""
        name = name.strip().strip('"').strip("'").strip("`")
        # Handle table.column format
        if "." in name:
            # Take the column part (after the last dot)
            parts = name.split(".")
            name = parts[-1].strip().strip('"').strip("'").strip("`")
        return name

    def execute_select(self, query: ParsedQuery) -> pl.DataFrame:
        """
        Execute a SELECT query and return results as a Polars DataFrame.

        Args:
            query: ParsedQuery object

        Returns:
            Polars DataFrame with query results
        """
        if not query.table_name:
            return pl.DataFrame()

        try:
            # Read from Iceberg
            df = self.iceberg.scan(query.table_name)
        except Exception:
            # Table might not exist yet
            return pl.DataFrame()

        # Apply column selection
        if query.columns and query.columns != ["*"]:
            # Deduplicate columns while preserving order
            seen = set()
            available_cols = []
            for c in query.columns:
                if c in df.columns and c not in seen:
                    available_cols.append(c)
                    seen.add(c)
            if available_cols:
                df = df.select(available_cols)

        # Apply WHERE clause
        if query.where_clause and query.where_params:
            df = self._apply_where(df, query.where_clause, query.where_params)

        # Apply ORDER BY
        if query.order_by:
            order_cols = []
            descending = []
            for col, direction in query.order_by:
                if col in df.columns:
                    order_cols.append(col)
                    descending.append(direction.upper() == "DESC")
            if order_cols:
                df = df.sort(order_cols, descending=descending)

        # Apply OFFSET
        if query.offset:
            df = df.slice(query.offset)

        # Apply LIMIT
        if query.limit:
            df = df.head(query.limit)

        return df

    def execute_insert(self, query: ParsedQuery) -> tuple[int, Any]:
        """
        Execute an INSERT query.

        Args:
            query: ParsedQuery object

        Returns:
            Tuple of (row_count, last_insert_id or returning values)
        """
        if not query.table_name or not query.columns:
            return 0, None

        # Build a single-row DataFrame
        row_data = {}
        for i, col in enumerate(query.columns):
            if i < len(query.values):
                row_data[col] = [query.values[i]]
            else:
                row_data[col] = [None]

        new_row = pl.DataFrame(row_data)

        # Handle auto-increment ID
        last_id = None

        # Check if 'id' is in the INSERT columns
        id_in_columns = "id" in [c.lower() for c in query.columns]

        if id_in_columns:
            # ID is explicitly provided
            id_col = next(c for c in query.columns if c.lower() == "id")
            idx = query.columns.index(id_col)
            if idx < len(query.values) and query.values[idx] is not None:
                last_id = query.values[idx]

        # Generate ID if not provided (or if id column not in INSERT but table has it)
        if last_id is None:
            try:
                existing = self.iceberg.scan(query.table_name)
                if len(existing) > 0 and "id" in existing.columns:
                    max_id = existing["id"].max()
                    last_id = (max_id + 1) if max_id is not None else 1
                else:
                    last_id = 1
            except Exception:
                last_id = 1

            # Add or update the ID in the row
            if id_in_columns:
                id_col = next(c for c in query.columns if c.lower() == "id")
                new_row = new_row.with_columns(pl.lit(last_id).alias(id_col))
            else:
                # ID not in columns - add it
                new_row = new_row.with_columns(pl.lit(last_id).alias("id"))

        # Append to table
        # The IcebergManager.append will handle schema alignment
        try:
            self.iceberg.append(query.table_name, new_row)
        except Exception as e:
            # Table might not exist or schema mismatch
            raise e

        # Handle RETURNING
        returning_values = None
        if query.returning:
            values = []
            for col in query.returning:
                # Try to find the column (handle case variations)
                col_lower = col.lower()
                matched_col = None
                for nc in new_row.columns:
                    if nc.lower() == col_lower:
                        matched_col = nc
                        break
                if matched_col:
                    values.append(new_row[matched_col][0])
                elif col_lower == "id":
                    # ID might have been auto-generated
                    values.append(last_id)
                else:
                    values.append(None)
            returning_values = tuple(values) if values else None

        return 1, returning_values or last_id

    def execute_update(self, query: ParsedQuery) -> int:
        """
        Execute an UPDATE query.

        Args:
            query: ParsedQuery object

        Returns:
            Number of rows affected
        """
        if not query.table_name:
            return 0

        try:
            df = self.iceberg.scan(query.table_name)
        except Exception:
            return 0

        if len(df) == 0:
            return 0

        # Apply WHERE to find rows to update
        if query.where_clause and query.where_params:
            mask = self._build_where_mask(df, query.where_clause, query.where_params)
        else:
            mask = pl.lit(True)

        # Count affected rows
        affected = df.filter(mask)
        affected_count = len(affected)

        if affected_count == 0:
            return 0

        # Apply SET clauses
        updates = []
        for col, val in query.set_clauses.items():
            if col in df.columns:
                updates.append(
                    pl.when(mask).then(pl.lit(val)).otherwise(pl.col(col)).alias(col)
                )

        if updates:
            df = df.with_columns(updates)
            self.iceberg.overwrite(query.table_name, df)

        return affected_count

    def execute_delete(self, query: ParsedQuery) -> int:
        """
        Execute a DELETE query.

        Args:
            query: ParsedQuery object

        Returns:
            Number of rows deleted
        """
        if not query.table_name:
            return 0

        try:
            df = self.iceberg.scan(query.table_name)
        except Exception:
            return 0

        if len(df) == 0:
            return 0

        original_count = len(df)

        # Apply WHERE to find rows to delete
        if query.where_clause and query.where_params:
            mask = self._build_where_mask(df, query.where_clause, query.where_params)
            # Keep rows that DON'T match the WHERE clause
            df = df.filter(~mask)
        else:
            # DELETE without WHERE deletes all rows
            df = df.clear()

        deleted_count = original_count - len(df)

        if deleted_count > 0:
            self.iceberg.overwrite(query.table_name, df)

        return deleted_count

    def _apply_where(self, df: pl.DataFrame, where_clause: str, params: list) -> pl.DataFrame:
        """Apply a WHERE clause to a DataFrame."""
        mask = self._build_where_mask(df, where_clause, params)
        return df.filter(mask)

    def _build_where_mask(self, df: pl.DataFrame, where_clause: str, params: list) -> pl.Expr:
        """
        Build a Polars expression from a WHERE clause.

        This is a simplified implementation that handles common patterns.
        """
        # Replace ? placeholders with actual values
        clause = where_clause
        for param in params:
            if isinstance(param, (list, tuple)):
                # Handle IN clause with list parameter
                items = []
                for item in param:
                    if isinstance(item, str):
                        items.append(f"'{item}'")
                    elif item is None:
                        items.append("NULL")
                    elif isinstance(item, bool):
                        items.append("true" if item else "false")
                    else:
                        items.append(str(item))
                replacement = ", ".join(items)
            elif isinstance(param, str):
                replacement = f"'{param}'"
            elif param is None:
                replacement = "NULL"
            elif isinstance(param, bool):
                replacement = "true" if param else "false"
            else:
                replacement = str(param)
            clause = clause.replace("?", replacement, 1)

        # Parse simple conditions: column = value, column > value, etc.
        # This handles: "id" = 1, "name" = 'test', "table"."column" = 1, etc.

        # Helper to extract column name (handles table.column format)
        def extract_col_name(s: str) -> str:
            # Remove quotes and table prefix
            s = s.strip().strip('"').strip("'")
            if "." in s:
                parts = s.split(".")
                return parts[-1].strip().strip('"').strip("'")
            return s

        # Handle simple equality: "column" = value or "table"."column" = value
        eq_match = re.match(r'("?\w+"?\.)?"?(\w+)"?\s*=\s*(.+)', clause.strip())
        if eq_match:
            col = eq_match.group(2)  # Column name is group 2
            val = self._parse_value(eq_match.group(3).strip())
            if col in df.columns:
                return pl.col(col) == val

        # Handle inequality operators
        for op, polars_op in [
            (">=", ">="),
            ("<=", "<="),
            ("<>", "!="),
            ("!=", "!="),
            (">", ">"),
            ("<", "<"),
        ]:
            match = re.match(rf'"?(\w+)"?\s*{re.escape(op)}\s*(.+)', clause.strip())
            if match:
                col = match.group(1)
                val = self._parse_value(match.group(2).strip())
                if col in df.columns:
                    if polars_op == ">=":
                        return pl.col(col) >= val
                    elif polars_op == "<=":
                        return pl.col(col) <= val
                    elif polars_op == "!=":
                        return pl.col(col) != val
                    elif polars_op == ">":
                        return pl.col(col) > val
                    elif polars_op == "<":
                        return pl.col(col) < val

        # Handle IS NULL / IS NOT NULL (with optional table prefix)
        if "IS NOT NULL" in clause.upper():
            col_match = re.match(r'("?\w+"?\.)?"?(\w+)"?\s+IS\s+NOT\s+NULL', clause, re.IGNORECASE)
            if col_match and col_match.group(2) in df.columns:
                return pl.col(col_match.group(2)).is_not_null()

        if "IS NULL" in clause.upper():
            col_match = re.match(r'("?\w+"?\.)?"?(\w+)"?\s+IS\s+NULL', clause, re.IGNORECASE)
            if col_match and col_match.group(2) in df.columns:
                return pl.col(col_match.group(2)).is_null()

        # Handle LIKE (with optional table prefix)
        like_match = re.match(r'("?\w+"?\.)?"?(\w+)"?\s+(?:I?LIKE)\s+(.+)', clause, re.IGNORECASE)
        if like_match:
            col = like_match.group(2)  # Column name is group 2
            pattern = self._parse_value(like_match.group(3).strip())
            if col in df.columns and isinstance(pattern, str):
                # Convert SQL LIKE pattern to regex
                regex_pattern = pattern.replace("%", ".*").replace("_", ".")
                return pl.col(col).str.contains(regex_pattern)

        # Handle IN clause (with optional table prefix)
        in_match = re.match(r'("?\w+"?\.)?"?(\w+)"?\s+IN\s*\((.+)\)', clause, re.IGNORECASE)
        if in_match:
            col = in_match.group(2)
            values_str = in_match.group(3)
            values = [self._parse_value(v.strip()) for v in values_str.split(",")]
            if col in df.columns:
                return pl.col(col).is_in(values)

        # Default: return all True (no filtering)
        return pl.lit(True)

    def _parse_value(self, value_str: str) -> Any:
        """Parse a SQL value string into a Python value."""
        value_str = value_str.strip()

        # NULL
        if value_str.upper() == "NULL":
            return None

        # Boolean
        if value_str.upper() == "TRUE":
            return True
        if value_str.upper() == "FALSE":
            return False

        # String (quoted)
        if (value_str.startswith("'") and value_str.endswith("'")) or \
           (value_str.startswith('"') and value_str.endswith('"')):
            return value_str[1:-1]

        # Integer
        try:
            return int(value_str)
        except ValueError:
            pass

        # Float
        try:
            return float(value_str)
        except ValueError:
            pass

        return value_str
