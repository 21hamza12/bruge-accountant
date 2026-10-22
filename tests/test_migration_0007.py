"""Migration 0007: partners and partner_fees (SQLite always; Postgres when a server is given)."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import UniqueConstraint, create_engine, delete, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import make_engine, make_session_factory
from app.models import Partner, PartnerFee, User
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import TEST_APP_DATABASE_URL_ENV, alembic_config, app_role_env, migrate


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _seed_agency(url: str) -> tuple[int, int]:
    """(agency id, manager id) written through the application models."""
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            agency_id = create_agency(s, "agence-m", "Agence M").id
            rebind_tenant(s, agency_id)
            user = User(agency_id=agency_id, phone="+22212345700", role="manager")
            s.add(user)
            s.commit()
            return agency_id, user.id
    finally:
        engine.dispose()


def _add_partner(url: str, agency_id: int, name_key: str, phone: str | None = None) -> int:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency_id)
            p = Partner(agency_id=agency_id, name=name_key, name_key=name_key, phone=phone)
            s.add(p)
            s.commit()
            return p.id
    finally:
        engine.dispose()


def _add_fee(url: str, agency_id: int, partner_id: int, user_id: int, fee: str = "500") -> int:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency_id)
            f = PartnerFee(
                agency_id=agency_id,
                partner_id=partner_id,
                fee=Decimal(fee),
                valid_from=date(2026, 1, 1),
                set_by_user_id=user_id,
            )
            s.add(f)
            s.commit()
            return f.id
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _scalar(url: str, sql: str) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_the_revision_chain_is_pinned_until_the_rebase() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0007")
    assert revision is not None
    # TODO rebase onto 0006 (B2): this becomes "0006" and the test is updated with it.
    assert revision.down_revision == "0005"


@pytest.mark.xfail(reason="B3 rebases onto 0006 before merge", strict=False)
def test_the_script_directory_has_a_single_head() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_upgrade_creates_both_tables_and_the_lookup_index(db_url: str) -> None:
    migrate(db_url, revision="0005")
    assert not {"partners", "partner_fees"} & _tables(db_url)
    command.upgrade(alembic_config(db_url), "0007")
    assert {"partners", "partner_fees"} <= _tables(db_url)
    engine = create_engine(db_url)
    try:
        indexes = {i["name"] for i in inspect(engine).get_indexes("partner_fees")}
    finally:
        engine.dispose()
    assert "ix_partner_fees_lookup" in indexes
    sql = str(
        _scalar(
            db_url,
            "SELECT sql FROM sqlite_master WHERE name = 'ix_partner_fees_lookup'"
            if not _is_postgres(db_url)
            else "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_partner_fees_lookup'",
        )
    )
    assert sql.count("DESC") == 3  # valid_from, created_at, id


def test_the_constraints_hold(db_url: str) -> None:
    migrate(db_url)
    agency, user = _seed_agency(db_url)
    partner = _add_partner(db_url, agency, "badi", "+22212345678")
    with pytest.raises(IntegrityError):
        _add_partner(db_url, agency, "badi")  # unique per agency
    with pytest.raises(IntegrityError):
        _add_partner(db_url, agency, "bruge")  # a direct sale is not a partner
    with pytest.raises(IntegrityError):
        _add_partner(db_url, agency, "")
    with pytest.raises(IntegrityError):
        _add_partner(db_url, agency, "sahel", "12345678")  # not E.164
    other = _seed_second_agency(db_url)
    _add_partner(db_url, other, "badi")  # the same name in another agency is fine
    with pytest.raises(IntegrityError):
        _add_fee(db_url, agency, partner, user, "-1")
    assert _add_fee(db_url, agency, partner, user, "0") > 0


def _seed_second_agency(url: str) -> int:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            return create_agency(s, "agence-n", "Agence N").id
    finally:
        engine.dispose()


def test_composite_foreign_keys_keep_a_fee_inside_its_agency(db_url: str) -> None:
    migrate(db_url)
    agency, user = _seed_agency(db_url)
    other = _seed_second_agency(db_url)
    partner = _add_partner(db_url, agency, "badi")
    engine = make_engine(db_url)  # the application engine turns foreign keys on
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, other)
            s.add(
                PartnerFee(
                    agency_id=other,
                    partner_id=partner,  # a partner of another agency
                    fee=Decimal("1"),
                    valid_from=date(2026, 1, 1),
                    set_by_user_id=user,
                )
            )
            with pytest.raises(IntegrityError):
                s.commit()
    finally:
        engine.dispose()


def test_a_partner_with_fees_cannot_be_deleted(db_url: str) -> None:
    migrate(db_url)
    agency, user = _seed_agency(db_url)
    partner = _add_partner(db_url, agency, "badi")
    _add_fee(db_url, agency, partner, user)
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            with pytest.raises((IntegrityError, DBAPIError)):
                s.execute(delete(Partner).where(Partner.id == partner))
                s.commit()
    finally:
        engine.dispose()


def test_partner_fees_are_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies (tests/test_rls.py)")
    migrate(db_url)
    agency, user = _seed_agency(db_url)
    partner = _add_partner(db_url, agency, "badi")
    fee = _add_fee(db_url, agency, partner, user)
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(update(PartnerFee).where(PartnerFee.id == fee).values(fee=Decimal("1")))
            s.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(delete(PartnerFee).where(PartnerFee.id == fee))
            s.rollback()
        # ...and partners stay editable.
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.execute(update(Partner).where(Partner.id == partner).values(is_active=False))
            s.commit()
    finally:
        engine.dispose()
    assert _scalar(db_url, "SELECT count(*) FROM partner_fees") == 1
    assert _scalar(db_url, "SELECT fee FROM partner_fees") == 500


def test_downgrade_drops_tables_and_triggers_and_upgrade_works_again(db_url: str) -> None:
    migrate(db_url)
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0005")
    assert not {"partners", "partner_fees"} & _tables(db_url)
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0005"
    if not _is_postgres(db_url):
        assert (
            _scalar(
                db_url,
                "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'trg_partner_fees%'",
            )
            == 0
        )
    command.upgrade(cfg, "head")
    assert {"partners", "partner_fees"} <= _tables(db_url)


def test_the_migration_matches_the_models(db_url: str) -> None:
    """Columns, nullability, unique constraints and foreign keys of the migrated tables equal
    the models' (the migration is written by hand, so this keeps the two in step)."""
    from app.db import Base

    migrate(db_url)
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        for name in ("partners", "partner_fees"):
            table = Base.metadata.tables[name]
            migrated = {c["name"]: c["nullable"] for c in insp.get_columns(name)}
            assert migrated == {c.name: c.nullable for c in table.columns}, name
            uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints(name)}
            assert uniques == {
                tuple(c.name for c in u.columns)
                for u in table.constraints
                if isinstance(u, UniqueConstraint)
            }, name
            fks = {
                (tuple(f["constrained_columns"]), f["referred_table"])
                for f in insp.get_foreign_keys(name)
            }
            assert fks == {
                (tuple(c.parent.name for c in f.elements), f.referred_table.name)
                for f in table.foreign_key_constraints
            }, name
    finally:
        engine.dispose()


