"""CLI commands (P0-6 skeleton, P2-C fills every stub in).

A33: `import` auto-confirms confident proposals and otherwise leaves the statement
`awaiting_extractor`, printing the exact command that confirms it; `report` prints monthly
category totals as a terminal table or `--csv`, built on `analytics/query.py`.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import webbrowser
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote

import typer
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from personal_finance_analyzer.analytics.query import SpendQuery, run_query
from personal_finance_analyzer.analytics.tax_query import TaxYearSummary
from personal_finance_analyzer.api.services import crud, gateway
from personal_finance_analyzer.config import load_settings, no_key_adoption
from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.core.logging import configure_logging, get_logger
from personal_finance_analyzer.core.paths import db_path, get_home, prepare_home
from personal_finance_analyzer.core.shellquote import join_command
from personal_finance_analyzer.core.tax_types import TaxFallbackExtractor
from personal_finance_analyzer.core.webui import web_ui_warning
from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.models import Transaction

app = typer.Typer(
    name="pfa",
    help=(
        "Personal Finance Analyzer: local-first spending, income and tax, and net worth analysis "
        "from your own statements and tax forms."
    ),
    no_args_is_help=True,
)

users_app = typer.Typer(help="Manage local users (D4).")
app.add_typer(users_app, name="users")

issuers_app = typer.Typer(help="Manage issuers (§3.12a, A23/A24), so later imports auto-confirm.")
app.add_typer(issuers_app, name="issuers")

from personal_finance_analyzer.cli_backup import backup_app  # noqa: E402
from personal_finance_analyzer.cli_llm import llm_app  # noqa: E402

app.add_typer(llm_app, name="llm")
app.add_typer(backup_app, name="backup")

tax_app = typer.Typer(help="Import and report W-2 / 1099 / 1040 tax forms (income & tax analysis).")
app.add_typer(tax_app, name="tax")

_NOT_YET_IMPLEMENTED = "not yet implemented in this build"

#: A12: same PDF magic-byte check the upload endpoint applies (`api/routers/statements.py`).
_PDF_MAGIC = b"%PDF-"


def _stub(command: str) -> None:
    typer.echo(f"pfa {command}: {_NOT_YET_IMPLEMENTED}", err=True)
    raise typer.Exit(code=2)


@contextmanager
def _open_engine() -> Iterator[Engine]:
    from personal_finance_analyzer.db.session import make_engine

    home = prepare_home()  # the explicit startup step: carries a pre-rename data dir/db over
    configure_logging(home=home)
    engine = make_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@contextmanager
def _open_session(engine: Engine) -> Iterator[Session]:
    from personal_finance_analyzer.db.session import make_session_factory

    factory = make_session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _session_factory_for(engine: Engine) -> sessionmaker[Session]:
    from personal_finance_analyzer.db.session import make_session_factory

    return make_session_factory(engine)


def _sync_taxonomy_and_defaults(session: Session) -> None:
    """Everything `migrate` and `serve` startup both need before the database is usable:
    taxonomy + builtin rules synced (idempotent), and a default user guaranteed to exist (D4) so
    `import`/`report`/`classify`/`export` always have a user to fall back to on a fresh home."""
    from personal_finance_analyzer.classify.rules import sync_builtin_rules
    from personal_finance_analyzer.classify.taxonomy import sync_taxonomy

    sync_taxonomy(session)
    sync_builtin_rules(session)
    crud.ensure_default_user(session)
    session.commit()


def _require_user_id(session: Session, user_id: int | None) -> int:
    """Resolve a required user id: the given `--user-id` (validated), or the `is_default` user
    when none was given.

    Raises:
        typer.Exit: an explicit `user_id` does not exist, or none was given and no user exists
            yet (a clean message, not a traceback — Gate 2's original failure mode).
    """
    if user_id is not None:
        if crud.get_user(session, user_id) is None:
            typer.echo(f"no user with id {user_id}", err=True)
            raise typer.Exit(code=1)
        return user_id
    default = crud.default_user(session)
    if default is None:
        typer.echo(
            "no user exists yet; run 'pfa migrate' first, or create one with "
            "'pfa users add <name>'",
            err=True,
        )
        raise typer.Exit(code=1)
    return default.id


def _resolve_user_filter(session: Session, user_id: int | None, *, all_users: bool) -> int | None:
    """Resolve the optional `--user-id`/`--all-users` pair to a filter value: `None` means "no
    filter, every user" (either `--all-users`, or no default user exists yet to fall back to);
    otherwise the id to filter on (D4: an omitted `--user-id` defaults to the `is_default` user).

    Raises:
        typer.Exit: an explicit `user_id` does not exist (a clean message, not a traceback).
    """
    if all_users:
        return None
    if user_id is not None:
        if crud.get_user(session, user_id) is None:
            typer.echo(f"no user with id {user_id}", err=True)
            raise typer.Exit(code=1)
        return user_id
    default = crud.default_user(session)
    return default.id if default is not None else None


@app.command()
def migrate() -> None:
    """Create/upgrade the local database to the latest schema, sync the taxonomy and builtin
    rules, and ensure a default user exists (D4)."""
    with _open_engine() as engine:
        upgrade_head()
        with _open_session(engine) as session:
            _sync_taxonomy_and_defaults(session)

    get_logger("cli").info("migrate: database ready")
    typer.echo("Database migrated and taxonomy synced.")


@app.command(name="serve")
def serve_cmd(
    open_browser: bool = typer.Option(
        True, help="Open the default browser once the server starts."
    ),
) -> None:
    """Start the local web server (I8: binds 127.0.0.1 only; A31: per-launch token)."""
    import uvicorn

    from personal_finance_analyzer.api.app import create_app

    settings = load_settings()
    package_dir = Path(__file__).resolve().parent
    warning = web_ui_warning(package_dir.parent.parent / "frontend", package_dir / "web")
    if warning is not None:
        typer.echo(f"WARNING: {warning}", err=True)
    with _open_engine() as engine:
        with _open_session(engine) as session:
            _sync_taxonomy_and_defaults(session)
        app_instance = create_app(engine=engine, settings=settings)
        token = app_instance.state.token
        port = settings.server.port
        url = f"http://127.0.0.1:{port}/?token={token}"
        typer.echo(f"Personal Finance Analyzer is running at {url}")
        typer.echo(f"Per-launch API token: {token}")
        if open_browser:
            webbrowser.open(url)
        uvicorn.run(app_instance, host="127.0.0.1", port=port, log_level="warning")


def _echo_tax_form_hint(pdf_path: Path, raw_path: str) -> None:
    """If `pdf_path` looks like a tax form (W-2 / 1099 / 1040), print a pointer to `tax import`.

    Best effort and read-only: scores the PDF with the tax-form classifier (no extract cache is
    written, nothing is stored) and prints nothing when the file can't be read or isn't a tax
    form. No pipeline change; a statement import is never altered by this."""
    from personal_finance_analyzer.ingest.extract import extract
    from personal_finance_analyzer.ingest.tax.registry import classify_form

    try:
        classification = classify_form(extract(pdf_path, write_cache=False))
    except PfaError:
        return
    if classification.parser_id is None:
        return
    kind = classification.form_type or "tax form"
    typer.echo(
        f"{raw_path}: this looks like a tax form ({kind}); use `pfa tax import {raw_path}` instead"
    )


@app.command(name="import")
def import_cmd(
    paths: list[str] = typer.Argument(  # noqa: B008 - typer's documented pattern
        ..., help="Statement PDF paths (globs are expanded by the shell, not by this command)."
    ),
    user_id: int | None = typer.Option(
        None,
        "--user-id",
        help="The user these statements belong to (defaults to the default user).",
    ),
    issuer_id: int | None = typer.Option(
        None, "--issuer-id", help="Confirm with this issuer instead of the auto-proposed one."
    ),
    parser_id: str | None = typer.Option(None, "--parser-id", help="Confirm with this parser id."),
    layout_spec_id: int | None = typer.Option(None, "--layout-spec-id"),
    remember: bool = typer.Option(
        False, "--remember", help="Remember this extractor for the issuer."
    ),
    pages: str | None = typer.Option(
        None, "--pages", help="Only parse these pages, e.g. '1-3,5' (default: all pages)."
    ),
) -> None:
    """Import one or more statement PDFs (A22/A33).

    Auto-confirms a confident proposal (unless `[ingest] always_confirm_extractor` is set);
    otherwise leaves the statement `awaiting_extractor` and prints the exact `import` command,
    with `--issuer-id`/`--parser-id`, that confirms it.
    """
    settings = load_settings()
    selected_pages: list[int] | None = None
    if pages is not None:
        from personal_finance_analyzer.ingest.pages import parse_page_spec

        try:
            selected_pages = parse_page_spec(pages)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--pages") from None
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _require_user_id(session, user_id)
        any_error = False
        for raw_path in paths:
            pdf_path = Path(raw_path)
            if not pdf_path.is_file():
                typer.echo(f"{raw_path}: no such file", err=True)
                any_error = True
                continue
            if pdf_path.read_bytes()[: len(_PDF_MAGIC)] != _PDF_MAGIC:
                typer.echo(f"{raw_path}: not a PDF file", err=True)
                any_error = True
                continue

            try:
                proposal = gateway.propose_import(
                    session, pdf_path, user_id=resolved_user_id, original_name=pdf_path.name
                )
                session.commit()
            except PfaError as exc:
                typer.echo(f"{raw_path}: {exc}", err=True)
                any_error = True
                continue

            if proposal.status != "awaiting_extractor":
                typer.echo(f"{raw_path}: statement #{proposal.statement_id} -> {proposal.status}")
                if proposal.status == "unsupported_layout":
                    _echo_tax_form_hint(pdf_path, raw_path)
                if proposal.status == "error":
                    any_error = True
                continue

            # An explicit --issuer-id/--parser-id/--layout-spec-id is the user (or the printed
            # confirm command above, on a re-run) pinning the extractor by hand: confirm with it
            # even when `propose_import` was not confident (or, on a re-run of the same file's
            # sha, never re-scored it at all — I9's idempotent "reported as-is" path). Falling
            # back to `proposal.issuer_id` covers the case where an issuer did match but the
            # score was still below the confidence threshold.
            explicit_extractor_given = (
                issuer_id is not None
                or parser_id is not None
                or layout_spec_id is not None
                or selected_pages is not None
            )
            resolved_issuer_id = issuer_id if issuer_id is not None else proposal.issuer_id
            should_confirm = explicit_extractor_given or (
                proposal.confident and not settings.ingest.always_confirm_extractor
            )

            if not should_confirm:
                if not proposal.confident:
                    _echo_tax_form_hint(pdf_path, raw_path)
                confirm_args = [
                    "pfa",
                    "import",
                    raw_path,
                    "--user-id",
                    str(resolved_user_id),
                ]
                if proposal.issuer_id is not None:
                    confirm_args += ["--issuer-id", str(proposal.issuer_id)]
                if proposal.parser_id is not None:
                    confirm_args += ["--parser-id", proposal.parser_id]
                if proposal.parser_id == "llm":
                    # LLM imports are never auto-confirmed: the user chooses the pages (their
                    # redacted text leaves the machine) before anything is sent.
                    stmt_row = crud.get_statement(session, proposal.statement_id)
                    last_page = (stmt_row.page_count if stmt_row else None) or 1
                    confirm_args += ["--pages", f"1-{last_page}"]
                    confirm_cmd = join_command(confirm_args)
                    typer.echo(
                        f"{raw_path}: statement #{proposal.statement_id} will be parsed by the "
                        "LLM; choose the pages to send (edit --pages, e.g. 2-3) and confirm "
                        f"with:\n  {confirm_cmd}"
                    )
                    continue
                confirm_cmd = join_command(confirm_args)
                typer.echo(
                    f"{raw_path}: statement #{proposal.statement_id} awaiting_extractor "
                    f"(detect_score={proposal.detect_score}); confirm with:\n  {confirm_cmd}"
                )
                continue

            if resolved_issuer_id is None:
                # No issuer matched (none is registered yet, or none matches this statement's
                # text) and the user did not pass --issuer-id either: confirm against the
                # placeholder "Unknown" issuer rather than blocking the import. `issuers add`
                # (below) lets a later import of the same bank's statements match confidently.
                resolved_issuer_id = crud.unknown_issuer_id(session)

            used_parser_id = parser_id if parser_id is not None else proposal.parser_id
            try:
                result = gateway.confirm_import(
                    session,
                    proposal.statement_id,
                    issuer_id=resolved_issuer_id,
                    parser_id=used_parser_id,
                    layout_spec_id=(
                        layout_spec_id if layout_spec_id is not None else proposal.layout_spec_id
                    ),
                    remember=remember or issuer_id is not None,
                    pages=selected_pages,
                )
                session.commit()
            except PfaError as exc:
                typer.echo(f"{raw_path}: statement #{proposal.statement_id} -> {exc}", err=True)
                any_error = True
                if used_parser_id != "llm" and layout_spec_id is None:
                    # A failing built-in parser is a warning, not a dead end: the PDF is still
                    # staged, so the same statement can be confirmed with the LLM parser.
                    stmt_row = crud.get_statement(session, proposal.statement_id)
                    last_page = (stmt_row.page_count if stmt_row else None) or 1
                    retry_cmd = join_command(
                        [
                            "pfa",
                            "import",
                            raw_path,
                            "--user-id",
                            str(resolved_user_id),
                            "--issuer-id",
                            str(resolved_issuer_id),
                            "--parser-id",
                            "llm",
                            "--pages",
                            f"1-{last_page}",
                        ]
                    )
                    typer.echo(
                        f"{raw_path}: warning: the built-in parser couldn't read this statement. "
                        "Retry with the LLM parser (edit --pages to choose which pages are "
                        f"sent):\n  {retry_cmd}",
                        err=True,
                    )
                continue

            typer.echo(
                f"{raw_path}: statement #{proposal.statement_id} imported "
                f"{result.inserted} transaction(s), skipped {result.skipped_duplicates} "
                "duplicate(s)" + (" [layout drift detected]" if result.layout_drift else "")
            )
            for warning in result.warnings:
                typer.echo(f"  warning: {warning}")

    if any_error:
        raise typer.Exit(code=1)


@app.command()
def classify(
    user_id: int | None = typer.Option(
        None, "--user-id", help="Only this user's transactions (defaults to the default user)."
    ),
    all_users: bool = typer.Option(
        False, "--all-users", help="Classify every user's transactions, ignoring --user-id."
    ),
    statement_id: int | None = typer.Option(None, "--statement-id"),
    only_unclassified: bool = typer.Option(
        True, help="Skip transactions that already have a category."
    ),
) -> None:
    """Run the classification cascade over unclassified transactions."""
    import uuid

    settings = load_settings()
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _resolve_user_filter(session, user_id, all_users=all_users)
        stmt = select(Transaction.id)
        if resolved_user_id is not None:
            stmt = stmt.where(Transaction.user_id == resolved_user_id)
        if statement_id is not None:
            stmt = stmt.where(Transaction.statement_id == statement_id)
        if only_unclassified:
            stmt = stmt.where(Transaction.category_id.is_(None))
        transaction_ids = tuple(session.execute(stmt).scalars().all())

        if not transaction_ids:
            typer.echo("Nothing to classify.")
            return

        # classify.cascade's `progress_cb` (P2-B) fires once per cascade stage, which often
        # reports the same (done, total, cost_usd) two or three times in a row when the
        # deterministic stages alone classify everything — print a line only when something
        # about it actually changed, so the run reads as one line per real change rather than
        # the same line repeated.
        last_progress: tuple[int, int, float] | None = None

        def progress_cb(done: int, total: int, cost_usd: float) -> None:
            nonlocal last_progress
            current = (done, total, cost_usd)
            if current == last_progress:
                return
            last_progress = current
            typer.echo(f"  {done}/{total} classified (${cost_usd:.4f})", err=True)

        result = gateway.classify_transactions(
            _session_factory_for(engine),
            transaction_ids,
            cfg=settings.llm,
            group_id=str(uuid.uuid4()),
            progress_cb=progress_cb,
        )

    typer.echo(
        f"Classified {result.classified} transaction(s); {result.needs_review} need review; "
        f"{result.llm_requests} LLM request(s); ${result.cost_usd:.4f} spent."
    )


@app.command()
def report(
    date_from: str | None = typer.Option(None, "--from", help="YYYY-MM-DD"),
    date_to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD"),
    user_id: int | None = typer.Option(
        None, "--user-id", help="Only this user's transactions (defaults to the default user)."
    ),
    all_users: bool = typer.Option(
        False, "--all-users", help="Report on every user's transactions, ignoring --user-id."
    ),
    currency: str = typer.Option("USD"),
    csv_output: bool = typer.Option(False, "--csv", help="Print as CSV instead of a table."),
) -> None:
    """Print monthly category totals for a date range (A33)."""
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _resolve_user_filter(session, user_id, all_users=all_users)
        query = SpendQuery(
            user_ids=[resolved_user_id] if resolved_user_id is not None else None,
            date_from=date.fromisoformat(date_from) if date_from else None,
            date_to=date.fromisoformat(date_to) if date_to else None,
            granularity="month",
            group_by=["period", "category"],
            currency=currency,
        )
        rows = run_query(session, query)

    _print_report(rows, as_csv=csv_output)


def _print_report(rows: list[dict[str, object]], *, as_csv: bool) -> None:
    if not rows:
        typer.echo("No transactions match this report.")
        return

    if as_csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["period", "category", "currency", "total_minor", "txn_count", "avg_minor"])
        for row in rows:
            writer.writerow(
                [
                    row["period"],
                    row["category"],
                    row["currency"],
                    row["total_minor"],
                    row["txn_count"],
                    row["avg_minor"],
                ]
            )
        return

    headers = ("Period", "Category", "Currency", "Total", "Count", "Avg")
    table_rows = [
        (
            str(row["period"]),
            str(row["category"]),
            str(row["currency"]),
            _format_minor(row["total_minor"]),  # type: ignore[arg-type]
            str(row["txn_count"]),
            _format_minor(row["avg_minor"]),  # type: ignore[arg-type]
        )
        for row in rows
    ]
    widths = [max(len(headers[i]), *(len(r[i]) for r in table_rows)) for i in range(len(headers))]
    typer.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    typer.echo("  ".join("-" * w for w in widths))
    for table_row in table_rows:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(table_row)))


def _format_minor(amount_minor: int) -> str:
    sign = "-" if amount_minor < 0 else ""
    return f"{sign}{abs(amount_minor) / 100:.2f}"


@app.command()
def export(
    output: str = typer.Option("-", "--output", help="File path, or '-' for stdout."),
    format: str = typer.Option("csv", "--format", help="csv or json"),
    user_id: int | None = typer.Option(
        None, "--user-id", help="Only this user's transactions (defaults to the default user)."
    ),
    all_users: bool = typer.Option(
        False, "--all-users", help="Export every user's transactions, ignoring --user-id."
    ),
) -> None:
    """Export transactions to CSV/JSON. Never includes `accounts.mask` (A12/export guard)."""
    if format not in ("csv", "json"):
        typer.echo("--format must be 'csv' or 'json'", err=True)
        raise typer.Exit(code=2)

    from personal_finance_analyzer.api.routers.export import export_transactions
    from personal_finance_analyzer.api.services.crud import TransactionFilters

    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _resolve_user_filter(session, user_id, all_users=all_users)
        response = export_transactions(
            format=format,  # type: ignore[arg-type]
            session=session,
            filters=TransactionFilters(include_non_spend=True, currency=None),
            user_id=resolved_user_id,
        )
        body = (
            response.body.decode("utf-8")
            if isinstance(response.body, bytes)
            else str(response.body)
        )

    if output == "-":
        typer.echo(body)
    else:
        Path(output).write_text(body, encoding="utf-8")
        typer.echo(f"Wrote {output}")


@app.command()
def doctor(
    write_test: bool = typer.Option(
        False,
        "--write-test",
        help="Also write, read back and delete a probe entry in the OS keychain. Off by default "
        "because a keychain write can raise an OS dialog; the default check only reads.",
    ),
) -> None:
    """Diagnose the local install and configuration: Python version, data directory permissions,
    DB migration state, which secret backend holds the API key (read-only), and LLM connectivity,
    one pass/fail line each."""
    ok = True
    ok &= _check("Python version >= 3.12", sys.version_info >= (3, 12))

    ok &= _doctor_home()

    ok &= _doctor_migrations()
    ok &= _doctor_keychain(write_test=write_test)
    with no_key_adoption():  # the connectivity check uses the key, but must not write
        ok &= _doctor_llm()

    if not ok:
        raise typer.Exit(code=1)


@users_app.command(name="list")
def users_list() -> None:
    """List every local user (D4)."""
    with _open_engine() as engine, _open_session(engine) as session:
        users = crud.list_users(session)
        if not users:
            typer.echo("No users yet. Create one with 'pfa users add <name>'.")
            return
        for user in users:
            marker = " (default)" if user.is_default else ""
            typer.echo(f"{user.id}\t{user.name}{marker}")


@users_app.command(name="add")
def users_add(
    name: str = typer.Argument(..., help="The new user's display name."),
    default: bool = typer.Option(
        False, "--default", help="Make this the default user (D4: used when --user-id is omitted)."
    ),
) -> None:
    """Create a new local user."""
    with _open_engine() as engine, _open_session(engine) as session:
        user = crud.create_user(session, name=name, is_default=default)
        session.commit()
        marker = " (default)" if user.is_default else ""
        typer.echo(f"Created user #{user.id}: {user.name}{marker}")


@users_app.command(name="set-default")
def users_set_default(user_id: int = typer.Argument(..., help="The user to make default.")) -> None:
    """Make an existing user the default (D4)."""
    with _open_engine() as engine, _open_session(engine) as session:
        user = crud.get_user(session, user_id)
        if user is None:
            typer.echo(f"no user with id {user_id}", err=True)
            raise typer.Exit(code=1)
        crud.update_user(session, user, name=None, is_default=True)
        session.commit()
        typer.echo(f"User #{user.id} ({user.name}) is now the default.")


@issuers_app.command(name="list")
def issuers_list() -> None:
    """List every issuer, with its match terms (§3.12a: used by `import`'s issuer matching)."""
    import json as _json

    with _open_engine() as engine, _open_session(engine) as session:
        issuers = crud.list_issuers(session)
        if not issuers:
            typer.echo(
                "No issuers yet. Create one with 'pfa issuers add <name>' so a later "
                "import of its statements can auto-confirm."
            )
            return
        for issuer in issuers:
            terms = ", ".join(_json.loads(issuer.match_terms))
            typer.echo(f"{issuer.id}\t{issuer.name}\tmatch_terms=[{terms}]")


@issuers_app.command(name="add")
def issuers_add(
    name: str = typer.Argument(..., help="The issuer's display name (e.g. a bank's name)."),
    match_term: list[str] = typer.Option(  # noqa: B008 - typer's documented pattern
        [],
        "--match-term",
        help="Text that identifies this issuer on a statement's first page (repeatable); "
        "defaults to the name itself.",
    ),
    default_spec: int | None = typer.Option(
        None, "--default-spec", help="An approved layout_specs.id to remember for this issuer."
    ),
) -> None:
    """Register an issuer so `import` can match its statements confidently (§3.12a)."""
    with _open_engine() as engine, _open_session(engine) as session:
        try:
            issuer = crud.create_issuer(session, name=name, match_terms=list(match_term))
        except crud.DuplicateIssuerError as exc:
            typer.echo(f"{exc} (issuer #{exc.existing.id})", err=True)
            raise typer.Exit(code=1) from None
        if default_spec is not None:
            issuer = crud.update_issuer(
                session, issuer, name=None, match_terms=None, default_spec_id=default_spec
            )
        session.commit()
        typer.echo(f"Created issuer #{issuer.id}: {issuer.name}")


def _doctor_home() -> bool:
    """Is the data directory usable? Read-only: a machine that has not run the app yet gets no
    data directory (or database) from a diagnosis; it is created on first real use."""
    try:
        home = get_home()
        if home.is_dir():
            return _check(f"Data directory ({home}) writable", os.access(home, os.W_OK))
        parent = next((p for p in home.parents if p.exists()), home)
        return _check(
            f"Data directory ({home}) not created yet; it will be on first use ({parent} writable)",
            os.access(parent, os.W_OK),
        )
    except OSError as exc:
        return _check(f"Data directory writable: {exc}", False)


def _doctor_migrations() -> bool:
    from alembic.runtime.migration import MigrationContext

    from personal_finance_analyzer.db.migrate import build_alembic_config

    db_file = db_path()
    if not db_file.exists():
        typer.echo("INFO  No database yet (it is created by the first import or `pfa migrate`)")
        return True
    try:
        # Read-only (`mode=ro`): the check must never create, or write to, the database.
        engine = create_engine(f"sqlite:///file:{quote(db_file.as_posix())}?mode=ro&uri=true")
        try:
            with engine.connect() as conn:
                context = MigrationContext.configure(conn)
                current = context.get_current_revision()
            from alembic.script import ScriptDirectory

            script = ScriptDirectory.from_config(build_alembic_config())
            head = script.get_current_head()
            return _check(f"Database migrated to head ({head})", current == head)
        finally:
            engine.dispose()
    except Exception as exc:
        return _check(f"Database migration check failed: {exc}", False)


def _doctor_keychain(*, write_test: bool = False) -> bool:
    """Report the secret backend in use. Read-only unless ``write_test``: the default pass never
    writes to the keychain (a write can pop an OS dialog, or fail on a machine whose login keychain
    is missing); ``--write-test`` adds an explicit set/get/delete round trip of a probe entry."""
    from personal_finance_analyzer.cli_llm import describe_secret_storage
    from personal_finance_analyzer.config import (
        get_llm_api_key,
        keychain_write_test,
        secret_storage_status,
    )
    from personal_finance_analyzer.secret_store import SecretStorageError

    cfg = load_settings().llm
    status = secret_storage_status(cfg)
    ok = True
    if status.keychain_usable:
        _check(f"Keychain backend: {status.keychain_name}", True)
    elif status.env_var is not None or status.file_store_enabled:
        typer.echo(
            f"INFO  No usable OS keychain ({status.keychain_detail}); using the fallback below"
        )
    else:
        typer.echo(f"WARN  No usable OS keychain ({status.keychain_detail}).")
        typer.echo(f"      {status.guidance}")
        typer.echo(
            "      Saving an API key will fail until one works, unless you set PFA_LLM_API_KEY "
            "or enable `[secrets] allow_file_store = true`."
        )
    typer.echo(f"INFO  Key source / save target: {describe_secret_storage(status)}")
    if status.file_store_enabled:
        typer.echo(f"WARN  {status.warning}")
    elif status.file_store_has_keys:
        typer.echo(
            f"WARN  {status.file_store_path} holds saved keys but the file store is not enabled "
            "(`[secrets] allow_file_store`); they are ignored. Delete the file or enable it."
        )

    if cfg.mode != "none":
        try:
            found = get_llm_api_key(cfg, adopt=False) is not None  # read-only
        except Exception as exc:  # a read must never crash the diagnosis
            ok &= _check(f"API key lookup failed: {exc}", False)
        else:
            if found:
                _check(f"API key for {cfg.key_account.removeprefix('llm:')} readable", True)
            else:
                typer.echo(
                    f"INFO  No API key found for {cfg.key_account.removeprefix('llm:')} "
                    "(fine for a local server; `pfa llm set-key` stores one)"
                )

    if write_test:
        try:
            keychain_write_test()
        except SecretStorageError as exc:
            ok &= _check(f"Keychain write test failed: {exc}", False)
        else:
            ok &= _check("Keychain write test (probe entry written, read back, deleted)", True)
    else:
        typer.echo(
            "SKIP  Keychain write test (read-only by default; run `pfa doctor --write-test`)"
        )
    return ok


def _doctor_llm() -> bool:
    from personal_finance_analyzer.llm.diagnostics import check_connection

    cfg = load_settings().llm
    if cfg.mode == "none":
        typer.echo("SKIP  LLM connectivity (disabled — run `pfa llm set ...` to configure)")
        return True
    result = check_connection(cfg)
    if result.ok:
        return _check(f"LLM connectivity ({result.detail})", True)
    return _check(f"LLM connectivity failed: {result.detail}", False)


def _check(label: str, passed: bool) -> bool:
    typer.echo(f"{'PASS' if passed else 'FAIL'}  {label}")
    return passed


@app.command(name="eval")
def eval_cmd(
    csv_path: str = typer.Option(
        "tests/eval/descriptions.csv",
        "--csv",
        help="Labelled CSV (description,category,subcategory).",
    ),
    mode: str = typer.Option(
        "rules", "--mode", help="'rules' (deterministic only) or 'llm' (rules, then the LLM)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON."),
) -> None:
    """Run the classifier evaluation harness (P4-C) against a labelled CSV."""
    if mode not in ("rules", "llm"):
        typer.echo("--mode must be 'rules' or 'llm'", err=True)
        raise typer.Exit(code=2)

    from personal_finance_analyzer.eval.harness import EvalMode, run_eval

    resolved_csv = Path(csv_path)
    if not resolved_csv.is_file():
        typer.echo(f"eval failed: no such file: {resolved_csv}", err=True)
        raise typer.Exit(code=2)

    settings = load_settings()
    eval_mode: EvalMode = "rules" if mode == "rules" else "llm"
    try:
        report = run_eval(settings.llm, csv_path=resolved_csv, mode=eval_mode)
    except PfaError as exc:
        typer.echo(f"eval failed: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(report.to_json() if as_json else report.text_table())


#: The command users type. Click derives the usage line's program name from `argv[0]`, so every
#: entry point pins it: `personal-finance-analyzer` and the deprecated `spend-analyzer` alias
#: both document `pfa`, the name the rest of the docs and every printed hint use.
PROG_NAME = "pfa"

LEGACY_COMMAND_NOTICE = (
    "spend-analyzer has been renamed to pfa; this alias will be removed in a future release"
)


def main() -> None:
    """Console-script entry point for `pfa` and `personal-finance-analyzer`."""
    app(prog_name=PROG_NAME)


def legacy_main() -> None:
    """Console-script entry point for the deprecated `spend-analyzer` alias: a one-line notice on
    stderr (stdout stays clean for `report --csv` and friends), then the same app."""
    print(LEGACY_COMMAND_NOTICE, file=sys.stderr)
    app(prog_name=PROG_NAME)


if __name__ == "__main__":  # pragma: no cover
    main()
    sys.exit(0)


# --- tax: income & tax analysis from tax forms ---------------------------------------------------

_TAX_MIN_YEAR = 1990
_TAX_MAX_YEAR = 2100


def _check_tax_year(value: int | None, option: str) -> int | None:
    if value is not None and not (_TAX_MIN_YEAR <= value <= _TAX_MAX_YEAR):
        raise typer.BadParameter(
            f"must be between {_TAX_MIN_YEAR} and {_TAX_MAX_YEAR}", param_hint=option
        )
    return value


def _tax_fallback(session: Session, user_id: int) -> TaxFallbackExtractor | None:
    """The opt-in LLM fallback (None unless an LLM is configured and `tax_llm_fallback` is on)."""
    from personal_finance_analyzer.api.services.tax_llm import build_tax_fallback

    return build_tax_fallback(session, user_id)


@tax_app.command(name="import")
def tax_import_cmd(
    paths: list[str] = typer.Argument(  # noqa: B008 - typer's documented pattern
        ..., help="Tax form PDF paths (W-2, 1099-*, 1040); globs are expanded by the shell."
    ),
    user_id: int | None = typer.Option(
        None, "--user-id", help="The user these forms belong to (defaults to the default user)."
    ),
    tax_year: int | None = typer.Option(
        None, "--tax-year", help="Tax year to use when the form does not print one."
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help="Never use the LLM fallback for this run, even if it is switched on in the settings.",
    ),
) -> None:
    """Import one or more tax-form PDFs in a single step. Parsing is local: nothing leaves this
    machine unless you opted in to the LLM fallback (`llm set --tax-llm-fallback`), which sends
    only redacted pages the built-in parsers could not read. Re-importing an identical file is a
    no-op."""
    _check_tax_year(tax_year, "--tax-year")
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _require_user_id(session, user_id)
        fallback = None if no_llm else _tax_fallback(session, resolved_user_id)
        any_error = False
        for raw_path in paths:
            pdf_path = Path(raw_path)
            if not pdf_path.is_file():
                typer.echo(f"{raw_path}: no such file", err=True)
                any_error = True
                continue
            if pdf_path.read_bytes()[: len(_PDF_MAGIC)] != _PDF_MAGIC:
                typer.echo(f"{raw_path}: not a PDF file", err=True)
                any_error = True
                continue
            try:
                result = gateway.import_tax_document(
                    session,
                    pdf_path,
                    user_id=resolved_user_id,
                    original_name=pdf_path.name,
                    tax_year=tax_year,
                    fallback=fallback,
                )
            except PfaError as exc:
                typer.echo(f"{raw_path}: {exc}", err=True)
                any_error = True
                continue

            prefix = f"{raw_path}: tax document #{result.document_id} -> {result.status}"
            if result.status == "parsed":
                forms = ", ".join(result.form_types)
                year = result.tax_year if result.tax_year is not None else "?"
                line = (
                    f"{prefix} ({year}: {forms}) {result.income_item_count} income / "
                    f"{result.tax_item_count} tax item(s)"
                )
                if result.skipped_duplicate_forms:
                    line += f", skipped {result.skipped_duplicate_forms} duplicate form(s)"
                if result.llm_used:
                    line += " [LLM fallback used: review the marked values]"
                typer.echo(line)
            elif result.status == "duplicate":
                typer.echo(prefix)
            else:
                typer.echo(f"{prefix}: {result.error or 'could not read this document'}", err=True)
                any_error = True
            for warning in result.warnings:
                typer.echo(f"  warning: {warning}")

    if any_error:
        raise typer.Exit(code=1)


@tax_app.command(name="list")
def tax_list_cmd(
    user_id: int | None = typer.Option(
        None, "--user-id", help="Only this user's documents (defaults to the default user)."
    ),
    all_users: bool = typer.Option(
        False, "--all-users", help="List every user's documents, ignoring --user-id."
    ),
    year: int | None = typer.Option(None, "--year", help="Only this tax year."),
) -> None:
    """List imported tax documents."""
    from personal_finance_analyzer.analytics import tax_query

    _check_tax_year(year, "--year")
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _resolve_user_filter(session, user_id, all_users=all_users)
        docs = tax_query.list_tax_documents(
            session,
            user_ids=[resolved_user_id] if resolved_user_id is not None else None,
            tax_year=year,
        )
        table_rows = [
            [
                str(d.id),
                str(d.tax_year) if d.tax_year is not None else "-",
                ", ".join(f.form_type for f in d.forms) or "-",
                d.status,
                str(len(json.loads(d.warnings)) if d.warnings else 0),
            ]
            for d in docs
        ]
    if not table_rows:
        typer.echo("No tax documents.")
        return
    headers = ["id", "year", "forms", "status", "warnings"]
    widths = [max(len(h), *(len(r[i]) for r in table_rows)) for i, h in enumerate(headers)]
    typer.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    typer.echo("  ".join("-" * w for w in widths))
    for table_row in table_rows:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(table_row)))


@tax_app.command(name="report")
def tax_report_cmd(
    year: int | None = typer.Option(None, "--year", help="Only this tax year (default: all)."),
    user_id: int | None = typer.Option(
        None, "--user-id", help="Only this user's forms (defaults to the default user)."
    ),
    all_users: bool = typer.Option(
        False, "--all-users", help="Report on every user's forms combined, ignoring --user-id."
    ),
    csv_output: bool = typer.Option(False, "--csv", help="Print as CSV instead of text."),
) -> None:
    """Income, withholding and tax per tax year (aggregate figures only)."""
    from personal_finance_analyzer.analytics import tax_query

    _check_tax_year(year, "--year")
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _resolve_user_filter(session, user_id, all_users=all_users)
        summaries = tax_query.run_tax_summary(
            session,
            tax_query.TaxQuery(
                user_ids=[resolved_user_id] if resolved_user_id is not None else None,
                tax_years=[year] if year is not None else None,
            ),
        )
    if not summaries:
        typer.echo("No tax data matches this report.")
        return
    if csv_output:
        _print_tax_csv(summaries)
    else:
        _print_tax_text(summaries)


def _tax_sections(s: TaxYearSummary) -> list[tuple[str, list[tuple[str, str, int]]]]:
    """(section, [(category, label, amount_minor)]) in display order for one year summary."""
    from personal_finance_analyzer.core.tax_types import (
        INCOME_CATEGORY_LABELS,
        MEMO_INCOME_CATEGORIES,
        TAX_CATEGORY_LABELS,
    )

    income: list[tuple[str, str, int]] = [
        (cat, INCOME_CATEGORY_LABELS[cat], amount)
        for cat, amount in s.income_by_category.items()
        if cat not in MEMO_INCOME_CATEGORIES
    ]
    income.append(("total_income", "Total income", s.total_income_minor))
    memo_labels: dict[str, str] = {
        **{cat: INCOME_CATEGORY_LABELS[cat] for cat in MEMO_INCOME_CATEGORIES},
        "tax_exempt": "Tax-exempt interest (memo)",
    }
    memo = [(k, memo_labels.get(k, k), v) for k, v in s.memo_by_category.items()]
    result: list[tuple[str, str, int]] = []
    if s.payments_minor is not None:
        result.append(("total_payments", TAX_CATEGORY_LABELS["total_payments"], s.payments_minor))
    if s.refund_minor is not None:
        result.append(("refund", TAX_CATEGORY_LABELS["refund"], s.refund_minor))
    if s.amount_owed_minor is not None:
        result.append(("amount_owed", TAX_CATEGORY_LABELS["amount_owed"], s.amount_owed_minor))
    return [
        ("income", income),
        ("memo", memo),
        ("withheld", [(c, TAX_CATEGORY_LABELS[c], a) for c, a in s.withheld.items()]),
        ("fica", [(c, TAX_CATEGORY_LABELS[c], a) for c, a in s.fica.items()]),
        ("liability", [(c, TAX_CATEGORY_LABELS[c], a) for c, a in s.liability.items()]),
        ("result", result),
    ]


def _print_tax_csv(summaries: Sequence[TaxYearSummary]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["tax_year", "section", "category", "amount_minor"])
    for s in summaries:
        for section, rows in _tax_sections(s):
            for category, _label, amount in rows:
                writer.writerow([s.tax_year, section, category, amount])


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.2f}%"


