"""Answering comparison flags, validating, and the gate in front of closing a day (B9).

Decision 18 (docs/design/02-open-decisions.md): the agent who issued the ticket answers; a change of
cost, ticket number, ticket type, sign or passenger type also needs the accountant or the manager,
and **nobody validates their own answer**. A void of an entry always needs a validator.

The parts, all importable from here:

* ``answer``: ``answer_flag``, ``validate_flag``, ``decisions_for``, ``prefill_for_not_sent``.
* ``overrides``: one-off partner fees and the validators' queue and counts.
* ``gate``: the partner list, ``can_close`` / ``close_day`` and the read-only ``gate_status``.
* ``corrections``: ``corrections_for``, what the TJQ APRES (B10) shows as corrected.
* ``common``: errors, messages, and the helpers the others share.

After a decision that edits or voids a sale the comparison of the day is run again, so the flags
stay true to the sales. Audit events carry ids only: ``flag_answered``, ``flag_validated``,
``flag_rejected``, ``override_validated``, ``partner_list_acknowledged``; a refusal is
``permission_denied``. Functions never commit (a refusal is the usual exception: its audit row is
committed before the error); the caller rolls back on any other error, except after
``SituationChanged`` and ``CloseBlocked``, where it commits first (the comparison was run again).
"""

from app.approvals.answer import (
    answer_flag,
    decisions_for,
    prefill_for_not_sent,
    validate_flag,
)
from app.approvals.common import (
    AMADEUS_REASON,
    SALE_FIELDS,
    SALE_MOVED_ON,
    AnswerResult,
    ApprovalError,
    ApprovalForbidden,
    ApprovalNotFound,
    CloseBlocked,
    NotSentPrefill,
    SituationChanged,
    ValidateResult,
    ValidationQueue,
    claim,
)
from app.approvals.corrections import COLUMN_TITLES, corrections_for
from app.approvals.gate import (
    GATE_LIST,
    GATE_NO_REPORT,
    GateStatus,
    PartnerList,
    acknowledge_partner_list,
    can_close,
    close_day,
    gate_status,
    partner_digest,
    partner_list,
    partner_list_seen,
    require_can_close,
    to_answer_count,
)
from app.approvals.overrides import (
    pending_override_count,
    to_validate_count,
    to_validate_query,
    validate_override,
    validation_queue,
)

__all__ = [
    "AMADEUS_REASON",
    "COLUMN_TITLES",
    "GATE_LIST",
    "GATE_NO_REPORT",
    "SALE_FIELDS",
    "SALE_MOVED_ON",
    "AnswerResult",
    "ApprovalError",
    "ApprovalForbidden",
    "ApprovalNotFound",
    "CloseBlocked",
    "GateStatus",
    "NotSentPrefill",
    "PartnerList",
    "SituationChanged",
    "ValidateResult",
    "ValidationQueue",
    "acknowledge_partner_list",
    "answer_flag",
    "can_close",
    "claim",
    "close_day",
    "corrections_for",
    "decisions_for",
    "gate_status",
    "partner_digest",
    "partner_list",
    "partner_list_seen",
    "pending_override_count",
    "prefill_for_not_sent",
    "require_can_close",
    "to_answer_count",
    "to_validate_count",
    "to_validate_query",
    "validate_flag",
    "validate_override",
    "validation_queue",
]
