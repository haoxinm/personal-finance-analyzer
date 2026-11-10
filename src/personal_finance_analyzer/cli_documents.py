"""`pfa documents ...`: the readable folder of your imported PDFs (see `core/documents.py`).

Kept in its own module, like `cli_backup.py`, and mounted from `cli.py`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from personal_finance_analyzer.core.paths import documents_dir

documents_app = typer.Typer(
    help=(
        "Your imported statements and tax forms are kept as ordinary PDFs in a documents folder "
        "under readable names. Re-apply the naming, or check the folder against the database."
    ),
    no_args_is_help=True,
)


@documents_app.command(name="where")
def documents_where() -> None:
    """Print the documents folder (set it with PFA_DOCUMENTS_DIR or [storage] documents_dir in
    config.toml)."""
    typer.echo(str(documents_dir()))


@documents_app.command(name="reorganize")
def documents_reorganize(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be renamed; change nothing."),
    ] = False,
) -> None:
    """Rename the kept PDFs to the names the current data calls for (after you renamed a person
    or an account, or moved a statement to another account). The app never renames files by
    itself; this is how you ask for it. A file you moved by hand is left where you put it (the
    database just learns the new place), and one you deleted is reported, not recreated. Also
    carries any PDFs still in the old `statements/` folder over to the new layout."""
    from personal_finance_analyzer.cli import _open_engine, _open_session
    from personal_finance_analyzer.db import documents

    with _open_engine(prepare_documents=not dry_run) as engine, _open_session(engine) as session:
        report = documents.reorganize(session, dry_run=dry_run)
    verb = "Would rename" if dry_run else "Renamed"
    typer.echo(f"Documents folder: {documents_dir()}")
    if report.legacy is not None and (report.legacy.changed or report.legacy.errors):
        legacy = report.legacy
        lead = "Would move" if dry_run else "Moved"
        typer.echo(
            f"{lead} {len(legacy.filed)} PDF(s) from the old statements/ folder into the "
            f"readable layout ({legacy.staged} back to the upload staging folder)."
        )
        for error in legacy.errors:
            typer.echo(f"  could not move {error}", err=True)
    renamed = [c for c in report.changes if c.action == "renamed"]
    adopted = [c for c in report.changes if c.action == "adopted"]
    if renamed:
        typer.echo(f"{verb} {len(renamed)} file(s):")
        for change in renamed:
            typer.echo(f"  {change.old}\n    -> {change.new}")
    if adopted:
        lead = "Would keep" if dry_run else "Kept"
        typer.echo(f"{lead} {len(adopted)} file(s) you moved yourself, where you put them:")
        for change in adopted:
            typer.echo(f"  {change.new}")
    if report.missing:
        typer.echo(
            f"{len(report.missing)} row(s) have no PDF in the folder any more "
            "(see `pfa documents check`).",
            err=True,
        )
    if not renamed and not adopted:
        typer.echo(f"Nothing to rename ({report.unchanged} file(s) already named correctly).")
    if dry_run:
        typer.echo("Dry run: nothing was changed.")


@documents_app.command(name="scan")
def documents_scan(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be imported; import and record nothing."),
    ] = False,
) -> None:
    """Import the PDFs you put into the documents folder, once, without the server.

    The folder says whose a file is and what it is: <Person>/Statements/... is a statement and
    <Person>/Tax/<year>/... a tax form for that person (a folder whose name is not a person's is
    reported, never turned into a person). Files stay where you put them. This is the same scan
    the server runs in the background when folder imports are switched on in Settings; it takes
    the same lock, so it waits its turn rather than importing a file twice. If an LLM is enabled
    in Settings, new statements' descriptions are sent to it to be categorized, as in the app.
    Exits with status 1 when a file needs attention, 3 when another scan is running."""
    from personal_finance_analyzer.cli import _open_engine, _session_factory_for
    from personal_finance_analyzer.db.migrate import is_at_head
    from personal_finance_analyzer.watch.scanner import MANUAL_SETTLE_S, FolderScanner

    with _open_engine(prepare_documents=not dry_run) as engine:
        if not is_at_head(engine):
            typer.echo("The database is not up to date: run `pfa migrate` first.", err=True)
            raise typer.Exit(code=2)
        scanner = FolderScanner(_session_factory_for(engine))
        typer.echo(f"Documents folder: {scanner.root}")
        report = scanner.scan(dry_run=dry_run, settle_seconds=MANUAL_SETTLE_S)

    if report.busy:
        typer.echo("Another scan is running (the server?); try again in a moment.", err=True)
        raise typer.Exit(code=3)
    if report.error:
        typer.echo(report.error, err=True)
        raise typer.Exit(code=1)
    labels = {
        "imported": "imported",
        "would_import": "would import",
        "known": "already in the app",
        "failed": "NEEDS ATTENTION",
        "ignored": "ignored",
        "waiting": "waiting",
    }
    for item in report.items:
        who = ", ".join(p for p in (item.person, item.kind) if p)
        typer.echo(
            f"{labels.get(item.action, item.action)}: {item.path}" + (f" ({who})" if who else "")
        )
        if item.message:
            typer.echo(f"    {item.message}")
    typer.echo(
        f"{report.scanned} PDF(s) in the folder: "
        + (
            f"{report.would_import} would be imported, "
            if dry_run
            else f"{report.imported} imported, "
        )
        + f"{report.known} already known, {report.failed} need attention, "
        f"{report.ignored} ignored, {report.waiting} waiting."
    )
    if dry_run:
        typer.echo("Dry run: nothing was imported or recorded.")
    if report.failed:
        raise typer.Exit(code=1)


@documents_app.command(name="check")
def documents_check() -> None:
    """List PDFs the database expects but cannot find, PDFs found somewhere other than where the
    database says (moved by hand), and PDFs in the folder that no row refers to. Read-only; exits
    with status 1 when a PDF is missing."""
    from personal_finance_analyzer.cli import _open_engine, _open_session
    from personal_finance_analyzer.db import documents

    with _open_engine(prepare_documents=False) as engine, _open_session(engine) as session:
        report = documents.check(session)
    typer.echo(f"Documents folder: {report.documents_dir}")
    if report.legacy_files:
        typer.echo(
            f"{report.legacy_files} PDF(s) are still in the old statements/ folder; they are "
            "carried over the next time the app starts or `pfa documents reorganize` runs."
        )
    if report.moved:
        typer.echo(f"Moved by hand ({len(report.moved)}), still found by content:")
        for kind, row_id, old, new in report.moved:
            typer.echo(f"  {kind} {row_id}: {old}\n    now at {new}")
        typer.echo("  (`pfa documents reorganize` records the new places.)")
    if report.missing:
        typer.echo(f"Missing ({len(report.missing)}): the database has no PDF to show for")
        for kind, row_id, old in report.missing:
            typer.echo(f"  {kind} {row_id}: {old}")
    if report.orphans:
        typer.echo(f"Not referenced by any statement or tax form ({len(report.orphans)}):")
        for rel in report.orphans:
            typer.echo(f"  {rel}")
        typer.echo("  (left alone: the app never deletes files it does not recognise.)")
    if report.leftovers:
        typer.echo(f"Leftover temporary files from an interrupted copy ({len(report.leftovers)}):")
        for rel in report.leftovers:
            typer.echo(f"  {rel}")
    if report.ok and not (report.orphans or report.moved or report.leftovers):
        typer.echo("Everything the database refers to is in the folder, and nothing else is.")
    if not report.ok:
        raise typer.Exit(code=1)
