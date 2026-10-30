"""Federal tax parameter sets: schema, bundled file, local cache and user override.

No network, no database, and no LLM import. The bundled ``tax_data/federal_params.yaml`` is the
offline fallback and always present. Two optional files live in ``<data dir>/tax_params/``:

- ``federal_params.yaml``: a copy downloaded by the opt-in yearly check
  (`api/services/tax_params_update.py`, the only code that may make that request);
- ``override.yaml``: a file the user imported themselves (``spend-analyzer tax params import``).

**Precedence** (`resolve`): a valid *override* always wins (the user chose it deliberately, for
example to correct a figure or to use a newer year before a release); otherwise the valid file with
the highest ``params_version`` of the cached download and the bundled one, the bundled one winning a
tie (so an app upgrade that ships newer data is never shadowed by an older cached copy). An invalid
or unreadable optional file is ignored (logged, never raised): the bundled data always loads.

Every file is validated by `parse_params`, whichever way it arrived. Amounts in the file are whole
dollars; the loader converts to integer cents. Files are written atomically with mode 0600.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Literal, cast

import yaml

from spend_analyzer.core.paths import ensure_home, tax_params_dir

_log = logging.getLogger(__name__)

_DATA_PACKAGE = "spend_analyzer.core"
_DATA_FILE = "federal_params.yaml"
_CACHED_NAME = "federal_params.yaml"
_OVERRIDE_NAME = "override.yaml"
_STATE_NAME = "state.json"
#: Largest params file read or accepted (the bundled one is about 20 KB).
MAX_PARAMS_BYTES = 256 * 1024

#: Filing statuses as emitted by the 1040 parser.
FILING_STATUSES = ("single", "mfj", "mfs", "hoh", "qss")
#: Statuses that get the larger additional standard deduction (unmarried, not a surviving spouse).
_UNMARRIED = frozenset({"single", "hoh"})

_CENTS = 100
_MIN_YEAR = 1990

ParamSource = Literal["bundled", "cached", "override"]


@dataclass(frozen=True, slots=True)
class Table:
    brackets: tuple[tuple[int, int], ...]  # (lower threshold in cents, rate in basis points)
    ltcg_0_to: int
    ltcg_15_to: int


@dataclass(frozen=True, slots=True)
class FederalParams:
    """All parameters for one tax year; every amount in cents. See the YAML header."""

    tax_year: int
    sources: tuple[str, ...]
    standard_deduction_minor: Mapping[str, int]
    additional_standard_deduction_unmarried_minor: int
    additional_standard_deduction_married_minor: int
    capital_loss_limit_minor: int
    capital_loss_limit_mfs_minor: int
    ss_wage_base_minor: int
    elective_deferral_limit_minor: int
    niit_threshold_minor: Mapping[str, int]
    additional_medicare_threshold_minor: Mapping[str, int]
    tables: Mapping[str, Table]

    def standard_deduction(self, filing_status: str, aged_or_blind_count: int = 0) -> int:
        """Basic standard deduction plus the additional amount per aged / blind box (0-4 boxes;
        the larger amount for single and head of household filers)."""
        extra = (
            self.additional_standard_deduction_unmarried_minor
            if filing_status in _UNMARRIED
            else self.additional_standard_deduction_married_minor
        )
        return self.standard_deduction_minor[filing_status] + extra * max(aged_or_blind_count, 0)

    def capital_loss_limit(self, filing_status: str) -> int:
        """Annual limit on a net capital loss deducted against other income (>= 0)."""
        if filing_status == "mfs":
            return self.capital_loss_limit_mfs_minor
        return self.capital_loss_limit_minor


@dataclass(frozen=True, slots=True)
class ParamSet:
    """A validated file: its ``params_version`` and the parameters per tax year."""

    version: int
    years: Mapping[int, FederalParams]
    source: ParamSource = "bundled"


# --- schema ----------------------------------------------------------------------------------------


def _need(raw: Mapping[str, object], key: str, where: str) -> object:
    if key not in raw:
        raise ValueError(f"{where}: missing {key!r}")
    return raw[key]


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: expected a mapping")
    return cast("Mapping[str, object]", value)


def _dollars(value: object, where: str, *, allow_zero: bool = False) -> int:
    """Whole dollars (a non-negative int, YAML bool excluded) as cents."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where}: expected a whole number of dollars, got {value!r}")
    if value == 0 and not allow_zero:
        raise ValueError(f"{where}: must be positive")
    return value * _CENTS


def _by_status(value: object, where: str) -> dict[str, int]:
    raw = _mapping(value, where)
    if set(raw) != set(FILING_STATUSES):
        raise ValueError(f"{where}: must list exactly {', '.join(FILING_STATUSES)}")
    return {status: _dollars(raw[status], f"{where}.{status}") for status in FILING_STATUSES}


