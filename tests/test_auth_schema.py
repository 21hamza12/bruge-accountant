"""Migration 0002 (the expand step) and the auth/tenancy models.

Constraints are exercised on a database built by Alembic (the real schema), on both engines.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import (
    Agency,
    AuditEvent,
    RateLimitBucket,
    StaffSignLink,
    TemporaryPassword,
    Upload,
    User,
    UserSession,
)
from app.parsing.amadeus import ParsedReport
from app.repository import create_agency, import_report
from tests.conftest import SHA

NEW_TABLES = {
    "users",
    "staff_sign_links",
    "user_sessions",
    "temporary_passwords",
    "audit_events",
    "rate_limit_buckets",
}
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
FAKE_HASH = "not-a-real-hash"


@pytest.fixture
def migrated(alembic_cfg: tuple[Config, str]) -> Iterator[Session]:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    disable_rls(url)
    engine = make_engine(url)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def disable_rls(url: str) -> None:
    """These tests are about constraints, not tenancy: switch row-level security off.

    From migration 0003 it also applies to the table owner on Postgres (that is tested in
    tests/test_rls.py), and the constraint tests would otherwise have to bind a tenant first.
    """
    engine = create_engine(url)
    try:
        if engine.dialect.name == "postgresql":
            with engine.begin() as conn:
                tables = conn.execute(
                    text(
                        "SELECT relname FROM pg_class WHERE relrowsecurity "
                        "AND relnamespace = 'public'::regnamespace"
                    )
                ).scalars()
                for name in list(tables):
                    conn.execute(text(f"ALTER TABLE {name} DISABLE ROW LEVEL SECURITY"))  # noqa: S608
    finally:
        engine.dispose()


def make_agencies(s: Session) -> tuple[int, int]:
    a = Agency(slug="a", name="A")
    b = Agency(slug="b", name="B")
    s.add_all([a, b])
    s.commit()
    return a.id, b.id


def make_user(s: Session, agency_id: int, phone: str = "+22212345678", **kw: Any) -> User:
    user = User(agency_id=agency_id, phone=phone, role=kw.pop("role", "staff"), **kw)
    s.add(user)
    s.commit()
    return user


def rejects(s: Session, *rows: Any) -> None:
    s.add_all(rows)
    # Postgres reports an over-long value as DataError (the column type), SQLite via the CHECK.
    with pytest.raises((IntegrityError, DataError)):
        s.commit()
    s.rollback()


# --- users ---------------------------------------------------------------------------


def test_user_defaults(migrated: Session) -> None:
    a, _ = make_agencies(migrated)
    user = make_user(migrated, a)
    migrated.refresh(user)
    assert user.password_hash is None
    assert user.must_change_password is True
    assert user.is_active is True
    assert user.created_at.tzinfo is not None
    assert user.updated_at.tzinfo is not None


def test_phone_is_unique_across_agencies(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    make_user(migrated, a, "+22212345678")
    rejects(migrated, User(agency_id=b, phone="+22212345678", role="staff"))
    make_user(migrated, b, "+22287654321")


@pytest.mark.parametrize("role", ["admin", "", "Manager", "owner"])
def test_role_must_be_manager_or_staff(migrated: Session, role: str) -> None:
    a, _ = make_agencies(migrated)
    rejects(migrated, User(agency_id=a, phone="+22212345678", role=role))


@pytest.mark.parametrize("role", ["manager", "staff"])
def test_valid_roles(migrated: Session, role: str) -> None:
    a, _ = make_agencies(migrated)
    assert make_user(migrated, a, role=role).role == role


@pytest.mark.parametrize("phone", ["22212345678", "+2221234", "+" + "1" * 16, "", "12345678"])
def test_phone_must_look_like_e164(migrated: Session, phone: str) -> None:
    a, _ = make_agencies(migrated)
    rejects(migrated, User(agency_id=a, phone=phone, role="staff"))


def test_user_agency_must_exist(migrated: Session) -> None:
    rejects(migrated, User(agency_id=999, phone="+22212345678", role="staff"))


def test_user_unique_constraints(migrated: Session) -> None:
    uniques = inspect(migrated.get_bind()).get_unique_constraints("users")
    assert {tuple(u["column_names"]) for u in uniques} >= {("agency_id", "id"), ("phone",)}


# --- staff sign links ----------------------------------------------------------------


def test_sign_is_unique_per_agency_but_reusable_in_another(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    ua = make_user(migrated, a, "+22211111111")
    ua2 = make_user(migrated, a, "+22222222222")
    ub = make_user(migrated, b, "+22233333333")
    migrated.add(StaffSignLink(agency_id=a, user_id=ua.id, staff_sign="SV"))
    migrated.commit()
    rejects(migrated, StaffSignLink(agency_id=a, user_id=ua2.id, staff_sign="SV"))
    migrated.add(StaffSignLink(agency_id=b, user_id=ub.id, staff_sign="SV"))
    migrated.commit()


def test_sign_cannot_link_to_another_agencys_user(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    user_b = make_user(migrated, b)
    rejects(migrated, StaffSignLink(agency_id=a, user_id=user_b.id, staff_sign="SV"))


@pytest.mark.parametrize("sign", ["", "X", "S" * 33])
def test_sign_length_is_bounded(migrated: Session, sign: str) -> None:
    a, _ = make_agencies(migrated)
    user = make_user(migrated, a)
    rejects(migrated, StaffSignLink(agency_id=a, user_id=user.id, staff_sign=sign))


# --- sessions ------------------------------------------------------------------------


def session_row(agency_id: int, user_id: int, token_hash: str = "a" * 64) -> UserSession:
    return UserSession(
        agency_id=agency_id,
        user_id=user_id,
        token_hash=token_hash,
        csrf_token="c" * 43,
        last_seen_at=NOW,
        idle_expires_at=NOW + timedelta(hours=12),
        absolute_expires_at=NOW + timedelta(days=7),
    )


def test_session_token_hash_is_unique(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    ua, ub = make_user(migrated, a, "+22211111111"), make_user(migrated, b, "+22222222222")
    migrated.add(session_row(a, ua.id))
    migrated.commit()
    rejects(migrated, session_row(b, ub.id))


def test_session_token_hash_must_be_a_sha256_hex_length(migrated: Session) -> None:
    a, _ = make_agencies(migrated)
    rejects(migrated, session_row(a, make_user(migrated, a).id, token_hash="a" * 63))


def test_session_cannot_belong_to_another_agencys_user(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    rejects(migrated, session_row(a, make_user(migrated, b).id))


def test_session_defaults(migrated: Session) -> None:
    a, _ = make_agencies(migrated)
    row = session_row(a, make_user(migrated, a).id)
    migrated.add(row)
    migrated.commit()
    migrated.refresh(row)
    assert row.revoked_at is None
    assert row.created_at.tzinfo is not None


# --- temporary passwords -------------------------------------------------------------


def test_temporary_password_cannot_target_another_agencys_user(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    user_b = make_user(migrated, b)
    rejects(
        migrated,
        TemporaryPassword(
            agency_id=a,
            user_id=user_b.id,
            password_hash=FAKE_HASH,
            expires_at=NOW + timedelta(minutes=30),
        ),
    )


def test_temporary_password_roundtrip(migrated: Session) -> None:
    a, _ = make_agencies(migrated)
    user = make_user(migrated, a)
    row = TemporaryPassword(
        agency_id=a,
        user_id=user.id,
        password_hash=FAKE_HASH,
        expires_at=NOW + timedelta(minutes=30),
    )
    migrated.add(row)
    migrated.commit()
    migrated.refresh(row)
    assert row.used_at is None
    assert row.expires_at == NOW + timedelta(minutes=30)


# --- audit events --------------------------------------------------------------------


def test_audit_event_may_have_no_agency_or_actor(migrated: Session) -> None:
    migrated.add(AuditEvent(event_type="login_failed", details={"reason": "unknown_phone"}))
    migrated.commit()
    row = migrated.scalars(select(AuditEvent)).one()
    assert row.agency_id is None
    assert row.actor_user_id is None
    assert row.details == {"reason": "unknown_phone"}


def test_audit_actor_must_belong_to_the_event_agency(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    user_b = make_user(migrated, b)
    rejects(migrated, AuditEvent(agency_id=a, actor_user_id=user_b.id, event_type="x", details={}))


# --- rate limit buckets --------------------------------------------------------------


def test_bucket_primary_key_is_key_and_window(migrated: Session) -> None:
    migrated.add(RateLimitBucket(key="login:phone:x", window_start=NOW, count=1))
    migrated.commit()
    rejects(migrated, RateLimitBucket(key="login:phone:x", window_start=NOW, count=1))
    migrated.add(
        RateLimitBucket(key="login:phone:x", window_start=NOW + timedelta(minutes=15), count=1)
    )
    migrated.add(RateLimitBucket(key="login:phone:y", window_start=NOW, count=1))
    migrated.commit()


def test_bucket_count_cannot_be_negative(migrated: Session) -> None:
    rejects(migrated, RateLimitBucket(key="k", window_start=NOW, count=-1))


def test_bucket_has_window_index_and_no_agency_column(migrated: Session) -> None:
    insp = inspect(migrated.get_bind())
    assert ["window_start"] in [i["column_names"] for i in insp.get_indexes("rate_limit_buckets")]
    assert "agency_id" not in {c["name"] for c in insp.get_columns("rate_limit_buckets")}
    assert insp.get_pk_constraint("rate_limit_buckets")["constrained_columns"] == [
        "key",
        "window_start",
    ]


# --- uploads gain counts and the uploader --------------------------------------------


def test_upload_new_columns_are_nullable(migrated: Session) -> None:
    cols = {c["name"]: c for c in inspect(migrated.get_bind()).get_columns("uploads")}
    for name in ("inserted_count", "updated_count", "uploaded_by_user_id"):
        assert cols[name]["nullable"] is True


def test_import_report_records_counts_and_leaves_the_uploader_unset(
    migrated: Session, parsed_fixture: ParsedReport
) -> None:
    agency = create_agency(migrated, "x", "X")
    result = import_report(migrated, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    upload = migrated.get(Upload, result.upload_id)
    assert upload is not None
    assert (upload.inserted_count, upload.updated_count) == (48, 0)
    assert upload.uploaded_by_user_id is None


def upload_kwargs(agency_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "agency_id": agency_id,
        "filename": "f.xlsx",
        "sha256": SHA,
        "report_range_raw": "01OCT",
        "report_date": NOW.date(),
        "generated_on": NOW.date(),
        "ticket_count": 0,
        "warnings": [],
        **extra,
    }


def test_upload_uploader_must_be_in_the_same_agency(migrated: Session) -> None:
    a, b = make_agencies(migrated)
    own, other = make_user(migrated, a, "+22211111111"), make_user(migrated, b, "+22222222222")
    migrated.add(Upload(**upload_kwargs(a, uploaded_by_user_id=own.id)))
    migrated.commit()
    rejects(migrated, Upload(**upload_kwargs(a, uploaded_by_user_id=other.id)))


@pytest.mark.parametrize("column", ["inserted_count", "updated_count"])
def test_upload_counts_cannot_be_negative(migrated: Session, column: str) -> None:
    a, _ = make_agencies(migrated)
    rejects(migrated, Upload(**upload_kwargs(a, **{column: -1})))
    migrated.add(Upload(**upload_kwargs(a, **{column: 0})))
    migrated.commit()


# --- schema hygiene ------------------------------------------------------------------


def test_new_constraints_are_named_and_restrict_deletes(migrated: Session) -> None:
    insp = inspect(migrated.get_bind())
    for table in sorted(NEW_TABLES):
        assert insp.get_pk_constraint(table)["name"] == f"pk_{table}"
        for fk in insp.get_foreign_keys(table):
            assert fk["name"]
            assert fk["options"].get("ondelete") == "RESTRICT"
        for uq in insp.get_unique_constraints(table):
            assert uq["name"]
        for ck in insp.get_check_constraints(table):
            assert ck["name"]


def test_deleting_a_user_with_children_is_restricted(migrated: Session) -> None:
    a, _ = make_agencies(migrated)
    user = make_user(migrated, a)
    migrated.add(StaffSignLink(agency_id=a, user_id=user.id, staff_sign="SV"))
    migrated.commit()
    migrated.delete(user)
    with pytest.raises(IntegrityError):
        migrated.commit()
    migrated.rollback()


def test_foreign_key_columns_are_indexed(migrated: Session) -> None:
    insp = inspect(migrated.get_bind())

    def leading(table: str) -> list[Any]:
        return [i["column_names"] for i in insp.get_indexes(table)] + [
            u["column_names"] for u in insp.get_unique_constraints(table)
        ]

    assert ["agency_id", "user_id"] in leading("user_sessions")
    assert ["agency_id", "user_id"] in leading("temporary_passwords")
    assert ["agency_id", "uploaded_by_user_id"] in leading("uploads")
    assert ["agency_id", "created_at"] in leading("audit_events")
    assert ["token_hash"] in leading("user_sessions")


# --- the migration itself ------------------------------------------------------------


def table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    assert table_names(url) >= NEW_TABLES
    command.downgrade(cfg, "0001")
    assert table_names(url).isdisjoint(NEW_TABLES)
    engine = create_engine(url)
    cols = {c["name"] for c in inspect(engine).get_columns("uploads")}
    engine.dispose()
    assert not cols & {"inserted_count", "updated_count", "uploaded_by_user_id"}
    command.upgrade(cfg, "head")
    assert table_names(url) >= NEW_TABLES


def test_upgrade_keeps_existing_rows(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "0001")
    engine = make_engine(url)
    with make_session_factory(engine)() as s:
        # Raw SQL against the 0001 schema: the models already know the new columns.
        s.execute(text("INSERT INTO agencies (slug, name, currency) VALUES ('old', 'Old', 'MRU')"))
        s.execute(
            text(
                "INSERT INTO uploads (agency_id, filename, sha256, report_range_raw, "
                "report_date, generated_on, ticket_count, warnings) VALUES "
                "(1, 'f.xlsx', :sha, '01OCT', '2026-10-01', '2026-10-02', 0, '[]')"
            ),
            {"sha": SHA},
        )
        s.commit()
    engine.dispose()
    command.upgrade(cfg, "head")
    disable_rls(url)
    engine = make_engine(url)
    with make_session_factory(engine)() as s:
        upload = s.scalars(select(Upload)).one()
        assert upload.inserted_count is None
        assert upload.uploaded_by_user_id is None
    engine.dispose()