_TAX_SECTION_TITLES = {
    "income": "Income",
    "memo": "Memo (not in total income)",
    "withheld": "Withheld",
    "fica": "FICA",
    "liability": "Tax liability (from return)",
    "result": "Payments and result",
}


def _print_tax_text(summaries: Sequence[TaxYearSummary]) -> None:
    for s in summaries:
        basis = s.income_basis.replace("_", " ")
        typer.echo(
            f"Tax year {s.tax_year}  (income basis: {basis}; {s.document_count} document(s), "
            f"{s.form_count} form(s))"
        )
        for section, rows in _tax_sections(s):
            if not rows:
                continue
            typer.echo(f"  {_TAX_SECTION_TITLES[section]}")
            width = max(len(label) for _c, label, _a in rows)
            for _category, label, amount in rows:
                typer.echo(f"    {label.ljust(width)}  {_format_minor(amount):>14}")
        if s.refund_or_owed_derived:
            typer.echo("    (refund / owed derived from total payments minus total tax)")
        typer.echo("  Rates")
        typer.echo(f"    Federal effective rate  {_pct(s.federal_effective_rate):>8}")
        typer.echo(f"    All-in rate             {_pct(s.all_in_rate):>8}")
        typer.echo(f"    Federal withholding     {_pct(s.withholding_rate):>8}")
        for warning in s.warnings:
            typer.echo(f"  warning: {warning}")
        typer.echo("")