def _parse_table(brackets: object, ltcg: object, where: str) -> Table:
    if not isinstance(brackets, list) or not brackets:
        raise ValueError(f"{where}: brackets must be a non-empty list")
    rows: list[tuple[int, int]] = []
    for row in brackets:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError(f"{where}: each bracket is [threshold_dollars, rate_percent]")
        threshold = _dollars(row[0], f"{where}.threshold", allow_zero=True)
        rate = row[1]
        if isinstance(rate, bool) or not isinstance(rate, int) or not 0 < rate < 100:
            raise ValueError(f"{where}: rate must be a whole percent between 1 and 99")
        rows.append((threshold, rate * 100))
    thresholds = [t for t, _ in rows]
    rates = [r for _, r in rows]
    if thresholds[0] != 0:
        raise ValueError(f"{where}: first bracket threshold must be 0")
    if thresholds != sorted(set(thresholds)) or rates != sorted(set(rates)):
        raise ValueError(f"{where}: thresholds and rates must be strictly increasing")
    if not isinstance(ltcg, list) or len(ltcg) != 2:
        raise ValueError(f"{where}: ltcg is [zero_rate_top, fifteen_rate_top]")
    zero_to = _dollars(ltcg[0], f"{where}.ltcg0")
    fifteen_to = _dollars(ltcg[1], f"{where}.ltcg15")
    if zero_to >= fifteen_to:
        raise ValueError(f"{where}: the 0% band must end below the 15% band")
    return Table(brackets=tuple(rows), ltcg_0_to=zero_to, ltcg_15_to=fifteen_to)


def _parse_year(year: int, raw: Mapping[str, object]) -> FederalParams:
    where = str(year)
    sources_raw = _need(raw, "sources", where)
    if (
        not isinstance(sources_raw, list)
        or not sources_raw
        or not all(isinstance(s, str) and s.strip() for s in sources_raw)
    ):
        raise ValueError(f"{where}: sources must be a non-empty list of citations")
    standard = _by_status(_need(raw, "standard_deduction", where), f"{where}.standard_deduction")
    extra = _mapping(
        _need(raw, "additional_standard_deduction", where), f"{where}.additional_standard_deduction"
    )
    loss = _mapping(_need(raw, "capital_loss_limit", where), f"{where}.capital_loss_limit")
    brackets = _mapping(_need(raw, "brackets", where), f"{where}.brackets")
    ltcg = _mapping(_need(raw, "ltcg", where), f"{where}.ltcg")
    for name, mapping in (("brackets", brackets), ("ltcg", ltcg)):
        if set(mapping) != set(FILING_STATUSES):
            raise ValueError(f"{where}.{name}: must list exactly {', '.join(FILING_STATUSES)}")
    tables = {
        status: _parse_table(brackets[status], ltcg[status], f"{where}.{status}")
        for status in FILING_STATUSES
    }
    if tables["qss"] != tables["mfj"] or standard["qss"] != standard["mfj"]:
        raise ValueError(f"{where}: qss must use the mfj tables and standard deduction")
    return FederalParams(
        tax_year=year,
        sources=tuple(cast("list[str]", sources_raw)),
        standard_deduction_minor=standard,
        additional_standard_deduction_unmarried_minor=_dollars(
            extra.get("unmarried"), f"{where}.additional_standard_deduction.unmarried"
        ),
        additional_standard_deduction_married_minor=_dollars(
            extra.get("married"), f"{where}.additional_standard_deduction.married"
        ),
        capital_loss_limit_minor=_dollars(loss.get("default"), f"{where}.capital_loss_limit"),
        capital_loss_limit_mfs_minor=_dollars(loss.get("mfs"), f"{where}.capital_loss_limit.mfs"),
        ss_wage_base_minor=_dollars(_need(raw, "ss_wage_base", where), f"{where}.ss_wage_base"),
        elective_deferral_limit_minor=_dollars(
            _need(raw, "elective_deferral_limit", where), f"{where}.elective_deferral_limit"
        ),
        niit_threshold_minor=_by_status(
            _need(raw, "niit_threshold", where), f"{where}.niit_threshold"
        ),
        additional_medicare_threshold_minor=_by_status(
            _need(raw, "additional_medicare_threshold", where),
            f"{where}.additional_medicare_threshold",
        ),
        tables=tables,
    )


def parse_params(raw: object, *, source: ParamSource = "bundled") -> ParamSet:
    """Validate a parsed params file (``params_version`` + ``years``) and convert dollars to cents.

    Raises:
        ValueError: any structural problem (missing key or filing status, non-increasing
            thresholds, a year without a source citation, qss differing from mfj, a missing or
            non-positive ``params_version``, ...).
    """
    top = _mapping(raw, "file")
    version = _need(top, "params_version", "file")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("params_version must be a positive integer")
    years_raw = _mapping(_need(top, "years", "file"), "years")
    years: dict[int, FederalParams] = {}
    for key, value in years_raw.items():
        if isinstance(key, bool) or not isinstance(key, int) or key < _MIN_YEAR:
            raise ValueError(f"invalid tax year key: {key!r}")
        years[key] = _parse_year(key, _mapping(value, str(key)))
    if not years:
        raise ValueError("no tax years")
    return ParamSet(version=version, years=years, source=source)


