# Polars + Iceberg Django Database Backend

Custom Django database backend powered by Polars (analytics) and Apache Iceberg (storage).

## Architecture

```
Django ORM → SQL → Query Compiler → Polars DataFrames → Iceberg Tables → Parquet Files
```

## Module Overview

| File | Purpose |
|------|---------|
| `backend/base.py` | DatabaseWrapper and PolarsCursor - main entry point |
| `backend/query_compiler.py` | Translates SQL to Polars DataFrame operations |
| `backend/iceberg_manager.py` | Manages Iceberg catalog, tables, and I/O |
| `backend/schema.py` | Handles Django migrations → Iceberg schema changes |
| `backend/constraints.py` | Application-level FK/Unique/NotNull validation |
| `backend/features.py` | Django capability flags |
| `backend/operations.py` | SQL generation helpers |
| `backend/introspection.py` | Schema introspection from Iceberg |
| `timetravel.py` | Time travel query API |

## Type Mappings

Django fields are mapped to Iceberg types:

| Django Field | Iceberg Type |
|--------------|--------------|
| AutoField, BigAutoField | LongType |
| IntegerField | IntegerType |
| CharField, TextField | StringType |
| BooleanField | BooleanType |
| DateField | DateType |
| DateTimeField | TimestampType |
| ForeignKey, OneToOneField | LongType |
| DecimalField | DecimalType |
| BinaryField | BinaryType |

## Query Flow

1. Django generates SQL with `%s` placeholders
2. `PolarsCursor.execute()` receives SQL + params
3. `QueryCompiler.parse()` extracts table, columns, WHERE, etc.
4. `QueryCompiler.execute_*()` translates to Polars operations
5. `IcebergManager` reads/writes Parquet via PyIceberg

## Key Implementation Details

### INSERT
- Auto-generates IDs by finding max(id) + 1
- Aligns DataFrame schema to match Iceberg table schema exactly
- Handles RETURNING clause for Django's ORM

### UPDATE
- Reads entire table, applies WHERE mask, updates columns
- Overwrites table with modified DataFrame

### DELETE
- Reads table, filters out matching rows, overwrites

### SELECT
- Scans Iceberg table → Polars DataFrame
- Applies column selection, WHERE, ORDER BY, LIMIT/OFFSET

## Constraints

Since Iceberg doesn't support database-level constraints, they're validated in Python:

- **ForeignKey**: Checks referenced table before INSERT
- **Unique**: Checks for duplicates before INSERT/UPDATE
- **NotNull**: Validates required fields

## Time Travel

```python
# Get historical data
manager.scan("table_name", snapshot_id=123)
manager.scan("table_name", timestamp=datetime(2024, 1, 1))

# List snapshots
manager.get_snapshots("table_name")
```

## Limitations

- No JOINs at database level (handled by Django in Python)
- No transactions (Iceberg provides ACID at table level)
- Updates/deletes rewrite entire table (suitable for small-medium data)
- Some Django features disabled (see features.py)

## Testing

```bash
# Run Django migrations
uv run python manage.py migrate

# Test CRUD
uv run python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'api.settings')
django.setup()
from django.contrib.auth.models import User
User.objects.create_user('test', 'test@example.com', 'pass')
print(User.objects.all())
"
```
