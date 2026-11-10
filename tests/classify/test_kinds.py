"""Tests for `classify/kinds.py` (§2b P2-B, A26, A29)."""

from __future__ import annotations

from personal_finance_analyzer.classify import kinds
from personal_finance_analyzer.classify.kinds import is_spend, resolve_kind


def test_payment_or_refund_hint_resolves_payment_pattern_to_payment() -> None:
    assert (
        resolve_kind(
            kind_hint="payment_or_refund",
            description_clean="ONLINE PAYMENT - THANK YOU",
            amount_minor=-50000,
            account_type="credit",
        )
        == "payment"
    )


def test_payment_or_refund_hint_resolves_non_payment_to_refund() -> None:
    assert (
        resolve_kind(
            kind_hint="payment_or_refund",
            description_clean="MERCHANDISE REFUND STORE X",
            amount_minor=-1500,
            account_type="credit",
        )
        == "refund"
    )


def test_concrete_kind_hint_is_returned_unchanged() -> None:
    for kind in ("purchase", "refund", "payment", "transfer", "fee", "interest", "adjustment"):
        assert (
            resolve_kind(
                kind_hint=kind,
                description_clean="ANYTHING AT ALL",
                amount_minor=100,
                account_type="checking",
            )
            == kind
        )


def test_no_hint_checking_card_payment_pattern_is_transfer() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="CARD AUTOPAY PAYMENT",
            amount_minor=20000,
            account_type="checking",
        )
        == "transfer"
    )


def test_no_hint_credit_account_same_wording_is_not_forced_to_transfer() -> None:
    # A26: the card-payment-to-transfer rule only applies to checking/savings accounts; on the
    # credit side, the same wording without a hint falls through to the generic payment pattern.
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="CARD AUTOPAY PAYMENT",
            amount_minor=-20000,
            account_type="credit",
        )
        == "payment"
    )


def test_no_hint_checking_issuer_match_term_next_to_payment_is_transfer() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="ACME BANK PAYMENT REF 998877",
            amount_minor=15000,
            account_type="checking",
            issuer_match_terms=("Acme Bank",),
        )
        == "transfer"
    )


def test_no_hint_checking_issuer_autopay_is_transfer() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="ACME BANK AUTOPAY",
            amount_minor=15000,
            account_type="checking",
            issuer_match_terms=("Acme Bank",),
        )
        == "transfer"
    )


def test_no_hint_checking_issuer_loan_payment_is_not_a_card_payment() -> None:
    # The lender shares the card issuer's name; a loan payment is a bill (spend), not a transfer.
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="ACME BANK AUTO LOAN PAYMENT",
            amount_minor=45000,
            account_type="checking",
            issuer_match_terms=("Acme Bank",),
        )
        == "purchase"
    )


def test_no_hint_checking_issuer_term_far_from_payment_does_not_match() -> None:
    # "Acme Bank" appears, but nowhere near a PAYMENT/PMT token: this is not a card payment.
    long_gap = "ACME BANK " + ("X" * 40) + " PURCHASE"
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean=long_gap,
            amount_minor=500,
            account_type="checking",
            issuer_match_terms=("Acme Bank",),
        )
        == "purchase"
    )


def test_no_hint_interest_pattern() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="INTEREST CHARGE ON PURCHASES",
            amount_minor=299,
            account_type="credit",
        )
        == "interest"
    )


def test_no_hint_fee_pattern() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="ANNUAL MEMBERSHIP FEE",
            amount_minor=9500,
            account_type="credit",
        )
        == "fee"
    )


def test_no_hint_transfer_pattern() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="ONLINE TRANSFER TO SAVINGS",
            amount_minor=10000,
            account_type="checking",
        )
        == "transfer"
    )


def test_no_hint_checking_card_payment_phrasings_are_transfers() -> None:
    for description in (
        "CRD PMT VISA 1234",
        "CREDIT CARD PAYMENT",
        "ACME CARD EPAY",
        "CC AUTOPAY",
        "CARD PAYMENT ACME",
    ):
        assert (
            resolve_kind(
                kind_hint=None,
                description_clean=description,
                amount_minor=20000,
                account_type="checking",
            )
            == "transfer"
        ), description