@tax_app.command(name="delete")
def tax_delete_cmd(
    document_id: int = typer.Argument(..., help="Tax document id (see `tax list`)."),
) -> None:
    """Delete an imported tax document and everything parsed from it."""
    from personal_finance_analyzer.ingest.tax.pipeline import delete_tax_document

    with _open_engine() as engine, _open_session(engine) as session:
        if not delete_tax_document(session, document_id):
            typer.echo(f"no tax document with id {document_id}", err=True)
            raise typer.Exit(code=1)
    typer.echo(f"Deleted tax document #{document_id}.")


# --- tax estimate and parameters ------------------------------------------------------------------

tax_params_app = typer.Typer(help="Federal tax parameters (brackets, deductions, limits) in use.")
tax_app.add_typer(tax_params_app, name="params")


def _echo_estimate_lines(rows: Sequence[tuple[str, int | None]], *, width: int = 44) -> None:
    for label, amount in rows:
        if amount is not None:
            typer.echo(f"    {label.ljust(width)}  {_format_minor(amount):>14}")


@tax_app.command(name="estimate")
def tax_estimate_cmd(
    year: int = typer.Option(..., "--year", help="Tax year to estimate (see `tax params show`)."),
    user_id: int | None = typer.Option(
        None, "--user-id", help="The user to estimate for (defaults to the default user)."
    ),
) -> None:
    """Federal tax estimate for one user and year, from the W-2 / 1099 forms and the tax profile
    (set it in the web UI); the Form 1040, when imported, is only a cross-check. Local only."""
    from personal_finance_analyzer.analytics import tax_estimate

    _check_tax_year(year, "--year")
    with _open_engine() as engine, _open_session(engine) as session:
        resolved_user_id = _require_user_id(session, user_id)
        try:
            est = tax_estimate.run_tax_estimate(session, resolved_user_id, year)
        except tax_estimate.EstimateUnavailable as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
        except tax_estimate.EstimateConflict as exc:
            typer.echo(f"conflicting tax profiles: {exc}", err=True)
            raise typer.Exit(code=1) from None
    c = est.calc
    household = ", ".join(f"#{u}" for u in est.household_user_ids)
    typer.echo(
        f"Federal tax estimate {est.tax_year}, user #{est.user_id}  "
        f"(filing status {c.filing_status}, from {est.filing_status_source}"
        + (f"; joint with {household}" if len(est.household_user_ids) > 1 else "")
        + f"; parameters v{est.parameters_version}, {est.parameters_source})"
    )
    typer.echo("  Income")
    _echo_estimate_lines(
        [
            ("Wages and salary", c.wages_minor),
            ("Interest", c.interest_minor),
            ("Ordinary dividends", c.ordinary_dividends_minor),
            ("Self-employment income", c.self_employment_minor),
            ("Other income (forms)", c.other_income_minor),
            ("Other income (profile)", c.profile_other_income_minor),
            ("Net capital gain/loss counted", c.capital_result_counted_minor),
            ("Gross income", c.gross_income_minor),
        ]
    )
    typer.echo("  Deduction and taxable income")
    _echo_estimate_lines(
        [
            ("Adjustments", c.adjustments_minor),
            ("Half of self-employment tax", c.se_tax_deduction_minor),
            ("Adjusted gross income", c.agi_minor),
            (f"Deduction ({c.deduction_mode})", c.deduction_minor),
            ("Senior deduction", c.senior_deduction_minor or None),
            ("Taxable income", c.taxable_income_minor),
            ("  of which dividends and gains", c.preferential_income_minor),
        ]
    )
    typer.echo("  Tax")
    _echo_estimate_lines(
        [
            ("Income tax", c.income_tax_minor),
            ("  on ordinary income", c.ordinary_tax_minor),
            ("  on dividends and gains", c.preferential_tax_minor),
            ("Nonrefundable credits used", -c.nonrefundable_credits_minor),
            ("Self-employment tax", c.se_tax_minor),
            ("Net investment income tax", c.net_investment_income_tax_minor or None),
            ("Additional Medicare Tax", c.additional_medicare_tax_minor or None),
            ("Other taxes", c.other_taxes_minor),
            ("Total tax", est.calc.total_tax_minor),
        ]
    )
    typer.echo("  Payments and result")
    _echo_estimate_lines(
        [
            ("Federal tax withheld", c.federal_withheld_minor),
            ("Estimated payments", c.estimated_payments_minor),
            ("Other payments", c.other_payments_minor),
            ("Refundable credits", c.refundable_credits_minor),
            ("Additional Medicare Tax withheld", c.additional_medicare_withheld_minor or None),
            ("Excess Social Security credit", c.excess_social_security_credit_minor or None),
            ("Total payments", c.total_payments_minor),
            ("Refund" if c.result_minor >= 0 else "Amount owed", abs(c.result_minor)),
        ]
    )
    if est.scope == "household_allocation":
        typer.echo("  This user's part (an allocation of the household tax by share of income)")
        share = f"{(est.share or 0) * 100:.1f}%"
        _echo_estimate_lines(
            [
                (f"Allocated tax ({share} of gross income)", est.user_total_tax_minor),
                ("Own payments", est.user_payments_minor),
                (
                    "Refund" if est.user_result_minor >= 0 else "Amount owed",
                    abs(est.user_result_minor),
                ),
            ]
        )
    typer.echo("  Rates")
    typer.echo(f"    Effective rate          {_pct(c.effective_rate):>8}")
    typer.echo(f"    Marginal (ordinary)     {_pct(c.marginal_rate):>8}")
    typer.echo(f"    Dividends and gains     {_pct(c.preferential_rate):>8}")
    if c.retirement_deferral_minor or c.hsa_employer_minor:
        typer.echo("  W-2 box 12 (memo, already excluded from wages)")
        _echo_estimate_lines(
            [
                ("Retirement plan deferrals", c.retirement_deferral_minor),
                ("Employer HSA contributions", c.hsa_employer_minor),
            ]
        )
    cc = est.cross_check
    if cc is not None:
        typer.echo("  Cross-check against the Form 1040 (not used in the estimate)")
        _echo_estimate_lines(
            [
                ("Return total tax", cc.return_total_tax_minor),
                ("  estimate minus return", cc.delta_total_tax_minor),
                ("Return federal withholding (line 25d)", cc.return_withheld_minor),
                ("Return refund (+) / owed (-)", cc.return_result_minor),
                ("  estimate minus return", cc.delta_result_minor),
            ],
            width=44,
        )
    for warning in est.warnings:
        typer.echo(f"  warning: {warning}")
    typer.echo("  Not modeled:")
    for item in c.not_modeled:
        typer.echo(f"    - {item}")


