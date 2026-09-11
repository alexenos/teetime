"""
Tests for engine construction in app/models/database.py.

The 06:28 booking job is often this service's only caller between one morning
and the next, so its first query runs against a connection that has sat idle for
hours. On 2026-08-19 the pool handed it one the far end had already closed and
the job raised "connection is closed" before it could look for a booking. These
tests pin the pool settings that keep that from recurring.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.database import (
    _ADDED_COLUMNS,
    _ADDED_CONVERSATION_STATES,
    Base,
    BookingRecord,
    SessionRecord,
    WaldenCredentialRecord,
    _normalize_database_url,
    _pool_options_for,
    _run_column_migrations,
    engine,
)
from app.models.schemas import ConversationState

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


class TestColumnMigrations:
    """Columns added after a table already exists in a deployed database.

    create_all() only creates missing *tables*, so a column added to a model
    later never reaches an install that already has that table - the deployed
    Postgres has carried `bookings` and `sessions` since the first release, and
    `walden_credentials` since #183. _ADDED_COLUMNS is what backfills them, and
    a column missing from that list fails only in production, on the first
    write that touches it.
    """

    # The columns each table had at its first deployment - what an existing
    # install already has on disk. Everything a model declares beyond these has
    # to be in _ADDED_COLUMNS to reach that install, so this is the baseline the
    # test below measures against. Do not extend it: a new column belongs in
    # _ADDED_COLUMNS, which is exactly what this is here to prove.
    ORIGINAL_COLUMNS = {
        "sessions": [
            "id INTEGER PRIMARY KEY",
            "phone_number VARCHAR(20)",
            "state VARCHAR(40)",
            "pending_request_json TEXT",
            "last_interaction DATETIME",
        ],
        "bookings": [
            "id INTEGER PRIMARY KEY",
            "booking_id VARCHAR(50)",
            "phone_number VARCHAR(20)",
            "requested_date DATE",
            "requested_time TIME",
            "num_players INTEGER",
            "fallback_window_minutes INTEGER",
            "status VARCHAR(20)",
            "scheduled_execution_time DATETIME",
            "actual_booked_time TIME",
            "confirmation_number VARCHAR(100)",
            "error_message TEXT",
            "created_at DATETIME",
            "updated_at DATETIME",
        ],
        "walden_credentials": [
            "id INTEGER PRIMARY KEY",
            "phone_number VARCHAR(20)",
            "member_number_encrypted TEXT",
            "password_encrypted TEXT",
            "label VARCHAR(100)",
            "created_at DATETIME",
            "updated_at DATETIME",
        ],
    }

    @pytest.mark.asyncio
    async def test_every_model_column_reaches_an_existing_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A column on a model but not in _ADDED_COLUMNS never reaches production.

        Measured against the models rather than against _ADDED_COLUMNS itself:
        checking that the list applies what the list contains would pass happily
        while the one column somebody forgot to add stayed missing.

        DATABASE_URL is pinned to Postgres while a SQLite connection is migrated,
        so the two deliberately disagree: the migration has to take its dialect
        from the connection it was handed. Reading the setting instead emits
        "ADD COLUMN IF NOT EXISTS" at SQLite, which is a syntax error - and this
        test would then pass or fail depending on whoever ran it had a Postgres
        URL in their environment.
        """
        monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@h/d")
        models = {
            "sessions": SessionRecord,
            "bookings": BookingRecord,
            "walden_credentials": WaldenCredentialRecord,
        }
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        try:
            async with engine.begin() as conn:
                for table, columns in self.ORIGINAL_COLUMNS.items():
                    await conn.execute(text(f"CREATE TABLE {table} ({', '.join(columns)})"))

                await _run_column_migrations(conn)

                for table, model in models.items():
                    result = await conn.execute(text(f"PRAGMA table_info({table})"))
                    present = {row[1] for row in result.fetchall()}
                    declared = {column.name for column in model.__table__.columns}
                    assert declared <= present, (
                        f"{table} is missing {sorted(declared - present)} on an existing "
                        "install - add it to _ADDED_COLUMNS"
                    )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_migrations_are_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run on every startup, so a second pass must be a no-op, not an error."""
        monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://u:p@h/d")
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await _run_column_migrations(conn)
                await _run_column_migrations(conn)
        finally:
            await engine.dispose()

    def test_added_columns_covers_the_proxy_fields(self) -> None:
        """The three columns #185 adds to already-deployed tables."""
        listed = {(table, column) for table, column, _ in _ADDED_COLUMNS}

        assert ("sessions", "pending_proxy_target") in listed
        assert ("walden_credentials", "name") in listed
        assert ("walden_credentials", "telegram_username") in listed

    def test_every_added_conversation_state_is_a_real_enum_member(self) -> None:
        """Postgres pins the enum at CREATE TABLE time; a typo here fails only there."""
        for value in _ADDED_CONVERSATION_STATES:
            assert value in ConversationState.__members__
