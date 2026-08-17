"""Shared fixtures for the HTTP-level registry tests.

The ``app_client`` fixture used to live in ``test_publish_flow.py``, and
``test_app_publish.py`` says in its own docstring that it avoided
duplicating it. It is here now so any test module can take it without a
second copy going stale.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

REVIEWER_SECRET = "test-reviewer-secret"


@pytest_asyncio.fixture
async def app_client(tmp_path, monkeypatch):
    """Spin up a fresh registry instance with a temp SQLite DB.

    We deliberately avoid ``importlib.reload`` here -- the SQLAlchemy
    declarative registry survives reloads in 2.x, which used to cause
    ``Table 'publishers' is already defined`` errors. Instead we point
    the existing modules at a fresh engine/session and reset cached
    settings.

    Yields ``(client, db_module, models_module)``.
    """

    blob_dir = tmp_path / "blobs"
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("FERAL_REGISTRY_BLOB_DIR", str(blob_dir))
    monkeypatch.setenv("FERAL_REGISTRY_DB_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("FEATURED_PUBLISHERS", "feral")
    monkeypatch.setenv("FERAL_REGISTRY_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("FERAL_REGISTRY_REVIEWER_SECRET", REVIEWER_SECRET)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from feral_registry import config as config_mod
    config_mod.get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = config_mod.get_settings()

    from feral_registry import db as db_mod
    from feral_registry import main as main_mod
    from feral_registry import models as models_mod

    new_engine = create_async_engine(settings.db_url, echo=False, future=True)
    new_session_factory = async_sessionmaker(
        new_engine, expire_on_commit=False, class_=db_mod.AsyncSession
    )
    monkeypatch.setattr(db_mod, "engine", new_engine, raising=False)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session_factory, raising=False)

    app = main_mod.create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        async with new_engine.begin() as conn:
            await conn.run_sync(db_mod.Base.metadata.create_all)
        yield client, db_mod, models_mod
    await new_engine.dispose()
