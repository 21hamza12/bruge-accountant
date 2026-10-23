"""The approval screens (B9): answering on /flags, /flags/validate, the close gate on the day page.

Page level, on the made-up 20 October report. Every POST carries a CSRF token, every page is
``no-store`` and French, user text is escaped, and the rules of ``app/approvals.py`` hold through
the browser: the agent of the sign answers, a validator never validates their own answer.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.auth import ratelimit
from app.models import AuditEvent, ComparisonFlag, FlagDecision, Sale, User
from app.tenancy import rebind_tenant
from tests.fixtures import synthetic_20oct as fx
from tests.flag_support import FlagWeb
from tests.web_support import PASSWORD_S, WebDb, add_user, csrf_for, owner_session

DAY = "2026-10-20"
TKTT = [str(x["DOC NUMBER"]) for x in fx.LINES if x["TRNC"] == "TKTT"]
SIGN_OF = {str(x["DOC NUMBER"]): str(x["AS"]) for x in fx.LINES}
OWN = [d for d in TKTT if SIGN_OF[d] == "0004SV"]  # the staff member's sign
OTHERS = [d for d in TKTT if SIGN_OF[d] == "0006AC"]
ACCOUNTANT_PHONE = "+22212345610"
XSS = "<script>alert(1)</script> reason"


@pytest.fixture
def world(web_db: WebDb) -> FlagWeb:
    return FlagWeb(web_db)


@contextmanager
def tenant_session(world: FlagWeb) -> Iterator[Session]:
    """A session of the owner bound to agency A: under row-level security an unbound session sees
    no row at all, so every read of a table with a policy goes through this."""
    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        yield s


def accountant(world: FlagWeb) -> Any:
    with tenant_session(world) as s:
        known = s.scalars(select(User.id).where(User.phone == ACCOUNTANT_PHONE)).first()
    if known is None:
        add_user(
            world.db.owner_url,
            world.seeded.agency_a,
            ACCOUNTANT_PHONE,
            role="accountant",
            password=PASSWORD_S,
            display_name="Compta",
        )
    return world.client(ACCOUNTANT_PHONE, PASSWORD_S)


def setup_diff(world: FlagWeb, doc: str, extra: int = 1000, skip: tuple[str, ...] = ()) -> None:
    """Sell the report with ``extra`` more on ``doc``'s cost, upload it, link the staff member."""
    world.link_staff("0004SV")
    world.sell_report(extra_cost={doc: extra}, skip=skip)
    with world.manager() as c:
        assert world.upload(c).status_code == 303


def flag_of(world: FlagWeb, doc: str, kind: str | None = None) -> ComparisonFlag:
    live = [
        f
        for f in world.flags_rows()
        if f.doc_number == doc and f.status in ("open", "answered", "resolved")
    ]
    return next(f for f in live if kind is None or f.kind == kind)


def status_of(world: FlagWeb, flag_id: int) -> str:
    return next(f.status for f in world.flags_rows() if f.id == flag_id)


def decisions(world: FlagWeb) -> list[FlagDecision]:
    with tenant_session(world) as s:
        rows = list(s.scalars(select(FlagDecision).order_by(FlagDecision.id)))
        s.expunge_all()
        return rows


def post(c: Any, path: str, back: str, **data: str) -> Any:
    return c.post(path, data={"csrf_token": csrf_for(c, back), **data}, follow_redirects=False)


def audit_types(world: FlagWeb) -> list[str]:
    with tenant_session(world) as s:
        return [e.event_type for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]


# --- the list: who gets which buttons ----------------------------------------------------------


