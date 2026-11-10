"""`/api/users` (§3.12). No SQL here — delegates to `api/services/crud.py`."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from personal_finance_analyzer.api import schemas
from personal_finance_analyzer.api.deps import SessionDep
from personal_finance_analyzer.api.services import crud

router = APIRouter(tags=["users"])


@router.get("/users", response_model=list[schemas.User])
def list_users(session: SessionDep) -> list[schemas.User]:
    return [schemas.User.model_validate(u) for u in crud.list_users(session)]


@router.post("/users", response_model=schemas.User, status_code=201)
def create_user(body: schemas.UserCreate, session: SessionDep) -> schemas.User:
    user = crud.create_user(session, name=body.name, is_default=body.is_default)
    session.commit()
    return schemas.User.model_validate(user)


@router.patch("/users/{user_id}", response_model=schemas.User)
def update_user(user_id: int, body: schemas.UserUpdate, session: SessionDep) -> schemas.User:
    user = crud.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    user = crud.update_user(session, user, name=body.name, is_default=body.is_default)
    session.commit()
    return schemas.User.model_validate(user)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, session: SessionDep) -> None:
    user = crud.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    crud.delete_user(session, user)
    session.commit()
