"""Plain CRUD used by the routers (§3.12a: "Plain CRUD ... is not part of these interfaces;
P2-C implements it in `api/services/` using the frozen models, so routers still contain no SQL").

Every function here takes an open `Session` and does not commit unless stated; the caller (a
route handler, inside the request's session dependency) commits.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from sqlalchemy import ColumnElement, Select, and_, func, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spend_analyzer.api.services import gateway
from spend_analyzer.core.paths import statements_dir
from spend_analyzer.core.types import USER_CLASSIFIED, Kind
from spend_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    LayoutSpec,
    LlmRun,
    Rule,
    Statement,
    Subcategory,
    Transaction,
    User,
    utcnow_iso,
)

# --- Users -----------------------------------------------------------------------------------------


def list_users(session: Session) -> Sequence[User]:
    return session.execute(select(User).order_by(User.id)).scalars().all()


def create_user(session: Session, *, name: str, is_default: bool) -> User:
    if is_default:
        _clear_default_user(session)
    user = User(name=name, is_default=is_default)
    session.add(user)
    session.flush()
    return user


def update_user(session: Session, user: User, *, name: str | None, is_default: bool | None) -> User:
    if name is not None:
        user.name = name
    if is_default is not None:
        if is_default:
            _clear_default_user(session)
        user.is_default = is_default
    session.flush()
    return user


def delete_user(session: Session, user: User) -> None:
    session.delete(user)
    session.flush()


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def default_user(session: Session) -> User | None:
    """The user with `is_default=True`, if one exists."""
    return session.execute(select(User).where(User.is_default.is_(True))).scalars().first()


def ensure_default_user(session: Session) -> User:
    """Idempotently ensure at least one user exists (D4): if the `users` table already has a
    default, return it unchanged; if it has users but none flagged default, promote the first
    (by id); otherwise create a neutral single-user default (`name="Me"`, `is_default=True`).

    Called from `migrate`/`serve` startup (never from a route: a route that needs a user id
    resolves it via `default_user`/`default_user_id` and reports a clean error if none exists,
    since an empty `users` table on a running server is a legitimate, if unusual, state).
    """
    existing_default = default_user(session)
    if existing_default is not None:
        return existing_default
    first_user = session.execute(select(User).order_by(User.id)).scalars().first()
    if first_user is not None:
        first_user.is_default = True
        session.flush()
        return first_user
    return create_user(session, name="Me", is_default=True)


def _clear_default_user(session: Session) -> None:
    for other in session.execute(select(User).where(User.is_default.is_(True))).scalars():
        other.is_default = False


# --- Accounts --------------------------------------------------------------------------------------


def list_accounts(session: Session) -> Sequence[Account]:
    return session.execute(select(Account).order_by(Account.id)).scalars().all()


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def create_account(
    session: Session,
    *,
    user_id: int,
    issuer_id: int | None,
    account_type: str,
    mask: str,
    currency: str,
) -> Account:
    account = Account(
        user_id=user_id,
        issuer_id=issuer_id if issuer_id is not None else unknown_issuer_id(session),
        account_type=account_type,
        display_name=f"{account_type} ...{mask}" if mask else account_type,
        mask=mask,
        currency=currency,
    )
    session.add(account)
    session.flush()
    return account


def update_account(
    session: Session,
    account: Account,
    *,
    issuer_id: int | None,
    account_type: str | None,
    mask: str | None,
    currency: str | None,
) -> Account:
    if issuer_id is not None:
        account.issuer_id = issuer_id
    if account_type is not None:
        account.account_type = account_type
    if mask is not None:
        account.mask = mask
    if currency is not None:
        account.currency = currency
    session.flush()
    return account


def unknown_issuer_id(session: Session) -> int:
    """A placeholder "Unknown" issuer, created on first use — for an `AccountCreate` that omits
    an issuer (accounts require a non-null `issuer_id`, §3.2), and for `cli.py`'s `import`
    command confirming a statement whose extractor was pinned explicitly
    (`--parser-id`/`--layout-spec-id`) but whose issuer never matched any registered `Issuer`
    (§3.12a)."""
    issuer = session.execute(select(Issuer).where(Issuer.slug == "unknown")).scalar_one_or_none()
    if issuer is None:
        issuer = Issuer(name="Unknown", slug="unknown", match_terms="[]")
        session.add(issuer)
        session.flush()
    return issuer.id


# --- Statements ------------------------------------------------------------------------------------


def get_statement(session: Session, statement_id: int) -> Statement | None:
    return session.get(Statement, statement_id)


def staged_pdf_path(statement: Statement) -> Path | None:
    """The one PDF `statement` can still be re-read from, or `None` if none is available
    (`[privacy] store_pdf_copies` was off and it was already confirmed, so `ingest.pipeline`
    deleted its staged copy — see that module's own docstring on durable staging).

    Checks `stored_path` first (the copy kept because `store_pdf_copies` is on), then the
    content-hash staging path every upload writes to at `propose_import` time (still present for
    a statement not yet confirmed, or whose Phase 2 hasn't reached a terminal outcome)."""
    if statement.stored_path and Path(statement.stored_path).is_file():
        return Path(statement.stored_path)
    staged = statements_dir() / f"{statement.file_sha256}.pdf"
    return staged if staged.is_file() else None


def list_statements(session: Session, *, user_id: int | None) -> Sequence[Statement]:
    stmt: Select[Statement] = select(Statement).order_by(Statement.id.desc())
    if user_id is not None:
        stmt = stmt.join(Account, Statement.account_id == Account.id, isouter=True).where(
            Account.user_id == user_id
        )
    return session.execute(stmt).scalars().all()


def statement_user_id(session: Session, statement: Statement) -> int | None:
    if statement.account_id is None:
        return None
    account = session.get(Account, statement.account_id)
    return account.user_id if account is not None else None


def reconciliation_delta_minor(session: Session, statement: Statement) -> int | None:
    """Signed minor units; 0 when the balance equation (§3.1, A25) reconciles exactly, `None`
    when no balance was read (or no account type is known) to check against.

    Credit: ``closing - opening == sum(amount)``; checking/savings:
    ``opening - closing == sum(amount)`` (§3.1). The delta returned is ``actual - expected`` for
    whichever equation applies to the statement's account.
    """
    if (
        statement.opening_balance_minor is None
        or statement.closing_balance_minor is None
        or statement.parsed_total_minor is None
    ):
        return None
    account = session.get(Account, statement.account_id) if statement.account_id else None
    if account is None:
        return None
    opening = statement.opening_balance_minor
    closing = statement.closing_balance_minor
    expected = (closing - opening) if account.account_type == "credit" else (opening - closing)
    return statement.parsed_total_minor - expected


def patch_statement(session: Session, statement: Statement, *, account_id: int | None) -> Statement:
    if account_id is not None:
        statement.account_id = account_id
    session.flush()
    return statement


def delete_statement(session: Session, statement: Statement) -> None:
    session.delete(statement)
    session.flush()


# --- Transactions ----------------------------------------------------------------------------------


def get_transaction(session: Session, transaction_id: int) -> Transaction | None:
    return session.get(Transaction, transaction_id)


@dataclass(frozen=True, slots=True)
class TransactionFilters:
    """The filter set `GET /transactions`, `GET /transactions/ids` and `GET /export` all share, so
    "the rows this filter shows", "select all of them" and "export them" can never disagree.

    `kinds` wins over `include_non_spend`; with neither, only spend-relevant kinds are listed.
    `uncategorized` selects rows with no category or the `others/uncategorized` fallback
    (`True`) or everything else (`False`). `currency=None` means every currency.
    """

    user_ids: list[int] | None = None
    account_ids: list[int] | None = None
    statement_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    category_keys: list[str] | None = None
    subcategory_keys: list[str] | None = None
    amount_min_minor: int | None = None
    amount_max_minor: int | None = None
    kinds: list[str] | None = None
    include_non_spend: bool = False
    needs_review: bool | None = None
    uncategorized: bool | None = None
    search: str | None = None
    currency: str | None = "USD"


def uncategorized_clause() -> ColumnElement[bool]:
    """Rows with no category at all, or parked in the `others/uncategorized` fallback (what the
    cascade writes when nothing, including the LLM, could place a transaction)."""
    fallback_ids = (
        select(Subcategory.id)
        .join(Category, Subcategory.category_id == Category.id)
        .where(Category.key == "others", Subcategory.key == "uncategorized")
    )
    return or_(Transaction.category_id.is_(None), Transaction.subcategory_id.in_(fallback_ids))


def unclassified_clause() -> ColumnElement[bool]:
    """Rows a re-classification should reconsider: uncategorized (see `uncategorized_clause`) or
    still flagged `needs_review`, but never one the user corrected by hand (those are final)."""
    return and_(
        or_(uncategorized_clause(), Transaction.needs_review.is_(True)),
        Transaction.classified_by.is_distinct_from(USER_CLASSIFIED),
    )


def _transaction_conditions(filters: TransactionFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.currency is not None:
        conditions.append(Transaction.currency == filters.currency)
    if filters.user_ids is not None:
        conditions.append(Transaction.user_id.in_(filters.user_ids))
    if filters.account_ids is not None:
        conditions.append(Transaction.account_id.in_(filters.account_ids))
    if filters.statement_id is not None:
        conditions.append(Transaction.statement_id == filters.statement_id)
    if filters.date_from is not None:
        conditions.append(Transaction.posted_date >= filters.date_from.isoformat())
    if filters.date_to is not None:
        conditions.append(Transaction.posted_date <= filters.date_to.isoformat())
    # Subqueries rather than joins: an inner join on `categories` would silently drop every
    # uncategorized row whenever any category filter is set, and the filters compose freely.
    if filters.category_keys is not None:
        conditions.append(
            Transaction.category_id.in_(
                select(Category.id).where(Category.key.in_(filters.category_keys))
            )
        )
    if filters.subcategory_keys is not None:
        conditions.append(
            Transaction.subcategory_id.in_(
                select(Subcategory.id).where(Subcategory.key.in_(filters.subcategory_keys))
            )
        )
    if filters.amount_min_minor is not None:
        conditions.append(Transaction.amount_minor >= filters.amount_min_minor)
    if filters.amount_max_minor is not None:
        conditions.append(Transaction.amount_minor <= filters.amount_max_minor)
    if filters.kinds is not None:
        conditions.append(Transaction.kind.in_(filters.kinds))
    elif not filters.include_non_spend:
        conditions.append(Transaction.kind.in_(("purchase", "fee", "interest", "refund")))
    if filters.needs_review is not None:
        conditions.append(Transaction.needs_review.is_(filters.needs_review))
    if filters.uncategorized is not None:
        clause = uncategorized_clause()
        conditions.append(clause if filters.uncategorized else not_(clause))
    if filters.search:
        conditions.append(Transaction.description_clean.contains(filters.search, autoescape=True))
    return conditions


def _transaction_order(sort: str | None) -> list[ColumnElement[Any]]:
    """`ORDER BY` for `sort` (`posted_date` or `amount_minor`, `-` prefix for descending; default
    newest first). `id` breaks ties so paging never repeats or skips a row."""
    key = sort or "-posted_date"
    column = {"posted_date": Transaction.posted_date, "amount_minor": Transaction.amount_minor}.get(
        key.removeprefix("-"), Transaction.posted_date
    )
    if key.startswith("-"):
        return [column.desc(), Transaction.id.desc()]
    return [column.asc(), Transaction.id.asc()]


def list_transactions(
    session: Session,
    filters: TransactionFilters,
    *,
    page: int,
    page_size: int,
    sort: str | None,
) -> tuple[list[Transaction], int]:
    conditions = _transaction_conditions(filters)
    total = session.execute(
        select(func.count()).select_from(Transaction).where(*conditions)
    ).scalar_one()

    page = max(page, 1)
    page_size = max(min(page_size, 500), 1)
    stmt = (
        select(Transaction)
        .where(*conditions)
        .order_by(*_transaction_order(sort))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(session.execute(stmt).scalars().all()), total


def transaction_ids(
    session: Session, filters: TransactionFilters, *, sort: str | None
) -> list[int]:
    """Every id `list_transactions` would page through for `filters`, in the same order (no page
    cap, so "select all N matching" really selects N)."""
    stmt = (
        select(Transaction.id)
        .where(*_transaction_conditions(filters))
        .order_by(*_transaction_order(sort))
    )
    return list(session.execute(stmt).scalars().all())


def export_transactions(session: Session, filters: TransactionFilters) -> list[Transaction]:
    """Every transaction matching `filters`, oldest first (the export's order)."""
    stmt = (
        select(Transaction)
        .where(*_transaction_conditions(filters))
        .order_by(Transaction.posted_date, Transaction.id)
    )
    return list(session.execute(stmt).scalars().all())


def category_key_of(session: Session, transaction: Transaction) -> str | None:
    if transaction.category_id is None:
        return None
    category = session.get(Category, transaction.category_id)
    return category.key if category is not None else None


def subcategory_key_of(session: Session, transaction: Transaction) -> str | None:
    if transaction.subcategory_id is None:
        return None
    sub = session.get(Subcategory, transaction.subcategory_id)
    return sub.key if sub is not None else None


def update_transaction_fields(
    session: Session, transaction: Transaction, *, kind: str | None, notes: str | None
) -> Transaction:
    """Update fields that are plain CRUD (not routed through `apply_user_correction`).

    Changing `kind` is a one-time edit of this transaction (see `cascade.apply_kind_edit`: a row
    moved between spend and payment/transfer is re-filed to match, so payments never stay
    frozen under "Payments & transfers" as spending). When the row already has a real
    category it is marked `classified_by='user'`, so no automated pass reverts the edit. A
    kind-only edit on a row that is still uncategorized does *not* freeze it: the user said
    something about the kind, not the category, and locking would leave the row uncategorized
    forever (every re-classify, rule and LLM pass skips a `user` row). Its category can still be
    filled in; the kind itself only changes again through a rule's explicit kind override.
    `notes` never lock a row.
    """
    if kind is not None and kind != transaction.kind:
        gateway.apply_kind_edit(session, transaction, cast(Kind, kind))
    if notes is not None:
        transaction.notes = notes
    transaction.updated_at = utcnow_iso()
    session.flush()
    return transaction


def merchant_display_names(session: Session, merchant_keys: Sequence[str]) -> dict[str, str]:
    """A friendlier label for each `merchant_key` in `merchant_keys`, for
    `GET /api/analytics/top-merchants` (never egressed — `description_clean` is egress-eligible
    in general, but this never leaves the response): the most common non-null
    `merchant_canonical` recorded for that key across every transaction, tie-broken
    lexicographically; or, when a key has none, the A5 representative `description_clean`
    (the modal value, tie-broken shortest then lexicographic — mirrors
    `classify.cascade._select_representative`'s own tie-break exactly, without importing that
    module's private helper across a package boundary)."""
    if not merchant_keys:
        return {}

    rows = session.execute(
        select(
            Transaction.merchant_key, Transaction.merchant_canonical, Transaction.description_clean
        ).where(Transaction.merchant_key.in_(merchant_keys))
    ).all()

    canonical_counts: dict[str, Counter[str]] = {}
    description_counts: dict[str, Counter[str]] = {}
    for merchant_key, merchant_canonical, description_clean in rows:
        if merchant_canonical:
            canonical_counts.setdefault(merchant_key, Counter())[merchant_canonical] += 1
        description_counts.setdefault(merchant_key, Counter())[description_clean] += 1

    display_names: dict[str, str] = {}
    for merchant_key in merchant_keys:
        canonical = canonical_counts.get(merchant_key)
        if canonical:
            top_count = max(canonical.values())
            display_names[merchant_key] = sorted(
                name for name, count in canonical.items() if count == top_count
            )[0]
            continue
        descriptions = description_counts.get(merchant_key)
        if descriptions:
            top_count = max(descriptions.values())
            display_names[merchant_key] = sorted(
                (d for d, count in descriptions.items() if count == top_count),
                key=lambda d: (len(d), d),
            )[0]
    return display_names


# --- Issuers ---------------------------------------------------------------------------------------


def list_issuers(session: Session) -> Sequence[Issuer]:
    return session.execute(select(Issuer).order_by(Issuer.id)).scalars().all()


def get_issuer(session: Session, issuer_id: int) -> Issuer | None:
    return session.get(Issuer, issuer_id)


class DuplicateIssuerError(Exception):
    """An issuer with the same name or slug already exists; `existing` is that issuer."""

    def __init__(self, existing: Issuer) -> None:
        super().__init__(f"An issuer named '{existing.name}' already exists.")
        self.existing = existing


class DuplicateLayoutSpecError(Exception):
    """A layout spec with the same `parser_id` (name + version) already exists."""

    def __init__(self, name: str, version: int) -> None:
        super().__init__(f"A layout spec named '{name}' (version {version}) already exists.")


def find_issuer_conflict(session: Session, name: str) -> Issuer | None:
    """The existing issuer `name` would collide with: same name, or same derived slug."""
    return session.execute(
        select(Issuer).where((Issuer.slug == _slugify(name)) | (Issuer.name == name)).limit(1)
    ).scalar_one_or_none()


def create_issuer(session: Session, *, name: str, match_terms: list[str]) -> Issuer:
    """Raises `DuplicateIssuerError` if the name or slug is taken (including a concurrent insert
    that wins the race between the check and the flush)."""
    conflict = find_issuer_conflict(session, name)
    if conflict is not None:
        raise DuplicateIssuerError(conflict)
    slug = _slugify(name)
    issuer = Issuer(name=name, slug=slug, match_terms=json.dumps(match_terms or [name]))
    session.add(issuer)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        conflict = find_issuer_conflict(session, name)
        if conflict is None:
            raise
        raise DuplicateIssuerError(conflict) from None
    return issuer


def update_issuer(
    session: Session,
    issuer: Issuer,
    *,
    name: str | None,
    match_terms: list[str] | None,
    default_spec_id: int | None,
) -> Issuer:
    if name is not None and name != issuer.name:
        conflict = session.execute(
            select(Issuer).where(Issuer.name == name, Issuer.id != issuer.id)
        ).scalar_one_or_none()
        if conflict is not None:
            raise DuplicateIssuerError(conflict)
        issuer.name = name
    if match_terms is not None:
        issuer.match_terms = json.dumps(match_terms)
    if default_spec_id is not None:
        issuer.default_spec_id = default_spec_id
    session.flush()
    return issuer


def delete_issuer(session: Session, issuer: Issuer) -> None:
    session.delete(issuer)
    session.flush()


def _slugify(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or uuid.uuid4().hex[:8]


# --- Layout specs ------------------------------------------------------------------------------------


def list_layout_specs(session: Session) -> Sequence[LayoutSpec]:
    return (
        session.execute(select(LayoutSpec).order_by(LayoutSpec.name, LayoutSpec.version))
        .scalars()
        .all()
    )


def get_layout_spec(session: Session, spec_id: int) -> LayoutSpec | None:
    return session.get(LayoutSpec, spec_id)


def create_layout_spec(
    session: Session, *, name: str, spec_yaml: str, issuer_id: int | None, version: int = 1
) -> LayoutSpec:
    slug = _slugify(name)
    parser_id = f"spec_{slug}_v{version}"
    if session.execute(
        select(LayoutSpec.id).where(
            or_(
                LayoutSpec.parser_id == parser_id,
                (LayoutSpec.name == name) & (LayoutSpec.version == version),
            )
        )
    ).first():
        raise DuplicateLayoutSpecError(name, version)
    spec = LayoutSpec(
        name=name,
        version=version,
        parser_id=parser_id,
        issuer_id=issuer_id,
        spec_yaml=spec_yaml,
        source="pasted",
        approved=False,
    )
    session.add(spec)
    session.flush()
    return spec


def revise_layout_spec(
    session: Session, previous: LayoutSpec, *, spec_yaml: str, issuer_id: int | None
) -> LayoutSpec:
    return create_layout_spec(
        session,
        name=previous.name,
        spec_yaml=spec_yaml,
        issuer_id=issuer_id if issuer_id is not None else previous.issuer_id,
        version=previous.version + 1,
    )


def approve_layout_spec(session: Session, spec: LayoutSpec) -> LayoutSpec:
    spec.approved = True
    session.flush()
    return spec


# --- Rules -----------------------------------------------------------------------------------------


def list_rules(session: Session) -> Sequence[Rule]:
    return session.execute(select(Rule).order_by(Rule.priority, Rule.id)).scalars().all()


def get_rule(session: Session, rule_id: int) -> Rule | None:
    return session.get(Rule, rule_id)


def create_rule(
    session: Session,
    *,
    pattern: str,
    match_type: str,
    category_id: int,
    subcategory_id: int,
    kind_override: str | None,
) -> Rule:
    rule = Rule(
        match_type=match_type,
        pattern=pattern,
        category_id=category_id,
        subcategory_id=subcategory_id,
        kind_override=kind_override,
        source="user",
    )
    session.add(rule)
    session.flush()
    return rule


def update_rule(
    session: Session,
    rule: Rule,
    *,
    pattern: str | None,
    category_id: int | None,
    subcategory_id: int | None,
    kind_override: str | None,
) -> Rule:
    if pattern is not None:
        rule.pattern = pattern
    if category_id is not None:
        rule.category_id = category_id
    if subcategory_id is not None:
        rule.subcategory_id = subcategory_id
    if kind_override is not None:
        rule.kind_override = kind_override
    session.flush()
    return rule


def delete_rule(session: Session, rule: Rule) -> None:
    session.delete(rule)
    session.flush()


def category_by_key(session: Session, key: str) -> Category | None:
    return session.execute(select(Category).where(Category.key == key)).scalar_one_or_none()


def subcategory_by_key(session: Session, category_id: int, key: str) -> Subcategory | None:
    return session.execute(
        select(Subcategory).where(Subcategory.category_id == category_id, Subcategory.key == key)
    ).scalar_one_or_none()


# --- Taxonomy ----------------------------------------------------------------------------------------


def list_categories_with_subcategories(session: Session) -> Sequence[Category]:
    return session.execute(select(Category).order_by(Category.sort_order)).scalars().all()


def subcategories_for_category(session: Session, category_id: int) -> Sequence[Subcategory]:
    return (
        session.execute(select(Subcategory).where(Subcategory.category_id == category_id))
        .scalars()
        .all()
    )


def get_subcategory(session: Session, subcategory_id: int) -> Subcategory | None:
    return session.get(Subcategory, subcategory_id)


# --- LLM runs ----------------------------------------------------------------------------------------


def list_llm_runs(session: Session) -> Sequence[LlmRun]:
    return session.execute(select(LlmRun).order_by(LlmRun.id.desc())).scalars().all()
