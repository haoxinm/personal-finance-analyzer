"""Classifier eval harness (P4-C).

Runs a labelled set of synthetic transaction descriptions (`tests/eval/descriptions.csv`, or any
CSV of the same shape) through `ingest.normalize.normalize` and the deterministic rules step of
the classification cascade (`classify.rules.BUILTIN_RULES`), and, in `mode="llm"`, sends whatever
those rules leave unclassified through the existing LLM egress path
(`classify.llm.batching.classify_all` -> `classify.llm.egress.classify_batch`) — the same code
path the real cascade uses, so this harness never re-implements egress or its safety checks.

This module touches no database: it is an offline batch tool, not part of the transaction
pipeline. Run it manually against a real provider before changing `classify.llm.prompt.
PROMPT_VERSION` (§5 P4-C); it is deliberately **not** part of CI in network mode — CI only
exercises it with the LLM mocked (`mode="rules"`, or `mode="llm"` with `personal_finance_analyzer.llm.client.complete`
monkeypatched).
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from personal_finance_analyzer.classify.llm.batching import classify_all
from personal_finance_analyzer.classify.rules import rule_matches_text
from personal_finance_analyzer.classify.taxonomy import CATEGORY_KEYS
from personal_finance_analyzer.config import LLMConfig
from personal_finance_analyzer.core.errors import ConfigError
from personal_finance_analyzer.ingest.normalize import normalize

#: The eval mode: "rules" runs only the deterministic builtin-rules step (no network call,
#: ever); "llm" additionally sends every description the rules leave unclassified through the
#: real LLM egress path, and requires `cfg` to name a configured provider and model.
EvalMode = Literal["rules", "llm"]


@dataclass(frozen=True, slots=True)
class EvalRow:
    """One labelled row of the eval CSV: a raw description and its ground-truth taxonomy pair."""

    description: str
    category: str
    subcategory: str


@dataclass(frozen=True, slots=True)
class MisclassifiedRow:
    """One eval row whose predicted `(category, subcategory)` did not match the label."""

    description: str
    expected_category: str
    expected_subcategory: str
    predicted_category: str
    predicted_subcategory: str
    classified_by: str  # "builtin_rule" | "llm" | "fallback"


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The outcome of one `run_eval` call.

    `accuracy` and `category_accuracy` both require an exact `(category, subcategory)` match
    (I4: a transaction has exactly one category and one subcategory, so a category-only match
    with the wrong subcategory is not "correct"). `confusion` is keyed by **category** only
    (`confusion[true_category][predicted_category] = count`); subcategory-level confusion is not
    reported, since 300 synthetic rows spread over ~40 subcategories would make most cells 0 or 1
    and add noise rather than signal.
    """

    mode: EvalMode
    total: int
    correct: int
    accuracy: float
    category_totals: dict[str, int]
    category_accuracy: dict[str, float]
    confusion: dict[str, dict[str, int]]
    llm_requests: int
    cost_usd: float
    misclassified: tuple[MisclassifiedRow, ...] = field(default_factory=tuple)

    def text_table(self) -> str:
        """Render accuracy-per-category and the confusion matrix as fixed-width text tables."""
        lines = [
            f"Eval report (mode={self.mode}): {self.correct}/{self.total} correct "
            f"({self.accuracy:.1%})",
        ]
        if self.mode == "llm":
            lines.append(f"LLM requests: {self.llm_requests}  cost: ${self.cost_usd:.4f}")
        lines.append("")
        lines.append("Accuracy by category:")
        cat_width = max((len(c) for c in self.category_totals), default=8)
        for category in sorted(self.category_totals):
            acc = self.category_accuracy[category]
            total = self.category_totals[category]
            lines.append(f"  {category:<{cat_width}}  {acc:>6.1%}  (n={total})")

        categories = sorted(self.confusion)
        if categories:
            lines.append("")
            lines.append("Confusion matrix (rows=expected, columns=predicted):")
            col_width = max(3, max((len(c) for c in categories), default=3))
            header = " " * (cat_width + 2) + " ".join(
                f"{c[:col_width]:>{col_width}}" for c in categories
            )
            lines.append(header)
            for true_cat in categories:
                row_counts = self.confusion[true_cat]
                cells = " ".join(f"{row_counts.get(c, 0):>{col_width}}" for c in categories)
                lines.append(f"  {true_cat:<{cat_width}}{cells}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Render the full report (including misclassified rows) as JSON."""
        return json.dumps(
            {
                "mode": self.mode,
                "total": self.total,
                "correct": self.correct,
                "accuracy": self.accuracy,
                "category_totals": self.category_totals,
                "category_accuracy": self.category_accuracy,
                "confusion": self.confusion,
                "llm_requests": self.llm_requests,
                "cost_usd": self.cost_usd,
                "misclassified": [
                    {
                        "description": row.description,
                        "expected_category": row.expected_category,
                        "expected_subcategory": row.expected_subcategory,
                        "predicted_category": row.predicted_category,
                        "predicted_subcategory": row.predicted_subcategory,
                        "classified_by": row.classified_by,
                    }
                    for row in self.misclassified
                ],
            },
            indent=2,
            ensure_ascii=False,
        )


@dataclass(frozen=True, slots=True)
class _Prediction:
    category: str
    subcategory: str
    classified_by: str


def load_eval_csv(csv_path: Path) -> tuple[EvalRow, ...]:
    """Load and validate `csv_path` (columns: `description,category,subcategory`).

    Raises:
        ConfigError: the file is missing a required column, is empty, or a row's
            `category`/`subcategory` is not a real taxonomy pair.
    """
    from personal_finance_analyzer.classify.taxonomy import subcategories_for

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or not {"description", "category", "subcategory"}.issubset(
            set(reader.fieldnames)
        ):
            raise ConfigError(
                f"{csv_path}: expected columns 'description,category,subcategory', "
                f"got {reader.fieldnames!r}"
            )
        rows: list[EvalRow] = []
        for line_no, raw in enumerate(reader, start=2):
            description = (raw["description"] or "").strip()
            category = (raw["category"] or "").strip()
            subcategory = (raw["subcategory"] or "").strip()
            if not description:
                raise ConfigError(f"{csv_path}:{line_no}: empty description")
            if category not in CATEGORY_KEYS:
                raise ConfigError(f"{csv_path}:{line_no}: unknown category {category!r}")
            if subcategory not in subcategories_for(category):
                raise ConfigError(
                    f"{csv_path}:{line_no}: unknown subcategory {subcategory!r} for "
                    f"category {category!r}"
                )
            rows.append(
                EvalRow(description=description, category=category, subcategory=subcategory)
            )

    if not rows:
        raise ConfigError(f"{csv_path}: no data rows")
    return tuple(rows)


def _match_builtin_rules(merchant_key: str) -> tuple[str, str] | None:
    """The first `classify.rules.BUILTIN_RULES` entry whose pattern matches `merchant_key`
    (mirrors cascade step 4 — builtin rules, in file order — without touching the database:
    `BUILTIN_RULES` is the same in-memory tuple the cascade seeds from)."""
    from personal_finance_analyzer.classify.rules import BUILTIN_RULES

    for defn in BUILTIN_RULES:
        if rule_matches_text(defn.match_type, defn.pattern, merchant_key):
            return (defn.category_key, defn.subcategory_key)
    return None


def run_eval(cfg: LLMConfig, *, csv_path: Path, mode: EvalMode) -> EvalReport:
    """Run the eval harness over `csv_path` and return an `EvalReport`.

    Every row is normalized (`ingest.normalize.normalize`, no PII terms) and matched against
    `classify.rules.BUILTIN_RULES`. In `mode="rules"`, anything the rules do not match falls
    back to `others/uncategorized` (`classified_by="fallback"`) — no network call is made, ever.
    In `mode="llm"`, everything the rules do not match is instead sent through
    `classify.llm.batching.classify_all` (deduplication by `merchant_key`, exactly as the real
    cascade's step 7 does, is *not* performed here: the eval set is one row per description, and
    the point of the harness is to score the LLM's per-description accuracy, not the cache).

    Args:
        cfg: the LLM provider configuration. Only read when `mode="llm"`.
        csv_path: a CSV with columns `description,category,subcategory` (ground truth).
        mode: `"rules"` (deterministic only) or `"llm"` (rules, then LLM for the rest).

    Returns:
        An `EvalReport` with overall and per-category accuracy, a category-level confusion
        matrix, and every misclassified row.

    Raises:
        ConfigError: `csv_path` is malformed, or `mode="llm"` but `cfg` names no endpoint.
    """
    if mode == "llm" and not cfg.is_configured:
        raise ConfigError(
            "run_eval(mode='llm') requires a configured [llm] endpoint (enabled, base_url); "
            "use mode='rules' to evaluate the deterministic cascade only"
        )

    rows = load_eval_csv(csv_path)
    predictions: list[_Prediction | None] = [None] * len(rows)
    unresolved: list[int] = []

    for idx, row in enumerate(rows):
        normalized = normalize(row.description, ())
        hit = _match_builtin_rules(normalized.merchant_key)
        if hit is not None:
            predictions[idx] = _Prediction(
                category=hit[0], subcategory=hit[1], classified_by="builtin_rule"
            )
        else:
            unresolved.append(idx)

    llm_requests = 0
    cost_usd = 0.0
    if unresolved and mode == "llm":
        descriptions = [
            normalize(rows[idx].description, ()).description_clean for idx in unresolved
        ]
        run_result = classify_all(descriptions, cfg)
        llm_requests = len(run_result.runs)
        cost_usd = sum(r.cost_usd for r in run_result.runs)
        for pos, idx in enumerate(unresolved):
            item = run_result.results[pos].item
            predictions[idx] = _Prediction(
                category=item.category, subcategory=item.subcategory, classified_by="llm"
            )
    else:
        for idx in unresolved:
            predictions[idx] = _Prediction(
                category="others", subcategory="uncategorized", classified_by="fallback"
            )

    return _score(rows, predictions, mode=mode, llm_requests=llm_requests, cost_usd=cost_usd)


def _score(
    rows: tuple[EvalRow, ...],
    predictions: list[_Prediction | None],
    *,
    mode: EvalMode,
    llm_requests: int,
    cost_usd: float,
) -> EvalReport:
    category_totals: dict[str, int] = defaultdict(int)
    category_correct: dict[str, int] = defaultdict(int)
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    misclassified: list[MisclassifiedRow] = []
    correct = 0

    for row, prediction in zip(rows, predictions, strict=True):
        if prediction is None:  # pragma: no cover - every row is filled above
            raise ConfigError("internal error: unresolved eval row was never predicted")
        category_totals[row.category] += 1
        confusion[row.category][prediction.category] += 1
        is_correct = (
            prediction.category == row.category and prediction.subcategory == row.subcategory
        )
        if is_correct:
            correct += 1
            category_correct[row.category] += 1
        else:
            misclassified.append(
                MisclassifiedRow(
                    description=row.description,
                    expected_category=row.category,
                    expected_subcategory=row.subcategory,
                    predicted_category=prediction.category,
                    predicted_subcategory=prediction.subcategory,
                    classified_by=prediction.classified_by,
                )
            )

    category_accuracy = {
        category: category_correct[category] / total for category, total in category_totals.items()
    }

    return EvalReport(
        mode=mode,
        total=len(rows),
        correct=correct,
        accuracy=correct / len(rows) if rows else 0.0,
        category_totals=dict(category_totals),
        category_accuracy=category_accuracy,
        confusion={cat: dict(preds) for cat, preds in confusion.items()},
        llm_requests=llm_requests,
        cost_usd=cost_usd,
        misclassified=tuple(misclassified),
    )


__all__ = [
    "EvalMode",
    "EvalReport",
    "EvalRow",
    "MisclassifiedRow",
    "load_eval_csv",
    "run_eval",
]
