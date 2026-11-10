"""Migration 0010: legacy `merchant_map(source='user')` pins become visible exact user rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from sqlalchemy import Connection, Engine, text

from spend_analyzer.classify import cascade
from spend_analyzer.classify.taxonomy import sync_taxonomy
from spend_analyzer.config import LLMConfig
from spend_analyzer.db.migrate import build_alembic_config, upgrade_head
from spend_analyzer.db.models import (
    Account,
    Classification,
    Issuer,
    Statement,
    Transaction,
    User,
)
from spend_analyzer.db.session import make_engine_for_path, make_session_factory

_PREVIOUS = "0009_tax_profiles"


def _seeded(tmp_path: Path) -> tuple[str, Path, Engine, dict[str, tuple[int, int]]]:
    """A migrated database with the taxonomy synced, ready to be given legacy pins."""
    db = tmp_path / "pins.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    engine = make_engine_for_path(db)
    with make_session_factory(engine)() as session:
        sync_taxonomy(session)
        session.commit()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.key || '/' || s.key, c.id, s.id FROM subcategories s "
                "JOIN categories c ON c.id = s.category_id"
            )
        ).all()
    return url, db, engine, {name: (cid, sid) for name, cid, sid in rows}


def _rerun(url: str) -> None:
    """Run 0010's upgrade again on an already-migrated database (its downgrade is a no-op)."""
    config = build_alembic_config(url)
    command.downgrade(config, _PREVIOUS)
    command.upgrade(config, "head")


def _pin(conn: Connection, key: str, pair: tuple[int, int], source: str = "user") -> None:
    conn.execute(
        text(
            "INSERT INTO merchant_map (merchant_key, merchant_canonical, category_id, "
            "subcategory_id, source, confidence, hit_count, updated_at) "
            "VALUES (:k, NULL, :c, :s, :src, 1.0, 3, 'now')"
        ),
        {"k": key, "c": pair[0], "s": pair[1], "src": source},
    )


def _rule(
    conn: Connection,
    key: str,
    pair: tuple[int, int],
    *,
    user_id: int | None = None,
    enabled: int = 1,
    match_type: str = "exact",
    priority: int = 50,
) -> None:
    conn.execute(
        text(
            "INSERT INTO rules (user_id, match_type, pattern, category_id, subcategory_id, "
            "priority, enabled, source, created_at) "
            "VALUES (:u, :m, :k, :c, :s, :p, :e, 'user', 'now')"
        ),
        {
            "u": user_id,
            "m": match_type,
            "k": key,
            "c": pair[0],
            "s": pair[1],
            "p": priority,
            "e": enabled,
        },
    )


def _user_rules(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row._mapping)
            for row in conn.execute(
                text(
                    "SELECT pattern, match_type, priority, user_id, enabled, category_id, "
                    "subcategory_id, kind_override FROM rules WHERE source = 'user' "
                    "ORDER BY pattern, id"
                )
            )
        ]