def parse_params_text(text: str, *, source: ParamSource = "bundled") -> ParamSet:
    """`parse_params` on YAML text. Raises `ValueError` (also for oversized or malformed YAML)."""
    if len(text.encode("utf-8")) > MAX_PARAMS_BYTES:
        raise ValueError("params file is too large")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("params file is not valid YAML") from None
    return parse_params(raw, source=source)


# --- files -----------------------------------------------------------------------------------------


def cached_path() -> Path:
    """The downloaded copy (``<data dir>/tax_params/federal_params.yaml``)."""
    return tax_params_dir() / _CACHED_NAME


def override_path() -> Path:
    """The user-imported file (``<data dir>/tax_params/override.yaml``)."""
    return tax_params_dir() / _OVERRIDE_NAME


def _state_path() -> Path:
    return tax_params_dir() / _STATE_NAME


@functools.cache
def load_bundled() -> ParamSet:
    """The parameters shipped in the package (always valid; a bad file is a packaging bug)."""
    text = resources.files(_DATA_PACKAGE).joinpath("tax_data", _DATA_FILE).read_text("utf-8")
    return parse_params_text(text, source="bundled")


def _read_optional(path: Path, source: ParamSource) -> ParamSet | None:
    """A valid `ParamSet` from `path`, or `None` (missing, unreadable or invalid; logged)."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_PARAMS_BYTES:
            return None
        return parse_params_text(path.read_text(encoding="utf-8"), source=source)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _log.warning("ignoring invalid %s tax parameter file: %s", source, type(exc).__name__)
        return None


def _signature() -> tuple[tuple[str, int, int], ...]:
    """(path, mtime_ns, size) of the optional files, so a changed file is picked up without a
    restart and a different data directory (tests) never shares a stale result."""
    out: list[tuple[str, int, int]] = []
    for path in (cached_path(), override_path()):
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(path), 0, 0))
    return tuple(out)


_resolved: tuple[tuple[tuple[str, int, int], ...], ParamSet] | None = None


def resolve() -> ParamSet:
    """The parameter set in effect (see the module docstring for the precedence)."""
    global _resolved
    sig = _signature()
    if _resolved is not None and _resolved[0] == sig:
        return _resolved[1]
    bundled = load_bundled()
    override = _read_optional(override_path(), "override")
    cached = _read_optional(cached_path(), "cached")
    chosen = bundled
    if cached is not None and cached.version > bundled.version:
        chosen = cached
    if override is not None:
        chosen = override
    _resolved = (sig, chosen)
    return chosen


def versions() -> dict[ParamSource, int | None]:
    """The ``params_version`` of each candidate (``None`` when absent or invalid)."""
    cached = _read_optional(cached_path(), "cached")
    override = _read_optional(override_path(), "override")
    return {
        "bundled": load_bundled().version,
        "cached": cached.version if cached else None,
        "override": override.version if override else None,
    }


def _write_private(path: Path, text: str) -> None:
    """Atomic write with mode 0600 (best effort where chmod is unsupported)."""
    ensure_home()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)


def install_cached(text: str) -> ParamSet:
    """Validate `text` and store it as the downloaded copy.

    Raises:
        ValueError: invalid file.
    """
    parsed = parse_params_text(text, source="cached")
    _write_private(cached_path(), text)
    return parsed


def install_override(text: str) -> ParamSet:
    """Validate `text` and store it as the user's override (wins over everything else).

    Raises:
        ValueError: invalid file.
    """
    parsed = parse_params_text(text, source="override")
    _write_private(override_path(), text)
    return parsed


def clear_override() -> bool:
    """Remove the user's override; True when there was one."""
    try:
        override_path().unlink()
    except FileNotFoundError:
        return False
    return True


# --- once-a-year check state -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckState:
    """When the yearly update check last ran and how it ended (``outcome`` is a short fixed
    word: ``updated``, ``current``, ``rejected`` or ``unreachable``)."""

    last_checked: date | None = None
    outcome: str | None = None


def read_check_state() -> CheckState:
    """The recorded state; a missing or corrupt state file reads as "never checked"."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        checked = date.fromisoformat(str(data["last_checked"]))
        outcome = data.get("outcome")
        return CheckState(checked, outcome if isinstance(outcome, str) else None)
    except (OSError, ValueError, KeyError, TypeError):
        return CheckState()


def record_check(day: date, outcome: str) -> None:
    """Record that a check ran on `day` (calendar date, local time)."""
    _write_private(
        _state_path(), json.dumps({"last_checked": day.isoformat(), "outcome": outcome}) + "\n"
    )
