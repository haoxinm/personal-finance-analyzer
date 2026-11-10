"""Proves the ORM metadata and the Alembic migration produce identical schemas (P0-3 Accepts).

A `CREATE TABLE` statement is compared clause-by-clause rather than as a literal string: once a
migration touches a table with `batch_alter_table` (needed for SQLite, e.g. `0002_...`, I4), the
table is recreated (copy-data-drop-rename) and the DDL SQLAlchemy emits for it, while
semantically identical, can quote the table name and order columns/constraints differently from a
single `Base.metadata.create_all()`. Comparing the *set* of top-level clauses (and doing the same
normalization for index DDL) is robust to that reordering/quoting while still catching a genuine
schema drift (a missing column, a changed type, a dropped constraint, ...).
"""

from __future__ import annotations

import re
from pathlib import Path

from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.models import Base
from personal_finance_analyzer.db.session import make_engine_for_path


def _split_top_level(body: str) -> list[str]:
    """Split ``body`` on commas that are not nested inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _normalize_sql(sql: str) -> str:
    """Canonicalize one `sqlite_master.sql` value: drop identifier quoting, collapse
    whitespace, and — for a `CREATE TABLE`, whose column/constraint order can legitimately differ
    after a `batch_alter_table` recreate — sort its top-level clauses."""
    sql = sql.replace('"', "").replace("\n", " ")
    sql = re.sub(r"\s+", " ", sql).strip()
    match = re.match(r"^(CREATE TABLE \S+) \((.*)\)$", sql)
    if not match:
        return sql
    header, body = match.groups()
    clauses = sorted(clause.strip() for clause in _split_top_level(body))
    return f"{header} ({', '.join(clauses)})"


def _sqlite_master_rows(db_path: Path) -> set[tuple[str, str, str]]:
    engine = make_engine_for_path(db_path)
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND name != 'alembic_version' "
                "ORDER BY type, name"
            ).fetchall()
    finally:
        engine.dispose()
    return {(str(r[0]), str(r[1]), _normalize_sql(str(r[2]))) for r in rows}


def test_orm_metadata_and_migration_produce_identical_schema(tmp_path: Path) -> None:
    via_migration_path = tmp_path / "via_migration.db"
    upgrade_head(f"sqlite:///{via_migration_path}")

    via_metadata_path = tmp_path / "via_metadata.db"
    engine = make_engine_for_path(via_metadata_path)
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()

    migration_schema = _sqlite_master_rows(via_migration_path)
    metadata_schema = _sqlite_master_rows(via_metadata_path)
    assert migration_schema == metadata_schema
