"""Shared helpers for the backup tests: a fully seeded data directory and tree snapshots."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from personal_finance_analyzer.core.paths import create_home_layout
from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.models import (
    Account,
    Issuer,
    Job,
    Statement,
    TaxDocument,
    TaxForm,
    Transaction,
    User,
)
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory

PASSPHRASE = "correct horse battery staple"
SECRET_API_KEY = "sk-live-this-must-never-be-archived"
SECRET_HEADER = "Bearer hunter2-header-secret"
SECRET_URL_PASSWORD = "urlpass-secret"

#: Content that must appear in the restored data, and (for the "no PII" checks) must never be
#: printed by `backup inspect`.
USER_NAME = "Alexandra Quillfeather"
DESCRIPTION = "ZZ UNIQUE COFFEE 0042"

CONFIG_TOML = f"""\
[llm]
enabled = true
base_url = "https://svc:{SECRET_URL_PASSWORD}@llm.example.com"
model = "some-model"
extra_headers = {{ Authorization = "{SECRET_HEADER}" }}

[privacy]
pii_terms = ["Quillfeather"]

[server]
port = 8756
"""


def pdf_bytes(tag: str) -> bytes:
    return b"%PDF-1.4\n" + tag.encode() * 500 + b"\n%%EOF"


STATEMENT_REL = f"{USER_NAME}/Statements/Fixture Bank/2026-01 Fixture Bank credit 9876.pdf"
TAX_REL = f"{USER_NAME}/Tax/2025/W-2 - Acme Corp.pdf"


def seed_home(home: Path, *, legacy: bool = False, documents: Path | None = None) -> dict[str, str]:
    """Create ``home`` as a migrated data directory holding one of everything a backup carries,
    plus the things it must leave out. The PDFs are in the documents folder (``documents``,
    default ``<home>/documents``) under readable names and the database records them relative to
    it; ``legacy=True`` lays them out the way versions before the documents folder did instead
    (hash-named in ``<home>/statements``, absolute ``stored_path``). Returns the PDFs' hashes
    (``statement_sha``, ``tax_sha``) and their relative paths (``statement_rel``, ``tax_rel``)."""
    create_home_layout(home)
    db_file = home / "finance.db"
    upgrade_head(f"sqlite:///{db_file}")

    statement_pdf = pdf_bytes("statement")
    statement_sha = hashlib.sha256(statement_pdf).hexdigest()
    tax_pdf = pdf_bytes("taxform")
    tax_sha = hashlib.sha256(tax_pdf).hexdigest()
    if legacy:
        legacy_dir = home / "statements"
        legacy_dir.mkdir(exist_ok=True)
        (legacy_dir / f"{statement_sha}.pdf").write_bytes(statement_pdf)
        (legacy_dir / f"{tax_sha}.pdf").write_bytes(tax_pdf)
        # A transient upload temp file sits next to the real ones; it must not be archived.
        (legacy_dir / "tmpabcd1234.pdf").write_bytes(b"%PDF-partial")
        statement_stored = str(legacy_dir / f"{statement_sha}.pdf")
        tax_stored = str(legacy_dir / f"{tax_sha}.pdf")
    else:
        docs = documents if documents is not None else home / "documents"
        for rel, data in ((STATEMENT_REL, statement_pdf), (TAX_REL, tax_pdf)):
            path = docs.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        statement_stored, tax_stored = STATEMENT_REL, TAX_REL

    engine = make_engine_for_path(db_file)
    try:
        with make_session_factory(engine)() as session:
            user = User(name=USER_NAME, is_default=True)
            issuer = Issuer(name="Fixture Bank", slug="fixture-bank")
            session.add_all([user, issuer])
            session.flush()
            account = Account(
                user_id=user.id,
                issuer_id=issuer.id,
                account_type="credit",
                display_name="Fixture Bank credit ...9876",
                mask="9876",
            )
            session.add(account)
            session.flush()
            statement = Statement(
                account_id=account.id,
                user_id=user.id,
                file_sha256=statement_sha,
                original_name="jan.pdf",
                stored_path=statement_stored,
                period_end="2026-01-31",
                status="parsed",
            )
            session.add(statement)
            session.flush()
            session.add(
                Transaction(
                    statement_id=statement.id,
                    account_id=account.id,
                    user_id=user.id,
                    posted_date="2026-01-02",
                    description_raw=DESCRIPTION,
                    description_clean="coffee",
                    merchant_key="coffee",
                    amount_minor=450,
                    currency="USD",
                    kind="purchase",
                    is_spend=True,
                    dedupe_hash="h1",
                )
            )
            document = TaxDocument(
                user_id=user.id,
                file_sha256=tax_sha,
                original_name="w2.pdf",
                stored_path=tax_stored,
                tax_year=2025,
                status="parsed",
            )
            session.add(document)
            session.flush()
            session.add(
                TaxForm(
                    document_id=document.id,
                    user_id=user.id,
                    tax_year=2025,
                    form_type="w2",
                    role="source",
                    payer_name="Acme Corp",
                    fingerprint="fixture-fingerprint",
                )
            )
            session.add(Job(id="job-1", kind="import", status="queued"))
            session.commit()
    finally:
        engine.dispose()

    (home / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    (home / "specs" / "my-bank.yaml").write_text("name: my-bank\n", encoding="utf-8")
    # Per-machine state that must stay behind.
    (home / "extract_cache" / "deadbeef.txt").write_text("page text", encoding="utf-8")
    (home / "logs" / "app.log").write_text("a log line", encoding="utf-8")
    (home / "tax_staging").mkdir()
    (home / "tax_staging" / "1.pdf").write_bytes(pdf_bytes("staged"))
    return {
        "statement_sha": statement_sha,
        "tax_sha": tax_sha,
        "statement_rel": STATEMENT_REL,
        "tax_rel": TAX_REL,
    }


def table_rows(
    db_file: Path, table: str, *, skip: tuple[str, ...] = ()
) -> list[tuple[object, ...]]:
    con = sqlite3.connect(db_file)
    try:
        columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})") if r[1] not in skip]
        cols = ", ".join(columns)
        return con.execute(f"SELECT {cols} FROM {table} ORDER BY 1").fetchall()
    finally:
        con.close()


def tree(root: Path) -> dict[str, bytes | None]:
    """Every file (bytes) and empty-or-not directory (None) under ``root``, for before/after
    comparisons proving a failed restore changed nothing."""
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = path.read_bytes() if path.is_file() else None
    return out
