from __future__ import annotations

import csv
import io
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_finance_analyzer import cli
from personal_finance_analyzer.cli import app
from personal_finance_analyzer.db.session import make_engine_for_path

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _plain(text: str) -> str:
    """Drop ANSI escapes, so help-text assertions hold even if a runner forces colour."""
    return _ANSI.sub("", text)


def _run_entry_point(
    entry: Callable[[], None], argv0: str, capsys: pytest.CaptureFixture[str]
) -> tuple[str, str]:
    sys.argv = [argv0, "--help"]
    with pytest.raises(SystemExit) as exit_info:
        entry()
    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    return _plain(captured.out), _plain(captured.err)


def test_pfa_entry_point_documents_pfa_and_prints_no_notice(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", sys.argv)
    out, err = _run_entry_point(cli.main, "personal-finance-analyzer", capsys)
    assert "Usage: pfa" in out
    assert "Personal Finance Analyzer" in out
    assert err == ""


def test_spend_analyzer_alias_prints_one_stderr_notice_then_runs_the_same_app(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", sys.argv)
    out, err = _run_entry_point(cli.legacy_main, "spend-analyzer", capsys)
    assert err.strip().splitlines() == [
        "spend-analyzer has been renamed to pfa; this alias will be removed in a future release"
    ]
    assert "Usage: pfa" in out  # stdout is the normal help, so scripts parsing it keep working


def test_console_scripts_are_declared() -> None:
    import tomllib

    pyproject = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    )
    assert pyproject["project"]["name"] == "personal-finance-analyzer"
    assert pyproject["project"]["scripts"] == {
        "pfa": "personal_finance_analyzer.cli:main",
        "personal-finance-analyzer": "personal_finance_analyzer.cli:main",
        "spend-analyzer": "personal_finance_analyzer.cli:legacy_main",
    }


#: The generated fixtures Gate 2 walks (§ tests/fixtures/generated/**): each layout's own
#: fictional bank name (found by `uv run python -c "extract(...)..."` against page 1's text)
#: is registered as an `Issuer` before importing, so `issuer_match.match_issuer` matches it
#: confidently by name (A24) and `import` auto-confirms with no manual --issuer-id step (A33).
_GATE2_FIXTURES = (
    ("layout_a_credit", "credit", "Fixture Bank"),
    ("layout_b_credit", "credit", "CASCADE TRUST BANK"),
    ("layout_c_credit", "credit", "Fabrikam Bank"),
    ("layout_d_bank", "checking", "Lakeshore Community Bank"),
)


def _expected_report_total() -> int:
    """Hand-computed grand total for every `_GATE2_FIXTURES` 'normal' golden: the same
    purchase/fee/interest/refund set the report's default `kinds` filter uses (A7), with each
    transaction's `Kind` resolved exactly as the ingest pipeline resolves it at import time
    (`classify.kinds.resolve_kind`, called with each golden's own `kind_hint`/description/amount/
    account_type) — payments, transfers, and the one `adjustment` row are excluded, and the one
    `refund` row nets in with its negative sign (I11/A7)."""
    from personal_finance_analyzer.classify.kinds import resolve_kind

    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "generated"
    total = 0
    for layout_dir, account_type, _issuer_name in _GATE2_FIXTURES:
        golden = json.loads((fixtures_dir / layout_dir / f"{layout_dir}_normal.json").read_text())
        parsed = golden.get("parsed", golden)  # layout_c's golden wraps success under "parsed"
        for txn in parsed["transactions"]:
            kind = resolve_kind(
                kind_hint=txn["kind_hint"],
                description_clean=txn["description"],
                amount_minor=txn["amount_minor"],
                account_type=account_type,
            )
            if kind in ("purchase", "fee", "interest", "refund"):
                total += txn["amount_minor"]
    return total


def test_migrate_creates_valid_db(home: Path) -> None:
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output

    db_file = home / "finance.db"
    assert db_file.exists()

    engine = make_engine_for_path(db_file)
    try:
        with engine.connect() as conn:
            names = {
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    finally:
        engine.dispose()
    assert {"users", "accounts", "transactions", "categories"} <= names


def test_migrate_creates_a_default_user_on_a_fresh_home(home: Path) -> None:
    """Gate 2 regression: a fresh `migrate` must leave a usable default user behind so `import`
    (and friends) have someone to attach statements to without requiring `--user-id` (D4)."""
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output

    list_result = runner.invoke(app, ["users", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "(default)" in list_result.output

    # Idempotent: migrating an already-migrated home never creates a second default user.
    runner.invoke(app, ["migrate"])
    second_list = runner.invoke(app, ["users", "list"])
    assert second_list.output.count("(default)") == 1


def test_users_add_and_set_default(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    added = runner.invoke(app, ["users", "add", "Jordan"])
    assert added.exit_code == 0, added.output
    assert "Jordan" in added.output

    list_result = runner.invoke(app, ["users", "list"])
    lines = [line for line in list_result.output.splitlines() if "Jordan" in line]
    assert len(lines) == 1
    jordan_id = lines[0].split("\t")[0]

    set_default = runner.invoke(app, ["users", "set-default", jordan_id])
    assert set_default.exit_code == 0, set_default.output

    final_list = runner.invoke(app, ["users", "list"])
    assert final_list.output.count("(default)") == 1
    assert f"{jordan_id}\tJordan (default)" in final_list.output


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("serve", "import", "classify", "report", "migrate", "export", "doctor", "eval"):
        assert name in result.output


def test_eval_rules_mode_prints_a_text_table(home: Path) -> None:
    # `eval` (P4-C's classifier eval harness): rules mode never touches the network, so it
    # needs no LLM configured and can run against the real fixture CSV as-is.
    result = runner.invoke(app, ["eval", "--mode", "rules"])
    assert result.exit_code == 0, result.output
    assert "Eval report (mode=rules)" in result.output
    assert "Accuracy by category:" in result.output


def test_eval_rules_mode_json(home: Path) -> None:
    result = runner.invoke(app, ["eval", "--mode", "rules", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "rules"
    assert payload["total"] > 0


def test_eval_rejects_an_unknown_mode(home: Path) -> None:
    result = runner.invoke(app, ["eval", "--mode", "bogus"])
    assert result.exit_code == 2


def test_eval_reports_a_clean_error_for_a_missing_csv(home: Path) -> None:
    result = runner.invoke(app, ["eval", "--csv", "does-not-exist.csv"])
    assert result.exit_code == 2
    assert "eval failed" in result.output


def test_report_prints_a_table(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "No transactions match this report." in result.output


def test_report_csv(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["report", "--csv"])
    assert result.exit_code == 0, result.output


def test_import_rejects_missing_file(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["import", "does-not-exist.pdf", "--user-id", "1"])
    assert result.exit_code == 1
    assert "no such file" in result.output


def test_import_rejects_non_pdf(home: Path, tmp_path: Path) -> None:
    runner.invoke(app, ["migrate"])
    bad_file = tmp_path / "not-a-pdf.pdf"
    bad_file.write_bytes(b"not a pdf")
    result = runner.invoke(app, ["import", str(bad_file), "--user-id", "1"])
    assert result.exit_code == 1
    assert "not a PDF file" in result.output


def test_import_of_unparseable_pdf_reports_error_cleanly(home: Path, tmp_path: Path) -> None:
    """A real PDF that isn't a recognizable statement fails with a clean per-file message
    (exit code 1, not an uncaught-exception traceback)."""
    runner.invoke(app, ["migrate"])
    pdf_file = tmp_path / "statement.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake statement bytes")
    result = runner.invoke(app, ["import", str(pdf_file), "--user-id", "1"], catch_exceptions=False)
    assert result.exit_code == 1, result.output


def test_import_without_user_id_uses_the_default_user(home: Path, tmp_path: Path) -> None:
    """Gate 2 regression: `import` with no `--user-id` must not require one (D4's default
    user), and must fail cleanly (not a raw traceback/IntegrityError) rather than crash."""
    runner.invoke(app, ["migrate"])
    pdf_file = tmp_path / "statement.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake statement bytes")
    result = runner.invoke(app, ["import", str(pdf_file)], catch_exceptions=False)
    # The fixture bytes aren't a real statement, so this fails at parsing (exit 1) — the point
    # is that it never fails on a missing --user-id or an unhandled FK IntegrityError.
    assert result.exit_code == 1, result.output
    assert "Missing option" not in result.output
    assert "IntegrityError" not in result.output


def test_import_with_unknown_user_id_reports_a_clean_error(home: Path, tmp_path: Path) -> None:
    runner.invoke(app, ["migrate"])
    pdf_file = tmp_path / "statement.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake statement bytes")
    result = runner.invoke(
        app, ["import", str(pdf_file), "--user-id", "999999"], catch_exceptions=False
    )
    assert result.exit_code == 1, result.output
    assert "no user with id 999999" in result.output


def test_classify_reports_nothing_to_classify(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["classify"])
    assert result.exit_code == 0, result.output
    assert "Nothing to classify." in result.output


def test_export_writes_empty_csv(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 0, result.output


def test_doctor_reports_pass_fail_lines(home: Path) -> None:
    runner.invoke(app, ["migrate"])
    result = runner.invoke(app, ["doctor"])
    assert "Python version" in result.output
    assert "Database migrated" in result.output


def _confirm_command_args(import_output: str) -> list[str]:
    """Parse the `import <path> --user-id ... [--parser-id ...]` command `import_cmd` prints
    after an `awaiting_extractor` result, and return it as the argv `runner.invoke` takes
    (everything after the leading `pfa`) — exactly what a terminal user would copy
    and run."""
    import re

    match = re.search(r"confirm with:\n\s+pfa (.+)", import_output)
    assert match is not None, f"no confirm command printed:\n{import_output}"
    return _split_printed_command(match.group(1))


def _split_printed_command(line: str) -> list[str]:
    """Split a command line the CLI printed (quoted for this platform's shell) into argv."""
    import os
    import shlex

    line = line.strip()
    if os.name != "nt":
        return shlex.split(line)
    # Windows quoting (subprocess.list2cmdline): backslashes are literal, tokens may be wrapped
    # in double quotes.
    tokens = shlex.split(line, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in tokens]


def test_gate2_end_to_end_import_classify_report(home: Path) -> None:
    """Reproduces Gate 2's own flow end-to-end on a fresh home, driving the CLI exactly as a
    terminal user would: `migrate` -> `import <fixture>` with no flags at all (no issuer is
    registered yet, so every statement lands `awaiting_extractor`) -> copy-paste-run the exact
    confirm command `import` printed -> `classify` (LLM `mode='none'`, D2's graceful fallback) ->
    `report`. Asserts the report's grand total matches the hand-computed total from
    `_expected_report_total()` (purchases/fees/interest by month; payments/transfers/the one
    adjustment excluded, the one refund netted in with its sign — I11/A7)."""
    migrate_result = runner.invoke(app, ["migrate"], catch_exceptions=False)
    assert migrate_result.exit_code == 0, migrate_result.output

    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "generated"
    for layout_dir, _account_type, _issuer_name in _GATE2_FIXTURES:
        pdf_path = fixtures_dir / layout_dir / f"{layout_dir}_normal.pdf"

        propose_result = runner.invoke(app, ["import", str(pdf_path)], catch_exceptions=False)
        assert propose_result.exit_code == 0, f"{layout_dir}: {propose_result.output}"
        assert "awaiting_extractor" in propose_result.output, (
            f"{layout_dir}: expected awaiting_extractor with no issuer registered yet, "
            f"got:\n{propose_result.output}"
        )

        confirm_args = _confirm_command_args(propose_result.output)
        confirm_result = runner.invoke(app, confirm_args, catch_exceptions=False)
        assert confirm_result.exit_code == 0, f"{layout_dir}: {confirm_result.output}"
        assert "imported" in confirm_result.output, f"{layout_dir}: {confirm_result.output}"

    classify_result = runner.invoke(app, ["classify"], catch_exceptions=False)
    assert classify_result.exit_code == 0, classify_result.output
    assert "Classified" in classify_result.output

    report_result = runner.invoke(app, ["report", "--csv"], catch_exceptions=False)
    assert report_result.exit_code == 0, report_result.output

    rows = list(csv.reader(io.StringIO(report_result.output)))
    header, data_rows = rows[0], [r for r in rows[1:] if r]
    total_idx = header.index("total_minor")
    actual_total = sum(int(row[total_idx]) for row in data_rows)
    assert actual_total == _expected_report_total()


def test_gate2_registering_an_issuer_first_makes_a_later_import_auto_confirm(home: Path) -> None:
    """The `issuers` CLI group (§3.12a/A23/A24) reaches the confident path on a *later* import:
    register the issuer whose match terms appear on the fixture's page 1, confirm one statement
    (with `--remember`, though it matters only for a layout spec — here it just documents intent),
    then import a second statement from the same issuer with no flags at all and confirm it
    auto-confirms (no `awaiting_extractor` message, no manual confirm step)."""
    migrate_result = runner.invoke(app, ["migrate"], catch_exceptions=False)
    assert migrate_result.exit_code == 0, migrate_result.output

    add_result = runner.invoke(
        app, ["issuers", "add", "Fixture Bank", "--match-term", "Fixture Bank"]
    )
    assert add_result.exit_code == 0, add_result.output

    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "generated" / "layout_a_credit"
    first_pdf = fixtures_dir / "layout_a_credit_normal.pdf"
    second_pdf = fixtures_dir / "layout_a_credit_refund_and_payment.pdf"

    first_result = runner.invoke(app, ["import", str(first_pdf)], catch_exceptions=False)
    assert first_result.exit_code == 0, first_result.output
    assert "imported" in first_result.output, first_result.output
    assert "awaiting_extractor" not in first_result.output, first_result.output

    second_result = runner.invoke(app, ["import", str(second_pdf)], catch_exceptions=False)
    assert second_result.exit_code == 0, second_result.output
    assert "imported" in second_result.output, second_result.output
    assert "awaiting_extractor" not in second_result.output, second_result.output


def test_issuers_add_duplicate_prints_friendly_error(home: Path) -> None:
    assert runner.invoke(app, ["migrate"], catch_exceptions=False).exit_code == 0
    assert runner.invoke(app, ["issuers", "add", "Discover"]).exit_code == 0

    result = runner.invoke(app, ["issuers", "add", "discover"])
    assert result.exit_code == 1
    assert "An issuer named 'Discover' already exists." in result.output
    assert "Traceback" not in result.output


def test_import_builtin_failure_warns_and_prints_llm_retry_command(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built-in parser that raises is a warning with a copy-pasteable `--parser-id llm`
    retry; the staged PDF is kept so that retry succeeds."""
    import re

    from tests.ingest.test_pipeline import _build_bank_pdf
    from tests.llm_fakes import install

    monkeypatch.setenv("PFA_PRIVACY__STORE_PDF_COPIES", "false")
    pdf = tmp_path / "both.pdf"
    _build_bank_pdf(
        pdf,
        [
            ("01/02/2026", "PAYCHECK DEPOSIT", "", "300.00", "1,300.00"),
            ("01/03/2026", "ATM WITHDRAWAL", "40.00", "", "1,260.00"),
            ("01/04/2026", "MIXED ROW", "20.00", "5.00", "1,245.00"),
            ("01/25/2026", "ONLINE SHOPPING", "60.00", "", "1,185.00"),
        ],
        opening="1,000.00",
        closing="1,185.00",
    )
    runner.invoke(app, ["migrate"], catch_exceptions=False)
    failed = runner.invoke(
        app, ["import", str(pdf), "--parser-id", "generic_table"], catch_exceptions=False
    )
    assert failed.exit_code == 1, failed.output
    assert "both the debit and credit" in failed.output
    assert "warning" in failed.output
    match = re.search(r"pfa (import .*--parser-id llm.*)", failed.output)
    assert match is not None, failed.output

    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "m")
    install(monkeypatch).queue(
        json.dumps({"account_type": "checking", "transactions": []})  # empty is fine too
    )
    retried = runner.invoke(app, _split_printed_command(match.group(1)), catch_exceptions=False)
    assert retried.exit_code == 0, retried.output
    assert "imported 0 transaction" in retried.output