def test_the_agent_gets_the_answer_buttons_on_their_own_flag(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        page = c.get(f"/flags?date={DAY}")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert f'action="/flags/{flag.id}/answer"' in page.text
    assert "Amadeus a raison" in page.text and "Ma valeur est juste" in page.text
    assert 'name="choice" value="amadeus_right"' in page.text
    assert 'name="csrf_token"' in page.text
    assert " style=" not in page.text
    assert not re.search(r"<script(?![^>]* src=)", page.text)  # no inline script


def test_not_in_amadeus_offers_cancel_or_wait(world: FlagWeb) -> None:
    world.link_staff("0004SV")
    world.sell_report()
    # a sale of the staff member that the report does not list
    from app import sales
    from app.models import User
    from app.sales import SaleInput, TicketInput
    from app.tenancy import rebind_tenant
    from tests.flag_support import NOW
    from tests.web_support import owner_session as owner

    with owner(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        sales.create_sale(
            s,
            manager,
            SaleInput(
                tickets=[
                    TicketInput(
                        "3949999999",
                        "199",
                        1000,
                        "SYNTH/EXTRA",
                        "ADT",
                        "ABCDEF",
                        "TKTT",
                    )
                ],
                ag_kind="direct", pb="1500", clt="Client Synth", payment_method="cash",
                staff_sign="0004SV",
            ),
            day=__import__("datetime").date(2026, 10, 20),
            now=NOW,
        )  # fmt: skip
        s.commit()
    with world.manager() as c:
        assert world.upload(c).status_code == 303
    flag = flag_of(world, "3949999999", "not_in_amadeus")
    with world.staff() as c:
        page = c.get(f"/flags?date={DAY}")
        assert "Annuler la vente" in page.text and "Attendre le prochain rapport" in page.text
        wait = post(c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}", choice="acknowledged")
        assert wait.status_code == 303
        assert status_of(world, flag.id) == "open"
        short = post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}", choice="void_entry", reason="no"
        )
        assert short.status_code == 400 and "Indiquez le motif" in short.text
        done = post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="void_entry", reason="Saisie par erreur, billet jamais émis",
        )  # fmt: skip
        assert done.status_code == 303
        assert status_of(world, flag.id) == "answered"  # the agent alone cannot void
    with accountant(world) as a:
        approved = post(a, f"/flags/{flag.id}/validate", "/flags/validate", decision="approve")
        assert approved.status_code == 303
    with tenant_session(world) as s:
        sale = s.scalars(select(Sale).order_by(Sale.id.desc())).first()
        assert sale is not None and sale.status == "voided"
    assert [f for f in world.flags_rows() if f.status in ("open", "answered", "resolved")] == []


def test_a_ticket_not_sent_leads_to_the_prefilled_form_for_the_manager(world: FlagWeb) -> None:
    setup_diff(world, OWN[1], extra=0, skip=(OWN[0],))
    flag = flag_of(world, OWN[0], "not_sent")
    with world.manager() as c:
        listing = c.get(f"/flags?date={DAY}")
        assert f'href="/flags/{flag.id}/enter"' in listing.text
        assert "Saisir cette vente" in listing.text
        form = c.get(f"/flags/{flag.id}/enter")
    assert form.status_code == 200 and form.headers["cache-control"] == "no-store"
    assert OWN[0] in form.text and "0004SV" in form.text
    assert f'value="{DAY}"' in form.text  # the sale goes on the day of the report
    pax = next(str(x["PAX NAME"]) for x in fx.LINES if x["DOC NUMBER"] == OWN[0])
    assert pax.split(" (")[0] in form.text


def test_staff_cannot_enter_a_past_day_sale_but_are_told_who_can(world: FlagWeb) -> None:
    setup_diff(world, OWN[1], extra=0, skip=(OWN[0],))
    flag = flag_of(world, OWN[0], "not_sent")
    with world.staff() as c:
        listing = c.get(f"/flags?date={DAY}")
        assert f'href="/flags/{flag.id}/enter"' not in listing.text
        assert "gérant ou au comptable" in listing.text
        refused = c.get(f"/flags/{flag.id}/enter")
    assert refused.status_code == 403


# --- answering ---------------------------------------------------------------------------------


def test_a_cost_answer_waits_and_the_accountant_validates_it(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        no_reason = post(c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}", choice="mine_right")
        assert no_reason.status_code == 400 and "Indiquez le motif" in no_reason.text
        assert status_of(world, flag.id) == "open"
        ok = post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié avec la compagnie",
        )  # fmt: skip
        assert ok.status_code == 303 and ok.headers["location"] == f"/flags?date={DAY}"
        page = c.get(f"/flags?date={DAY}")
        assert "En attente de validation" in page.text
        assert f'action="/flags/{flag.id}/answer"' not in page.text  # nothing left to answer
        assert "À valider" not in page.text  # staff have no validation screen
    assert status_of(world, flag.id) == "answered"
    with accountant(world) as a:
        queue = a.get("/flags/validate")
        assert queue.status_code == 200 and queue.headers["cache-control"] == "no-store"
        assert OWN[0] in queue.text and "Tarif négocié avec la compagnie" in queue.text
        assert f'action="/flags/{flag.id}/validate"' in queue.text
        assert 'class="badge"' in queue.text  # the sidebar count
        done = post(a, f"/flags/{flag.id}/validate", "/flags/validate", decision="approve")
        assert done.status_code == 303
    assert status_of(world, flag.id) == "resolved"
    assert [(d.stage, d.choice) for d in decisions(world)] == [
        ("agent", "mine_right"),
        ("validator", "validated"),
    ]
    assert {"flag_answered", "flag_validated"} <= set(audit_types(world))


