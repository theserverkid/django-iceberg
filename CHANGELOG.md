# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release of Django Iceberg
- Django database backend powered by Apache Iceberg and Polars
- Time travel queries with `as_of()`, `snapshots()`, and `history()`
- Cloud-native storage support (S3, GCS, Azure, local)
- Application-level constraints (foreign keys, unique, NOT NULL)
- Schema evolution with instant migrations
- Polars DataFrame export for analytics
- Full Django ORM compatibility
- ACID transactions on object storage
- Complete test suite
- Comprehensive documentation

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- N/A

### Security
- N/A

## [0.1.0] - 2026-01-18

### Added
- Initial alpha release
- Core database backend implementation
- Time travel functionality
- Iceberg table management
- Polars query compilation
- Django migrations support
- Basic documentation and examples

[Unreleased]: https://github.com/yourusername/django-iceberg/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yourusername/django-iceberg/releases/tag/v0.1.0
