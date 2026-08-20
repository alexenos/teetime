"""
Tests for engine construction in app/models/database.py.

The 06:28 booking job is often this service's only caller between one morning
and the next, so its first query runs against a connection that has sat idle for
hours. On 2026-08-19 the pool handed it one the far end had already closed and
the job raised "connection is closed" before it could look for a booking. These
tests pin the pool settings that keep that from recurring.
"""

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.database import _normalize_database_url, _pool_options_for, engine

POSTGRES_URL = "postgresql+asyncpg://user:pw@host/db"


class TestPoolOptionsForUrl:
    """Pool options are chosen by driver rather than applied blanket."""

    def test_postgres_enables_pre_ping(self):
        """A dead pooled connection must be caught before it reaches a query."""
        assert _pool_options_for(POSTGRES_URL)["pool_pre_ping"] is True

    def test_postgres_recycles_connections(self):
        """Long-lived connections retire on a normal request, not the racing one."""
        assert 0 < _pool_options_for(POSTGRES_URL)["pool_recycle"] <= 1800

    def test_sqlite_gets_no_pool_options(self):
        """SQLite is a local file or memory - it has no connection to go stale."""
        assert _pool_options_for("sqlite+aiosqlite:///./teetime.db") == {}
        assert _pool_options_for("sqlite+aiosqlite:///:memory:") == {}

    def test_in_memory_sqlite_is_served_by_a_static_pool(self):
        """
        The reason SQLite is excluded rather than merely untested: a StaticPool
        holds one connection, so recycling it would discard the schema with it.
        """
        memory_engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        assert isinstance(memory_engine.sync_engine.pool, StaticPool)


class TestNormalizeDatabaseUrl:
    """A bare sqlite:// URL is pointed at the async driver."""

    def test_bare_sqlite_url_gets_the_async_driver(self):
        assert (
            _normalize_database_url("sqlite:///./teetime.db") == "sqlite+aiosqlite:///./teetime.db"
        )

    def test_only_the_scheme_is_rewritten(self):
        """A path may contain the scheme text; replacing every match corrupts it."""
        assert (
            _normalize_database_url("sqlite:///./data/sqlite:///backup.db")
            == "sqlite+aiosqlite:///./data/sqlite:///backup.db"
        )

    def test_non_sqlite_urls_are_untouched(self):
        assert _normalize_database_url(POSTGRES_URL) == POSTGRES_URL

    def test_an_already_async_sqlite_url_is_untouched(self):
        """It does not start with the bare scheme, so nothing is rewritten."""
        url = "sqlite+aiosqlite:///:memory:"

        assert _normalize_database_url(url) == url


class TestOptionsReachThePool:
    """The chosen options survive the trip into the engine's pool."""

    def test_postgres_engine_pool_is_configured_from_them(self):
        """
        Built without connecting - create_async_engine is lazy, so this needs no
        Postgres server and still proves the wiring.
        """
        options = _pool_options_for(POSTGRES_URL)

        pool = create_async_engine(POSTGRES_URL, **options).sync_engine.pool

        assert pool._pre_ping is True
        assert pool._recycle == options["pool_recycle"]

    def test_configured_engine_matches_its_own_url(self):
        """Whatever URL is configured for this run, the engine agrees with it."""
        expected = _pool_options_for(engine.url.drivername)

        assert engine.sync_engine.pool._pre_ping is bool(expected)
