# The application's version, and the only place it is bumped. Semantic
# Versioning; operations/versioning.md says which part a PR bumps. pyproject.toml
# carries a frozen version that is not bumped, to keep the Docker dependency
# layer cached (#424).
__version__ = "1.0.2"
