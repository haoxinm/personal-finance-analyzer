"""Net worth forecast storage: manual past-year net worth and saved presets (local only; no
LLM/HTTP imports; no commit here)."""

from __future__ import annotations

import logging

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spend_analyzer.api import schemas
from spend_analyzer.core.forecast import is_builtin_name, normalize_preset_name
from spend_analyzer.db.models import ForecastPreset, NetWorthHistory, User

_log = logging.getLogger(__name__)


class PresetNameConflict(ValueError):
    """The name is taken by a built-in preset or by another saved preset in the same scope."""


def user_exists(session: Session, user_id: int) -> bool:
    return session.get(User, user_id) is not None


# --- Manual past-year net worth ----------------------------------------------------------------


def get_history(session: Session, user_id: int, year: int) -> NetWorthHistory | None:
    return session.scalar(
        select(NetWorthHistory).where(
            NetWorthHistory.user_id == user_id, NetWorthHistory.year == year
        )
    )


def list_history(session: Session, user_id: int | None) -> list[NetWorthHistory]:
    stmt = select(NetWorthHistory).order_by(NetWorthHistory.year.desc(), NetWorthHistory.user_id)
    if user_id is not None:
        stmt = stmt.where(NetWorthHistory.user_id == user_id)
    return list(session.scalars(stmt))


def put_history(
    session: Session, user_id: int, year: int, body: schemas.NetWorthHistoryPut
) -> tuple[NetWorthHistory, bool]:
    """Insert or replace the (user, year) value; the bool is True when a row was created."""
    row = get_history(session, user_id, year)
    created = row is None
    if row is None:
        row = NetWorthHistory(user_id=user_id, year=year, amount_minor=body.amount_minor)
        session.add(row)
    row.amount_minor = body.amount_minor
    row.note = body.note
    session.flush()
    return row, created


def delete_history(session: Session, row: NetWorthHistory) -> None:
    session.delete(row)
    session.flush()


# --- Presets -----------------------------------------------------------------------------------


def preset_to_schema(row: ForecastPreset) -> schemas.ForecastPreset:
    return schemas.ForecastPreset(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        params=schemas.ForecastParams.model_validate_json(row.params),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def invalid_preset_to_schema(row: ForecastPreset) -> schemas.InvalidForecastPreset:
    return schemas.InvalidForecastPreset(id=row.id, user_id=row.user_id, name=row.name)


def is_valid_preset(row: ForecastPreset) -> bool:
    """True when the stored parameters still pass validation."""
    try:
        schemas.ForecastParams.model_validate_json(row.params)
    except ValidationError:
        return False
    return True


def split_saved_presets(
    rows: list[ForecastPreset],
) -> tuple[list[schemas.ForecastPreset], list[schemas.InvalidForecastPreset]]:
    """Convert stored presets for the list endpoint. A preset whose stored JSON no longer
    validates (hand-edited, or written by a build with looser limits) cannot be loaded, so one bad
    row cannot take the whole list down: it is returned separately (name only) so it can still be
    renamed, repaired with new parameters or deleted. The row stays in the database and a warning
    names its id."""
    valid: list[schemas.ForecastPreset] = []
    invalid: list[schemas.InvalidForecastPreset] = []
    for row in rows:
        try:
            valid.append(preset_to_schema(row))
        except ValidationError as exc:
            _log.warning(
                "forecast preset id=%s: stored parameters are invalid (%d error(s))",
                row.id,
                exc.error_count(),
            )
            invalid.append(invalid_preset_to_schema(row))
    return valid, invalid


def get_preset(session: Session, preset_id: int) -> ForecastPreset | None:
    return session.get(ForecastPreset, preset_id)


def list_presets(session: Session, user_id: int | None) -> list[ForecastPreset]:
    """Shared presets plus, for a given user, that user's own; shared ones first, by name."""
    stmt = select(ForecastPreset)
    if user_id is None:
        stmt = stmt.where(ForecastPreset.user_id.is_(None))
    else:
        stmt = stmt.where((ForecastPreset.user_id.is_(None)) | (ForecastPreset.user_id == user_id))
    stmt = stmt.order_by(ForecastPreset.user_id.is_not(None), func.lower(ForecastPreset.name))
    return list(session.scalars(stmt))


def preset_visible_to(row: ForecastPreset, user_id: int | None) -> bool:
    """The same scope `list_presets` uses: shared presets, plus the given user's own."""
    return row.user_id is None or row.user_id == user_id


def _check_name(
    session: Session, name: str, user_id: int | None, *, exclude_id: int | None = None
) -> None:
    """Raises PresetNameConflict when the name is built in or already used in that scope.

    Every saved preset counts, including one whose stored parameters no longer validate: it still
    holds its name in the database. The comparison uses ``casefold`` in Python (SQL ``lower()``
    only folds ASCII), so it is at least as strict as the database's exact-case constraints."""
    if is_builtin_name(name):
        raise PresetNameConflict(f"{name!r} is the name of a built-in preset")
    stmt = select(ForecastPreset).where(
        ForecastPreset.user_id.is_(None) if user_id is None else ForecastPreset.user_id == user_id
    )
    if exclude_id is not None:
        stmt = stmt.where(ForecastPreset.id != exclude_id)
    folded = name.casefold()
    if any(other.name.casefold() == folded for other in session.scalars(stmt)):
        raise PresetNameConflict(f"a preset named {name!r} already exists")


def _flush_checking_name(session: Session, name: str) -> None:
    """Flush, turning a unique-constraint violation (a race, or a name the check missed) into a
    PresetNameConflict instead of an uncaught IntegrityError."""
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise PresetNameConflict(f"a preset named {name!r} already exists") from None


def create_preset(session: Session, body: schemas.ForecastPresetCreate) -> ForecastPreset:
    """Raises:
    ValueError: the name is blank. PresetNameConflict: the name is already used in that scope.
    """
    name = normalize_preset_name(body.name)
    _check_name(session, name, body.user_id)
    row = ForecastPreset(user_id=body.user_id, name=name, params=body.params.model_dump_json())
    session.add(row)
    _flush_checking_name(session, name)
    return row


def update_preset(
    session: Session, row: ForecastPreset, body: schemas.ForecastPresetUpdate
) -> ForecastPreset:
    """Raises:
    ValueError: the new name is blank. PresetNameConflict: the new name is already used.

    Repairing a preset (new params for one whose stored params are invalid) re-checks its current
    name too, since nothing guaranteed it was unique while the preset was hidden."""
    name = row.name
    if body.name is not None:
        name = normalize_preset_name(body.name)
        _check_name(session, name, row.user_id, exclude_id=row.id)
    elif body.params is not None and not is_valid_preset(row):
        _check_name(session, name, row.user_id, exclude_id=row.id)
    row.name = name
    if body.params is not None:
        row.params = body.params.model_dump_json()
    _flush_checking_name(session, name)
    return row


def delete_preset(session: Session, row: ForecastPreset) -> None:
    session.delete(row)
    session.flush()
