"""Migration 0011: absolute ``stored_path`` values inside the documents directory become relative to
it; everything else is left exactly as it was (the reader finds those files by content)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.migrate import build_alembic_config

_PREVIOUS = "0010_user_merchant_pins_to_rules"


def _database(tmp_path: Path) -> tuple[str, Path]:
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"
    command.upgrade(build_alembic_config(url), _PREVIOUS)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO users (id, name, is_default, pii_aliases, created_at) VALUES (1, 'A', 1, '[]', 'x')"
    )
    con.commit()
    con.close()
    return url, db


def _add(db: Path, statements: list[str | None], documents: list[str | None]) -> None:
    con = sqlite3.connect(db)
    for i, path in enumerate(statements):
        con.execute(
            "INSERT INTO statements (file_sha256, original_name, stored_path, status, ingested_at) "
            "VALUES (?, 'a.pdf', ?, 'parsed', 'x')",
            (f"{i:064x}", path),
        )
    for i, path in enumerate(documents):
        con.execute(
            "INSERT INTO tax_documents (user_id, file_sha256, original_name, stored_path, status, "
            "ingested_at) VALUES (1, ?, 'w2.pdf', ?, 'parsed', 'x')",
            (f"{i + 100:064x}", path),
        )
    con.commit()
    con.close()


def _paths(db: Path, table: str) -> list[str | None]:
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute(f"SELECT stored_path FROM {table} ORDER BY id")]
    finally:
        con.close()


def test_absolute_paths_inside_the_documents_dir_become_relative(tmp_path: Path) -> None:
    url, db = _database(tmp_path)
    docs = documents_dir()
    _add(
        db,
        [str(docs / "Alex" / "Statements" / "Chase" / "2024-03 Chase.pdf"), None],
        [str(docs / "Alex" / "Tax" / "2024" / "W-2 - Acme.pdf")],
    )
    command.upgrade(build_alembic_config(url), "head")
    assert _paths(db, "statements") == ["Alex/Statements/Chase/2024-03 Chase.pdf", None]
    assert _paths(db, "tax_documents") == ["Alex/Tax/2024/W-2 - Acme.pdf"]


def test_everything_else_is_left_untouched(tmp_path: Path, home: Path) -> None:
    url, db = _database(tmp_path)
    old = str(home / "statements" / f"{'a' * 64}.pdf")  # the old folder: moved by the startup step
    foreign = "/old/machine/statements/x.pdf"
    windows = r"C:\Users\x\AppData\statements\y.pdf"
    already = "Alex/Statements/Chase/2024-03 Chase.pdf"
    sneaky = str(documents_dir()) + "/../../etc/passwd"
    unparseable = "not a path at all ???"
    root_only = str(documents_dir())
    _add(db, [old, foreign, windows, already], [sneaky, unparseable, root_only])
    command.upgrade(build_alembic_config(url), "head")
    assert _paths(db, "statements") == [old, foreign, windows, already]
    assert _paths(db, "tax_documents") == [sneaky, unparseable, root_only]


def test_a_sibling_directory_sharing_the_prefix_is_not_inside(tmp_path: Path) -> None:
    url, db = _database(tmp_path)
    sibling = str(documents_dir()) + "-old/Alex/x.pdf"
    _add(db, [sibling], [])
    command.upgrade(build_alembic_config(url), "head")
    assert _paths(db, "statements") == [sibling]


def test_downgrade_restores_absolute_paths(tmp_path: Path) -> None:
    url, db = _database(tmp_path)
    inside = str(documents_dir() / "Alex" / "x.pdf")
    _add(db, [inside], [])
    command.upgrade(build_alembic_config(url), "head")
    assert _paths(db, "statements") == ["Alex/x.pdf"]
    command.downgrade(build_alembic_config(url), _PREVIOUS)
    assert _paths(db, "statements") == [inside]


def test_upgrading_twice_changes_nothing(tmp_path: Path) -> None:
    url, db = _database(tmp_path)
    _add(db, [str(documents_dir() / "Alex" / "x.pdf")], [])
    command.upgrade(build_alembic_config(url), "head")
    command.upgrade(build_alembic_config(url), "head")
    assert _paths(db, "statements") == ["Alex/x.pdf"]


def test_there_is_exactly_one_head() -> None:
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(build_alembic_config()).get_heads()
    assert heads == ["0011_relative_stored_paths"]


@pytest.mark.parametrize("value", ["", " "])
def test_blank_values_are_left_alone(tmp_path: Path, value: str) -> None:
    url, db = _database(tmp_path)
    _add(db, [value], [])
    command.upgrade(build_alembic_config(url), "head")
    assert _paths(db, "statements") == [value]
