# Django Iceberg Project

Django REST API project using a custom Polars + Iceberg database backend.

## Project Structure

```
django-iceberg/
├── api/                    # Django project settings
│   ├── settings.py         # Database configured to use polars_iceberg.backend
│   └── urls.py
├── polars_iceberg/         # Custom Django database backend
│   └── backend/            # Polars + Iceberg implementation
├── data/                   # Database storage (gitignored)
│   ├── warehouse/          # Iceberg tables (Parquet files)
│   └── catalog.db          # SQLite catalog for table metadata
└── manage.py
```

## Quick Start

```bash
# Install dependencies
uv sync

# Run migrations
uv run python manage.py migrate

# Start dev server
uv run python manage.py runserver
```

## Database Configuration

The project uses a custom `polars_iceberg.backend` instead of traditional SQL databases:

```python
DATABASES = {
    "default": {
        "ENGINE": "polars_iceberg.backend",
        "WAREHOUSE": "data/warehouse",
        "CATALOG_URI": "sqlite:///data/catalog.db",
        "NAMESPACE": "default",
    }
}
```

## Key Features

- **Polars**: Blazing fast in-memory DataFrame operations
- **Apache Iceberg**: Time travel, schema evolution, cloud-ready storage
- **Django ORM**: Full compatibility with Django models and migrations
- **Application-level constraints**: FK and unique validation handled in Python

## Dependencies

- Django 6.0+
- Polars 1.0+
- PyIceberg 0.7+
- PyArrow 15.0+

## Commands

```bash
# Run tests
uv run python manage.py test

# Create superuser
uv run python manage.py createsuperuser

# Django shell
uv run python manage.py shell
```
