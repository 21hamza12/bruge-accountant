"""Airline names for the IATA numeric ticket prefixes (the first three digits of a ticket number).

Only prefixes that are certain are listed. Anything else renders as ``Cie 495`` so the code stays
visible and nobody is shown a wrong airline.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

AIRLINES: Mapping[str, str] = MappingProxyType(
    {
        "147": "Royal Air Maroc",
        "157": "Qatar Airways",
        "199": "Tunisair",
        "235": "Turkish Airlines",
        "057": "Air France",
        "074": "KLM",
        "125": "British Airways",
        "071": "Ethiopian Airlines",
        "065": "Saudia",
        "176": "Emirates",
        "607": "Etihad Airways",
        "077": "EgyptAir",
        "124": "Air Algérie",
        "082": "Brussels Airlines",
        "220": "Lufthansa",
        "047": "TAP Air Portugal",
        "075": "Iberia",
    }
)


def airline_name(code: str) -> str:
    """The airline for a numeric prefix, or ``Cie <code>`` when it is not in the table."""
    code = code.strip()
    if not code:
        return "Cie inconnue"
    return AIRLINES.get(code, f"Cie {code}")
