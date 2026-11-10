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
