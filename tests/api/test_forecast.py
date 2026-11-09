"""`/api/net-worth/forecast/*` and `/api/net-worth/history/*`."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spend_analyzer.db.models import ForecastPreset, NetWorthHistory
from tests.forecast_seed import add_balance, add_user

HIST = "/api/net-worth/history"
PRESETS = "/api/net-worth/forecast/presets"


def _user(client: TestClient, name: str = "Alex") -> int:
    return int(client.post("/api/users", json={"name": name}).json()["id"])


def _params(**kw: Any) -> dict[str, Any]:
    return {"horizon_years": 20, "income_growth_pct": 4.0, "return_pct": 7.0, **kw}


# --- baseline ----------------------------------------------------------------------------------


def test_baseline_empty_and_unknown_user(client: TestClient) -> None:
    body = client.get("/api/net-worth/forecast/baseline").json()
    assert body["history"] == [] and body["user_id"] is None and body["currency"] == "USD"
    assert body["defaults"]["net_worth_minor"] is None
    assert client.get("/api/net-worth/forecast/baseline", params={"user_id": 99}).status_code == 404


def test_baseline_manual_vs_entries_precedence(client: TestClient, session: Session) -> None:
    uid = add_user(session, "Alex")
    add_balance(session, uid, "savings", "Ally", 2024, 12, 500_000)
    session.commit()
    assert client.put(f"{HIST}/{uid}/2024", json={"amount_minor": 1}).status_code == 201
    assert (
        client.put(f"{HIST}/{uid}/2020", json={"amount_minor": 250_000, "note": "n"}).status_code
        == 201
    )

    body = client.get("/api/net-worth/forecast/baseline", params={"user_id": uid}).json()
    rows = {r["year"]: r for r in body["history"]}
    assert rows[2024]["source"] == "entries" and rows[2024]["net_worth_minor"] == 500_000
    assert rows[2024]["manual_net_worth_minor"] == 1  # shown, not used
    assert rows[2020]["source"] == "manual" and rows[2020]["net_worth_minor"] == 250_000
    assert body["defaults"]["net_worth_minor"] == 500_000
    assert body["defaults"]["net_worth_year"] == 2024

    assert client.delete(f"{HIST}/{uid}/2024").status_code == 204
    rows = {
        r["year"]: r
        for r in client.get("/api/net-worth/forecast/baseline", params={"user_id": uid}).json()[
            "history"
        ]
    }
    assert rows[2024]["manual_net_worth_minor"] is None


# --- manual past-year net worth ----------------------------------------------------------------


def test_history_crud_and_negative_values(client: TestClient, session: Session) -> None:
    uid = _user(client)
    assert client.get(f"{HIST}/{uid}/2020").status_code == 404
    created = client.put(
        f"{HIST}/{uid}/2020", json={"amount_minor": -5_000, "note": "student loan"}
    )
    assert created.status_code == 201
    assert created.json()["amount_minor"] == -5_000 and created.json()["user_id"] == uid
    updated = client.put(f"{HIST}/{uid}/2020", json={"amount_minor": 7_000})
    assert updated.status_code == 200
    assert updated.json()["id"] == created.json()["id"] and updated.json()["note"] is None
    assert client.get(f"{HIST}/{uid}/2020").json()["amount_minor"] == 7_000
    assert [r["year"] for r in client.get(HIST, params={"user_id": uid}).json()] == [2020]
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(NetWorthHistory)) == 1
    assert client.delete(f"{HIST}/{uid}/2020").status_code == 204
    assert client.delete(f"{HIST}/{uid}/2020").status_code == 404


def test_history_validation_and_ownership(client: TestClient) -> None:
    uid = _user(client)
    other = _user(client, "Sam")
    assert client.put(f"{HIST}/999/2020", json={"amount_minor": 1}).status_code == 404
    assert client.put(f"{HIST}/{uid}/1800", json={"amount_minor": 1}).status_code == 422
    assert client.put(f"{HIST}/{uid}/2999", json={"amount_minor": 1}).status_code == 422  # > 2300
    assert client.put(f"{HIST}/{uid}/2250", json={"amount_minor": 1}).status_code == 422  # future
    assert client.put(f"{HIST}/{uid}/2020", json={"amount_minor": 10**14}).status_code == 422
    assert client.put(f"{HIST}/{uid}/2020", json={"amount_minor": "x"}).status_code == 422
    assert (
        client.put(f"{HIST}/{uid}/2020", json={"amount_minor": 1, "note": "x" * 501}).status_code
        == 422
    )
    # One user's value is invisible to, and untouched by, another's.
    client.put(f"{HIST}/{uid}/2020", json={"amount_minor": 100})
    assert client.get(f"{HIST}/{other}/2020").status_code == 404
    assert client.delete(f"{HIST}/{other}/2020").status_code == 404
    assert client.get(f"{HIST}/{uid}/2020").status_code == 200
    assert client.get(HIST, params={"user_id": other}).json() == []


def test_user_delete_cascades_history_and_presets(client: TestClient, session: Session) -> None:
    uid = _user(client)
    client.put(f"{HIST}/{uid}/2020", json={"amount_minor": 100})
    client.post(PRESETS, json={"name": "Mine", "user_id": uid, "params": _params()})
    client.post(PRESETS, json={"name": "Shared", "params": _params()})
    assert client.delete(f"/api/users/{uid}").status_code == 204
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(NetWorthHistory)) == 0
    assert [p.name for p in session.scalars(select(ForecastPreset))] == ["Shared"]


# --- presets -----------------------------------------------------------------------------------


def test_builtin_presets_are_listed_and_read_only(client: TestClient) -> None:
    body = client.get(PRESETS).json()
    assert [p["name"] for p in body["builtin"]] == ["Conservative", "Moderate", "Aggressive"]
    moderate = body["builtin"][1]["params"]
    assert (moderate["income_growth_pct"], moderate["return_pct"]) == (3.0, 6.0)
    assert (moderate["spending_growth_pct"], moderate["inflation_pct"]) == (2.5, 2.5)
    assert moderate["horizon_years"] is None
    assert body["saved"] == []
    # Built-in names cannot be shadowed by a saved preset, in any case.
    for name in ("Moderate", "conservative", "  AGGRESSIVE "):
        r = client.post(PRESETS, json={"name": name, "params": _params()})
        assert r.status_code == 409, name


def test_preset_crud_roundtrip(client: TestClient) -> None:
    uid = _user(client)
    created = client.post(
        PRESETS,
        json={
            "name": " Early  retirement ",
            "user_id": uid,
            "params": _params(
                retirement_year=2045,
                retirement_income_minor=2_000_000,
                events=[{"year": 2030, "amount_minor": -10_000_000, "label": "House"}],
            ),
        },
    )
    assert created.status_code == 201
    preset = created.json()
    assert preset["name"] == "Early retirement" and preset["user_id"] == uid
    assert preset["params"]["events"] == [
        {"year": 2030, "amount_minor": -10_000_000, "label": "House"}
    ]
    assert preset["params"]["retirement_year"] == 2045
    assert (
        preset["params"]["start_net_worth_minor"] is None
    )  # "use the data" survives the round trip

    renamed = client.put(f"{PRESETS}/{preset['id']}", json={"name": "FIRE"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "FIRE"
    assert renamed.json()["params"] == preset["params"]  # params untouched by a rename
    reparam = client.put(f"{PRESETS}/{preset['id']}", json={"params": _params(return_pct=5.0)})
    assert reparam.json()["params"]["return_pct"] == 5.0 and reparam.json()["name"] == "FIRE"

    assert [p["name"] for p in client.get(PRESETS, params={"user_id": uid}).json()["saved"]] == [
        "FIRE"
    ]
    assert client.delete(f"{PRESETS}/{preset['id']}").status_code == 204
    assert client.delete(f"{PRESETS}/{preset['id']}").status_code == 404
    assert client.put(f"{PRESETS}/{preset['id']}", json={"name": "x"}).status_code == 404


def test_preset_visibility_scopes(client: TestClient) -> None:
    alex, sam = _user(client), _user(client, "Sam")
    client.post(PRESETS, json={"name": "Shared one", "params": _params()})
    client.post(PRESETS, json={"name": "Alex only", "user_id": alex, "params": _params()})
    names = lambda **q: [p["name"] for p in client.get(PRESETS, params=q).json()["saved"]]  # noqa: E731
    assert names(user_id=alex) == ["Shared one", "Alex only"]
    assert names(user_id=sam) == ["Shared one"]
    assert names() == ["Shared one"]  # all-users view: shared only
    assert client.get(PRESETS, params={"user_id": 99}).status_code == 404


def test_list_skips_saved_presets_with_invalid_stored_params(
    client: TestClient, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    good = client.post(PRESETS, json={"name": "Good", "params": _params()}).json()
    bad = client.post(PRESETS, json={"name": "Bad", "params": _params()}).json()
    broken = client.post(PRESETS, json={"name": "Broken JSON", "params": _params()}).json()
    # Stored JSON is re-validated on read: out-of-range, wrong type and unparseable all fail.
    session.get(ForecastPreset, bad["id"]).params = '{"horizon_years": 9999}'  # type: ignore[union-attr]
    session.get(ForecastPreset, broken["id"]).params = "not json"  # type: ignore[union-attr]
    session.commit()

    with caplog.at_level(logging.WARNING, logger="spend_analyzer.api.services.forecast"):
        r = client.get(PRESETS)
    assert r.status_code == 200
    assert [p["id"] for p in r.json()["saved"]] == [good["id"]]
    # They are not lost from view: listed by name so they can be renamed, repaired or deleted.
    assert {p["id"]: p["name"] for p in r.json()["invalid"]} == {
        bad["id"]: "Bad",
        broken["id"]: "Broken JSON",
    }
    assert len(r.json()["builtin"]) > 0
    assert str(bad["id"]) in caplog.text and str(broken["id"]) in caplog.text
    # The rows are kept, so nothing is lost; the user can still replace the parameters.
    assert session.get(ForecastPreset, bad["id"]) is not None
    assert client.put(f"{PRESETS}/{bad['id']}", json={"params": _params()}).status_code == 200
    assert [p["name"] for p in client.get(PRESETS).json()["saved"]] == ["Bad", "Good"]


def test_invalid_preset_can_be_renamed_deleted_and_its_name_reused(
    client: TestClient, session: Session
) -> None:
    bad = client.post(PRESETS, json={"name": "Old plan", "params": _params()}).json()
    session.get(ForecastPreset, bad["id"]).params = '{"horizon_years": 9999}'  # type: ignore[union-attr]
    session.commit()

    # Its hidden name does not block a new preset with the same name.
    fresh = client.post(PRESETS, json={"name": "old plan", "params": _params()})
    assert fresh.status_code == 201

    # Renaming it works (no 500) and returns the name-only form; the row is still invalid.
    renamed = client.put(f"{PRESETS}/{bad['id']}", json={"name": "Archived"})
    assert renamed.status_code == 200
    assert renamed.json() == {"id": bad["id"], "user_id": None, "name": "Archived"}
    listing = client.get(PRESETS).json()
    assert [p["name"] for p in listing["invalid"]] == ["Archived"]
    assert [p["name"] for p in listing["saved"]] == ["old plan"]

    # A valid preset's name still conflicts, and a repair brings the preset back.
    assert client.put(f"{PRESETS}/{bad['id']}", json={"name": "Old Plan"}).status_code == 409
    fixed = client.put(f"{PRESETS}/{bad['id']}", json={"params": _params()})
    assert fixed.status_code == 200 and fixed.json()["params"]["horizon_years"] == 20
    assert client.get(PRESETS).json()["invalid"] == []

    other = client.post(PRESETS, json={"name": "Other", "params": _params()}).json()
    session.get(ForecastPreset, other["id"]).params = "not json"  # type: ignore[union-attr]
    session.commit()
    assert client.delete(f"{PRESETS}/{other['id']}").status_code == 204
    assert client.get(PRESETS).json()["invalid"] == []


def test_preset_keeps_the_starting_year(client: TestClient) -> None:
    created = client.post(
        PRESETS, json={"name": "From 2020", "params": _params(base_year=2020)}
    ).json()
    assert created["params"]["base_year"] == 2020
    assert client.get(PRESETS).json()["saved"][0]["params"]["base_year"] == 2020
    plain = client.post(PRESETS, json={"name": "Latest", "params": _params()}).json()
    assert plain["params"]["base_year"] is None  # None = the latest year with data
    bad = client.post(PRESETS, json={"name": "Bad", "params": _params(base_year=1800)})
    assert bad.status_code == 422


def test_baseline_all_users_mixes_entries_and_manual_per_person(
    client: TestClient, session: Session
) -> None:
    alex, sam = add_user(session, "Alex"), add_user(session, "Sam")
    add_balance(session, alex, "savings", "A", 2024, 12, 100_000)
    session.commit()
    assert client.put(f"{HIST}/{sam}/2024", json={"amount_minor": 5_000_000}).status_code == 201
    body = client.get("/api/net-worth/forecast/baseline").json()
    row = {r["year"]: r for r in body["history"]}[2024]
    assert (row["net_worth_minor"], row["source"]) == (5_100_000, "mixed")
    assert body["defaults"]["net_worth_minor"] == 5_100_000


def test_preset_name_uniqueness_per_scope(client: TestClient) -> None:
    alex, sam = _user(client), _user(client, "Sam")
    body = {"name": "Plan A", "params": _params()}
    assert client.post(PRESETS, json=body).status_code == 201
    assert (
        client.post(PRESETS, json={**body, "name": "plan a"}).status_code == 409
    )  # shared, NULL user
    # The same name is fine for each user, but not twice for one user.
    assert client.post(PRESETS, json={**body, "user_id": alex}).status_code == 201
    assert client.post(PRESETS, json={**body, "user_id": sam}).status_code == 201
    assert client.post(PRESETS, json={**body, "user_id": alex}).status_code == 409
    other = client.post(PRESETS, json={**body, "name": "Plan B", "user_id": alex}).json()
    assert client.put(f"{PRESETS}/{other['id']}", json={"name": "plan a"}).status_code == 409
    assert client.put(f"{PRESETS}/{other['id']}", json={"name": "Moderate"}).status_code == 409
    assert (
        client.put(f"{PRESETS}/{other['id']}", json={"name": "PLAN B"}).status_code == 200
    )  # own name


def test_preset_validation(client: TestClient) -> None:
    uid = _user(client)

    def post(**over: Any) -> int:
        body = {"name": "X", "params": _params(), **over}
        return client.post(PRESETS, json=body).status_code

    assert post(user_id=999) == 404
    assert post(name="") == 422
    assert post(name="   ") == 422  # whitespace only: rejected after normalisation
    assert post(name="n" * 61) == 422
    assert post(params=_params(horizon_years=0)) == 422
    assert post(params=_params(horizon_years=61)) == 422
    assert post(params=_params(return_pct=31)) == 422
    assert post(params=_params(return_pct=-21)) == 422
    assert post(params=_params(income_growth_pct=51)) == 422
    assert post(params=_params(inflation_pct=31)) == 422
    assert post(params=_params(tax_rate_pct=101)) == 422
    assert post(params=_params(tax_rate_pct=-1)) == 422
    assert post(params=_params(annual_income_minor=-1)) == 422
    assert post(params=_params(annual_spending_minor=-1)) == 422
    assert post(params=_params(retirement_year=1800)) == 422
    assert post(params=_params(events=[{"year": 1800, "amount_minor": 1}])) == 422
    assert post(params=_params(events=[{"year": 2030, "amount_minor": 10**14}])) == 422
    assert post(params=_params(events=[{"year": 2030, "amount_minor": 1}] * 51)) == 422
    assert post(params=_params(start_net_worth_minor="lots")) == 422
    # Bounds are inclusive, and negatives are allowed where they make sense.
    edge = client.post(
        PRESETS,
        json={
            "name": "Edge",
            "user_id": uid,
            "params": _params(
                horizon_years=60,
                return_pct=-20,
                income_growth_pct=-50,
                tax_rate_pct=0,
                start_net_worth_minor=-500,
                spending_change_minor=-100,
            ),
        },
    )
    assert edge.status_code == 201
    # An update is validated like a create, and a rejected one changes nothing.
    url = f"{PRESETS}/{edge.json()['id']}"
    assert client.put(url, json={"params": _params(return_pct=99)}).status_code == 422
    assert client.put(url, json={"name": ""}).status_code == 422
    assert (
        client.get(PRESETS, params={"user_id": uid}).json()["saved"][0]["params"]["return_pct"]
        == -20
    )
