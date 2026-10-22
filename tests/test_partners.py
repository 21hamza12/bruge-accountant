"""app/partners.py: partners per agency and their fee history (service level, SQLite)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, partners
from app.models import AuditEvent, Partner, PartnerFee, User
from app.partners import PartnerError, parse_valid_from
from app.repository import create_agency

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class World:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.agency_id = create_agency(session, "agence-p", "Agence P").id
        self.actor = self._user(self.agency_id, "+22212340001", "manager")
        other = create_agency(session, "agence-q", "Agence Q").id
        self.other_agency_id = other
        self.other_actor = self._user(other, "+22212340002", "manager")

    def _user(self, agency_id: int, phone: str, role: str) -> User:
        user = User(agency_id=agency_id, phone=phone, role=role)
        self.db.add(user)
        self.db.commit()
        return user

    def partner(self, name: str = "BADI", phone: str | None = None) -> Partner:
        return partners.create_partner(self.db, self.actor, name, phone, now=NOW)

    def fee(self, p: Partner, amount: str, day: date, note: str | None = None) -> PartnerFee:
        return partners.set_fee(self.db, p, Decimal(amount), day, self.actor, note, now=NOW)

    def fee_on(self, p: Partner, day: date) -> Decimal | None:
        found = partners.fee_on(self.db, self.agency_id, p.id, day)
        return None if found is None else found.fee

    def events(self, event_type: str) -> list[AuditEvent]:
        query = select(AuditEvent).where(AuditEvent.event_type == event_type)
        return list(self.db.scalars(query.order_by(AuditEvent.id)))


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


# --- fee_on ---------------------------------------------------------------------------------


def test_badi_example_fee_changes_on_16_july(w: World) -> None:
    badi = w.partner("BADI")
    w.fee(badi, "500", date(2026, 1, 1))
    w.fee(badi, "300", date(2026, 7, 16))
    assert w.fee_on(badi, date(2026, 7, 15)) == Decimal("500.00")
    assert w.fee_on(badi, date(2026, 7, 16)) == Decimal("300.00")


def test_fee_on_picks_the_latest_valid_from_on_or_before_the_day(w: World) -> None:
    p = w.partner()
    w.fee(p, "100", date(2026, 3, 1))
    w.fee(p, "200", date(2026, 5, 1))
    w.fee(p, "400", date(2026, 9, 1))
    days = (date(2026, 4, 30), date(2026, 5, 1), date(2026, 8, 31), date(2027, 1, 1))
    assert [w.fee_on(p, d) for d in days] == [
        Decimal("100.00"),
        Decimal("200.00"),
        Decimal("200.00"),
        Decimal("400.00"),
    ]


def test_no_fee_before_the_first_one_or_without_any(w: World) -> None:
    p = w.partner()
    assert w.fee_on(p, date(2026, 6, 1)) is None
    w.fee(p, "100", date(2026, 6, 2))
    assert w.fee_on(p, date(2026, 6, 1)) is None
    assert w.fee_on(p, date(2026, 6, 2)) == Decimal("100.00")


def test_a_future_fee_is_not_yet_valid_and_a_past_one_can_be_entered_later(w: World) -> None:
    p = w.partner()
    w.fee(p, "100", date(2026, 1, 1))
    w.fee(p, "250", date(2027, 1, 1))  # future
    w.fee(p, "150", date(2026, 6, 1))  # back-dated, entered after
    assert w.fee_on(p, date(2026, 10, 21)) == Decimal("150.00")
    assert w.fee_on(p, date(2027, 1, 1)) == Decimal("250.00")


def test_same_valid_from_the_later_entry_wins(w: World) -> None:
    p = w.partner()
    first = w.fee(p, "500", date(2026, 1, 1))
    second = w.fee(p, "450", date(2026, 1, 1))
    assert second.id > first.id
    got = partners.fee_on(w.db, w.agency_id, p.id, date(2026, 2, 1))
    assert got is not None and got.id == second.id and got.fee == Decimal("450.00")


def test_same_valid_from_and_created_at_the_highest_id_wins(w: World) -> None:
    p = w.partner()
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    a = w.fee(p, "1", date(2026, 1, 1))
    b = w.fee(p, "2", date(2026, 1, 1))
    a.created_at = b.created_at = stamp
    w.db.commit()
    got = partners.fee_on(w.db, w.agency_id, p.id, date(2026, 1, 1))
    assert got is not None and got.id == b.id


def test_an_earlier_created_at_loses_even_with_a_higher_id(w: World) -> None:
    p = w.partner()
    a = w.fee(p, "1", date(2026, 1, 1))
    b = w.fee(p, "2", date(2026, 1, 1))
    a.created_at = datetime(2026, 2, 1, tzinfo=UTC)
    b.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    w.db.commit()
    got = partners.fee_on(w.db, w.agency_id, p.id, date(2026, 1, 1))
    assert got is not None and got.id == a.id


def test_fee_on_never_crosses_agencies(w: World) -> None:
    p = w.partner()
    w.fee(p, "500", date(2026, 1, 1))
    assert partners.fee_on(w.db, w.other_agency_id, p.id, date(2026, 6, 1)) is None


def test_fee_history_is_newest_first(w: World) -> None:
    p = w.partner()
    w.fee(p, "500", date(2026, 1, 1))
    w.fee(p, "300", date(2026, 7, 16))
    w.fee(p, "400", date(2026, 3, 1))
    history = partners.fee_history(w.db, w.agency_id, p.id)
    assert [f.valid_from for f in history] == [
        date(2026, 7, 16),
        date(2026, 3, 1),
        date(2026, 1, 1),
    ]
    assert partners.fee_history(w.db, w.other_agency_id, p.id) == []


# --- names ------------------------------------------------------------------------------------


def test_create_stores_a_cleaned_name_and_key_and_audits(w: World) -> None:
    p = w.partner("  Al   Badi​  Travel ", "22 33 44 55")
    assert p.name == "Al Badi Travel"
    assert p.name_key == "al badi travel"
    assert p.phone == "+22222334455"
    assert p.is_active is True
    assert p.agency_id == w.agency_id
    (event,) = w.events(audit.PARTNER_CREATED)
    assert event.agency_id == w.agency_id and event.actor_user_id == w.actor.id
    assert event.details == {"partner_id": p.id}


@pytest.mark.parametrize(
    ("existing", "dup"),
    [
        ("BADI", "badi"),
        ("BADI", "  BaDi  "),
        ("ba di", "BA  DI"),
        ("Ba di", "ba di"),
    ],
)
def test_duplicate_names_are_refused_by_case_and_spaces(w: World, existing: str, dup: str) -> None:
    w.partner(existing)
    with pytest.raises(PartnerError, match="existe déjà"):
        w.partner(dup)
    assert w.db.scalar(select(func.count()).select_from(Partner)) == 1


def test_a_name_with_a_space_is_not_the_name_without(w: World) -> None:
    w.partner("Badi")
    assert w.partner("Ba di").name_key == "ba di"  # another partner, not a duplicate


def test_inner_spaces_and_no_break_spaces_collapse_in_the_key(w: World) -> None:
    w.partner("Al Badi")
    with pytest.raises(PartnerError, match="existe déjà"):
        w.partner("al  badi")


def test_the_same_name_may_exist_in_two_agencies(w: World) -> None:
    w.partner("BADI")
    other = partners.create_partner(w.db, w.other_actor, "BADI", None, now=NOW)
    assert other.agency_id == w.other_agency_id


def test_a_deactivated_partners_name_stays_taken(w: World) -> None:
    p = w.partner("BADI")
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    with pytest.raises(PartnerError, match="existe déjà"):
        w.partner("badi")


@pytest.mark.parametrize("name", ["Bruge", "BRUGE", " BRUGE ", "bruge", "B​ruge", "bRuGe"])
def test_bruge_is_refused(w: World, name: str) -> None:
    with pytest.raises(PartnerError, match="vente directe"):
        w.partner(name)
    assert w.db.scalar(select(func.count()).select_from(Partner)) == 0


def test_bruge_is_refused_on_rename_too(w: World) -> None:
    p = w.partner("BADI")
    with pytest.raises(PartnerError, match="vente directe"):
        partners.update_partner(w.db, w.actor, p, name=" Bruge ", phone=None, now=NOW)


@pytest.mark.parametrize("name", ["", "   ", "​​", "\n\t"])
def test_an_empty_name_is_refused(w: World, name: str) -> None:
    with pytest.raises(PartnerError, match="nom"):
        w.partner(name)


def test_name_length_boundary(w: World) -> None:
    assert w.partner("x" * 120).name == "x" * 120
    with pytest.raises(PartnerError, match="120"):
        w.partner("y" * 121)


def test_non_text_names_are_refused(w: World) -> None:
    with pytest.raises(PartnerError):
        partners.create_partner(w.db, w.actor, None, None, now=NOW)  # type: ignore[arg-type]


def test_unicode_and_hostile_names_are_kept_as_text(w: World) -> None:
    p = w.partner("<b>Agence</b> '; DROP TABLE partners;-- é 😀")
    assert "DROP TABLE" in p.name
    assert w.db.scalar(select(func.count()).select_from(Partner)) == 1


def test_casefold_handles_special_letters(w: World) -> None:
    w.partner("Straße")
    with pytest.raises(PartnerError, match="existe déjà"):
        w.partner("STRASSE")


# --- phone and update -------------------------------------------------------------------------


def test_a_bad_phone_is_refused_and_a_blank_phone_is_none(w: World) -> None:
    with pytest.raises(PartnerError, match="téléphone"):
        w.partner("X", "abc")
    assert w.partner("Y", "   ").phone is None
    assert w.partner("Z", None).phone is None


def test_update_changes_name_and_phone_and_audits(w: World) -> None:
    p = w.partner("BADI", "12 34 56 78")
    partners.update_partner(w.db, w.actor, p, name="Badi Voyages", phone="", now=NOW)
    assert (p.name, p.name_key, p.phone) == ("Badi Voyages", "badi voyages", None)
    (event,) = w.events(audit.PARTNER_UPDATED)
    assert event.details["partner_id"] == p.id
    assert set(event.details["changed"]) == {"name", "phone"}


def test_update_without_a_change_writes_no_event(w: World) -> None:
    p = w.partner("BADI", "12 34 56 78")
    partners.update_partner(w.db, w.actor, p, name="BADI", phone="+22212345678", now=NOW)
    assert w.events(audit.PARTNER_UPDATED) == []


def test_renaming_to_its_own_key_is_allowed_but_to_another_is_not(w: World) -> None:
    p = w.partner("BADI")
    w.partner("SAHEL")
    partners.update_partner(w.db, w.actor, p, name="badi", phone=None, now=NOW)
    assert p.name == "badi"
    with pytest.raises(PartnerError, match="existe déjà"):
        partners.update_partner(w.db, w.actor, p, name="Sahel", phone=None, now=NOW)


def test_update_refuses_another_agencys_partner(w: World) -> None:
    p = w.partner("BADI")
    with pytest.raises(PartnerError):
        partners.update_partner(w.db, w.other_actor, p, name="Mine", phone=None, now=NOW)
    assert p.name == "BADI"


def test_update_on_a_deactivated_partner_is_refused(w: World) -> None:
    p = w.partner("BADI")
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    with pytest.raises(PartnerError, match="désactivé"):
        partners.update_partner(w.db, w.actor, p, name="Other", phone=None, now=NOW)


# --- fees -------------------------------------------------------------------------------------


def test_set_fee_stores_audits_and_notes(w: World) -> None:
    p = w.partner()
    fee = w.fee(p, "500", date(2026, 1, 1), "  accord   verbal ")
    assert fee.fee == Decimal("500.00") and fee.note == "accord verbal"
    assert fee.set_by_user_id == w.actor.id and fee.agency_id == w.agency_id
    (event,) = w.events(audit.PARTNER_FEE_SET)
    assert event.details == {
        "partner_id": p.id,
        "fee_id": fee.id,
        "fee": "500.00",
        "valid_from": "2026-01-01",
    }


def test_set_fee_accepts_zero_and_text_amounts_and_blank_note(w: World) -> None:
    p = w.partner()
    zero = partners.set_fee(w.db, p, Decimal("0"), date(2026, 1, 1), w.actor, "  ", now=NOW)
    assert zero.fee == Decimal("0.00") and zero.note is None
    text_fee = partners.set_fee(w.db, p, "1 500,50", date(2026, 2, 1), w.actor, now=NOW)
    assert text_fee.fee == Decimal("1500.50")


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("-1"),
        Decimal("-0.01"),
        300.0,
        Decimal("1.234"),
        "abc",
        "-5",
        "",
        None,
        Decimal(10) ** 12,
        Decimal("NaN"),
        True,
    ],
)
def test_bad_fees_are_refused_and_nothing_is_written(w: World, bad: object) -> None:
    p = w.partner()
    with pytest.raises(PartnerError):
        partners.set_fee(w.db, p, bad, date(2026, 1, 1), w.actor, now=NOW)  # type: ignore[arg-type]
    assert w.db.scalar(select(func.count()).select_from(PartnerFee)) == 0
    assert w.events(audit.PARTNER_FEE_SET) == []


def test_the_fee_boundaries(w: World) -> None:
    p = w.partner()
    top = partners.set_fee(w.db, p, "999999999999,99", date(2026, 1, 1), w.actor, now=NOW)
    assert top.fee == Decimal("999999999999.99")
    with pytest.raises(PartnerError):
        partners.set_fee(w.db, p, "1000000000000", date(2026, 1, 2), w.actor, now=NOW)


@pytest.mark.parametrize("day", [None, "2026-01-01", datetime(2026, 1, 1, tzinfo=UTC), 20260101])
def test_valid_from_must_be_a_date(w: World, day: object) -> None:
    p = w.partner()
    with pytest.raises(PartnerError, match="date"):
        partners.set_fee(w.db, p, Decimal("1"), day, w.actor, now=NOW)  # type: ignore[arg-type]


def test_a_note_longer_than_the_limit_is_refused(w: World) -> None:
    p = w.partner()
    assert w.fee(p, "1", date(2026, 1, 1), "n" * 500).note == "n" * 500
    with pytest.raises(PartnerError, match="500"):
        w.fee(p, "1", date(2026, 1, 2), "n" * 501)


def test_set_fee_for_another_agencys_partner_is_refused(w: World) -> None:
    p = w.partner()
    with pytest.raises(PartnerError):
        partners.set_fee(w.db, p, Decimal("1"), date(2026, 1, 1), w.other_actor, now=NOW)


def test_set_fee_on_a_deactivated_partner_is_refused(w: World) -> None:
    p = w.partner()
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    with pytest.raises(PartnerError, match="désactivé"):
        w.fee(p, "1", date(2026, 1, 1))


# --- deactivate / list --------------------------------------------------------------------------


def test_deactivate_keeps_history_and_fee_on(w: World) -> None:
    p = w.partner()
    w.fee(p, "500", date(2026, 1, 1))
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    assert p.is_active is False
    assert len(partners.fee_history(w.db, w.agency_id, p.id)) == 1
    assert w.fee_on(p, date(2026, 6, 1)) == Decimal("500.00")
    (event,) = w.events(audit.PARTNER_DEACTIVATED)
    assert event.details == {"partner_id": p.id}


def test_deactivating_twice_is_refused(w: World) -> None:
    p = w.partner()
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    with pytest.raises(PartnerError, match="déjà"):
        partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    assert len(w.events(audit.PARTNER_DEACTIVATED)) == 1


def test_list_partners_is_scoped_ordered_and_carries_the_current_fee(w: World) -> None:
    b = w.partner("Zed")
    a = w.partner("alpha")
    gone = w.partner("Gone")
    w.fee(a, "500", date(2026, 1, 1))
    w.fee(a, "300", date(2026, 7, 16))
    w.fee(b, "900", date(2099, 1, 1))  # not valid yet
    partners.deactivate_partner(w.db, w.actor, gone, now=NOW)
    partners.create_partner(w.db, w.other_actor, "Other", None, now=NOW)
    rows = partners.list_partners(w.db, w.agency_id, today=date(2026, 10, 21))
    assert [r.partner.name for r in rows] == ["alpha", "Zed", "Gone"]  # active first, by name
    assert [r.current_fee.fee if r.current_fee else None for r in rows] == [
        Decimal("300.00"),
        None,
        None,
    ]


def test_list_partners_is_empty_for_an_agency_without_partners(w: World) -> None:
    assert partners.list_partners(w.db, w.agency_id, today=date(2026, 1, 1)) == []


def test_get_partner_is_scoped(w: World) -> None:
    p = w.partner()
    assert partners.get_partner(w.db, w.agency_id, p.id) is p
    assert partners.get_partner(w.db, w.other_agency_id, p.id) is None
    assert partners.get_partner(w.db, w.agency_id, 10**9) is None


def test_a_year_of_fee_changes(w: World) -> None:
    p = w.partner()
    for month in range(1, 13):
        w.fee(p, str(100 * month), date(2026, month, 1))
    assert w.fee_on(p, date(2026, 6, 30)) == Decimal("600.00")
    assert len(partners.fee_history(w.db, w.agency_id, p.id)) == 12


# --- review follow-ups ----------------------------------------------------------------------


def test_a_name_whose_key_expands_past_the_column_is_refused(w: World) -> None:
    name = "ﷺ" * 120  # 120 characters, but 2160 after NFKC
    assert len(name) == 120
    with pytest.raises(PartnerError, match="120"):
        w.partner(name)
    assert w.db.scalar(select(func.count()).select_from(Partner)) == 0


def test_a_name_whose_key_just_fits_is_accepted(w: World) -> None:
    assert w.partner("ß" * 120).name_key == "ss" * 120  # 240 characters


def test_a_concurrent_duplicate_is_caught_at_flush(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests pass the pre-check at once: the unique constraint decides, and the loser gets
    the same French message."""
    w.partner("BADI")
    other = w.partner("SAHEL")
    w.db.commit()
    monkeypatch.setattr(partners, "_taken", lambda *a, **k: False)
    with pytest.raises(PartnerError, match="existe déjà"):
        w.partner("badi")
    w.db.rollback()  # what the route does on any PartnerError
    with pytest.raises(PartnerError, match="existe déjà"):
        partners.update_partner(w.db, w.actor, other, name="Badi", phone=None, now=NOW)
    w.db.rollback()
    assert sorted(p.name for p in w.db.scalars(select(Partner))) == ["BADI", "SAHEL"]