def _print_params_status(status: Mapping[str, object]) -> None:
    typer.echo(
        f"Tax parameters in use: version {status['version']} ({status['source']}); "
        f"bundled v{status['bundled']}, downloaded {status['cached'] or 'none'}, "
        f"imported {status['override'] or 'none'}"
    )
    typer.echo(f"Years: {status['years']}")
    typer.echo(
        f"Last update check: {status['last_checked'] or 'never'}"
        + (f" ({status['outcome']})" if status["outcome"] else "")
        + f"; automatic yearly check: {'on' if status['auto'] else 'off'} "
        "([tax] auto_update_params)"
    )


def _params_status() -> dict[str, object]:
    from personal_finance_analyzer.config import load_settings
    from personal_finance_analyzer.core import tax_params

    current = tax_params.resolve()
    versions = tax_params.versions()
    state = tax_params.read_check_state()
    return {
        "version": current.version,
        "source": current.source,
        "bundled": versions["bundled"],
        "cached": versions["cached"],
        "override": versions["override"],
        "years": ", ".join(str(y) for y in sorted(current.years)),
        "last_checked": state.last_checked.isoformat() if state.last_checked else None,
        "outcome": state.outcome,
        "auto": load_settings().tax.auto_update_params,
    }


@tax_params_app.command(name="show")
def tax_params_show_cmd() -> None:
    """Which parameter file is in effect, the supported years and the update-check state."""
    _print_params_status(_params_status())


