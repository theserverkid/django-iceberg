# Contributing to Django Iceberg

Thank you for your interest in contributing to Django Iceberg! We welcome contributions from the community.

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager (recommended) or pip
- Git

### Development Setup

1. Fork the repository on GitHub

2. Clone your fork:
```bash
git clone https://github.com/yourusername/django-iceberg.git
cd django-iceberg
```

3. Install dependencies:
```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e ".[dev]"
```

4. Run tests to verify setup:
```bash
uv run python manage.py test
```

## Development Workflow

### 1. Create a Branch

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

### 2. Make Changes

- Write clear, concise code following Django conventions
- Add tests for new functionality
- Update documentation as needed
- Keep commits focused and atomic

### 3. Run Tests

```bash
# Run all tests
uv run python manage.py test

# Run specific test module
uv run python manage.py test polars_iceberg.tests.test_queries

# Run with coverage
uv run coverage run --source='.' manage.py test
uv run coverage report
```

### 4. Format and Lint

```bash
# Format with black
uv run black .

# Lint with ruff
uv run ruff check .

# Type check with mypy
uv run mypy polars_iceberg/
```

### 5. Commit Changes

Write clear commit messages following [Conventional Commits](https://www.conventionalcommits.org/):

```bash
git commit -m "feat: add support for JSON field type"
git commit -m "fix: handle null values in unique constraints"
git commit -m "docs: update time travel examples"
git commit -m "test: add tests for DELETE operations"
```

### 6. Push and Create PR

```bash
git push origin feature/your-feature-name
```

Then create a Pull Request on GitHub with:
- Clear description of changes
- Reference to related issues (e.g., "Fixes #123")
- Screenshots/examples if applicable

## Code Style

### Python Style

- Follow [PEP 8](https://pep8.org/)
- Use [Black](https://black.readthedocs.io/) for formatting (line length: 88)
- Use type hints where possible
- Write docstrings for public APIs

```python
def scan_table(
    self,
    table_name: str,
    filters: list[tuple[str, str, Any]] | None = None,
    snapshot_id: int | None = None,
) -> pl.DataFrame:
    """
    Scan an Iceberg table and return a Polars DataFrame.

    Args:
        table_name: Name of the table to scan
        filters: Optional list of (column, operator, value) tuples
        snapshot_id: Optional snapshot ID for time travel

    Returns:
        Polars DataFrame with table data

    Raises:
        TableNotFoundError: If table doesn't exist
        SnapshotNotFoundError: If snapshot_id is invalid
    """
    # Implementation...
```

### Django Conventions

- Follow [Django coding style](https://docs.djangoproject.com/en/stable/internals/contributing/writing-code/coding-style/)
- Use Django's built-in exceptions (e.g., `IntegrityError`, `ValidationError`)
- Maintain compatibility with Django 6.0+

### Testing

- Write tests for all new features
- Maintain test coverage above 80%
- Use Django's `TestCase` for database tests
- Mock external dependencies (S3, REST catalog)

```python
from django.test import TestCase
from myapp.models import Article

class TimeravelTestCase(TestCase):
    def test_as_of_query(self):
        """Test querying historical data."""
        article = Article.objects.create(title="Test")
        timestamp = timezone.now()

        article.title = "Updated"
        article.save()

        # Query old version
        old_article = Article.objects.as_of(timestamp).get(pk=article.pk)
        self.assertEqual(old_article.title, "Test")
```

## Documentation

### Code Documentation

- Add docstrings to all public functions, classes, and modules
- Use [Google style](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings) docstrings
- Include examples in docstrings for complex APIs

### User Documentation

Update relevant documentation files:
- [README.md](README.md) - User-facing features and quick start
- [WHY.md](WHY.md) - High-level vision and use cases
- [polars_iceberg/CLAUDE.md](polars_iceberg/CLAUDE.md) - Technical architecture
- [CHANGELOG.md](CHANGELOG.md) - User-visible changes

## Pull Request Guidelines

### Before Submitting

- [ ] Tests pass locally
- [ ] Code is formatted with Black
- [ ] No lint errors from Ruff
- [ ] Documentation updated
- [ ] CHANGELOG.md updated (if user-facing change)
- [ ] Commit messages follow Conventional Commits

### PR Description Template

```markdown
## Description
Brief summary of changes

## Type of Change
- [ ] Bug fix (non-breaking change fixing an issue)
- [ ] New feature (non-breaking change adding functionality)
- [ ] Breaking change (fix or feature causing existing functionality to change)
- [ ] Documentation update

## Related Issues
Fixes #123

## Testing
- Describe testing done
- Include test coverage if applicable

## Screenshots (if applicable)
Add screenshots for UI changes

## Checklist
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] CHANGELOG.md updated
- [ ] No breaking changes (or documented if necessary)
```

## Reporting Bugs

### Before Reporting

1. Search existing issues to avoid duplicates
2. Try to reproduce on latest version
3. Test with minimal configuration

### Bug Report Template

```markdown
## Description
Clear description of the bug

## Steps to Reproduce
1. Configure database with...
2. Run migration...
3. Execute query...
4. See error

## Expected Behavior
What should happen

## Actual Behavior
What actually happens

## Environment
- Django Iceberg version: x.y.z
- Django version: x.y.z
- Python version: 3.x
- OS: Windows/macOS/Linux
- Storage backend: S3/GCS/Local

## Traceback
```python
Paste full traceback here
```

## Additional Context
Any other relevant information
```

## Feature Requests

We welcome feature requests! Please:

1. Search existing issues first
2. Describe the use case and problem being solved
3. Provide examples of desired API/behavior
4. Consider implementation complexity and tradeoffs

### Feature Request Template

```markdown
## Feature Description
Clear description of the proposed feature

## Use Case
Why is this feature needed? What problem does it solve?

## Proposed API
```python
# Example of how the feature would be used
Article.objects.some_new_method()
```

## Alternatives Considered
Other approaches you've thought about

## Implementation Ideas
If you have thoughts on implementation

## Additional Context
Any other relevant information
```

## Community Guidelines

### Code of Conduct

- Be respectful and inclusive
- Welcome newcomers
- Assume good intentions
- Focus on constructive feedback
- See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for details

### Communication

- **GitHub Issues**: Bug reports and feature requests
- **GitHub Discussions**: Questions and general discussion
- **Pull Requests**: Code contributions
- **Discord**: Real-time chat with community

## Review Process

### For Contributors

- Maintainers will review PRs within 3-5 business days
- Address review feedback promptly
- Be open to suggestions and changes
- CI must pass before merge

### For Reviewers

- Review code for correctness, style, and performance
- Test locally if possible
- Provide constructive feedback
- Approve when ready or request changes

## Release Process

Django Iceberg follows [Semantic Versioning](https://semver.org/):

- **MAJOR**: Breaking changes
- **MINOR**: New features (backward compatible)
- **PATCH**: Bug fixes (backward compatible)

Releases are published to PyPI when:
1. All tests pass
2. CHANGELOG.md is updated
3. Version is bumped in pyproject.toml
4. Git tag is created

## Areas We Need Help With

- **Testing**: More test coverage, especially edge cases
- **Documentation**: Examples, tutorials, and guides
- **Performance**: Query optimization and benchmarking
- **Cloud Providers**: GCS and Azure testing
- **Integrations**: Django REST Framework serializers
- **DevOps**: Docker images, Helm charts, Terraform modules

## Getting Help

If you're stuck:

1. Check existing documentation
2. Search closed issues
3. Ask in GitHub Discussions
4. Join Discord for real-time help

## Recognition

Contributors are recognized in:
- [CHANGELOG.md](CHANGELOG.md) - Per-release credits
- GitHub contributors page
- Annual acknowledgments blog post

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

Thank you for contributing to Django Iceberg! Together we're building the future of Django databases.