def test_other_integrity_errors_are_not_swallowed(w: World) -> None:
    from sqlalchemy.exc import IntegrityError

    p = w.partner()
    p.name_key = "bruge"  # the CHECK, not a unique violation
    with pytest.raises(IntegrityError):
        partners._flush_unique(w.db)
    w.db.rollback()


def test_non_text_phone_and_note_are_refused(w: World) -> None:
    with pytest.raises(PartnerError, match="invalide"):
        partners.create_partner(w.db, w.actor, "X", 12345678, now=NOW)  # type: ignore[arg-type]
    p = w.partner("Y")
    with pytest.raises(PartnerError, match="invalide"):
        partners.set_fee(w.db, p, "1", date(2026, 1, 1), w.actor, 5, now=NOW)  # type: ignore[arg-type]


def test_a_stale_partner_object_is_reloaded_under_the_lock(w: World) -> None:
    """The row was deactivated behind this object's back (another request): the check sees it."""
    from sqlalchemy import update

    p = w.partner()
    w.db.execute(
        update(Partner)
        .where(Partner.id == p.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert p.is_active is True  # stale in memory
    with pytest.raises(PartnerError, match="désactivé"):
        w.fee(p, "1", date(2026, 1, 1))
    assert w.db.scalar(select(func.count()).select_from(PartnerFee)) == 0


def test_reactivate_restores_the_partner_and_audits(w: World) -> None:
    p = w.partner()
    w.fee(p, "500", date(2026, 1, 1))
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    partners.reactivate_partner(w.db, w.actor, p, now=NOW)
    assert p.is_active is True
    w.fee(p, "400", date(2026, 6, 1))  # can be given a fee again
    (event,) = w.events(audit.PARTNER_REACTIVATED)
    assert event.details == {"partner_id": p.id}
    with pytest.raises(PartnerError, match="déjà actif"):
        partners.reactivate_partner(w.db, w.actor, p, now=NOW)
    assert len(w.events(audit.PARTNER_REACTIVATED)) == 1


def test_reactivate_refuses_another_agencys_partner(w: World) -> None:
    p = w.partner()
    partners.deactivate_partner(w.db, w.actor, p, now=NOW)
    with pytest.raises(PartnerError, match="introuvable"):
        partners.reactivate_partner(w.db, w.other_actor, p, now=NOW)
    assert p.is_active is False


def test_deactivate_refuses_another_agencys_partner(w: World) -> None:
    p = w.partner()
    with pytest.raises(PartnerError, match="introuvable"):
        partners.deactivate_partner(w.db, w.other_actor, p, now=NOW)


def test_list_partners_current_fee_follows_the_same_tie_rules_as_fee_on(w: World) -> None:
    p = w.partner()
    w.fee(p, "500", date(2026, 1, 1))
    last = w.fee(p, "450", date(2026, 1, 1))  # same valid_from: the later entry wins
    w.fee(p, "300", date(2026, 7, 16))
    q = w.partner("Q")
    rows = {
        r.partner.id: r for r in partners.list_partners(w.db, w.agency_id, today=date(2026, 2, 1))
    }
    assert rows[p.id].current_fee is not None and rows[p.id].current_fee.id == last.id  # type: ignore[union-attr]
    assert rows[q.id].current_fee is None
    later = partners.list_partners(w.db, w.agency_id, today=date(2026, 7, 16))
    assert {r.partner.id: r.current_fee.fee for r in later if r.current_fee} == {
        p.id: Decimal("300.00")
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2026-07-16", date(2026, 7, 16)), (" 2026-07-16 ", date(2026, 7, 16))],
)
def test_parse_valid_from_accepts_iso_dates(raw: str, expected: date) -> None:
    assert parse_valid_from(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_parse_valid_from_requires_a_date(raw: object) -> None:
    with pytest.raises(PartnerError, match="à partir de laquelle"):
        parse_valid_from(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw", ["16/07/2026", "2026-02-30", "20260716", "2026-7-16", "٢026-01-01", "2026-13-01"]
)
def test_parse_valid_from_refuses_other_shapes(raw: str) -> None:
    with pytest.raises(PartnerError, match="date valide"):
        parse_valid_from(raw)
