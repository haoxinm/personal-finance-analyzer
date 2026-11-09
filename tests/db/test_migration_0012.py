"""Migration 0012: LLM-sourced `others/uncategorized` merchant-cache rows are deleted; real cache
answers and rows from other sources stay."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory

from personal_finance_analyzer.classify.taxonomy import sync_taxonomy
from personal_finance_analyzer.db.migrate import build_alembic_config
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

_PREVIOUS = "0011_relative_stored_paths"


def _database(tmp_path: Path) -> tuple[str, Path, dict[str, tuple[int, int]]]:
    db = tmp_path / "mig.db"
    url = f"sqlite:///{db}"
    command.upgrade(build_alembic_config(url), _PREVIOUS)
    engine = make_engine_for_path(db)
    with make_session_factory(engine)() as session:
        sync_taxonomy(session)
        session.commit()
    engine.dispose()
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT c.key || '/' || s.key, c.id, s.id FROM subcategories s "
            "JOIN categories c ON c.id = s.category_id"
        ).fetchall()
    finally:
        con.close()
    return url, db, {name: (cid, sid) for name, cid, sid in rows}


def _seed(db: Path, rows: list[tuple[str, tuple[int, int], str]]) -> None:
    con = sqlite3.connect(db)
    for key, (cid, sid), source in rows:
        con.execute(
            "INSERT INTO merchant_map (merchant_key, merchant_canonical, category_id, "
            "subcategory_id, source, confidence, hit_count, updated_at) "
            "VALUES (?, NULL, ?, ?, ?, 0.5, 4, 'now')",
            (key, cid, sid, source),
        )
    con.commit()
    con.close()


def _left(db: Path) -> dict[str, str]:
    con = sqlite3.connect(db)
    try:
        return dict(con.execute("SELECT merchant_key, source FROM merchant_map"))
    finally:
        con.close()


def test_only_llm_uncategorized_cache_rows_are_deleted(tmp_path: Path) -> None:
    url, db, pairs = _database(tmp_path)
    unc, fuel = pairs["others/uncategorized"], pairs["fuel/gasoline"]
    _seed(
        db,
        [
            ("placeholder", unc, "llm"),  # deleted
            ("placeholder two", unc, "llm"),  # deleted
            ("real answer", fuel, "llm"),  # a genuine cached answer: kept
            ("user pin", unc, "user"),  # not a cache row: kept
            ("issuer row", unc, "issuer"),  # kept
            ("builtin row", unc, "builtin"),  # kept
        ],
    )

    command.upgrade(build_alembic_config(url), "head")

    assert _left(db) == {
        "real answer": "llm",
        "user pin": "user",
        "issuer row": "issuer",
        "builtin row": "builtin",
    }


def test_upgrade_is_idempotent_and_downgrade_is_a_noop(tmp_path: Path) -> None:
    url, db, pairs = _database(tmp_path)
    _seed(
        db,
        [
            ("placeholder", pairs["others/uncategorized"], "llm"),
            ("pin", pairs["others/uncategorized"], "user"),
        ],
    )
    config = build_alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, _PREVIOUS)
    assert _left(db) == {"pin": "user"}
    command.upgrade(config, "head")
    assert _left(db) == {"pin": "user"}


def test_a_database_with_no_cache_rows_upgrades(tmp_path: Path) -> None:
    url, db, _ = _database(tmp_path)
    command.upgrade(build_alembic_config(url), "head")
    assert _left(db) == {}


def test_single_alembic_head() -> None:
    heads = ScriptDirectory.from_config(build_alembic_config("sqlite://")).get_heads()
    assert heads == ["0012_drop_unplaced_merchant_cache"]
