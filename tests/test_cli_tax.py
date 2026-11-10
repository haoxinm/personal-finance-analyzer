"""`spend-analyzer tax ...` and the tax-form hint in `import`."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spend_analyzer.cli import app
from spend_analyzer.db.models import TaxDocument
from spend_analyzer.db.session import make_engine_for_path
from tests.ingest.tax_support import generated_pdf

runner = CliRunner()

_SSN_SHAPE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


@pytest.fixture
def migrated(home: Path) -> Path:
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    return home


def _pdf(layout: str, variant: str) -> str:
    return str(generated_pdf(layout, variant))


def _csv_rows(output: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(output)))


def test_import_each_form_type(migrated: Path) -> None:
    result = runner.invoke(
        app,
        [
            "tax",
            "import",
            _pdf("tax_w2", "normal"),
            _pdf("tax_1099_div", "normal"),
            _pdf("tax_1040", "normal_wages_only"),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert "tax document #1 -> parsed (2024: w2) 3 income / 5 tax item(s)" in lines[0]
    assert "(2024: 1099-div)" in lines[1]
    assert "(2024: 1040)" in lines[2]
    assert not _SSN_SHAPE.search(result.output)


def test_reimport_is_a_noop(migrated: Path) -> None:
    pdf = _pdf("tax_w2", "normal")
    assert runner.invoke(app, ["tax", "import", pdf]).exit_code == 0
    again = runner.invoke(app, ["tax", "import", pdf])
    assert again.exit_code == 0
    assert "tax document #1 -> duplicate" in again.output
    listing = runner.invoke(app, ["tax", "list"])
    assert listing.output.count("parsed") == 1


def test_non_tax_pdf_is_an_error(migrated: Path) -> None:
    result = runner.invoke(app, ["tax", "import", _pdf("layout_d_bank", "normal")])
    assert result.exit_code == 1
    assert "unsupported" in result.output
    assert "doesn't look like a supported tax form" in result.output


def test_missing_and_non_pdf_files(migrated: Path, tmp_path: Path) -> None:
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"hello")
    result = runner.invoke(app, ["tax", "import", str(tmp_path / "nope.pdf"), str(fake)])
    assert result.exit_code == 1
    assert "no such file" in result.output
    assert "not a PDF file" in result.output


def test_tax_year_override(migrated: Path) -> None:
    pdf = _pdf("tax_w2", "no_year")
    failed = runner.invoke(app, ["tax", "import", pdf])
    assert failed.exit_code == 1
    ok = runner.invoke(app, ["tax", "import", pdf, "--tax-year", "2023"])
    assert ok.exit_code == 0, ok.output
    assert "(2023: w2)" in ok.output
    assert runner.invoke(app, ["tax", "import", pdf, "--tax-year", "1500"]).exit_code == 2


def test_report_csv(migrated: Path) -> None:
    runner.invoke(app, ["tax", "import", _pdf("tax_w2", "two_employers")])
    runner.invoke(app, ["tax", "import", _pdf("tax_1099_div", "normal")])
    result = runner.invoke(app, ["tax", "report", "--csv"])
    assert result.exit_code == 0, result.output
    rows = _csv_rows(result.output)
    assert rows[0] == ["tax_year", "section", "category", "amount_minor"]
    table = {(r[0], r[1], r[2]): int(r[3]) for r in rows[1:]}
    assert table[("2024", "income", "salary_wages")] == 8543210 + 2431050
    assert table[("2024", "income", "ordinary_dividends")] == 250000
    assert table[("2024", "income", "long_term_capital_gain")] == 64000
    assert table[("2024", "income", "total_income")] == 8543210 + 2431050 + 250000 + 64000
    assert table[("2024", "memo", "qualified_dividends")] == 180000
    assert table[("2024", "withheld", "federal_income_tax_withheld")] == 1187640 + 210400 + 25000
    assert ("2024", "fica", "medicare_tax") in table
    assert not any(r[1] == "liability" for r in rows[1:])  # no return imported


def test_report_text_year_filter_and_return(migrated: Path) -> None:
    runner.invoke(app, ["tax", "import", _pdf("tax_1040", "normal_wages_only")])
    result = runner.invoke(app, ["tax", "report", "--year", "2024"])
    assert result.exit_code == 0, result.output
    assert "Tax year 2024" in result.output
    assert "Total federal tax" in result.output
    assert "10547.00" in result.output
    assert "Refund" in result.output and "812.00" in result.output
    assert "Federal effective rate" in result.output
    empty = runner.invoke(app, ["tax", "report", "--year", "2019"])
    assert empty.exit_code == 0
    assert "No tax data" in empty.output


def test_user_scoping(migrated: Path) -> None:
    runner.invoke(app, ["users", "add", "bob"])
    runner.invoke(app, ["tax", "import", _pdf("tax_w2", "normal")])  # default user
    runner.invoke(app, ["tax", "import", _pdf("tax_1099_int", "normal"), "--user-id", "2"])

    default = _csv_rows(runner.invoke(app, ["tax", "report", "--csv"]).output)
    table = {(r[1], r[2]): int(r[3]) for r in default[1:]}
    assert table[("income", "interest")] == 0
    bob = _csv_rows(runner.invoke(app, ["tax", "report", "--csv", "--user-id", "2"]).output)
    assert {(r[1], r[2]): int(r[3]) for r in bob[1:]}[("income", "interest")] == 123456 + 24580
    everyone = _csv_rows(runner.invoke(app, ["tax", "report", "--csv", "--all-users"]).output)
    every = {(r[1], r[2]): int(r[3]) for r in everyone[1:]}
    assert every[("income", "salary_wages")] == 8543210
    assert every[("income", "interest")] == 123456 + 24580

    assert runner.invoke(app, ["tax", "report", "--user-id", "99"]).exit_code == 1
    assert (
        runner.invoke(app, ["tax", "import", _pdf("tax_w2", "normal"), "--user-id", "99"]).exit_code
        == 1
    )
    only_default = runner.invoke(app, ["tax", "list"]).output
    all_users = runner.invoke(app, ["tax", "list", "--all-users"]).output
    assert len(all_users.splitlines()) == len(only_default.splitlines()) + 1


def test_list_and_delete(migrated: Path) -> None:
    assert "No tax documents." in runner.invoke(app, ["tax", "list"]).output
    runner.invoke(app, ["tax", "import", _pdf("tax_w2", "two_employers")])
    listing = runner.invoke(app, ["tax", "list"])
    assert "w2, w2" in listing.output and "2024" in listing.output
    assert "No tax documents." in runner.invoke(app, ["tax", "list", "--year", "2020"]).output

    deleted = runner.invoke(app, ["tax", "delete", "1"])
    assert deleted.exit_code == 0, deleted.output
    assert "No tax documents." in runner.invoke(app, ["tax", "list"]).output
    assert "No tax data" in runner.invoke(app, ["tax", "report"]).output
    engine = make_engine_for_path(migrated / "spend.db")
    try:
        from sqlalchemy.orm import Session

        with Session(engine) as s:
            assert s.query(TaxDocument).count() == 0
    finally:
        engine.dispose()

    missing = runner.invoke(app, ["tax", "delete", "1"])
    assert missing.exit_code == 1
    assert "no tax document with id 1" in missing.output


def test_statement_import_hints_at_tax_import(migrated: Path) -> None:
    result = runner.invoke(app, ["import", _pdf("tax_w2", "normal")])
    assert "looks like a tax form (w2)" in result.output
    assert "spend-analyzer tax import" in result.output


def test_statement_import_has_no_hint_for_a_real_statement(migrated: Path) -> None:
    pdf = str(
        Path(__file__).resolve().parent
        / "fixtures/generated/layout_d_bank/layout_d_bank_normal.pdf"
    )
    result = runner.invoke(app, ["import", pdf])
    assert "tax form" not in result.output


def test_import_scrubs_ssn_shape_in_file_name(migrated: Path, tmp_path: Path) -> None:
    named = tmp_path / "W2 123-45-6789.pdf"
    named.write_bytes(Path(_pdf("tax_w2", "normal")).read_bytes())
    result = runner.invoke(app, ["tax", "import", str(named)])
    assert result.exit_code == 0, result.output
    engine = make_engine_for_path(migrated / "spend.db")
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        (doc,) = session.query(TaxDocument).all()
        assert "123-45" not in doc.original_name
        assert "6789" in doc.original_name
    listed = runner.invoke(app, ["tax", "list"])
    assert "123-45" not in listed.output
