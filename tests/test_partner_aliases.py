"""app/partner_aliases.py: names an agency is known by in the Amadeus text (service level)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, partner_aliases, partners
from app.models import (
    ALIAS_APPROVED,
    ALIAS_PENDING,
    ALIAS_REJECTED,
    AuditEvent,
    Partner,
    PartnerAlias,
    User,
)
from app.partner_aliases import AliasError
from app.repository import create_agency

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class World:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.agency_id = create_agency(session, "agence-p", "Agence P").id
        self.manager = self._user(self.agency_id, "+22212340001", "manager")
        self.agent = self._user(self.agency_id, "+22212340003", "staff")
        other = create_agency(session, "agence-q", "Agence Q").id
        self.other_manager = self._user(other, "+22212340002", "manager")
        self.rend = self.partner("REND TOURISME ET VOYAGES")
        self.absher = self.partner("ABSHER VOYAGE")

    def _user(self, agency_id: int, phone: str, role: str) -> User:
        user = User(agency_id=agency_id, phone=phone, role=role)
        self.db.add(user)
        self.db.commit()
        return user

    def partner(self, name: str) -> Partner:
        return partners.create_partner(self.db, self.manager, name, now=NOW)

    def propose(self, text: str, partner: Partner | None = None) -> int | None:
        alias = partner_aliases.propose(
            self.db, self.agent, (partner or self.rend).id, text, now=NOW
        )
        return None if alias is None else alias.id

    def events(self, kind: str) -> list[AuditEvent]:
        query = select(AuditEvent).where(AuditEvent.event_type == kind)
        return list(self.db.scalars(query.order_by(AuditEvent.id)))

    def rows(self) -> list[PartnerAlias]:
        return list(self.db.scalars(select(PartnerAlias)))


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


def test_a_proposal_is_pending_and_audited_with_ids_only(w: World) -> None:
    alias_id = w.propose("  Rend  ")
    assert alias_id is not None
    (row,) = partner_aliases.pending(w.db, w.agency_id)
    assert (row.alias, row.alias_key, row.status, row.proposed_by_user_id) == (
        "Rend",
        "rend",
        ALIAS_PENDING,
        w.agent.id,
    )
    (event,) = w.events(audit.ALIAS_PROPOSED)
    assert event.details == {"alias_id": alias_id, "partner_id": w.rend.id}


def test_a_pending_alias_never_matches(w: World) -> None:
    w.propose("REND")
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == []


def test_approval_makes_it_match(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    partner_aliases.approve(w.db, w.manager, alias_id, now=NOW)
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == [(w.rend.id, "REND")]
    assert [a.alias for a in partner_aliases.approved_for(w.db, w.agency_id, w.rend.id)] == ["REND"]
    assert partner_aliases.pending(w.db, w.agency_id) == []
    (event,) = w.events(audit.ALIAS_APPROVED)
    assert event.details == {"alias_id": alias_id, "partner_id": w.rend.id}


def test_rejection_is_final_and_never_proposed_again(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    partner_aliases.reject(w.db, w.manager, alias_id, now=NOW)
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == []
    assert w.propose("rend") is None  # the same text, whatever the case
    assert w.propose("REND", w.absher) is None  # nor for another partner
    assert partner_aliases.pending(w.db, w.agency_id) == []
    (row,) = w.rows()
    assert row.status == ALIAS_REJECTED and row.decided_by_user_id == w.manager.id
    assert len(w.events(audit.ALIAS_REJECTED)) == 1


def test_an_existing_alias_is_not_proposed_twice(w: World) -> None:
    assert w.propose("REND") is not None
    assert w.propose("Rend") is None
    assert len(partner_aliases.pending(w.db, w.agency_id)) == 1
    assert len(w.events(audit.ALIAS_PROPOSED)) == 1


def test_a_text_that_is_a_partner_name_or_empty_or_bruge_is_not_proposed(w: World) -> None:
    assert w.propose("absher  voyage") is None
    assert w.propose("   ") is None
    assert w.propose("---") is None
    assert w.propose("BRUGE") is None
    assert w.propose("x" * 500) is not None  # cut to the column, never an error
    assert len(partner_aliases.pending(w.db, w.agency_id)[0].alias) <= 120


def test_a_partner_of_another_agency_is_refused(w: World) -> None:
    foreign = partners.create_partner(w.db, w.other_manager, "AUTRE", now=NOW)
    with pytest.raises(AliasError):
        partner_aliases.propose(w.db, w.agent, foreign.id, "AUT", now=NOW)
    with pytest.raises(AliasError):
        partner_aliases.propose(w.db, w.agent, 999_999, "AUT", now=NOW)


def test_an_inactive_partner_gets_no_proposal(w: World) -> None:
    partners.deactivate_partner(w.db, w.manager, w.absher, now=NOW)
    with pytest.raises(AliasError):
        partner_aliases.propose(w.db, w.agent, w.absher.id, "ABSHR", now=NOW)


def test_reassign_approves_for_another_partner(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    partner_aliases.approve(w.db, w.manager, alias_id, partner_id=w.absher.id, now=NOW)
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == [(w.absher.id, "REND")]
    (event,) = w.events(audit.ALIAS_APPROVED)
    assert event.details == {"alias_id": alias_id, "partner_id": w.absher.id}


def test_reassign_to_an_unknown_or_inactive_partner_is_refused(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    with pytest.raises(AliasError):
        partner_aliases.approve(w.db, w.manager, alias_id, partner_id=999_999, now=NOW)
    partners.deactivate_partner(w.db, w.manager, w.absher, now=NOW)
    with pytest.raises(AliasError):
        partner_aliases.approve(w.db, w.manager, alias_id, partner_id=w.absher.id, now=NOW)
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == []


def test_only_a_pending_alias_can_be_approved_or_rejected(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    partner_aliases.approve(w.db, w.manager, alias_id, now=NOW)
    with pytest.raises(AliasError):
        partner_aliases.approve(w.db, w.manager, alias_id, now=NOW)
    with pytest.raises(AliasError):
        partner_aliases.reject(w.db, w.manager, alias_id, now=NOW)


def test_remove_turns_an_approved_alias_into_a_rejected_one_and_keeps_the_row(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    partner_aliases.approve(w.db, w.manager, alias_id, now=NOW)
    partner_aliases.remove(w.db, w.manager, alias_id, now=NOW)
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == []
    (row,) = w.rows()
    assert row.status == ALIAS_REJECTED and row.partner_id == w.rend.id
    assert w.propose("REND") is None
    with pytest.raises(AliasError):
        partner_aliases.remove(w.db, w.manager, alias_id, now=NOW)  # no longer approved


def test_another_agency_cannot_see_or_decide_an_alias(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    assert partner_aliases.pending(w.db, w.other_manager.agency_id) == []
    for action in (partner_aliases.approve, partner_aliases.reject, partner_aliases.remove):
        with pytest.raises(AliasError):
            action(w.db, w.other_manager, alias_id, now=NOW)
    (row,) = w.rows()
    assert row.status == ALIAS_PENDING


def test_a_decision_is_recorded_with_who_and_when(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    partner_aliases.approve(w.db, w.manager, alias_id, now=NOW)
    (row,) = w.rows()
    assert row.status == ALIAS_APPROVED
    assert row.decided_by_user_id == w.manager.id
    assert row.decided_at is not None


def test_the_pending_count(w: World) -> None:
    assert partner_aliases.pending_count(w.db, w.agency_id) == 0
    w.propose("REND")
    w.propose("ABSHR", w.absher)
    assert partner_aliases.pending_count(w.db, w.agency_id) == 2
    assert partner_aliases.pending_count(w.db, w.other_manager.agency_id) == 0


def test_reactivate_turns_a_rejected_alias_back_into_a_pending_one(w: World) -> None:
    alias_id = w.propose("REND")
    assert alias_id is not None
    with pytest.raises(AliasError):
        partner_aliases.reactivate(w.db, w.manager, alias_id, now=NOW)  # not rejected yet
    partner_aliases.reject(w.db, w.manager, alias_id, now=NOW)
    assert [a.id for a in partner_aliases.rejected(w.db, w.agency_id)] == [alias_id]
    partner_aliases.reactivate(w.db, w.manager, alias_id, now=NOW)
    (row,) = w.rows()
    assert (row.status, row.decided_at, row.decided_by_user_id) == (ALIAS_PENDING, None, None)
    assert w.events(audit.ALIAS_REACTIVATED)[0].details == {
        "alias_id": alias_id,
        "partner_id": w.rend.id,
    }


def test_the_pending_cap_stops_new_proposals(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(partner_aliases, "MAX_PENDING", 2)
    assert w.propose("AAA ONE") is not None
    assert w.propose("AAA TWO") is not None
    assert w.propose("AAA THREE") is None
    assert partner_aliases.pending_count(w.db, w.agency_id) == 2
    assert partner_aliases.at_cap(w.db, w.agency_id)


def test_approval_refuses_a_text_that_is_now_a_partner_name(w: World) -> None:
    alias_id = w.propose("NOUVEAU NOM")
    assert alias_id is not None
    w.partner("Nouveau  Nom")
    with pytest.raises(AliasError, match="nom d'un partenaire"):
        partner_aliases.approve(w.db, w.manager, alias_id, now=NOW)
    assert partner_aliases.approved_pairs(w.db, w.agency_id) == []