# --- Postgres only ------------------------------------------------------------------------------


@pytest.fixture
def pg_url(db_url: str) -> str:
    if not _is_postgres(db_url):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    return db_url


def _privileges(url: str, role: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                p
                for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": table, "p": p}
                ).scalar()
            }
    finally:
        engine.dispose()


def _app_role() -> str:
    url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    if not url:
        pytest.skip("needs TEST_APP_DATABASE_URL")
    return make_url(url).username or ""


def test_postgres_forces_rls_with_the_expected_policies(pg_url: str) -> None:
    migrate(pg_url)
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            flags = {
                n: (rls, forced)
                for n, rls, forced in conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('partners', 'partner_fees')"
                    )
                )
            }
            policies = {
                (t, n, cmd)
                for t, n, cmd in conn.execute(
                    text(
                        "SELECT tablename, policyname, cmd FROM pg_policies "
                        "WHERE tablename IN ('partners', 'partner_fees')"
                    )
                )
            }
    finally:
        engine.dispose()
    assert flags == {"partners": (True, True), "partner_fees": (True, True)}
    assert policies == {
        ("partners", "tenant_isolation", "ALL"),
        ("partner_fees", "tenant_read", "SELECT"),
        ("partner_fees", "tenant_insert", "INSERT"),
    }


def test_postgres_grants_follow_the_map_and_survive_a_downgrade_of_only_these_tables(
    pg_url: str,
) -> None:
    role = _app_role()
    migrate(pg_url, app_role=role)
    assert _privileges(pg_url, role, "partners") == {"SELECT", "INSERT", "UPDATE"}
    assert _privileges(pg_url, role, "partner_fees") == {"SELECT", "INSERT"}
    with app_role_env(role):
        command.downgrade(alembic_config(pg_url), "0005")
    assert _privileges(pg_url, role, "tickets") == {"SELECT", "INSERT", "UPDATE"}
    assert _privileges(pg_url, role, "users") == {"SELECT", "INSERT", "UPDATE"}  # untouched
    assert not {"partners", "partner_fees"} & _tables(pg_url)


def test_postgres_single_role_mode_grants_nothing_and_does_not_fail(pg_url: str) -> None:
    role = _app_role()
    migrate(pg_url, app_role=None)
    assert _privileges(pg_url, role, "partners") == set()
    assert _privileges(pg_url, role, "partner_fees") == set()
