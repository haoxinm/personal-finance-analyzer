"""`/api/net-worth/forecast/*` and `/api/net-worth/history/*`. No SQL here: delegates to
`api/services/forecast.py` and `analytics/forecast_baseline.py`. The projection itself runs in the
browser; this router only supplies history and stores manual net worth and presets. Local only
(never exported, never sent to an LLM)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response

from personal_finance_analyzer.analytics.forecast_baseline import run_forecast_baseline
from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import forecast as service
from personal_finance_analyzer.core.forecast import BUILTIN_PRESETS, MAX_YEAR, MIN_YEAR

router = APIRouter(tags=["net-worth"])

_UserId = Annotated[int, Path(description="User id.")]
_Year = Annotated[int, Path(ge=MIN_YEAR, le=MAX_YEAR, description="Calendar year.")]


@router.get("/net-worth/forecast/baseline", response_model=schemas.ForecastBaseline)
def baseline(session: SessionDep, user_id: int | None = None) -> schemas.ForecastBaseline:
    if user_id is not None and not service.user_exists(session, user_id):
        raise HTTPException(status_code=404, detail="user not found")
    result = run_forecast_baseline(session, user_id)
    return schemas.ForecastBaseline.model_validate(asdict(result))


# --- Manual past-year net worth ----------------------------------------------------------------


@router.get("/net-worth/history", response_model=list[schemas.NetWorthHistory])
def list_history(session: SessionDep, user_id: int | None = None) -> list[schemas.NetWorthHistory]:
    rows = service.list_history(session, user_id)
    return [schemas.NetWorthHistory.model_validate(r) for r in rows]


@router.get("/net-worth/history/{user_id}/{year}", response_model=schemas.NetWorthHistory)
def get_history(user_id: _UserId, year: _Year, session: SessionDep) -> schemas.NetWorthHistory:
    row = service.get_history(session, user_id, year)
    if row is None:
        raise HTTPException(status_code=404, detail="no manual net worth for that year")
    return schemas.NetWorthHistory.model_validate(row)


@router.put("/net-worth/history/{user_id}/{year}", response_model=schemas.NetWorthHistory)
def put_history(
    user_id: _UserId,
    year: _Year,
    body: schemas.NetWorthHistoryPut,
    response: Response,
    session: SessionDep,
) -> schemas.NetWorthHistory:
    if not service.user_exists(session, user_id):
        raise HTTPException(status_code=404, detail="user not found")
    if year > date.today().year:
        raise HTTPException(status_code=422, detail="year must not be in the future")
    row, created = service.put_history(session, user_id, year, body)
    session.commit()
    response.status_code = 201 if created else 200
    return schemas.NetWorthHistory.model_validate(row)


@router.delete("/net-worth/history/{user_id}/{year}", status_code=204)
def delete_history(user_id: _UserId, year: _Year, session: SessionDep) -> None:
    row = service.get_history(session, user_id, year)
    if row is None:
        raise HTTPException(status_code=404, detail="no manual net worth for that year")
    service.delete_history(session, row)
    session.commit()


# --- Presets -----------------------------------------------------------------------------------


@router.get("/net-worth/forecast/presets", response_model=schemas.ForecastPresetList)
def list_presets(session: SessionDep, user_id: int | None = None) -> schemas.ForecastPresetList:
    """Built-in presets plus the saved ones visible to `user_id`: shared presets and that user's
    own. Without `user_id` (the all-users view), shared presets only. A saved preset whose stored
    parameters no longer validate is listed under `invalid` (name only, and logged) rather than
    failing the list; it can be renamed, repaired by saving new parameters, or deleted."""
    if user_id is not None and not service.user_exists(session, user_id):
        raise HTTPException(status_code=404, detail="user not found")
    saved, invalid = service.split_saved_presets(service.list_presets(session, user_id))
    return schemas.ForecastPresetList(
        builtin=[
            schemas.BuiltinForecastPreset(
                key=p.key,
                name=p.name,
                description=p.description,
                params=schemas.ForecastParams(
                    income_growth_pct=p.income_growth_pct,
                    return_pct=p.return_pct,
                    spending_growth_pct=p.spending_growth_pct,
                    inflation_pct=p.inflation_pct,
                ),
            )
            for p in BUILTIN_PRESETS
        ],
        saved=saved,
        invalid=invalid,
    )


@router.post("/net-worth/forecast/presets", response_model=schemas.ForecastPreset, status_code=201)
def create_preset(
    body: schemas.ForecastPresetCreate, session: SessionDep
) -> schemas.ForecastPreset:
    if body.user_id is not None and not service.user_exists(session, body.user_id):
        raise HTTPException(status_code=404, detail="user not found")
    try:
        row = service.create_preset(session, body)
    except service.PresetNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return service.preset_to_schema(row)


@router.put(
    "/net-worth/forecast/presets/{id}",
    response_model=schemas.ForecastPreset | schemas.InvalidForecastPreset,
)
def update_preset(
    id: int, body: schemas.ForecastPresetUpdate, session: SessionDep, user_id: int | None = None
) -> schemas.ForecastPreset | schemas.InvalidForecastPreset:
    """Rename and/or replace the parameters. A preset whose stored parameters are invalid can be
    renamed (the response is then the name-only `InvalidForecastPreset`) or repaired by sending
    new `params`. `user_id` is the viewing user, as for the list: another user's preset is 404."""
    row = service.get_preset(session, id)
    if row is None or not service.preset_visible_to(row, user_id):
        raise HTTPException(status_code=404, detail="preset not found")
    try:
        service.update_preset(session, row, body)
    except service.PresetNameConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    if not service.is_valid_preset(row):
        return service.invalid_preset_to_schema(row)
    return service.preset_to_schema(row)


@router.delete("/net-worth/forecast/presets/{id}", status_code=204)
def delete_preset(id: int, session: SessionDep, user_id: int | None = None) -> None:
    row = service.get_preset(session, id)
    if row is None or not service.preset_visible_to(row, user_id):
        raise HTTPException(status_code=404, detail="preset not found")
    service.delete_preset(session, row)
    session.commit()