def test_amadeus_right_on_a_name_changes_the_sale_and_clears_the_flag(world: FlagWeb) -> None:
    world.link_staff("0004SV")
    world.sell_report()
    # a sale whose passenger name differs from the report
    from app.models import SaleVersion

    with tenant_session(world) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, world.seeded.agency_a)
        # use the service to change a name, as an agent would have typed it
        from app import sales
        from app.models import User
        from app.sales import SaleInput, TicketInput
        from tests.flag_support import NOW

        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        sale_row = s.scalars(
            select(Sale).where(Sale.staff_sign == "0004SV").order_by(Sale.id)
        ).first()
        assert sale_row is not None
        view = sales.get_sale_for(s, manager, sale_row.id)
        assert view is not None
        t = view.tickets[0]
        sales.edit_sale(
            s, manager, view.id,
            SaleInput(
                tickets=[
                    TicketInput(
                        t.doc_number,
                        t.airline,
                        t.total_doc,
                        "SYNTH/WRONG",
                        t.pax_type,
                        t.rloc,
                        t.trnc,
                    )
                ],
                ag_kind="direct", pb=view.pb, clt=view.clt, payment_method=view.payment_method,
                staff_sign=view.staff_sign,
            ),
            "Faute de frappe", now=NOW,
        )  # fmt: skip
        s.commit()
        doc = t.doc_number
        versions_before = len(
            list(s.scalars(select(SaleVersion).where(SaleVersion.sale_id == view.id)))
        )
    with world.manager() as c:
        assert world.upload(c).status_code == 303
    flag = flag_of(world, doc)
    assert flag.field == "pax_name" and not flag.needs_validation
    with world.staff() as c:
        done = post(c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}", choice="amadeus_right")
        assert done.status_code == 303
    assert [f for f in world.flags_rows() if f.status in ("open", "answered", "resolved")] == []
    with tenant_session(world) as s:
        count = len(list(s.scalars(select(SaleVersion).where(SaleVersion.sale_id == flag.sale_id))))
    assert count == versions_before + 1


def test_staff_cannot_answer_another_signs_flag(world: FlagWeb) -> None:
    setup_diff(world, OTHERS[0])
    flag = flag_of(world, OTHERS[0])
    with world.staff() as c:
        page = c.get(f"/flags?date={DAY}")
        assert OTHERS[0] not in page.text
        gone = post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Je pense avoir raison",
        )  # fmt: skip
    assert gone.status_code == 404
    assert status_of(world, flag.id) == "open"
    assert decisions(world) == []


