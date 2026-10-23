"""Migration 0014: ai_usage, agency_ai_settings and the 'screenshot' sale source (SQLite; on a
Postgres server the policies are covered by tests/test_rls.py and the shape checks that need
SQLite are skipped)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import Base, make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import AgencyAiSettings, AiUsage, SaleVersion
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate
from tests.rls_support import Tenant, seed_tenant

TABLES = ("ai_usage", "agency_ai_settings")
COUNT_VERSIONS = "SELECT count(*) FROM sale_versions WHERE agency_id = :agency"


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _names(url: str, kind: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = :t"), {"t": kind})
            return {str(n) for n in rows.scalars()}
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _world(url: str, *, ai: bool = True) -> tuple[Tenant, Tenant]:
    a = seed_tenant(url, "agence-a", "+22212390101", "a1" * 32, 3940000000, ai=ai, chat=False)
    b = seed_tenant(url, "agence-b", "+22212390102", "b2" * 32, 3950000000, ai=ai, chat=False)
    return a, b


def _scalar(url: str, sql: str, agency: int | None = None) -> object:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            if agency is not None:
                rebind_tenant(s, agency)
            params = {} if agency is None else {"agency": agency}
            return s.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def usage(t: Tenant, **kw: Any) -> AiUsage:
    fields: dict[str, Any] = {
        "agency_id": t.agency_id,
        "user_id": t.user_id,
        "purpose": "screenshot",
        "model": "claude-haiku-5-5",
        "input_tokens": 650,
        "output_tokens": 400,
        "cost_micro_usd": 795,
        "outcome": "ok",
    }
    fields.update(kw)
    return AiUsage(**fields)


def _add(url: str, agency: int, *rows: Any) -> None:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.add_all(rows)
            s.commit()
    finally:
        engine.dispose()


# --- chain and shape ---------------------------------------------------------------------------


def test_0014_follows_0013_and_the_chain_has_a_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0014")
    assert revision is not None and revision.down_revision == "0013"
    assert len(script.get_heads()) == 1  # a newer migration is the head: see its own test


def test_grants_follow_the_design() -> None:
    assert GRANTS["ai_usage"] == "SELECT, INSERT"
    assert GRANTS["agency_ai_settings"] == "SELECT"  # written by the owner (ai-budget) only


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0013")
    assert not set(TABLES) & _tables(db_url)
    command.upgrade(cfg, "0014")
    assert set(TABLES) <= _tables(db_url)
    triggers = _names(db_url, "trigger")
    assert {"trg_ai_usage_no_update", "trg_ai_usage_no_delete"} <= triggers
    assert {"trg_sale_versions_no_update", "trg_sale_versions_no_delete"} <= triggers
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("ai_usage")}
        assert indexes["ix_ai_usage_agency_created"] == ["agency_id", "created_at"]
        assert indexes["ix_ai_usage_agency_user"] == ["agency_id", "user_id"]
        assert insp.get_pk_constraint("agency_ai_settings")["constrained_columns"] == ["agency_id"]
    finally:
        engine.dispose()
    command.downgrade(cfg, "0013")
    assert not set(TABLES) & _tables(db_url)
    assert not {"trg_ai_usage_no_update", "trg_ai_usage_no_delete"} & _names(db_url, "trigger")
    assert {"trg_sale_versions_no_update", "trg_sale_versions_no_delete"} <= _names(
        db_url, "trigger"
    )
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0013"
    command.upgrade(cfg, "0014")
    assert set(TABLES) <= _tables(db_url)


def _shape(url: str) -> dict[str, dict[str, Any]]:
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        out: dict[str, dict[str, Any]] = {}
        for name in (*TABLES, "sale_versions"):
            out[name] = {
                "columns": {
                    c["name"]: (c["nullable"], str(c["type"]).upper(), str(c["default"]))
                    for c in insp.get_columns(name)
                },
                "indexes": {
                    i["name"]: (i["column_names"], i["unique"]) for i in insp.get_indexes(name)
                },
                "foreign_keys": sorted(
                    (
                        fk["name"],
                        fk["constrained_columns"],
                        fk["referred_table"],
                        fk["referred_columns"],
                        fk["options"].get("ondelete"),
                    )
                    for fk in insp.get_foreign_keys(name)
                ),
                "checks": sorted(
                    (c["name"], " ".join(str(c["sqltext"]).split()))
                    for c in insp.get_check_constraints(name)
                ),
            }
        return out
    finally:
        engine.dispose()


def test_the_migrated_tables_match_the_models(db_url: str, tmp_path: Any) -> None:
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url, revision="0014")
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["ai_usage"]["checks"]) == 3, "the comparison must see every CHECK"
    assert len(migrated["ai_usage"]["foreign_keys"]) == 2
    for table in (*TABLES, "sale_versions"):
        for part in model[table]:
            assert migrated[table][part] == model[table][part], (table, part)


# --- constraints and append-only ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"purpose": "chat"},
        {"outcome": "weird"},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"cost_micro_usd": -1},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_usage_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(db_url, a.agency_id, usage(a, **overrides))


def test_the_valid_usage_rows_are_accepted(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    outcomes = ("ok", "fallback", "error", "refused_budget")
    _add(db_url, a.agency_id, *(usage(a, outcome=o, cost_micro_usd=1) for o in outcomes))
    assert (
        _scalar(db_url, "SELECT count(*) FROM ai_usage WHERE agency_id = :agency", a.agency_id) == 5
    )  # + the seeded one


def test_a_large_cost_fits_a_bigint(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    _add(db_url, a.agency_id, usage(a, cost_micro_usd=3_000_000_000))
    total = _scalar(
        db_url, "SELECT sum(cost_micro_usd) FROM ai_usage WHERE agency_id = :agency", a.agency_id
    )
    assert total == 3_000_000_795


def test_usage_cannot_point_at_another_agencys_user(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, b = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(db_url, a.agency_id, usage(a, user_id=b.user_id))


def test_usage_is_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies and grants")
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    engine = make_engine(db_url)
    try:
        for statement in (update(AiUsage).values(cost_micro_usd=0), delete(AiUsage)):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, a.agency_id)
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(statement)
                s.rollback()
    finally:
        engine.dispose()
    assert (
        _scalar(
            db_url, "SELECT cost_micro_usd FROM ai_usage WHERE agency_id = :agency", a.agency_id
        )
        == 795
    )


def test_the_settings_are_mutable_one_row_per_agency(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)  # the seed gives each agency its row
    with pytest.raises(IntegrityError):  # a second row for the same agency
        _add(
            db_url,
            a.agency_id,
            AgencyAiSettings(agency_id=a.agency_id, enabled=True, monthly_budget_micro_usd=1),
        )
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, a.agency_id)
            s.execute(update(AgencyAiSettings).values(enabled=False, monthly_budget_micro_usd=42))
            s.commit()
    finally:
        engine.dispose()
    assert (
        _scalar(
            db_url,
            "SELECT monthly_budget_micro_usd FROM agency_ai_settings WHERE agency_id = :agency",
            a.agency_id,
        )
        == 42
    )


def test_a_negative_budget_is_refused(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, a.agency_id)
            with pytest.raises(IntegrityError):
                s.execute(update(AgencyAiSettings).values(monthly_budget_micro_usd=-1))
                s.commit()
    finally:
        engine.dispose()


# --- the sale source ---------------------------------------------------------------------------


def _version(t: Tenant, no: int, source: str) -> SaleVersion:
    return SaleVersion(
        agency_id=t.agency_id,
        sale_id=t.sale_id,
        version_no=no,
        report_day_id=t.report_day_id,
        staff_sign="0004SV",
        ag_kind="partner",
        partner_id=t.partner_id,
        ag_label="BADI",
        fee_applied=Decimal("500"),
        pb=Decimal("1500"),
        cost_total=Decimal("1000"),
        payment_method="credit",
        tickets=[],
        source=source,
        actor_user_id=t.user_id,
    )


def test_the_screenshot_source_is_accepted_and_others_still_are_not(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    _add(db_url, a.agency_id, _version(a, 2, "screenshot"), _version(a, 3, "paste"))
    with pytest.raises(IntegrityError):
        _add(db_url, a.agency_id, _version(a, 4, "telepathy"))
    count = _scalar(db_url, COUNT_VERSIONS, a.agency_id)
    assert count == 3


def test_the_old_source_is_refused_before_the_migration(db_url: str) -> None:
    migrate(db_url, revision="0013")
    a, _ = _world(db_url, ai=False)
    with pytest.raises(IntegrityError):
        _add(db_url, a.agency_id, _version(a, 2, "screenshot"))


def test_the_rebuild_keeps_the_versions_and_their_append_only_triggers(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("the SQLite rebuild")
    migrate(db_url, revision="0013")
    a, _ = _world(db_url, ai=False)
    before = _scalar(db_url, COUNT_VERSIONS, a.agency_id)
    command.upgrade(alembic_config(db_url), "0014")
    assert _scalar(db_url, COUNT_VERSIONS, a.agency_id) == before == 1
    engine = make_engine(db_url)
    try:
        for statement in (update(SaleVersion).values(pb=Decimal(1)), delete(SaleVersion)):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, a.agency_id)
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(statement)
                s.rollback()
    finally:
        engine.dispose()


def test_the_downgrade_is_refused_while_a_screenshot_sale_exists(db_url: str) -> None:
    migrate(db_url, revision="0014")
    a, _ = _world(db_url)
    _add(db_url, a.agency_id, _version(a, 2, "screenshot"))
    with pytest.raises((RuntimeError, IntegrityError, DBAPIError)):
        command.downgrade(alembic_config(db_url), "0013")
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0014"
    assert set(TABLES) <= _tables(db_url)  # nothing was half done
    if not _is_postgres(db_url):
        assert {"trg_sale_versions_no_update", "trg_ai_usage_no_delete"} <= _names(
            db_url, "trigger"
        )
