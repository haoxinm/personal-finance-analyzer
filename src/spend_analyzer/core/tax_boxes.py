"""The allow-list of form boxes the LLM fallback may fill, and what each one means.

The model never picks a category: it reports ``(form_type, box, amount)`` and the category,
source form, label and memo flag come from this table, which mirrors exactly what the
deterministic parsers emit (`tests/core/test_tax_boxes.py` checks every golden item against it).
A box that is not in the table is rejected.

Keys: ``w2`` and the single-form 1099 types use the printed box number (``"1"``, ``"1a"``,
``"17"``); 1099-B uses the pseudo-boxes ``"ST"`` / ``"LT"`` (net short- / long-term gain or
loss); ``1099-consolidated`` prefixes the section: ``"INT:1"``, ``"DIV:1a"``, ``"B:LT"``.

Pure data; no I/O, no LLM import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from spend_analyzer.core.tax_types import FormType, IncomeCategory, TaxCategory

#: Form types the fallback may produce (never ``1040`` / ``state-return``: line-number driven).
FALLBACK_FORM_TYPES: Final[tuple[FormType, ...]] = (
    "w2",
    "1099-int",
    "1099-div",
    "1099-b",
    "1099-nec",
    "1099-misc",
    "1099-consolidated",
)


@dataclass(frozen=True, slots=True)
class BoxSpec:
    kind: Literal["income", "tax"]
    category: IncomeCategory | TaxCategory
    source_form: str  # as the parsers print it: "W-2", "1099-INT", ...
    label: str
    box: str  # the box as stored (consolidated keys store the bare box, e.g. "1a")
    is_memo: bool = False
    #: Tax items only: the box carries a state/locality (needs a jurisdiction).
    jurisdictional: bool = False


def _inc(
    category: IncomeCategory, source: str, box: str, label: str, *, memo: bool = False
) -> BoxSpec:
    return BoxSpec("income", category, source, label, box, is_memo=memo)


def _tax(
    category: TaxCategory, source: str, box: str, label: str, *, jurisdictional: bool = False
) -> BoxSpec:
    return BoxSpec("tax", category, source, label, box, jurisdictional=jurisdictional)


_FED = "Federal income tax withheld"


def _section(source: str, key: str) -> dict[str, BoxSpec]:
    """Boxes of one 1099 section, keyed for the consolidated form (``"INT:1"``)."""
    table = {
        "1099-INT": _INT,
        "1099-DIV": _DIV,
        "1099-B": _B,
        "1099-MISC": _MISC,
    }[source]
    out = {f"{key}:{box}": spec for box, spec in table.items()}
    if source == "1099-INT":
        # A consolidated statement prints the state withholding of every section as box 16.
        out.pop(f"{key}:17")
        out[f"{key}:16"] = _tax(
            "state_income_tax_withheld", source, "16", "State tax withheld", jurisdictional=True
        )
    return out


_W2: Final = {
    "1": _inc("salary_wages", "W-2", "1", "Wages, tips, other compensation"),
    "2": _tax("federal_income_tax_withheld", "W-2", "2", _FED),
    "4": _tax("social_security_tax", "W-2", "4", "Social security tax withheld"),
    "6": _tax("medicare_tax", "W-2", "6", "Medicare tax withheld"),
    "17": _tax("state_income_tax_withheld", "W-2", "17", "State income tax", jurisdictional=True),
    "19": _tax("local_income_tax_withheld", "W-2", "19", "Local income tax", jurisdictional=True),
    # Box 12 memos (already excluded from box 1). The box is stored as "12-" plus its code ("12-D"), not the a-d slot.
    **{
        f"12-{code}": _inc(
            "retirement_deferral", "W-2", f"12-{code}", f"Box 12 code {code} deferral", memo=True
        )
        for code in ("D", "E", "F", "G", "S")
    },
    "12-W": _inc("hsa_employer", "W-2", "12-W", "Box 12 code W employer HSA", memo=True),
}
_INT: Final = {
    "1": _inc("interest", "1099-INT", "1", "Interest income"),
    "3": _inc(
        "interest", "1099-INT", "3", "Interest on U.S. savings bonds and Treasury obligations"
    ),
    "8": _inc("other", "1099-INT", "8", "Tax-exempt interest", memo=True),
    "4": _tax("federal_income_tax_withheld", "1099-INT", "4", _FED),
    "17": _tax(
        "state_income_tax_withheld", "1099-INT", "17", "State tax withheld", jurisdictional=True
    ),
}
_DIV: Final = {
    "1a": _inc("ordinary_dividends", "1099-DIV", "1a", "Ordinary dividends"),
    "1b": _inc("qualified_dividends", "1099-DIV", "1b", "Qualified dividends", memo=True),
    "2a": _inc("long_term_capital_gain", "1099-DIV", "2a", "Capital gain distributions"),
    "12": _inc("other", "1099-DIV", "12", "Exempt-interest dividends", memo=True),
    "4": _tax("federal_income_tax_withheld", "1099-DIV", "4", _FED),
    "16": _tax(
        "state_income_tax_withheld", "1099-DIV", "16", "State tax withheld", jurisdictional=True
    ),
}
_B: Final = {
    "ST": _inc("short_term_capital_gain", "1099-B", "ST", "Net short-term gain or loss"),
    "LT": _inc("long_term_capital_gain", "1099-B", "LT", "Net long-term gain or loss"),
    "4": _tax("federal_income_tax_withheld", "1099-B", "4", _FED),
    "16": _tax(
        "state_income_tax_withheld",
        "1099-B",
        "16",
        "State income tax withheld",
        jurisdictional=True,
    ),
}
_NEC: Final = {
    "1": _inc("self_employment", "1099-NEC", "1", "Nonemployee compensation"),
    "4": _tax("federal_income_tax_withheld", "1099-NEC", "4", _FED),
    "5": _tax(
        "state_income_tax_withheld", "1099-NEC", "5", "State tax withheld", jurisdictional=True
    ),
}
_MISC: Final = {
    "1": _inc("other", "1099-MISC", "1", "Rents"),
    "2": _inc("other", "1099-MISC", "2", "Royalties"),
    "3": _inc("other", "1099-MISC", "3", "Other income"),
    "7": _inc("self_employment", "1099-MISC", "7", "Nonemployee compensation"),
    "4": _tax("federal_income_tax_withheld", "1099-MISC", "4", _FED),
    "16": _tax(
        "state_income_tax_withheld", "1099-MISC", "16", "State tax withheld", jurisdictional=True
    ),
}
_OID: Final = {
    "1": _inc("interest", "1099-OID", "1", "Original issue discount"),
    "8": _inc("interest", "1099-OID", "8", "OID on U.S. Treasury obligations"),
}

BOX_MAP: Final[dict[str, dict[str, BoxSpec]]] = {
    "w2": _W2,
    "1099-int": _INT,
    "1099-div": _DIV,
    "1099-b": _B,
    "1099-nec": _NEC,
    "1099-misc": _MISC,
    "1099-consolidated": {
        **_section("1099-INT", "INT"),
        **_section("1099-DIV", "DIV"),
        **_section("1099-B", "B"),
        **_section("1099-MISC", "MISC"),
        **{f"OID:{box}": spec for box, spec in _OID.items()},
    },
}

_BOX_WORD = re.compile(r"^box", re.IGNORECASE)
_WS = re.compile(r"\s+")
_BOX_12_CODE = re.compile(r"^12-?([a-z]{1,2})$", re.IGNORECASE)


def normalize_box(raw: str) -> str:
    """``"Box 1A"`` -> ``"1a"``; ``"int : 1"`` -> ``"INT:1"``; ``"st"`` -> ``"ST"``; ``"12 d"`` -> ``"12-D"`` (W-2 box 12 code)."""
    text = _BOX_WORD.sub("", _WS.sub("", raw))
    code = _BOX_12_CODE.match(text)
    if code:
        return f"12-{code.group(1).upper()}"
    section, sep, box = text.rpartition(":")
    box = box.upper() if box.upper() in ("ST", "LT") else box.lower()
    return f"{section.upper()}:{box}" if sep else box


def lookup(form_type: str, box: str) -> BoxSpec | None:
    """The spec for ``(form_type, box)`` or `None` when it is not on the allow-list."""
    return BOX_MAP.get(form_type, {}).get(normalize_box(box))