def test_pins_become_visible_low_priority_rules_and_the_pins_are_removed(tmp_path: Path) -> None:
    url, db, engine, pairs = _seeded(tmp_path)
    grocery, fuel, hotels = (
        pairs["grocery/grocery_stores"],
        pairs["fuel/gasoline"],
        pairs["travel/hotels"],
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, name, is_default, pii_aliases, created_at) "
                "VALUES (7, 'bob', 0, '[]', 'now')"
            )
        )
        _pin(conn, "acme widgets", grocery)
        _pin(conn, "beta corp", fuel)
        _pin(conn, "gamma inc", hotels, source="llm")  # a cache row: not ours to convert
        _pin(conn, "delta ltd", hotels)
        _rule(conn, "delta ltd", fuel)  # an enabled global exact rule already outranks the pin
        _pin(conn, "eps llc", grocery)
        _rule(conn, "eps llc", fuel, enabled=0)  # a disabled rule covers nothing
        _pin(conn, "zeta co", hotels)
        _rule(conn, "zeta co", fuel, user_id=7)  # one user's rule does not cover everybody
    engine.dispose()

    _rerun(url)

    engine = make_engine_for_path(db)
    try:
        rules = _user_rules(engine)
        by_pattern: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            by_pattern.setdefault(rule["pattern"], []).append(rule)

        # Converted: global, exact, enabled, ranked after every ordinary rule, same category.
        for key, pair in (("acme widgets", grocery), ("beta corp", fuel)):
            (converted,) = by_pattern[key]
            assert converted["match_type"] == "exact"
            assert converted["priority"] == 1000
            assert converted["user_id"] is None and converted["enabled"] == 1
            assert (converted["category_id"], converted["subcategory_id"]) == pair
            assert converted["kind_override"] is None
        # Already covered by an enabled global rule: no second rule, the original untouched.
        (existing,) = by_pattern["delta ltd"]
        assert existing["priority"] == 50 and existing["subcategory_id"] == fuel[1]
        # A disabled rule or another user's rule does not cover the pin: it is converted too.
        assert sorted(r["priority"] for r in by_pattern["eps llc"]) == [50, 1000]
        assert sorted(r["priority"] for r in by_pattern["zeta co"]) == [50, 1000]

        with engine.connect() as conn:
            left = {
                r.merchant_key: r.source
                for r in conn.execute(text("SELECT merchant_key, source FROM merchant_map"))
            }
        assert left == {"gamma inc": "llm"}  # every user pin gone, the cache row kept
    finally:
        engine.dispose()


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    url, db, engine, pairs = _seeded(tmp_path)
    with engine.begin() as conn:
        _pin(conn, "acme widgets", pairs["grocery/grocery_stores"])
    engine.dispose()

    for _ in range(3):
        _rerun(url)

    engine = make_engine_for_path(db)
    try:
        assert [r["pattern"] for r in _user_rules(engine)] == ["acme widgets"]
        with engine.connect() as conn:
            pins = conn.execute(
                text("SELECT count(*) FROM merchant_map WHERE source = 'user'")
            ).scalar_one()
        assert pins == 0
    finally:
        engine.dispose()


def test_a_database_without_pins_is_untouched(tmp_path: Path) -> None:
    url, db, engine, pairs = _seeded(tmp_path)
    with engine.begin() as conn:
        _rule(conn, "acme widgets", pairs["fuel/gasoline"])
        _pin(conn, "gamma inc", pairs["travel/hotels"], source="llm")
    before = _user_rules(engine)
    engine.dispose()

    _rerun(url)

    engine = make_engine_for_path(db)
    try:
        assert _user_rules(engine) == before
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM merchant_map")).scalar_one() == 1
    finally:
        engine.dispose()


def test_a_converted_pin_keeps_the_precedence_the_pin_had(tmp_path: Path) -> None:
    """After conversion the merchant still lands where the pin put it, now through a rule that
    any rule of the user's still outranks."""
    url, db, engine, pairs = _seeded(tmp_path)
    grocery, fuel = pairs["grocery/grocery_stores"], pairs["fuel/gasoline"]
    with engine.begin() as conn:
        _pin(conn, "acme widgets", fuel)
    engine.dispose()
    _rerun(url)

    engine = make_engine_for_path(db)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            user = User(name="alex")
            issuer = Issuer(name="Bank", slug="bank")
            session.add_all([user, issuer])
            session.flush()
            account = Account(
                user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
            )
            statement = Statement(
                file_sha256="a" * 64, original_name="s.pdf", status="parsed", ingested_at="now"
            )
            session.add_all([account, statement])
            session.flush()
            txn = Transaction(
                statement_id=statement.id,
                account_id=account.id,
                user_id=user.id,
                posted_date="2026-01-05",
                description_raw="ACME WIDGETS",
                description_clean="ACME WIDGETS",
                merchant_key="acme widgets",
                amount_minor=100,
                currency="USD",
                kind="purchase",
                is_spend=True,
                dedupe_hash="h1",
            )
            session.add(txn)
            session.commit()

            def run() -> Transaction:
                cascade.classify_transactions(
                    factory,
                    [txn.id],
                    cfg=LLMConfig(),
                    group_id="g",
                    progress_cb=lambda done, total, cost: None,
                )
                session.expire_all()
                row = session.get(Transaction, txn.id)
                assert row is not None
                return row

            row = run()
            assert row.category_id == fuel[0] and row.subcategory_id == fuel[1]
            assert row.classified_by == "user_rule"

            # The user's own contains-rule (priority 100) now beats the converted pin (1000).
            with engine.begin() as conn:
                _rule(conn, "widgets", grocery, match_type="contains", priority=100)
            row = run()
            assert row.category_id == grocery[0] and row.subcategory_id == grocery[1]
    finally:
        engine.dispose()


