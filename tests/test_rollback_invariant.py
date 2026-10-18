"""Migration 0003 is a CONTRACT migration, not an expand-only one.

Once it has run, a release that does no tenant binding (release N, before this one) sees zero
rows and cannot insert, because every policy compares agency_id with a setting that code never
sets. So 0003 can only ship together with the tenant-binding code, and rolling back past it needs
``alembic downgrade 0002`` before release N is redeployed. These tests prove the documented
procedure (docs/runbook.md section 5, docs/phase2-plan.md decision 4) on a real server.

"The old release" below is a plain session on the owner connection: before 0003 there was no
second role, so release N always connected as the owner.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import Agency, Ticket, Upload
from tests.conftest import RlsDatabase, alembic_config, app_role_env
from tests.rls_support import seed_tenant


@contextmanager
def old_release_session(owner_url: str) -> Iterator[Session]:
    """What release N does: open a session and query, never binding a tenant."""
    engine = make_engine(owner_url)
    try:
        with make_session_factory(engine)() as s:
            yield s
    finally:
        engine.dispose()


def visible(s: Session) -> dict[str, int]:
    return {
        model.__tablename__: s.scalar(select(func.count()).select_from(model)) or 0
        for model in (Agency, Upload, Ticket)
    }


def old_release_insert(s: Session, agency_id: int) -> None:
    s.add(
        Upload(
            agency_id=agency_id,
            filename="old-release.xlsx",
            sha256="b" * 64,
            report_range_raw="01OCT",
            report_date=date(2026, 10, 1),
            generated_on=date(2026, 10, 2),
            ticket_count=0,
            warnings=[],
        )
    )
    s.commit()


def test_an_unbound_old_release_is_locked_out_by_0003_and_works_again_after_downgrade_0002(
    rls_db: RlsDatabase,
) -> None:
    tenant = seed_tenant(rls_db.owner_url, "agency-a", "+22211111111", "a1" * 32, 3940000000)
    role = make_url(rls_db.app_url).username if rls_db.mode == "app-role" else None
    cfg = alembic_config(rls_db.owner_url)

    # With 0003 in place the old release sees nothing and cannot write.
    with old_release_session(rls_db.owner_url) as s:
        assert visible(s) == {"agencies": 0, "uploads": 0, "tickets": 0}
    with old_release_session(rls_db.owner_url) as s:
        with pytest.raises(DBAPIError, match="row-level security"):
            old_release_insert(s, tenant.agency_id)
        s.rollback()

    # The documented rollback: downgrade to 0002 first, then redeploy release N.
    with app_role_env(role):
        command.downgrade(cfg, "0002")
    with old_release_session(rls_db.owner_url) as s:
        seen = visible(s)
        assert seen["agencies"] == 1 and seen["uploads"] == 1 and seen["tickets"] == 2
    with old_release_session(rls_db.owner_url) as s:
        old_release_insert(s, tenant.agency_id)  # and it can write again
        assert visible(s)["uploads"] == 2

    # Rolling forward again restores the isolation (and keeps the row the old release wrote).
    with app_role_env(role):
        command.upgrade(cfg, "head")
    with old_release_session(rls_db.owner_url) as s:
        assert visible(s) == {"agencies": 0, "uploads": 0, "tickets": 0}
