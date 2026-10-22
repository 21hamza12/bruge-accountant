"""app/web/sale_forms.py: reading a posted form, mapping it to the service's input, and the
wording of a sale's history. Pure functions; the pages are tested in test_sale_pages.py."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from starlette.datastructures import FormData, UploadFile

from app.models import SaleVersion
from app.sales import MAX_TICKETS_PER_SALE, FieldError
from app.web import sale_forms
from app.web.sale_forms import SaleForm, TicketForm


def posted(*pairs: tuple[str, Any]) -> FormData:
    return FormData(list(pairs))


def row(n: int = 1, **kw: str) -> list[tuple[str, str]]:
    values = {
        "doc_number": f"394000000{n}",
        "airline": "199",
        "total_doc": "1000",
        "pax_name": "SYNTH01/PAX",
        "pax_type": "ADT",
        "rloc": "ABC123",
        "trnc": "TKTT",
        **kw,
    }
    return list(values.items())


# --- reading -------------------------------------------------------------------------------


def test_a_form_with_nothing_in_it_is_one_empty_row_and_direct() -> None:
    form = sale_forms.read(posted())
    assert len(form.tickets) == 1 and form.tickets[0].is_blank
    assert form.ag == "direct" and form.client_refused_phone is False


def test_ticket_rows_are_read_in_order_and_missing_cells_are_empty() -> None:
    form = sale_forms.read(posted(*row(1), *row(2)))
    assert [t.doc_number for t in form.tickets] == ["3940000001", "3940000002"]
    short = sale_forms.read(posted(("doc_number", "1"), ("doc_number", "2"), ("airline", "TU")))
    assert [t.airline for t in short.tickets] == ["TU", ""]


def test_values_are_clipped_and_rows_are_capped_one_over_the_limit() -> None:
    form = sale_forms.read(posted(("clt", "x" * 5000), *[p for n in range(30) for p in row(n)]))
    assert len(form.clt) == sale_forms.CLIP
    assert len(form.tickets) == MAX_TICKETS_PER_SALE + 1


def test_non_text_values_become_empty() -> None:
    form = sale_forms.read(
        FormData([("clt", UploadFile(io.BytesIO(b"x"), filename="x"))])
    )  # an uploaded file would look like this
    assert form.clt == ""


@pytest.mark.parametrize(
    ("raw", "expected"), [("1", True), ("on", True), ("TRUE", True), ("0", False), ("", False)]
)
def test_the_refusal_checkbox(raw: str, expected: bool) -> None:
    assert sale_forms.read(posted(("client_refused_phone", raw))).client_refused_phone is expected


def test_with_extra_row_stops_at_the_cap() -> None:
    form = SaleForm(tickets=[TicketForm() for _ in range(19)])
    assert len(sale_forms.with_extra_row(form).tickets) == 20
    assert len(sale_forms.with_extra_row(form).tickets) == 20


def test_hidden_pairs_carry_every_value_but_no_secret() -> None:
    form = sale_forms.read(
        posted(*row(1), ("clt", "A"), ("client_refused_phone", "1"), ("csrf_token", "t"))
    )
    names = [name for name, _ in form.hidden_pairs]
    assert "client_refused_phone" in names and "csrf_token" not in names
    assert names.count("doc_number") == 1


# --- parsing single fields ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("direct", ("direct", None)),
        (" 12 ", ("partner", 12)),
        ("0", ("?", None)),
        ("9" * 30, ("?", None)),
        ("9223372036854775808", ("?", None)),
        ("abc", ("?", None)),
        ("", ("?", None)),
    ],
)
def test_parse_ag(raw: str, expected: tuple[str, int | None]) -> None:
    assert sale_forms.parse_ag(raw) == expected


@pytest.mark.parametrize(
    ("raw", "day", "bad"),
    [
        ("", None, False),
        ("2026-10-18", date(2026, 10, 18), False),
        ("2026-02-30", None, True),
        ("18/10/2026", None, True),
        ("20261018", None, True),
    ],
)
def test_parse_day(raw: str, day: date | None, bad: bool) -> None:
    found, problem = sale_forms.parse_day(raw)
    assert found == day and (problem is not None) is bad


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3", 3), (" 7 ", 7), ("", None), ("0", None), ("-1", None), ("x", None), ("9" * 30, None)],
)
def test_parse_version(raw: str, expected: int | None) -> None:
    assert sale_forms.parse_version(raw) == expected


# --- to the service -------------------------------------------------------------------------


def test_blank_rows_are_dropped_and_the_mapping_remembers_the_rows_kept() -> None:
    form = sale_forms.read(
        posted(
            *row(1), *row(2, doc_number="", airline="", total_doc="", pax_name="", rloc=""), *row(3)
        )
    )
    built = sale_forms.to_input(form, choose_day=False)
    assert [t.doc_number for t in built.data.tickets] == ["3940000001", "3940000003"]
    assert built.rows == [0, 2]


def test_staff_cannot_choose_a_day_and_a_bad_day_is_a_problem_for_managers() -> None:
    form = sale_forms.read(posted(*row(1), ("day", "nonsense")))
    assert sale_forms.to_input(form, choose_day=False).problems == []
    problems = sale_forms.to_input(form, choose_day=True).problems
    assert [p.field for p in problems] == ["day"]
    good = sale_forms.read(posted(*row(1), ("day", "2026-10-01")))
    assert sale_forms.to_input(good, choose_day=True).day == date(2026, 10, 1)


def test_blank_optional_fields_become_none_and_a_partner_id_is_an_int() -> None:
    form = sale_forms.read(
        posted(*row(1), ("ag", "5"), ("pb", " "), ("ap", ""), ("fee_override", " "))
    )
    data = sale_forms.to_input(form, choose_day=False).data
    assert (data.partner_id, data.pb, data.ap, data.fee_override, data.staff_sign) == (
        5,
        None,
        None,
        None,
        None,
    )


def test_error_map_points_at_the_row_where_it_was_typed() -> None:
    errors = [
        FieldError("tickets[1].total_doc", "Montant invalide."),
        FieldError("tickets[0].rloc", "RLOC."),
        FieldError("tickets", "Ajoutez au moins un billet."),
        FieldError("partner_id", "Choisissez un partenaire."),
        FieldError("ag_kind", "Type invalide."),
        FieldError("clt", "Nom."),
        FieldError("clt", "Encore."),
        FieldError("tickets[5].rloc", "hors liste"),
    ]
    found = sale_forms.error_map(errors, [0, 3])
    assert found["t3.total_doc"] == "Montant invalide." and found["t0.rloc"] == "RLOC."
    assert found["tickets"] == "Ajoutez au moins un billet."
    assert found["ag"] == "Choisissez un partenaire. Type invalide."
    assert found["clt"] == "Nom. Encore." and found["t5.rloc"] == "hors liste"


# --- the history ------------------------------------------------------------------------------


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def version(n: int = 1, **kw: Any) -> SaleVersion:
    values: dict[str, Any] = {
        "version_no": n,
        "report_day_id": 1,
        "staff_sign": "0004SV",
        "ag_kind": "direct",
        "partner_id": None,
        "ag_label": "BRUGE",
        "fee_applied": None,
        "pb": Decimal("24000"),
        "cost_total": Decimal("21368"),
        "clt": "Client",
        "ap": None,
        "client_refused_phone": False,
        "payment_method": "cash",
        "voided": False,
        "tickets": [
            {
                "doc_number": "3940000001",
                "airline": "199",
                "total_doc": "21368.00",
                "pax_name": "SYNTH01/PAX",
                "pax_type": "ADT",
                "rloc": "ABC123",
                "trnc": "TKTT",
                "position": 1,
            }
        ],
        "created_at": NOW,
    }
    values.update(kw)
    return SaleVersion(**values)


def test_the_first_version_is_a_creation_and_a_void_says_so() -> None:
    assert sale_forms.changes(None, version()) == ["Création de la vente"]
    assert sale_forms.changes(version(), version(2, voided=True)) == ["Vente annulée"]


def test_a_changed_price_names_both_amounts() -> None:
    found = sale_forms.changes(version(), version(2, pb=Decimal("25000")))
    assert found == ["P.B : 24000 → 25000"]


def test_changes_to_a_client_are_said_not_shown() -> None:
    found = sale_forms.changes(
        version(), version(2, clt="Autre Nom", ap="+22222334455", payment_method="bankily")
    )
    assert "Nom du client modifié" in found and "Numéro du client modifié" in found
    assert "Paiement : Espèces → Bankily" in found
    assert not any("Autre Nom" in line or "22334455" in line for line in found)


def test_ticket_changes_name_the_numbers() -> None:
    second = {
        "doc_number": "3940000002",
        "airline": "SV",
        "total_doc": "5000.00",
        "pax_name": "SYNTH01/PAX",
        "pax_type": "ADT",
        "rloc": "ABC123",
        "trnc": "TKTT",
        "position": 2,
    }
    first = version().tickets[0]
    added = sale_forms.changes(
        version(), version(2, tickets=[first, second], cost_total=Decimal("26368"))
    )
    assert "Billet ajouté : 3940000002" in added and "Coût : 21368 → 26368" in added
    removed = sale_forms.changes(version(tickets=[first, second]), version(2, tickets=[first]))
    assert "Billet retiré : 3940000002" in removed
    edited = sale_forms.changes(version(), version(2, tickets=[{**first, "airline": "TU"}]))
    assert "Billet modifié : 3940000001" in edited


def test_other_changes_and_the_no_visible_change_case() -> None:
    found = sale_forms.changes(
        version(),
        version(
            2,
            staff_sign="0006AC",
            report_day_id=2,
            ag_kind="partner",
            partner_id=3,
            ag_label="BADI",
            fee_applied=Decimal("500"),
        ),
    )
    assert "Journée modifiée" in found and "Signe : 0004SV → 0006AC" in found
    assert "AG : Direct → BADI" in found and "Frais du partenaire modifiés" in found
    assert sale_forms.changes(version(), version(2)) == ["Aucun champ modifié"]


# --- the error summary ---------------------------------------------------------------------


def test_field_ids_match_the_inputs_of_the_form() -> None:
    assert sale_forms.field_id("t0.doc_number") == "ticket-1"
    assert sale_forms.field_id("t2.total_doc") == "total_doc_2"
    assert sale_forms.field_id("tickets") == "tickets-title"
    assert sale_forms.field_id("clt") == "clt"


def test_the_summary_lists_ticket_rows_first_then_the_other_fields_in_form_order() -> None:
    errors = {
        "payment_method": "Choisissez.",
        "t1.rloc": "RLOC.",
        "clt": "Nom.",
        "t0.doc_number": "Numero.",
        "tickets": "Ajoutez.",
        "zzz": "Autre.",
    }
    found = sale_forms.error_summary(errors)
    assert [href for href, _ in found] == [
        "#ticket-1",
        "#rloc_1",
        "#tickets-title",
        "#clt",
        "#payment_method",
        "#zzz",
    ]
    assert found[0][1] == "Billet 1, Numéro du billet : Numero."
    assert found[3][1] == "CLT : Nom."
