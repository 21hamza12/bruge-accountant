"""Migration 0004 (expand-only): a non-empty agency slug and fillfactor 70 on the rate limiter."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.db import Base, make_engine
from tests.conftest import alembic_config

ROOT = Path(__file__).resolve().parent.parent


def _insert_agency(url: str, slug: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO agencies (slug, name, currency) VALUES (:s, 'Agence', 'MRU')"),
                {"s": slug},
            )
    finally:
        engine.dispose()


def _slugs(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT slug FROM agencies ORDER BY id"))]
    finally:
        engine.dispose()


def test_an_empty_slug_is_refused_after_the_migration(db_url: str) -> None:
    command.upgrade(alembic_config(db_url), "head")
    _insert_agency(db_url, "agence-a")
    with pytest.raises(IntegrityError):
        _insert_agency(db_url, "")
    assert _slugs(db_url) == ["agence-a"]


def test_upgrading_from_0003_keeps_the_data_and_adds_the_rule(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0003")
    _insert_agency(db_url, "existing")
    command.upgrade(cfg, "0004")
    assert _slugs(db_url) == ["existing"]
    with pytest.raises(IntegrityError):
        _insert_agency(db_url, "")


def test_downgrading_to_0003_removes_the_rule_again(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0003")
    _insert_agency(db_url, "")  # allowed again, as under the previous release
    assert _slugs(db_url) == [""]


def test_the_model_carries_the_same_rule_for_databases_built_from_it(db_url: str) -> None:
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with pytest.raises(IntegrityError):
        _insert_agency(db_url, "")


def test_the_previous_release_still_works_against_0004(db_url: str) -> None:
    """Expand-only: the only change a 0003 release can notice is a refused empty slug, which
    it never wrote (the CLI validates slugs against ^[a-z0-9][a-z0-9-]{0,62}$)."""
    command.upgrade(alembic_config(db_url), "head")
    _insert_agency(db_url, "a")
    _insert_agency(db_url, "b-2")
    assert _slugs(db_url) == ["a", "b-2"]


def test_the_migration_is_labelled_expand_only() -> None:
    text_ = (ROOT / "migrations" / "versions" / "0004_slug_check_and_fillfactor.py").read_text(
        encoding="utf-8"
    )
    assert "expand-only" in text_.lower()
    assert "CONTRACT MIGRATION" not in text_


def _reloptions(url: str) -> list[str] | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            value = conn.execute(
                text("SELECT reloptions FROM pg_class WHERE relname = 'rate_limit_buckets'")
            ).scalar()
    finally:
        engine.dispose()
    return list(value) if value else None


def test_rate_limit_buckets_get_fillfactor_70_on_postgres(db_url: str) -> None:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("fillfactor is a Postgres storage parameter")
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0003")
    assert _reloptions(db_url) is None
    command.upgrade(cfg, "head")
    assert _reloptions(db_url) == ["fillfactor=70"]
    command.downgrade(cfg, "0003")
    assert _reloptions(db_url) is None
