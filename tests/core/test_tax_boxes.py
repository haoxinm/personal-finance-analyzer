"""`core/tax_boxes.BOX_MAP` must describe exactly what the deterministic parsers emit."""

from __future__ import annotations

import json

import pytest

from personal_finance_analyzer.core.tax_boxes import (
    BOX_MAP,
    FALLBACK_FORM_TYPES,
    lookup,
    normalize_box,
)
from tests.ingest.tax_support import GOLDEN_DIR

_GOLDENS = sorted(GOLDEN_DIR.glob("*/*.json"))


def _key(form_type: str, source_form: str, box: str | None) -> str | None:
    if box is None:
        return None
    if form_type == "1099-consolidated":
        return f"{source_form.removeprefix('1099-')}:{box}"
    return box


def test_goldens_exist() -> None:
    assert len(_GOLDENS) > 20


@pytest.mark.parametrize("path", _GOLDENS, ids=lambda p: p.stem)
def test_every_parser_item_is_in_the_box_map(path) -> None:  # type: ignore[no-untyped-def]
    data = json.loads(path.read_text())
    for form in data.get("forms", []):
        ftype = form["form_type"]
        if ftype not in FALLBACK_FORM_TYPES:
            continue
        for kind, items in (("income", form["income"]), ("tax", form["taxes"])):
            for item in items:
                key = _key(ftype, item["source_form"], item["box"])
                if key is None:
                    continue  # e.g. ordinary 1099-B gain: has no box
                spec = BOX_MAP[ftype].get(key)
                assert spec is not None, (path.stem, ftype, key)
                assert (spec.kind, spec.category, spec.source_form) == (
                    kind,
                    item["category"],
                    item["source_form"],
                )
                if kind == "income":
                    assert spec.is_memo == item["is_memo"]


def test_lookup_normalises_the_box() -> None:
    assert lookup("1099-div", "Box 1A") is not None
    assert normalize_box("int : 1") == "INT:1"
    assert lookup("1099-consolidated", "div:1a") is not None
    assert lookup("1099-b", "lt") is not None
    assert lookup("w2", "99") is None
    assert lookup("1040", "1") is None