def test_a_flag_of_another_agency_is_a_404(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.other_manager() as c:
        gone = post(c, f"/flags/{flag.id}/answer", "/flags", choice="amadeus_right")
    assert gone.status_code == 404


def test_the_manager_can_answer_for_anyone(world: FlagWeb) -> None:
    setup_diff(world, OTHERS[0])
    flag = flag_of(world, OTHERS[0])
    with world.manager() as c:
        done = post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
    assert done.status_code == 303 and status_of(world, flag.id) == "answered"


# --- validating --------------------------------------------------------------------------------


def test_staff_cannot_see_or_use_the_validation_screen(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
        assert c.get("/flags/validate").status_code == 403
        denied = post(c, f"/flags/{flag.id}/validate", f"/flags?date={DAY}", decision="approve")
        assert denied.status_code == 403
        denied_override = post(
            c, "/flags/overrides/1/validate", f"/flags?date={DAY}", decision="approve"
        )
        assert denied_override.status_code == 403
    assert status_of(world, flag.id) == "answered"
    assert audit_types(world).count(audit.PERMISSION_DENIED) == 3


def test_a_validator_cannot_validate_their_own_answer(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.manager() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
        queue = c.get("/flags/validate")
        assert "Votre réponse" in queue.text
        assert re.search(r"<button[^>]*disabled[^>]*>", queue.text)
        refused = post(c, f"/flags/{flag.id}/validate", "/flags/validate", decision="approve")
    assert refused.status_code == 403 and "votre propre réponse" in refused.text
    assert status_of(world, flag.id) == "answered"


def test_a_rejection_needs_a_reason_and_the_agent_sees_it(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
    with accountant(world) as a:
        bare = post(a, f"/flags/{flag.id}/validate", "/flags/validate", decision="reject")
        assert bare.status_code == 400 and "Indiquez le motif" in bare.text
        assert status_of(world, flag.id) == "answered"
        done = post(
            a, f"/flags/{flag.id}/validate", "/flags/validate",
            decision="reject", reason="Justificatif manquant",
        )  # fmt: skip
        assert done.status_code == 303
    assert status_of(world, flag.id) == "open"
    with world.staff() as c:
        page = c.get(f"/flags?date={DAY}")
    assert "Rejeté" in page.text and "Justificatif manquant" in page.text
    assert f'action="/flags/{flag.id}/answer"' in page.text  # the agent can answer again
    assert "flag_rejected" in audit_types(world)


def test_a_bad_decision_word_is_refused(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
    with accountant(world) as a:
        odd = post(a, f"/flags/{flag.id}/validate", "/flags/validate", decision="maybe")
    assert odd.status_code == 400 and status_of(world, flag.id) == "answered"


def test_user_text_is_escaped_on_every_page(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason=XSS,
        )  # fmt: skip
        mine = c.get(f"/flags?date={DAY}")
    with accountant(world) as a:
        queue = a.get("/flags/validate")
        post(a, f"/flags/{flag.id}/validate", "/flags/validate", decision="reject", reason=XSS)
    with world.staff() as c:
        after = c.get(f"/flags?date={DAY}")
    for text in (queue.text, after.text, mine.text):
        assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in queue.text


def test_fee_overrides_are_listed_and_validated_or_rejected(world: FlagWeb) -> None:
    from datetime import date

    from app import partners, sales
    from app.models import User
    from app.sales import SaleInput, TicketInput
    from app.tenancy import rebind_tenant
    from tests.flag_support import NOW

    world.link_staff("0004SV")
    world.sell_report()
    with tenant_session(world) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        partner = partners.create_partner(s, manager, "BADI", now=NOW)
        partners.set_fee(s, partner, 500, date(2026, 1, 1), manager, now=NOW)
        sale = sales.create_sale(
            s, manager,
            SaleInput(
                tickets=[
                    TicketInput(
                        "3949999990",
                        "199",
                        1000,
                        "SYNTH/PART",
                        "ADT",
                        "ABCDEF",
                        "TKTT",
                    )
                ],
                ag_kind="partner", partner_id=partner.id, payment_method="credit",
                staff_sign="0004SV", fee_override=300, fee_override_reason="Remise accordée",
            ),
            day=date(2026, 10, 20), now=NOW,
        ).sale  # fmt: skip
        s.commit()
        sale_id = sale.id
    with world.manager() as c:
        world.upload(c)
        own = c.get("/flags/validate")
        assert "Remise accordée" in own.text and "Frais exceptionnels" in own.text
        assert re.search(r"<button[^>]*disabled[^>]*>", own.text)  # the manager wrote it
        refused = post(
            c, f"/flags/overrides/{sale_id}/validate", "/flags/validate", decision="approve"
        )
        assert refused.status_code == 403
    with accountant(world) as a:
        queue = a.get("/flags/validate")
        assert f'action="/flags/overrides/{sale_id}/validate"' in queue.text
        bare = post(a, f"/flags/overrides/{sale_id}/validate", "/flags/validate", decision="reject")
        assert bare.status_code == 400
        rejected = post(
            a, f"/flags/overrides/{sale_id}/validate", "/flags/validate",
            decision="reject", reason="Remise non autorisée",
        )  # fmt: skip
        assert rejected.status_code == 303
        assert "Remise accordée" not in a.get("/flags/validate").text
    with tenant_session(world) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        view = sales.get_sale_for(s, manager, sale_id)
        assert view is not None and view.fee_override_reason is None and view.fee_applied == 500
    assert "override_validated" in audit_types(world)


# --- the close gate on the day page ------------------------------------------------------------


def test_the_day_page_lists_what_blocks_the_close(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    with world.manager() as c:
        page = c.get(f"/days/{DAY}")
    assert page.status_code == 200
    assert "Avant de clôturer" in page.text
    assert "1 écart bloquant reste à traiter." in page.text


def test_closing_is_refused_with_the_reasons_then_allowed_when_clear(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with accountant(world) as a:
        refused = post(a, f"/days/{DAY}/close", f"/days/{DAY}")
        assert refused.status_code == 409
        assert "La journée ne peut pas être clôturée" in refused.text
        box = re.search(r'id="form-error"[^>]*>([^<]*)<', refused.text)
        assert box is not None and "1 écart bloquant reste à traiter." in box.group(1)
        assert world.flags_rows()  # the day is still open
    with world.staff() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
    with accountant(world) as a:
        waiting = post(a, f"/days/{DAY}/close", f"/days/{DAY}")
        assert waiting.status_code == 409 and "attend la validation" in waiting.text
        post(a, f"/flags/{flag.id}/validate", "/flags/validate", decision="approve")
        closed = post(a, f"/days/{DAY}/close", f"/days/{DAY}")
        assert closed.status_code == 303
        page = a.get(f"/days/{DAY}")
    assert "Clôturée" in page.text


def test_the_partner_list_must_be_seen_before_closing(world: FlagWeb) -> None:
    from datetime import date

    from app import partners, sales
    from app.models import User
    from app.sales import SaleInput, TicketInput
    from app.tenancy import rebind_tenant
    from tests.flag_support import NOW

    world.sell_report()
    with tenant_session(world) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        partner = partners.create_partner(s, manager, "BADI", now=NOW)
        partners.set_fee(s, partner, 500, date(2026, 1, 1), manager, now=NOW)
        sales.create_sale(
            s, manager,
            SaleInput(
                tickets=[
                    TicketInput(
                        "3949999991",
                        "199",
                        1000,
                        "SYNTH/PART",
                        "ADT",
                        "ABCDEF",
                        "TKTT",
                    )
                ],
                ag_kind="partner", partner_id=partner.id, payment_method="credit",
                staff_sign="0004SV",
            ),
            day=date(2026, 10, 20), now=NOW,
        )  # fmt: skip
        s.commit()
    with world.manager() as c:
        world.upload(c)
        page = c.get(f"/days/{DAY}")
        assert "J'ai vu la liste" in html.unescape(page.text)
        assert f'action="/days/{DAY}/partner-list"' in page.text
        blocked = post(c, f"/days/{DAY}/close", f"/days/{DAY}")
        assert blocked.status_code == 409 and "J'ai vu la liste" in html.unescape(blocked.text)
        digest = re.search(r'name="digest" value="([0-9a-f]+)"', page.text)
        assert digest is not None
        assert "SYNTH" in page.text  # the listed ticket is shown on the card
        own = post(c, f"/days/{DAY}/partner-list", f"/days/{DAY}", digest=digest.group(1))
        assert own.status_code == 403  # the manager wrote the listed sale
    with accountant(world) as a:
        stale = post(a, f"/days/{DAY}/partner-list", f"/days/{DAY}", digest="0" * 16)
        assert stale.status_code == 400 and "La liste a changé" in stale.text
        seen = post(a, f"/days/{DAY}/partner-list", f"/days/{DAY}", digest=digest.group(1))
        assert seen.status_code == 303
        assert "J'ai vu la liste" not in html.unescape(a.get(f"/days/{DAY}").text)
        closed = post(a, f"/days/{DAY}/close", f"/days/{DAY}")
        assert closed.status_code == 303
    assert "partner_list_acknowledged" in audit_types(world)


def test_staff_cannot_acknowledge_the_partner_list(world: FlagWeb) -> None:
    world.sell_report()
    with world.manager() as c:
        world.upload(c)
    with world.staff() as c:
        denied = post(c, f"/days/{DAY}/partner-list", "/flags")
    assert denied.status_code == 403


def test_only_the_manager_reopens_a_closed_day(world: FlagWeb) -> None:
    world.sell_report()
    with world.manager() as c:
        world.upload(c)
    with accountant(world) as a:
        assert post(a, f"/days/{DAY}/close", f"/days/{DAY}").status_code == 303
        assert a.get(f"/days/{DAY}/reopen").status_code == 403
        assert (
            post(a, f"/days/{DAY}/reopen", f"/days/{DAY}", reason="Erreur de saisie").status_code
            == 403
        )
    with world.manager() as c:
        done = post(c, f"/days/{DAY}/reopen", f"/days/{DAY}", reason="Erreur de saisie")
        assert done.status_code == 303


def test_a_sale_changed_after_the_comparison_is_compared_again_at_the_close(world: FlagWeb) -> None:
    from app import sales
    from app.models import User
    from app.sales import SaleInput, TicketInput
    from app.tenancy import rebind_tenant
    from tests.flag_support import NOW

    world.sell_report()
    with world.manager() as c:
        world.upload(c)
    with tenant_session(world) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        sale_row = s.scalars(select(Sale).order_by(Sale.id)).first()
        assert sale_row is not None
        view = sales.get_sale_for(s, manager, sale_row.id)
        assert view is not None
        t = view.tickets[0]
        sales.edit_sale(
            s, manager, view.id,
            SaleInput(
                tickets=[
                    TicketInput(
                        t.doc_number,
                        t.airline,
                        t.total_doc + 5,
                        t.pax_name,
                        t.pax_type,
                        t.rloc,
                        t.trnc,
                    )
                ],
                ag_kind="direct", pb=view.pb, clt=view.clt, payment_method=view.payment_method,
                staff_sign=view.staff_sign,
            ),
            "Prix corrigé", now=NOW,
        )  # fmt: skip
        s.commit()
    with world.manager() as c:
        refused = post(c, f"/days/{DAY}/close", f"/days/{DAY}")
    assert refused.status_code == 409 and "1 écart bloquant reste à traiter." in refused.text
    assert [f.kind for f in world.flags_rows() if f.status == "open"] == ["different"]


# --- the plumbing every POST shares ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/flags/{flag}/answer",
        "/flags/{flag}/validate",
        "/flags/overrides/1/validate",
        "/days/2026-10-20/partner-list",
        "/days/2026-10-20/close",
    ],
)
def test_every_post_needs_a_csrf_token(world: FlagWeb, path: str) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    url = path.format(flag=flag.id)
    with world.manager() as c:
        none = c.post(url, data={"choice": "amadeus_right"}, follow_redirects=False)
        bad = c.post(url, data={"csrf_token": "nope"}, follow_redirects=False)
    assert none.status_code == 403 and bad.status_code == 403
    assert status_of(world, flag.id) == "open"


def test_the_writes_share_the_sale_write_rate_limit(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    monkeypatch.setattr(ratelimit, "SALE_WRITE_USER", ratelimit.Limit("sale_write_user", 1, 3600))
    with world.staff() as c:
        token = csrf_for(c, f"/flags?date={DAY}")
        first = c.post(
            f"/flags/{flag.id}/answer",
            data={"csrf_token": token, "choice": "mine_right", "reason": "ab"},
            follow_redirects=False,
        )
        second = c.post(
            f"/flags/{flag.id}/answer",
            data={"csrf_token": token, "choice": "mine_right", "reason": "Tarif négocié"},
            follow_redirects=False,
        )
    assert first.status_code == 400 and second.status_code == 429
    assert status_of(world, flag.id) == "open"


def test_ids_out_of_range_are_not_found(world: FlagWeb) -> None:
    with world.manager() as c:
        token = csrf_for(c, "/flags")
        huge = c.post(
            f"/flags/{2**70}/answer", data={"csrf_token": token, "choice": "amadeus_right"}
        )
        zero = c.get("/flags/0/enter")
    assert huge.status_code == 404 and zero.status_code == 404


def test_the_validation_screen_is_empty_when_nothing_waits(world: FlagWeb) -> None:
    with accountant(world) as a:
        page = a.get("/flags/validate")
    assert page.status_code == 200 and "Rien à valider" in page.text


def test_the_sidebar_link_and_badge_are_for_validators(world: FlagWeb) -> None:
    setup_diff(world, OWN[0])
    flag = flag_of(world, OWN[0])
    with world.staff() as c:
        post(
            c, f"/flags/{flag.id}/answer", f"/flags?date={DAY}",
            choice="mine_right", reason="Tarif négocié",
        )  # fmt: skip
        assert 'href="/flags/validate"' not in c.get("/").text
    with accountant(world) as a:
        home = a.get("/")
    assert 'href="/flags/validate"' in home.text
    match = re.search(r'href="/flags/validate".*?<span class="badge">(\d+)', home.text, re.DOTALL)
    assert match and match.group(1) == "1"


def test_the_day_page_does_not_compare_again_but_the_confirmation_does(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import flags

    setup_diff(world, OWN[0])
    calls: list[int] = []
    real = flags.is_current

    def spy(*args: Any, **kwargs: Any) -> bool:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(flags, "is_current", spy)
    with world.manager() as c:
        assert c.get(f"/days/{DAY}").status_code == 200
        assert calls == []  # a view of the day never runs the staleness comparison
        assert c.get(f"/days/{DAY}/close").status_code == 200
        assert calls != []