@tax_params_app.command(name="update")
def tax_params_update_cmd() -> None:
    """Check for updated parameters now: one plain GET of the project's public parameter file (no
    user data), whatever `[tax] auto_update_params` says. Records the check date."""
    from personal_finance_analyzer.api.services import tax_params_update

    result = tax_params_update.check_for_update()
    typer.echo(f"{result.outcome}: {result.detail}")
    _print_params_status(_params_status())
    if result.outcome in ("rejected", "unreachable"):
        raise typer.Exit(code=1)


@tax_params_app.command(name="import")
def tax_params_import_cmd(
    path: str = typer.Argument(..., help="A parameter file in the bundled file's format."),
) -> None:
    """Use your own parameter file (it wins over the bundled and downloaded ones; `tax params clear`
    removes it). It is validated like the bundled file first."""
    from personal_finance_analyzer.core import tax_params

    source = Path(path)
    if not source.is_file():
        typer.echo(f"{path}: no such file", err=True)
        raise typer.Exit(code=1)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        typer.echo(f"{path}: cannot read it as UTF-8 text", err=True)
        raise typer.Exit(code=1) from None
    try:
        installed = tax_params.install_override(text)
    except ValueError as exc:
        typer.echo(f"{path}: not a valid parameter file: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Imported parameter file (version {installed.version}).")
    _print_params_status(_params_status())


@tax_params_app.command(name="clear")
def tax_params_clear_cmd() -> None:
    """Remove the imported parameter file, going back to the downloaded or bundled one."""
    from personal_finance_analyzer.core import tax_params

    typer.echo(
        "Removed the imported parameter file."
        if tax_params.clear_override()
        else "No imported parameter file."
    )