# --- rows the pins classified are not frozen as one-time edits -------------------------------------

_PIN_TIME = "2026-02-01T10:00:00.000000+00:00"
_LATER = "2026-03-15T09:30:00.000000+00:00"


def _txn_rows(
    engine: Engine,
    pairs: dict[str, tuple[int, int]],
    specs: list[tuple[str, str, str, list[tuple[str, str]]]],
) -> list[int]:
    """Transactions given as (merchant key, category pair name, classified_by, audit entries
    (method, created_at) oldest first). Returns their ids."""
    ids: list[int] = []
    with make_session_factory(engine)() as session:
        user = User(name="alex")
        issuer = Issuer(name="Bank", slug="bank")
        session.add_all([user, issuer])
        session.flush()
        account = Account(
            user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
        )
        statement = Statement(
            file_sha256="b" * 64, original_name="s.pdf", status="parsed", ingested_at="now"
        )
        session.add_all([account, statement])
        session.flush()
        for n, (key, pair_name, classified_by, audits) in enumerate(specs):
            category_id, subcategory_id = pairs[pair_name]
            txn = Transaction(
                statement_id=statement.id,
                account_id=account.id,
                user_id=user.id,
                posted_date="2026-01-05",
                description_raw=key.upper(),
                description_clean=key.upper(),
                merchant_key=key,
                amount_minor=100,
                currency="USD",
                kind="purchase",
                is_spend=True,
                dedupe_hash=f"h{n}",
                category_id=category_id,
                subcategory_id=subcategory_id,
                classified_by=classified_by,
            )
            session.add(txn)
            session.flush()
            for method, created_at in audits:
                session.add(
                    Classification(
                        transaction_id=txn.id,
                        category_key="x",
                        subcategory_key="y",
                        confidence=1.0,
                        method=method,
                        created_at=created_at,
                    )
                )
            ids.append(txn.id)
        session.commit()
    return ids


def _classified_by(engine: Engine) -> dict[int, str]:
    with engine.connect() as conn:
        return {
            row.id: row.classified_by
            for row in conn.execute(text("SELECT id, classified_by FROM transactions"))
        }


