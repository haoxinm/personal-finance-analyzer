"""record kept-PDF paths relative to the documents directory

Revision ID: 0011_relative_stored_paths
Revises: 0010_user_merchant_pins_to_rules
Create Date: 2026-11-09 00:00:00

`statements.stored_path` and `tax_documents.stored_path` used to hold the absolute path of the kept
PDF copy (``<data dir>/statements/<sha256>.pdf``). The copies now live in a *documents directory*
under readable names, and the column holds the path relative to it
(``Alex/Statements/Chase/2024-03 Chase credit 1234.pdf``), so the folder can be moved, mounted
elsewhere or restored on another machine without touching the database.

This migration rewrites an absolute path that already points inside the configured documents
directory (`core.paths.documents_dir`) to its relative form. Every other value is left exactly as
it is, deliberately:

- a path inside the old ``statements/`` folder names a file that has not been moved yet: the
  startup step that carries those files over (`db.documents.migrate_legacy_files`, which runs once
  the schema is current) files each under a readable name and records the new relative path;
- a path that cannot be parsed or points somewhere else (another machine's backup) stays as is:
  the reader falls back to finding the file by its SHA-256, so nothing is ever lost.

Downgrade turns relative values back into absolute ones under the documents directory.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import sqlalchemy as sa
from alembic import op

from personal_finance_analyzer.core.paths import documents_dir

# revision identifiers, used by Alembic.
revision: str = "0011_relative_stored_paths"
down_revision: str | None = "0010_user_merchant_pins_to_rules"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_TABLES = ("statements", "tax_documents")


def _prefixes(root: Path) -> list[str]:
    found = [str(root)]
    try:
        resolved = str(root.resolve())
    except OSError:
        resolved = str(root)
    if resolved not in found:
        found.append(resolved)
    return found


def to_relative(stored: str, root: Path) -> str | None:
    """``stored`` relative to ``root`` when it is an absolute path inside it, else ``None``."""
    if not os.path.isabs(stored):
        return None
    for prefix in _prefixes(root):
        base = prefix.rstrip(os.sep) + os.sep
        if stored.startswith(base) and len(stored) > len(base):
            parts = Path(stored[len(base) :]).parts
            if parts and ".." not in parts:
                return PurePosixPath(*parts).as_posix()
    return None


def upgrade() -> None:
    bind = op.get_bind()
    root = documents_dir()
    for table in _TABLES:
        columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        if "stored_path" not in columns:
            continue
        rows = bind.execute(
            sa.text(f"SELECT id, stored_path FROM {table} WHERE stored_path IS NOT NULL")
        ).fetchall()
        for row_id, stored in rows:
            relative = to_relative(stored, root)
            if relative is not None:
                bind.execute(
                    sa.text(f"UPDATE {table} SET stored_path = :p WHERE id = :i"),
                    {"p": relative, "i": row_id},
                )


def downgrade() -> None:
    bind = op.get_bind()
    root = documents_dir()
    for table in _TABLES:
        rows = bind.execute(
            sa.text(f"SELECT id, stored_path FROM {table} WHERE stored_path IS NOT NULL")
        ).fetchall()
        for row_id, stored in rows:
            if os.path.isabs(stored):
                continue
            bind.execute(
                sa.text(f"UPDATE {table} SET stored_path = :p WHERE id = :i"),
                {"p": str(root.joinpath(*PurePosixPath(stored).parts)), "i": row_id},
            )
