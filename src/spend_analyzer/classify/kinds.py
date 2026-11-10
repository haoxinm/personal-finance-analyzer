"""Kind resolution — the cascade's first step (§2b P2-B, A26, A29).

A parser's `kind_hint` is the most reliable signal available (it comes from a statement's own
section headings), but two situations need help from this module, the single source of truth for
every pattern used to resolve a `Kind`:

- ``kind_hint == 'payment_or_refund'``: a heading like "PAYMENTS AND OTHER CREDITS" cannot tell a
  payment from a refund on its own (A29, `core/types.KindHint`). `resolve_kind` applies the
  payment pattern here: a match is a `payment`, anything else is a `refund`.
- ``kind_hint is None``: a parser with no section headings at all (the generic fallback parser,
  P1-B) leaves kind detection entirely to sign-plus-pattern rules, applied in the order the plan
  specifies: (for checking/savings accounts only) a card payment to a credit card — A26/I11, so
  that spend already counted on the card statement is never double-counted as a `purchase` on
  the checking side — then interest charge, then fee, then a generic transfer pattern, then (on
  a credit account only) a generic payment pattern for an inflow, then the sign default.

  The card-payment rule is deliberately narrow, but not so narrow that it misses the wording banks
  actually print: a bare PAYMENT/PMT/EPAY on a bank statement is just as likely rent, a utility or a
  loan, which is real spend, so it needs card-specific words (`CARD_PAYMENT_RE`: "CRD AUTOPAY",
  "CARD ENDING 1234", ...), a well-known card brand or issuer next to a payment word ("CHASE
  AUTOPAY", "DISCOVER E-PAYMENT", "AMEX EPAYMENT ACH PMT"), or a configured issuer's name next to
  one (`looks_like_card_payment`). A loan/mortgage/rent/insurance word vetoes the brand and issuer
  matches ("CHASE MORTGAGE PAYMENT" stays spend). The same test overrides a concrete `purchase`
  hint on a bank account (Layout D emits one for every row it does not itself recognise), so the
  issuer fallback is never skipped. For the same reason
  the sign default on a checking/savings account differs from a card's: an unexplained deposit is
  far more likely a paycheck or a transfer in than a merchant refund, and netting it against
  spend would silently shrink the dashboard's totals, so it is a `transfer` unless the
  description says refund/return/reversal (and earned interest likewise is not spend).

Every other `kind_hint` value is already a concrete `Kind` (e.g. Layout D resolves its own
card-payment rows to `transfer` at parse time, per §2c) and is returned unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from spend_analyzer.core.types import Kind, KindHint

#: A payment leaving through this description (§2b: "PAYMENT THANK YOU", "AUTOPAY", "ONLINE
#: PAYMENT - THANK YOU", "CARD AUTOPAY PAYMENT", ...). Used both to resolve the ambiguous
#: ``'payment_or_refund'`` hint and, on a credit account with no hint at all, as a general payment
#: pattern for an inflow.
_PAYMENT_RE = re.compile(r"\b(PAYMENT|AUTOPAY|PMT)\b", re.IGNORECASE)

#: A card payment specifically — the same phrasing Layout D resolves itself (§2c), kept here so
#: the generic (hint-less) fallback path applies the identical rule (A26). Every alternative names
#: a *card*: a bare PAYMENT, PMT, EPAY or AUTOPAY is as likely rent or a utility as a card, and
#: calling those `transfer` would hide real spend. "DEBIT CARD ..." / "CHECK CARD ..." are
#: purchases made with a card, not payments to one. Brand- and issuer-named payments
#: ("CHASE AUTOPAY", `ACME BANK PAYMENT`) are caught by `looks_like_card_payment` instead.
CARD_PAYMENT_RE = re.compile(
    r"\b(?:CRD\s*(?:PMT|PAYMENT|AUTOPAY|AUTO\s*PAY|EPAY|E-PAYMENT)"
    r"|CC\s*(?:PMT|PAYMENT|AUTOPAY|AUTO\s*PAY|EPAY)"
    r"|CREDIT\s*CARD\s*(?:BILL\s*)?(?:PAYMENT|PMT|AUTOPAY|AUTO\s*PAY|EPAY)"
    r"|(?<!DEBIT )(?<!CHECK )CARD\s*(?:AUTOPAY|AUTO\s*PAY|EPAY|PAYMENT|PMT)"
    r"|(?<!DEBIT )(?<!CHECK )CARD\s+ENDING)\b",
    re.IGNORECASE,
)

#: Words for "a payment" as printed on a bank statement, used next to a card brand: PAYMENT, PMT,
#: AUTOPAY / AUTO PAY, EPAY / EPAYMENT / E-PAYMENT.
_PAY_WORD_RE = re.compile(r"\b(?:E-?PAY(?:MENT)?|AUTO\s*PAY(?:MENT)?|PAYMENT|PMT)\b", re.IGNORECASE)

#: Names that are (almost) only ever a credit-card issuer or network.
_CARD_BRAND_RE = re.compile(
    r"\b(?:CHASE|CITI(?:BANK|\s*CARDS?)?|AMEX|AMERICAN\s+EXPRESS|DISCOVER|CAPITAL\s*ONE"
    r"|BARCLAYS?(?:\s*CARD)?|SYNCHRONY|GOLDMAN\s+SACHS|GS\s+BANK|APPLE\s+CARD)\b",
    re.IGNORECASE,
)

#: Banks that also hold checking, mortgage and auto accounts: only a card payment when the
#: description also says it is about a card (and has a payment word, anywhere in the line).
_GENERIC_BANK_RE = re.compile(
    r"\b(?:WELLS\s*FARGO|BANK\s+OF\s+AMERICA|BOFA|BOA|BK\s+OF\s+AMER(?:ICA)?|U\.?S\.?\s*BANK"
    r"|PNC|TD\s+BANK|TRUIST|USAA|CITIZENS|REGIONS|FIFTH\s+THIRD)\b",
    re.IGNORECASE,
)
_CARD_WORD_RE = re.compile(r"(?<!DEBIT )(?<!CHECK )\b(?:CARD|CRD|VISA|MASTERCARD)\b", re.IGNORECASE)

#: How close (characters) a brand must be to a payment word.
_BRAND_PAYMENT_PROXIMITY_CHARS = 25

#: A merchant refund/return/reversal credit — on a bank account this is the only inflow wording
#: that is treated as spend coming back (mirrors Layout D's own section-level refund pattern).
_REFUND_RE = re.compile(r"\b(REFUND|RETURN|REVERSAL)\b", re.IGNORECASE)

#: A bare "TRANSFER" (e.g. "ONLINE TRANSFER TO ...") when no parser hint is available.
_TRANSFER_RE = re.compile(r"\bTRANSFER\b", re.IGNORECASE)

#: "INTEREST CHARGE", "INTEREST CHARGED", etc.
_INTEREST_RE = re.compile(r"\bINTEREST\b", re.IGNORECASE)

#: "ANNUAL MEMBERSHIP FEE", "LATE FEE", "SERVICE FEE", ...
_FEE_RE = re.compile(r"\bFEE\b", re.IGNORECASE)

#: How close a configured issuer's `match_terms` occurrence must be to a payment token to count
#: as "next to" it (A26): within this many characters, either order.
_ISSUER_PAYMENT_PROXIMITY_CHARS = 20

_PAYMENT_TOKEN_RE = re.compile(r"\b(?:PAYMENT|PMT|AUTOPAY|EPAY)\b", re.IGNORECASE)

#: A lender, landlord or insurer that happens to share a configured issuer's name ("ACME BANK
#: MORTGAGE PAYMENT") is a bill, not a card payment; these words veto the issuer-proximity match.
_NOT_A_CARD_RE = re.compile(
    r"\b(LOAN|MORTGAGE|LEASE|RENT|INSURANCE|STUDENT|TUITION|HELOC|LENDING|FINANCE|FINANCING"
    r"|ZELLE|QUICKPAY|VENMO|PAYPAL)\b",
    re.IGNORECASE,
)


def resolve_kind(
    *,
    kind_hint: KindHint | None,
    description_clean: str,
    amount_minor: int,
    account_type: str,
    issuer_match_terms: Sequence[str] = (),
) -> Kind:
    """Resolve the final `Kind` for one transaction (cascade step 1).

    Args:
        kind_hint: the parser's own hint, if any (`RawTransaction.kind_hint`). `None` means the
            parser (typically the generic fallback) could not tell.
        description_clean: the normalized, egress-safe description (`ingest.normalize`). Never
            sent anywhere by this function; used only for local pattern matching.
        amount_minor: the transaction's signed amount (I5: positive = outflow). Only consulted
            when no hint and no pattern resolves the kind (and, on a credit account, to tell a
            payment received from a purchase whose description merely contains "PAYMENT").
        account_type: the owning account's type (``credit`` | ``checking`` | ``savings``). Card
            payments only ever count as `transfer` on a checking/savings account (A26) — on a
            credit account the identical wording is the *card's own* payment-received row, which
            reaches this function via the ambiguous hint path, not this one. Checking/savings
            also changes the sign default: an unexplained deposit is a `transfer`, not a `refund`.
        issuer_match_terms: local-only (I1b) `match_terms` of every configured issuer, consulted
            only for a checking/savings account, to catch a card payment described with the
            issuer's own name next to "PAYMENT"/"PMT"/"AUTOPAY"/"EPAY" instead of a card-specific
            word (A26). Never egressed by this or any caller.

    Returns:
        The resolved `Kind`. Never raises.
    """
    is_bank = account_type in ("checking", "savings")
    if kind_hint is not None:
        if kind_hint == "payment_or_refund":
            return "payment" if _PAYMENT_RE.search(description_clean) else "refund"
        if (
            kind_hint == "purchase"
            and is_bank
            and looks_like_card_payment(description_clean, issuer_match_terms)
        ):
            # A parser that sees only a bank statement's section ("Electronic Withdrawals") hints
            # `purchase`; the card issuer's name in the description is what says otherwise (B1).
            return "transfer"
        return kind_hint

    if is_bank and looks_like_card_payment(description_clean, issuer_match_terms):
        return "transfer"
    if _INTEREST_RE.search(description_clean):
        # Interest *earned* (an inflow on a bank account) is income, not spend: as `interest` it
        # would net negative spend into the dashboard.
        return "transfer" if is_bank and amount_minor < 0 else "interest"
    if _FEE_RE.search(description_clean):
        return "fee"
    if _TRANSFER_RE.search(description_clean):
        return "transfer"
    if is_bank:
        if amount_minor >= 0:
            return "purchase"
        return "refund" if _REFUND_RE.search(description_clean) else "transfer"
    if amount_minor < 0 and _PAYMENT_RE.search(description_clean):
        return "payment"
    return "purchase" if amount_minor >= 0 else "refund"


def is_spend(kind: Kind) -> bool:
    """`transactions.is_spend` (§3.2): true for `purchase`, `fee`, and `interest` only."""
    return kind in ("purchase", "fee", "interest")


def looks_like_card_payment(description: str, issuer_match_terms: Sequence[str] = ()) -> bool:
    """True if a bank-account line is a payment *to a credit card* (A26/I11): explicit card
    wording (`CARD_PAYMENT_RE`), a well-known card brand next to a payment word, a bank that also
    issues cards next to both a card word and a payment word, or a configured issuer's name next
    to one. A loan/mortgage/rent/insurance word (or a P2P service) vetoes everything but the
    explicit card wording."""
    if CARD_PAYMENT_RE.search(description):
        return True
    if _NOT_A_CARD_RE.search(description):
        return False
    pay_positions = [m.start() for m in _PAY_WORD_RE.finditer(description)]
    if not pay_positions:
        return False

    def near_payment(match: re.Match[str]) -> bool:
        return any(abs(match.start() - p) <= _BRAND_PAYMENT_PROXIMITY_CHARS for p in pay_positions)

    if any(near_payment(m) for m in _CARD_BRAND_RE.finditer(description)):
        return True
    if _CARD_WORD_RE.search(description) and _GENERIC_BANK_RE.search(description):
        return True
    return _issuer_payment_match(description, issuer_match_terms)


def _issuer_payment_match(description: str, issuer_match_terms: Sequence[str]) -> bool:
    """True if any `issuer_match_terms` entry appears within
    `_ISSUER_PAYMENT_PROXIMITY_CHARS` characters of a payment token in `description`
    (A26: "PAYMENT"/"PMT" next to any configured issuer's `match_terms`), unless the description
    names a loan, rent or the like (`_NOT_A_CARD_RE`)."""
    if not issuer_match_terms or _NOT_A_CARD_RE.search(description):
        return False
    payment_positions = [m.start() for m in _PAYMENT_TOKEN_RE.finditer(description)]
    if not payment_positions:
        return False
    for term in issuer_match_terms:
        term = term.strip()
        if not term:
            continue
        for m in re.finditer(re.escape(term), description, re.IGNORECASE):
            if any(
                abs(m.start() - p) <= _ISSUER_PAYMENT_PROXIMITY_CHARS for p in payment_positions
            ):
                return True
    return False