def test_rows_a_pin_classified_are_unfrozen_but_real_edits_stay(tmp_path: Path) -> None:
    url, db, engine, pairs = _seeded(tmp_path)
    grocery = pairs["grocery/grocery_stores"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO merchant_map (merchant_key, merchant_canonical, category_id, "
                "subcategory_id, source, confidence, hit_count, updated_at) "
                "VALUES ('acme widgets', NULL, :c, :s, 'user', 1.0, 3, :t)"
            ),
            {"c": grocery[0], "s": grocery[1], "t": _PIN_TIME},
        )
    edit_audit = [("llm", "2026-01-06T00:00:00+00:00"), ("user", _PIN_TIME)]
    ids = _txn_rows(
        engine,
        pairs,
        [
            # the correction that created the pin: audit written with it -> a real edit
            ("acme widgets", "grocery/grocery_stores", "user", edit_audit),
            # a later import hit the pin: an audit entry weeks after the pin -> not an edit
            (
                "acme widgets",
                "grocery/grocery_stores",
                "user",
                [("cache", "2026-01-06T00:00:00+00:00"), ("user", _LATER)],
            ),
            # edited to another category than the pin's: a real edit
            ("acme widgets", "fuel/gasoline", "user", [("user", _LATER)]),
            # a merchant that never had a pin (a one-time edit made after pins were retired)
            ("no pin here", "grocery/grocery_stores", "user", [("user", _LATER)]),
            # the latest audit entry is not a user one: the row is not what it claims, leave it
            ("acme widgets", "grocery/grocery_stores", "user", [("user", _LATER), ("llm", _LATER)]),
            # already a rule hit: untouched
            ("acme widgets", "grocery/grocery_stores", "user_rule", [("user_rule", _LATER)]),
        ],
    )
    engine.dispose()

    _rerun(url)

    engine = make_engine_for_path(db)
    try:
        after = _classified_by(engine)
        assert [after[i] for i in ids] == [
            "user",  # the edit that made the pin
            "user_rule",  # the pin hit: now follows the converted rule
            "user",  # edited away from the pin's target
            "user",  # no pin for this merchant
            "user",  # latest audit entry is not a user entry
            "user_rule",
        ]
    finally:
        engine.dispose()
    # idempotent: another pass changes nothing (the pin is gone)
    for _ in range(2):
        _rerun(url)
    engine = make_engine_for_path(db)
    try:
        assert [_classified_by(engine)[i] for i in ids] == [after[i] for i in ids]
    finally:
        engine.dispose()


def test_an_unfrozen_row_follows_new_rules_but_an_edit_does_not(tmp_path: Path) -> None:
    """The point of the fix: after migration "Apply to existing" / re-classify reaches the rows the
    pin classified and still skips the one-time edit."""
    url, db, engine, pairs = _seeded(tmp_path)
    grocery, fuel = pairs["grocery/grocery_stores"], pairs["fuel/gasoline"]
    with engine.begin() as conn:
        _pin(conn, "acme widgets", grocery)  # updated_at is not a timestamp: nothing is "the edit"
    ids = _txn_rows(
        engine,
        pairs,
        [
            ("acme widgets", "grocery/grocery_stores", "user", [("user", _LATER)]),
            ("acme widgets", "fuel/gasoline", "user", [("user", _LATER)]),  # a real edit
        ],
    )
    engine.dispose()
    _rerun(url)

    engine = make_engine_for_path(db)
    try:
        with engine.begin() as conn:
            _rule(conn, "acme", fuel, match_type="contains", priority=100)
        factory = make_session_factory(engine)
        cascade.classify_transactions(
            factory,
            ids,
            cfg=LLMConfig(),
            group_id="g",
            progress_cb=lambda done, total, cost: None,
        )
        with factory() as session:
            hit = session.get(Transaction, ids[0])
            edit = session.get(Transaction, ids[1])
            assert hit is not None and edit is not None
            assert (hit.category_id, hit.subcategory_id) == fuel  # re-classified by the new rule
            assert hit.classified_by == "user_rule"
            assert edit.classified_by == "user"  # the edit was never touched
    finally:
        engine.dispose()


def test_covered_check_is_case_and_whitespace_insensitive(tmp_path: Path) -> None:
    url, db, engine, pairs = _seeded(tmp_path)
    grocery, fuel = pairs["grocery/grocery_stores"], pairs["fuel/gasoline"]
    with engine.begin() as conn:
        _pin(conn, "Acme Widgets", grocery)
        _rule(conn, " acme widgets ", fuel)  # matches the pin's key as the cascade compares them
        _pin(conn, "Beta Corp", grocery)
        _pin(conn, "beta corp", fuel)  # two pins that fold to one key: one rule is enough
    engine.dispose()

    _rerun(url)

    engine = make_engine_for_path(db)
    try:
        patterns = [r["pattern"] for r in _user_rules(engine)]
        assert patterns.count(" acme widgets ") == 1 and "Acme Widgets" not in patterns
        assert len([p for p in patterns if p.casefold() == "beta corp"]) == 1
    finally:
        engine.dispose()
