"""Tests for the built-in rules corpus (D8, P4-B): `classify/builtin_rules.yaml`.

`rules.load_builtin_rules()` already validates every entry against the taxonomy and
self-tests each rule against its own `test_description` at import time (`classify/rules.py`);
this file adds the corpus-quality checks that are P4-B's own job: the corpus is close to the
~120-pattern target, every rule's own example matches (re-asserted explicitly here, not just
relied on at import), a set of near-miss merchants that share a substring with some pattern but
are a *different* real merchant do **not** match any rule (precision over recall), and no two
rules that disagree on category/subcategory both match the same merchant_key (an ambiguous rule
pair would make the cascade's builtin-rule step non-deterministic in effect, since only the
first hit in list order would apply, silently discarding another rule's claim).
"""

from __future__ import annotations

from collections import Counter

from personal_finance_analyzer.classify import rules
from personal_finance_analyzer.ingest.normalize import normalize

# --------------------------------------------------------------------------------------------
# Corpus-level checks.
# --------------------------------------------------------------------------------------------


def test_corpus_is_close_to_the_120_pattern_target() -> None:
    # D8: "~120 high-precision patterns". Not an exact count -- a floor that keeps the corpus
    # from silently shrinking, and a ceiling that would flag an accidental duplication bug.
    assert 110 <= len(rules.BUILTIN_RULES) <= 140


def test_every_rule_has_a_justification_and_a_matching_test_case() -> None:
    for rule in rules.BUILTIN_RULES:
        assert rule.justification.strip(), f"{rule.pattern!r} has an empty justification"
        assert rule.test_description.strip(), f"{rule.pattern!r} has an empty test_description"
        merchant_key = normalize(rule.test_description, ()).merchant_key
        assert rules.rule_matches_text(rule.match_type, rule.pattern, merchant_key), (
            f"rule {rule.pattern!r} does not match the merchant_key derived from its own "
            f"test_description {rule.test_description!r}"
        )


def test_no_duplicate_pattern_match_type_pairs() -> None:
    pairs = [(r.pattern, r.match_type) for r in rules.BUILTIN_RULES]
    counts = Counter(pairs)
    dupes = {pair: n for pair, n in counts.items() if n > 1}
    assert not dupes, f"duplicate (pattern, match_type) pairs: {dupes}"


def test_no_two_rules_with_different_categories_both_match_any_test_case() -> None:
    """Precision over recall (D8): if two rules disagree on category/subcategory yet both
    match some merchant_key, the cascade's first-hit-wins order would silently pick one and
    discard the other's claim -- exactly the kind of ambiguity D8 says a builtin rule must not
    introduce. Checked over every rule's own `test_description`, not just a hand-picked sample."""
    ambiguous: list[tuple[str, str, set[tuple[str, str]]]] = []
    for rule in rules.BUILTIN_RULES:
        merchant_key = normalize(rule.test_description, ()).merchant_key
        matches = [
            other
            for other in rules.BUILTIN_RULES
            if rules.rule_matches_text(other.match_type, other.pattern, merchant_key)
        ]
        categories = {(m.category_key, m.subcategory_key) for m in matches}
        if len(categories) > 1:
            ambiguous.append((rule.pattern, merchant_key, categories))
    assert not ambiguous, f"ambiguous test cases (matched by >1 category/subcategory): {ambiguous}"


def test_no_bank_or_card_issuer_name_appears_in_any_pattern() -> None:
    # D14: issuer identity is user data; no bank/card-issuer name is ever hard-coded.
    forbidden_terms = (
        "chase",
        "bank of america",
        "wells fargo",
        "citibank",
        "citi ",
        "capital one",
        "discover",
        "american express",
        "amex",
        "us bank",
        "barclay",
        "synchrony",
    )
    for rule in rules.BUILTIN_RULES:
        lowered = rule.pattern.lower()
        for term in forbidden_terms:
            assert term not in lowered, f"pattern {rule.pattern!r} contains issuer name {term!r}"


# --------------------------------------------------------------------------------------------
# Near-miss negatives: real, plausible merchant/transaction descriptions that share a word or
# fragment with a builtin pattern but name a genuinely different merchant, and must not be
# swept in by any rule (precision over recall).
# --------------------------------------------------------------------------------------------

