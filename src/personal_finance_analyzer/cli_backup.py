"""`pfa backup ...`: move all your data to another machine (create -> copy the file ->
restore). Kept in its own module, like `cli_llm.py`, and mounted from `cli.py`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from personal_finance_analyzer.backup import archive, crypto
from personal_finance_analyzer.backup.errors import BackupError

backup_app = typer.Typer(
    help=(
        "Back up all your data to one file and restore it on another machine. The LLM API key "
        "in your OS keychain is never included; set it again with `pfa llm set-key`."
    ),
    no_args_is_help=True,
)

_PassphraseFile = Annotated[
    Path | None,
    typer.Option(
        "--passphrase-file",
        help="Read the passphrase from the first line of this file instead of prompting.",
    ),
]


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=1)


def _read_passphrase_file(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(f"cannot read {path}: {exc}") from exc
    if not lines or not lines[0]:
        raise _fail(f"{path} is empty; put the passphrase on its first line")
    return lines[0]


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in counts.items())


@backup_app.command(name="create")
def backup_create(
    out: Annotated[
        Path,
        typer.Argument(help="File to write, or a directory to create a dated file in."),
    ],
    passphrase_file: _PassphraseFile = None,
    no_encrypt: Annotated[
        bool,
        typer.Option(
            "--no-encrypt",
            help="Write the backup unencrypted. It holds your statements and tax forms in clear.",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite OUT if it exists.")] = False,
) -> None:
    """Write a backup of the whole data directory (database, statement PDFs, layout specs and
    settings). Safe to run while `serve` is running. Encrypted with a passphrase unless
    `--no-encrypt` is given."""
    dest = out / archive.default_filename() if out.is_dir() else out
    if dest.exists() and not force:
        raise _fail(f"{dest} already exists; pass --force to overwrite it")
    passphrase: str | None
    if no_encrypt:
        if passphrase_file is not None:
            raise _fail("--no-encrypt and --passphrase-file cannot be used together")
        passphrase = None
    elif passphrase_file is not None:
        passphrase = _read_passphrase_file(passphrase_file)
    else:
        passphrase = typer.prompt(
            "Passphrase (needed to restore; there is no recovery if you lose it)",
            hide_input=True,
            confirmation_prompt=True,
        )
    try:
        manifest = archive.create_backup(dest, passphrase=passphrase)
    except BackupError as exc:
        raise _fail(str(exc)) from exc
    size_mb = dest.stat().st_size / (1024 * 1024)
    typer.echo(f"Backup written to {dest} ({size_mb:.1f} MB)")
    typer.echo(f"Contains: {_counts_line(manifest.counts)}; {len(manifest.files) - 2} files.")
    if passphrase is None:
        typer.echo(
            "WARNING: this backup is NOT encrypted; anyone who gets the file can read your "
            "statements and tax forms. Keep it somewhere private.",
            err=True,
        )
    typer.echo(
        "Not included: your LLM API key (it stays in this machine's keychain; run "
        "`pfa llm set-key` on the new one), the extract cache and logs."
    )


@backup_app.command(name="inspect")
def backup_inspect(
    file: Annotated[Path, typer.Argument(help="A .pfabackup file.")],
    passphrase_file: _PassphraseFile = None,
) -> None:
    """Show what a backup contains (dates and counts only, nothing personal) without restoring
    it. An encrypted backup needs its passphrase."""
    info = _open_info(file, passphrase_file)
    manifest = info.manifest
    pdfs = sum(1 for f in manifest.files if f.path.startswith("statements/"))
    typer.echo(f"File:       {file}")
    typer.echo(f"Encrypted:  {'yes' if info.encrypted else 'no'}")
    typer.echo(
        f"Created:    {manifest.created_at} (Personal Finance Analyzer {manifest.app_version}, "
        f"{manifest.source_platform})"
    )
    typer.echo(
        f"Format:     version {manifest.format_version}; database schema "
        f"{manifest.schema_revision} (this app: {archive.head_revision()})"
    )
    for name, count in manifest.counts.items():
        typer.echo(f"  {name.replace('_', ' '):<14}{count}")
    typer.echo(f"  {'statement PDFs':<14}{pdfs}")
    typer.echo(
        f"Total size: {sum(f.size for f in manifest.files) / (1024 * 1024):.1f} MB uncompressed"
    )


def _open_info(file: Path, passphrase_file: Path | None) -> archive.BackupInfo:
    if not file.is_file():
        raise _fail(f"{file}: no such file")
    try:
        passphrase = _passphrase_for(file, passphrase_file)
        return archive.inspect_backup(file, passphrase=passphrase)
    except BackupError as exc:
        raise _fail(str(exc)) from exc


def _passphrase_for(file: Path, passphrase_file: Path | None) -> str | None:
    """The passphrase to open ``file`` with: from ``--passphrase-file``, else a prompt when the
    file is encrypted, else none."""
    if passphrase_file is not None:
        return _read_passphrase_file(passphrase_file)
    try:
        with file.open("rb") as fh:
            encrypted = crypto.is_encrypted(fh)
    except OSError as exc:
        raise _fail(f"cannot read {file}: {exc}") from exc
    if not encrypted:
        return None
    return str(typer.prompt("Passphrase", hide_input=True))


@backup_app.command(name="restore")
def backup_restore(
    file: Annotated[Path, typer.Argument(help="A .pfabackup file.")],
    passphrase_file: _PassphraseFile = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help=(
                "Replace existing data. The current data directory is moved to "
                "`<name>.<timestamp>.before-restore`, not deleted."
            ),
        ),
    ] = False,
) -> None:
    """Restore a backup as this machine's data directory. The archive is fully verified before
    anything is touched; an older database is migrated; stop `serve` first."""
    if not file.is_file():
        raise _fail(f"{file}: no such file")
    try:
        passphrase = _passphrase_for(file, passphrase_file)
        result = archive.restore_backup(file, passphrase=passphrase, replace=replace)
    except BackupError as exc:
        raise _fail(str(exc)) from exc
    manifest = result.manifest
    typer.echo(f"Restored backup from {manifest.created_at}: {_counts_line(manifest.counts)}.")
    if result.migrated:
        typer.echo(f"Database upgraded from schema {manifest.schema_revision} to this version.")
    if result.previous_dir is not None:
        typer.echo(f"Your previous data directory was kept at {result.previous_dir}")
    if result.kept_secrets:
        typer.echo(
            "Your plaintext API key file (secrets/api_keys.toml) was carried over from the "
            "previous data directory; it is not part of a backup."
        )
    typer.echo(
        "If you use an LLM that needs an API key, set it again with `pfa llm "
        "set-key` (keys are never part of a backup). Then run `pfa serve`."
    )
