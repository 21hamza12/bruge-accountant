"""The cheap gates of POST /uploads run before the body is read, and the quota is fair.

Order (docs/phase2-plan.md, PR 4): login, Origin, CSRF header, rate limits, and only then the
multipart body. Every test here goes through the REAL route; a spy on starlette's
``MultiPartParser`` proves when the body is (not) parsed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import starlette.requests
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from starlette.formparsers import MultiPartParser

import app.web.routes.uploads as upload_routes
from app import audit
from app.auth import ratelimit
from app.models import RateLimitBucket, Ticket
from app.parsing.amadeus import ParsedReport, ParseError, parse_report
from app.parsing.sandbox import SandboxBusy
from tests.test_uploads import XLSX, Env, fixture_bytes
from tests.web_support import (
    PASSWORD_S,
    PHONE_STAFF_A,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    login,
    owner_session,
)

PHONE_STAFF_2 = "+22212345611"
PHONE_STAFF_3 = "+22212345612"


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


class Spy(MultiPartParser):
    constructed = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).constructed += 1
        super().__init__(*args, **kwargs)


@pytest.fixture
def parser_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[Spy]]:
    Spy.constructed = 0
    monkeypatch.setattr(starlette.requests, "MultiPartParser", Spy)
    yield Spy


def override_parser(env: Env, parser: Any) -> None:
    env.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: parser  # type: ignore[attr-defined]


def limits(monkeypatch: pytest.MonkeyPatch, *, user: int = 1000, agency: int = 1000) -> None:
    monkeypatch.setattr(ratelimit, "UPLOAD_USER", ratelimit.Limit("upload_user", user, 3600))
    monkeypatch.setattr(ratelimit, "UPLOAD_AGENCY", ratelimit.Limit("upload_agency", agency, 3600))


def post_with_header(c: TestClient, token: str, **kwargs: Any) -> Any:
    return c.post(
        "/uploads",
        files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
        headers={"X-CSRF-Token": token, **kwargs.pop("headers", {})},
        follow_redirects=False,
        **kwargs,
    )


def bucket_total(env: Env, limit_name: str) -> int:
    with owner_session(env.db.owner_url) as s:
        rows = s.scalars(
            select(RateLimitBucket).where(RateLimitBucket.key.like(f"{limit_name}:%"))
        ).all()
    return sum(r.count for r in rows)


# --- nothing is parsed before the gates pass ---------------------------------------------------


def test_an_anonymous_post_never_constructs_the_multipart_parser(
    env: Env, parser_spy: type[Spy]
) -> None:
    with env.factory() as c:
        response = c.post(
            "/uploads",
            data={"csrf_token": "x" * 40},
            files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
    assert response.status_code == 401
    assert parser_spy.constructed == 0
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_a_bad_origin_post_never_constructs_the_multipart_parser(
    env: Env, parser_spy: type[Spy]
) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        parser_spy.constructed = 0
        response = env.upload(c, token=token, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert parser_spy.constructed == 0
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "csrf_failed")) == 1


def test_a_missing_origin_is_refused_before_the_body_too(env: Env, parser_spy: type[Spy]) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        c.headers.pop("Origin", None)
        response = post_with_header(c, token)
    assert response.status_code == 403
    assert parser_spy.constructed == 0


def test_a_wrong_header_token_is_refused_before_the_body(env: Env, parser_spy: type[Spy]) -> None:
    with env.manager_a() as c:
        response = post_with_header(c, "z" * 40)
    assert response.status_code == 403
    assert parser_spy.constructed == 0
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_a_rate_limited_post_never_constructs_the_multipart_parser(
    env: Env, parser_spy: type[Spy], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(monkeypatch, user=1)
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        assert post_with_header(c, token).status_code == 303
        parser_spy.constructed = 0
        second = post_with_header(c, token)
    assert second.status_code == 429
    assert parser_spy.constructed == 0


def test_a_user_who_must_change_the_password_is_refused_before_the_body(
    env: Env, parser_spy: type[Spy]
) -> None:
    add_user(
        env.db.owner_url, env.seeded.agency_a, PHONE_STAFF_2, password=PASSWORD_S, must_change=True
    )
    c = env.factory()
    assert login(c, PHONE_STAFF_2, PASSWORD_S).status_code == 303
    with c:
        response = c.post(
            "/uploads",
            data={"csrf_token": "x" * 40},
            files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert parser_spy.constructed == 0


def test_with_every_gate_passed_the_body_is_parsed_once(env: Env, parser_spy: type[Spy]) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        parser_spy.constructed = 0
        response = post_with_header(c, token)
    assert response.status_code == 303
    assert parser_spy.constructed == 1


def test_the_form_field_token_is_checked_after_the_body_is_read_and_before_the_file(
    env: Env, parser_spy: type[Spy], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(monkeypatch, user=1)
    seen: list[int] = []

    def spy(data: bytes) -> ParsedReport:
        seen.append(len(data))
        return parse_report(data)

    override_parser(env, spy)
    with env.manager_a() as c:
        wrong = env.upload(c, token="y" * 40)
        assert wrong.status_code == 403
        assert parser_spy.constructed == 1  # no header, so the form had to be read
        assert seen == []  # ...but the file was never used
        assert env.counts(env.seeded.agency_a) == (0, 0)
        # a bad token does not use up the quota: the one allowed upload still works
        assert env.upload(c).status_code == 303
    assert seen == [len(fixture_bytes())]
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "csrf_failed")) == 1


def test_a_form_without_a_token_and_without_a_header_is_refused(env: Env) -> None:
    with env.manager_a() as c:
        response = c.post(
            "/uploads",
            files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_the_plain_html_form_still_works_without_javascript(env: Env) -> None:
    with env.manager_a() as c:
        page = c.get("/uploads/new")
        assert 'enctype="multipart/form-data"' in page.text
        assert 'name="csrf_token"' in page.text
        assert env.upload(c).status_code == 303


def test_a_form_with_too_many_files_or_fields_is_a_400(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        many_files = c.post(
            "/uploads",
            data={"csrf_token": token},
            files=[
                ("file", ("a.xlsx", fixture_bytes(), XLSX)),
                ("file", ("b.xlsx", fixture_bytes(), XLSX)),
            ],
            follow_redirects=False,
        )
        many_fields = c.post(
            "/uploads",
            data={"csrf_token": token, **{f"f{i}": "x" for i in range(10)}},
            files={"file": ("a.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
    assert many_files.status_code == 400
    assert many_fields.status_code == 400
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_a_huge_text_field_is_refused_as_a_400(env: Env) -> None:
    with env.manager_a() as c:
        response = c.post(
            "/uploads",
            data={"csrf_token": csrf_for(c, "/uploads/new"), "note": "x" * (200 * 1024)},
            files={"file": ("a.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
    assert response.status_code == 400


# --- the per-user and per-agency limits --------------------------------------------------------


def test_the_eleventh_upload_by_one_user_in_an_hour_is_a_429(env: Env) -> None:
    assert ratelimit.UPLOAD_USER.max_hits == 10
    assert ratelimit.UPLOAD_USER.window_seconds == 3600
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        codes = [env.upload(c, token=token).status_code for _ in range(11)]
    assert codes == [303] * 10 + [429]
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "rate_limited")
    assert [e.details["scope"] for e in events] == ["upload_user"]


def test_one_user_cannot_burn_the_agencys_whole_budget(env: Env) -> None:
    with env.manager_a() as a:
        token = csrf_for(a, "/uploads/new")
        for _ in range(11):
            env.upload(a, token=token)
    # the rejected 11th did not touch the agency's counter
    assert bucket_total(env, "upload_agency") == 10
    with env.staff_a() as s:
        assert env.upload(s).status_code == 303  # a colleague still has a budget


def test_the_agency_limit_is_thirty_across_users(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ratelimit.UPLOAD_AGENCY.max_hits == 30
    limits(monkeypatch, user=1000, agency=ratelimit.UPLOAD_AGENCY.max_hits)
    with env.manager_a() as m, env.staff_a() as s:
        mt, st = csrf_for(m, "/uploads/new"), csrf_for(s, "/uploads/new")
        codes = [
            env.upload(m if i % 2 else s, token=mt if i % 2 else st).status_code for i in range(31)
        ]
    assert codes[-1] == 429
    assert codes.count(303) == 30
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "rate_limited")
    assert [e.details["scope"] for e in events] == ["upload_agency"]


def test_an_agency_refusal_gives_the_users_hit_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(monkeypatch, user=1000, agency=1)
    with env.manager_a() as m, env.staff_a() as s:
        assert env.upload(m).status_code == 303
        assert env.upload(s).status_code == 429
    assert bucket_total(env, "upload_user") == 1  # only the manager's successful upload


def test_another_agency_has_its_own_budgets(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, user=1, agency=1)
    with env.manager_a() as a, env.manager_b() as b:
        assert env.upload(a).status_code == 303
        assert env.upload(a).status_code == 429
        assert env.upload(b).status_code == 303


def test_the_user_window_ends_after_an_hour(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        for _ in range(11):
            env.upload(c, token=token)
        env.clock.advance(hours=1, seconds=1)
        assert env.upload(c, token=csrf_for(c, "/uploads/new")).status_code == 303


# --- the quota is refunded when it is not the user's fault -------------------------------------


def test_a_busy_503_gives_the_quota_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, user=1, agency=1)

    def busy(data: bytes) -> ParsedReport:
        raise SandboxBusy

    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        override_parser(env, busy)
        assert env.upload(c, token=token).status_code == 503
        assert bucket_total(env, "upload_user") == 0
        assert bucket_total(env, "upload_agency") == 0
        override_parser(env, parse_report)
        assert env.upload(c, token=token).status_code == 303  # not a 429


def test_an_internal_error_gives_the_quota_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, user=1, agency=1)

    def explode(data: bytes) -> ParsedReport:
        raise RuntimeError("secret /srv/app/internals.py")

    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        override_parser(env, explode)
        assert env.upload(c, token=token).status_code == 500
        assert bucket_total(env, "upload_user") == 0
        override_parser(env, parse_report)
        assert env.upload(c, token=token).status_code == 303


def test_a_file_the_user_got_wrong_still_counts(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, user=1)

    def bad(data: bytes) -> ParsedReport:
        raise ParseError("no header row")

    override_parser(env, bad)
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        assert env.upload(c, token=token).status_code == 400
        assert env.upload(c, token=token).status_code == 429
    assert bucket_total(env, "upload_user") == 2  # counted, and the rejected one counted too


def test_a_missing_file_and_a_non_workbook_still_count(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(monkeypatch, user=2)
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        assert env.upload(c, b"text", token=token).status_code == 400
        assert env.upload(c, b"", token=token).status_code == 400
        assert env.upload(c, token=token).status_code == 429


# --- the database fails ------------------------------------------------------------------------


def boom_ingest(*args: Any, **kwargs: Any) -> Any:
    raise OperationalError("INSERT ...", {}, Exception("connection lost"))


def test_a_database_error_is_a_friendly_500_audited_and_refunded(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits(monkeypatch, user=1, agency=1)
    monkeypatch.setattr(upload_routes, "ingest_report", boom_ingest)
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        response = env.upload(c, token=token)
        assert response.status_code == 500
        assert "connection lost" not in response.text
        assert "INSERT" not in response.text
        assert "Traceback" not in response.text
        assert bucket_total(env, "upload_user") == 0
        monkeypatch.undo()
        limits(monkeypatch, user=1, agency=1)
        assert env.upload(c, token=token).status_code == 303
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "upload_failed")
    assert event.details == {"filename": "1OCT.xlsx", "reason": "db_error"}


def test_a_failing_success_audit_does_not_undo_a_committed_upload(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = audit.record_and_commit

    def flaky(db: Any, event_type: str, **kwargs: Any) -> Any:
        if event_type == audit.UPLOAD_SUCCEEDED:
            raise OperationalError("INSERT audit", {}, Exception("audit table locked"))
        return real(db, event_type, **kwargs)

    monkeypatch.setattr(audit, "record_and_commit", flaky)
    with env.manager_a() as c:
        response = env.upload(c)
    assert response.status_code == 303
    assert env.counts(env.seeded.agency_a) == (48, 1)
    assert any("audit" in r.getMessage() for r in caplog.records)
    location = response.headers["location"]
    with env.manager_a() as c:
        assert c.get(location).status_code == 200


def test_two_staff_members_have_separate_user_budgets(env: Env) -> None:
    add_user(env.db.owner_url, env.seeded.agency_a, PHONE_STAFF_2, password=PASSWORD_S)
    with env.staff_a() as one, env.as_user(PHONE_STAFF_2, PASSWORD_S) as two:
        t1, t2 = csrf_for(one, "/uploads/new"), csrf_for(two, "/uploads/new")
        assert [env.upload(one, token=t1).status_code for _ in range(11)][-1] == 429
        assert env.upload(two, token=t2).status_code == 303
    assert PHONE_STAFF_A != PHONE_STAFF_2


def test_the_rejected_uploads_stored_no_tickets(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        for _ in range(11):
            env.upload(c, token=token)
    with owner_session(env.db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, env.seeded.agency_a)
        assert s.scalar(select(func.count()).select_from(Ticket)) == 48
