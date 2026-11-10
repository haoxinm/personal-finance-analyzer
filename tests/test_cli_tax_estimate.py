"""`pfa tax estimate` and `tax params ...` (no network: the update check is faked)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from personal_finance_analyzer.api.services import tax_params_update as upd
from personal_finance_analyzer.cli import app
from personal_finance_analyzer.core import tax_params
from tests.ingest.tax_support import generated_pdf
from tests.tax_params_support import with_next_year

runner = CliRunner()


@pytest.fixture
def migrated(home: Path) -> Path:
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    return home


def _bundled_text() -> str:
    return (
        resources.files("personal_finance_analyzer.core")
        .joinpath("tax_data", "federal_params.yaml")
        .read_text(encoding="utf-8")
    )


def test_estimate_prints_the_breakdown_from_an_imported_w2(migrated: Path) -> None:
    imported = runner.invoke(app, ["tax", "import", str(generated_pdf("tax_w2", "normal"))])
    assert imported.exit_code == 0, imported.output
    result = runner.invoke(app, ["tax", "estimate", "--year", "2024"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Federal tax estimate 2024" in out
    assert "filing status single, from default" in out
    assert "Wages and salary" in out and "85432.10" in out  # the fixture W-2's box 1
    assert "Taxable income" in out and "Total tax" in out
    assert "Retirement plan deferrals" in out and "3200.00" in out  # box 12 D memo
    assert "Employer HSA contributions" in out and "1250.00" in out
    assert "Not modeled:" in out and "Alternative minimum tax" in out
    assert "warning: No tax profile" in out
    assert "Pinecrest" not in out  # no payer names in an estimate


def test_estimate_unsupported_year_and_unknown_user(migrated: Path) -> None:
    unsupported = runner.invoke(app, ["tax", "estimate", "--year", "2020"])
    assert unsupported.exit_code == 1 and "No federal tax parameters for 2020" in unsupported.output
    missing = runner.invoke(app, ["tax", "estimate", "--year", "2024", "--user-id", "999"])
    assert missing.exit_code == 1 and "no user with id 999" in missing.output
    assert runner.invoke(app, ["tax", "estimate", "--year", "1800"]).exit_code != 0
    assert runner.invoke(app, ["tax", "estimate"]).exit_code != 0  # --year is required


def test_params_show(migrated: Path) -> None:
    out = runner.invoke(app, ["tax", "params", "show"]).output
    assert "Tax parameters in use: version" in out and "(bundled)" in out
    assert "Years: 2022, 2023, 2024, 2025, 2026" in out
    assert "Last update check: never" in out and "automatic yearly check: off" in out


def test_params_update_runs_one_check_and_reports(
    migrated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = with_next_year(tax_params.load_bundled().version + 1)
    monkeypatch.setattr(upd, "fetch_text", lambda url: yaml.safe_dump(raw))
    result = runner.invoke(app, ["tax", "params", "update"])
    assert result.exit_code == 0, result.output
    assert "updated: Added tax year(s) 2027." in result.output
    assert "(cached)" in result.output and "(updated)" in result.output

    def down(url: str) -> str:
        raise upd.FetchError("ConnectError")

    monkeypatch.setattr(upd, "fetch_text", down)
    failed = runner.invoke(app, ["tax", "params", "update"])
    assert failed.exit_code == 1 and "unreachable" in failed.output


def test_params_import_and_clear(migrated: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(_bundled_text())
    raw["params_version"] = 1
    raw["years"][2026]["ss_wage_base"] = 185_000
    good = tmp_path / "mine.yaml"
    good.write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = runner.invoke(app, ["tax", "params", "import", str(good)])
    assert result.exit_code == 0, result.output
    assert "(override)" in result.output
    params = tax_params.resolve().years[2026]
    assert params.ss_wage_base_minor == 185_000 * 100
    assert (
        "Removed the imported parameter file"
        in runner.invoke(app, ["tax", "params", "clear"]).output
    )
    assert "No imported parameter file" in runner.invoke(app, ["tax", "params", "clear"]).output
    assert tax_params.resolve().source == "bundled"


def test_params_import_rejects_bad_input(migrated: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("params_version: 5\nyears: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["tax", "params", "import", str(bad)])
    assert result.exit_code == 1 and "not a valid parameter file" in result.output
    assert (
        runner.invoke(app, ["tax", "params", "import", str(tmp_path / "none.yaml")]).exit_code == 1
    )
    assert tax_params.resolve().source == "bundled"
