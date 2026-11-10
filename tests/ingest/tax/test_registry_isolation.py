"""The tax registry and the bank-statement registry never see each other's parsers."""

from __future__ import annotations

from personal_finance_analyzer.ingest import registry as bank_registry
from personal_finance_analyzer.ingest.tax import registry as tax_registry


def test_bank_discovery_contains_no_tax_parser() -> None:
    bank = bank_registry.discover_all_parsers()
    assert all(not hasattr(p, "form_types") for p in bank)
    tax_ids = {p.id for p in tax_registry.discover_tax_parsers()}
    assert tax_ids.isdisjoint({p.id for p in bank})


def test_tax_discovery_contains_no_bank_parser() -> None:
    for parser in tax_registry.discover_tax_parsers():
        assert hasattr(parser, "form_types")
        assert not hasattr(parser, "account_type")