_NEAR_MISS_DESCRIPTIONS: tuple[str, ...] = (
    "PEETS COFFEE SEATTLE WA",  # not Starbucks
    "WENDY SMITH CONSULTING LLC",  # not Wendy's -- a person's name, not the chain
    "MOBILE DEPOSIT CHECK 001",  # not Mobil (fuel) -- "MOBIL\\b" must not match "mobile"
    "UNITEDHEALTH GROUP PREMIUM",  # not United Airlines
    "BUDGET GROCERY OUTLET",  # not a rental-car brand ("ENTERPRISE RENT"/"NATIONAL CAR RENTAL")
    "NEW YORK CITY TRANSIT MTA FARE",  # not the Subway restaurant chain
    "AMAZON MKTP US MERCHANDISE",  # deliberately uncovered: a general marketplace order
    "IN N OUT BURGER LOS ANGELES CA",  # deliberately uncovered: no rule claims every burger chain
    "PANDA EXPRESS ORLANDO FL",  # deliberately uncovered, same reasoning
    "SPIRIT AIRLINES ORLANDO FL",  # deliberately uncovered: only the listed airline brands match
    "SAMSUNG ELECTRONICS STORE",  # not Sam's Club
    "CHASE SAPPHIRE ANNUAL FEE",  # not the "LATE FEE" rule, and never a bank-issuer pattern anyway
    "AMERICAN EAGLE OUTFITTERS",  # not American Airlines
    "GREAT WOLF LODGE RESORT",  # not "GREAT CLIPS" (personal_care)
    "STEAMBOAT SPRINGS RESORT",  # not the Steam game platform ("\\bSTEAM\\b" needs a word boundary)
    "ANGIE PARKS PHOTOGRAPHY",  # not "ANGI" home services
    "MIDASTOUCH DAY SPA",  # not "\\bMIDAS\\b" auto repair
)


def test_near_miss_descriptions_match_no_builtin_rule() -> None:
    for description in _NEAR_MISS_DESCRIPTIONS:
        merchant_key = normalize(description, ()).merchant_key
        matches = [
            rule
            for rule in rules.BUILTIN_RULES
            if rules.rule_matches_text(rule.match_type, rule.pattern, merchant_key)
        ]
        assert not matches, (
            f"{description!r} (merchant_key={merchant_key!r}) unexpectedly matched: "
            f"{[(r.pattern, r.category_key, r.subcategory_key) for r in matches]}"
        )


# --------------------------------------------------------------------------------------------
# Spot checks that a rule resolves to the right category/subcategory (beyond the plain
# match-only check above), covering the direct-vs-third-party-delivery-app split and a few
# subtleties introduced by `store_aliases.yaml`.
# --------------------------------------------------------------------------------------------


def _lookup(pattern: str) -> rules.BuiltinRuleDef:
    matching = [r for r in rules.BUILTIN_RULES if r.pattern == pattern]
    assert len(matching) == 1, f"expected exactly one rule with pattern {pattern!r}"
    return matching[0]


def test_direct_restaurant_charge_is_dine_in_not_delivery() -> None:
    for pattern in ("DOMINOS", "PIZZA HUT", "SUBWAY"):
        rule = _lookup(pattern)
        assert rule.category_key == "restaurant"
        assert rule.subcategory_key == "dine_in"


def test_third_party_delivery_app_is_delivery_not_dine_in() -> None:
    for pattern in ("DOORDASH", "GRUBHUB", "UBER EATS"):
        rule = _lookup(pattern)
        assert rule.category_key == "restaurant"
        assert rule.subcategory_key == "delivery"


def test_uber_ride_and_uber_eats_do_not_share_a_category() -> None:
    ride_key = normalize("UBER *TRIP HELP.UBER.COM", ()).merchant_key
    eats_key = normalize("UBER *EATS ORDER SEATTLE WA", ()).merchant_key

    ride_matches = {
        (r.category_key, r.subcategory_key)
        for r in rules.BUILTIN_RULES
        if rules.rule_matches_text(r.match_type, r.pattern, ride_key)
    }
    eats_matches = {
        (r.category_key, r.subcategory_key)
        for r in rules.BUILTIN_RULES
        if rules.rule_matches_text(r.match_type, r.pattern, eats_key)
    }
    assert ride_matches == {("transportation", "rideshare")}
    assert eats_matches == {("restaurant", "delivery")}


def test_disney_streaming_and_disney_theme_park_resolve_differently() -> None:
    streaming_key = normalize("PARAMOUNT PLUS NEW YORK NY", ()).merchant_key
    theme_park_key = normalize("DISNEYLAND RESORT ANAHEIM CA", ()).merchant_key

    streaming_rule = next(
        r
        for r in rules.BUILTIN_RULES
        if rules.rule_matches_text(r.match_type, r.pattern, streaming_key)
    )
    theme_park_rule = next(
        r
        for r in rules.BUILTIN_RULES
        if rules.rule_matches_text(r.match_type, r.pattern, theme_park_key)
    )
    assert streaming_rule.category_key == "entertainment"
    assert streaming_rule.subcategory_key == "streaming"
    assert theme_park_rule.category_key == "entertainment"
    assert theme_park_rule.subcategory_key == "theme_parks"


def test_no_amazon_marketplace_rule_exists() -> None:
    # D8's own example of an ambiguous merchant to avoid: a generic Amazon marketplace order's
    # category depends on what was bought, not who sold it. Only the unambiguous "AMAZON PRIME"
    # membership/streaming charge gets a builtin rule.
    marketplace_key = normalize("AMZN MKTP US MERCHANDISE", ()).merchant_key
    matches = [
        r
        for r in rules.BUILTIN_RULES
        if rules.rule_matches_text(r.match_type, r.pattern, marketplace_key)
    ]
    assert not matches