def test_no_hint_checking_bills_with_payment_words_stay_purchases() -> None:
    # Regression: a bare PAYMENT/PMT/EPAY/AUTOPAY on a bank statement is rent, a utility, a loan
    # or an insurer — real spend that must not be hidden as a transfer or a payment.
    for description in (
        "RENT PAYMENT UNIT 4B",
        "CITY WATER EPAY",
        "ELECTRIC UTILITY AUTOPAY",
        "AUTO LOAN PMT",
        "INSURANCE PAYMENT",
        "DEBIT CARD PAYMENT CORNER CAFE",
    ):
        for account_type in ("checking", "savings"):
            assert (
                resolve_kind(
                    kind_hint=None,
                    description_clean=description,
                    amount_minor=120000,
                    account_type=account_type,
                )
                == "purchase"
            ), description


#: Realistic checking-side wording for a payment to a credit card (B1: these must never count as
#: spend, because the purchases were already counted on the card statement).
_BANK_CARD_PAYMENTS = (
    "CHASE CREDIT CRD AUTOPAY",
    "Payment To Chase Card Ending IN 1234",
    "DISCOVER E-PAYMENT",
    "CITI AUTOPAY PAYMENT",
    "AMEX EPAYMENT ACH PMT",
    "AMERICAN EXPRESS ACH PMT",
    "CAPITAL ONE AUTOPAY",
    "BARCLAYCARD US PAYMENT",
    "SYNCHRONY BANK PAYMENT",
    "WELLS FARGO CARD PAYMENT",
    "WELLS FARGO CARD ONLINE PMT",
    "BANK OF AMERICA CREDIT CARD BILL PAYMENT",
    "BOA CARD AUTO PAY",
    "BK OF AMER CRD PMT",
    "US BANK CARD PAYMENT",
    "APPLE CARD PAYMENT",
    "GS BANK PAYMENT",
    "GOLDMAN SACHS BANK USA PAYMENT",
)

_BANK_BILLS = (
    "CHASE MORTGAGE PAYMENT",
    "CITI LOAN PMT",
    "VENMO PAYMENT",
    "ATT PAYMENT",
    "WELLS FARGO HOME MORTGAGE PAYMENT",
    "WELLS FARGO AUTO PAYMENT",
    "BANK OF AMERICA MORTGAGE PMT",
    "US BANK STUDENT LOAN PAYMENT",
    "CHASE AUTO LOAN PAYMENT",
    "DISCOVER STUDENT LOANS PAYMENT",
    "ZELLE PAYMENT TO CHASE QUICKPAY",
    "DEBIT CARD PAYMENT CORNER CAFE",
    "DEBIT CARD PURCHASE CHASE FARMERS MARKET",
    "CHECK CARD PAYMENT GROCER",
    "PAYMENT TO DEBIT CARD ENDING 4455",
    "CITIZENS PROPANE PAYMENT",
)


def test_checking_card_payments_with_realistic_wording_are_transfers() -> None:
    for description in _BANK_CARD_PAYMENTS:
        for account_type in ("checking", "savings"):
            assert (
                resolve_kind(
                    kind_hint=None,
                    description_clean=description,
                    amount_minor=80000,
                    account_type=account_type,
                )
                == "transfer"
            ), description


def test_purchase_hint_on_a_bank_account_is_overridden_by_card_wording() -> None:
    # Layout D emits `purchase` for any withdrawal it does not itself recognise; the issuer or
    # brand wording must still win so the payment is not double counted.
    for description in _BANK_CARD_PAYMENTS:
        assert (
            resolve_kind(
                kind_hint="purchase",
                description_clean=description,
                amount_minor=80000,
                account_type="checking",
            )
            == "transfer"
        ), description


