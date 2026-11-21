# The application's version. Semantic Versioning; operations/versioning.md says
# which part a PR bumps. Must equal [tool.poetry] version in pyproject.toml:
# tests/test_version.py fails otherwise. The image installs with --no-root, so
# importlib.metadata cannot supply it at runtime; this constant does.
__version__ = "1.0.0"