def test_purchase_hint_with_configured_issuer_name_is_overridden_on_a_bank_account() -> None:
    terms = ("Acme Bank",)

    def kind(description: str, account_type: str) -> str:
        return resolve_kind(
            kind_hint="purchase",
            description_clean=description,
            amount_minor=15000,
            account_type=account_type,
            issuer_match_terms=terms,
        )

    assert kind("ACME BANK PAYMENT", "checking") == "transfer"
    # ... but a lender sharing the name is still a bill, and a credit account is never affected.
    assert kind("ACME BANK AUTO LOAN PAYMENT", "checking") == "purchase"
    assert kind("ACME BANK PAYMENT", "credit") == "purchase"


def test_bills_and_debit_card_spend_stay_purchases_with_or_without_a_hint() -> None:
    for description in _BANK_BILLS:
        for hint in (None, "purchase"):
            assert (
                resolve_kind(
                    kind_hint=hint,
                    description_clean=description,
                    amount_minor=120000,
                    account_type="checking",
                )
                == "purchase"
            ), (hint, description)


def test_no_hint_checking_unexplained_inflow_is_a_transfer_not_a_refund() -> None:
    # Regression: a paycheck from a generic-parsed bank statement must not be netted against spend.
    for description in (
        "ACME CORP PAYROLL",
        "DIRECT DEP ACME",
        "ZELLE FROM JAMIE",
        "MOBILE DEPOSIT",
    ):
        for account_type in ("checking", "savings"):
            assert (
                resolve_kind(
                    kind_hint=None,
                    description_clean=description,
                    amount_minor=-250000,
                    account_type=account_type,
                )
                == "transfer"
            ), description


def test_no_hint_checking_inflow_with_refund_wording_is_a_refund() -> None:
    for description in ("MERCHANT REFUND WIDGET CO", "PURCHASE RETURN", "REVERSAL OF CHARGE"):
        assert (
            resolve_kind(
                kind_hint=None,
                description_clean=description,
                amount_minor=-3000,
                account_type="checking",
            )
            == "refund"
        ), description


def test_no_hint_checking_interest_earned_is_not_spend() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="INTEREST PAID",
            amount_minor=-212,
            account_type="savings",
        )
        == "transfer"
    )
    # ...but interest *charged* (an outflow) still is.
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="OVERDRAFT INTEREST",
            amount_minor=212,
            account_type="checking",
        )
        == "interest"
    )


def test_no_hint_credit_purchase_with_payment_word_is_a_purchase() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="PAYMENT PROCESSING INC",
            amount_minor=4500,
            account_type="credit",
        )
        == "purchase"
    )


def test_no_hint_payment_pattern() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="PAYMENT THANK YOU",
            amount_minor=-1500,
            account_type="credit",
        )
        == "payment"
    )


def test_no_hint_default_sign_purchase_for_outflow() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="GROCERY STORE",
            amount_minor=4599,
            account_type="credit",
        )
        == "purchase"
    )


def test_no_hint_default_sign_refund_for_inflow_on_a_card() -> None:
    assert (
        resolve_kind(
            kind_hint=None,
            description_clean="GROCERY STORE",
            amount_minor=-4599,
            account_type="credit",
        )
        == "refund"
    )


def test_is_spend() -> None:
    assert is_spend("purchase")
    assert is_spend("fee")
    assert is_spend("interest")
    for kind in ("refund", "payment", "transfer", "adjustment"):
        assert not is_spend(kind)


# `_issuer_payment_match` is also exercised directly, for its proximity edge cases.


def test_issuer_payment_match_no_terms_configured() -> None:
    assert kinds._issuer_payment_match("ACME BANK PAYMENT", ()) is False


def test_issuer_payment_match_no_payment_token_at_all() -> None:
    assert kinds._issuer_payment_match("ACME BANK PURCHASE", ("Acme Bank",)) is False


def test_issuer_payment_match_skips_blank_terms() -> None:
    assert kinds._issuer_payment_match("SOME PAYMENT HERE", ("", "   ")) is False


def test_issuer_payment_match_term_adjacent_to_token() -> None:
    assert kinds._issuer_payment_match("ACME BANK PAYMENT REF", ("Acme Bank",)) is True


def test_issuer_payment_match_term_far_from_token() -> None:
    far = "ACME BANK " + ("X" * 40) + " PAYMENT"
    assert kinds._issuer_payment_match(far, ("Acme Bank",)) is False
