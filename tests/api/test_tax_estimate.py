"""`/api/tax/estimate`, `/api/tax/profiles/...`, `/api/tax/parameters` and the headline on
`/api/tax/summary`."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from spend_analyzer.api.services import tax_params_update as upd
from spend_analyzer.core import tax_params
from tests.analytics.tax_seed import add_form
from tests.tax_params_support import with_next_year

D = 100


def _user(client: TestClient, name: str = "Alex") -> int:
    return int(client.post("/api/users", json={"name": name}).json()["id"])


def _profile(**kw: Any) -> dict[str, Any]:
    return {"filing_status": "single", **kw}


def _seed_w2(session: Session, user: int, wages: int, withheld: int, year: int = 2024) -> None:
    add_form(
        session,
        user,
        year,
        "w2",
        income={"salary_wages": wages * D},
        taxes={"federal_income_tax_withheld": withheld * D},
        tin="0001",
    )
    session.commit()


# --- profiles ---------------------------------------------------------------------------------------


def test_profile_crud(client: TestClient) -> None:
    uid = _user(client)
    url = f"/api/tax/profiles/{uid}/2024"
    assert client.get(url).status_code == 404
    created = client.put(url, json=_profile(deduction_mode="itemized", itemized_deductions_minor=5))
    assert created.status_code == 201
    body = created.json()
    assert body["user_id"] == uid and body["tax_year"] == 2024
    assert body["deduction_mode"] == "itemized" and body["itemized_deductions_minor"] == 5
    assert body["adjustments_minor"] == 0 and body["joint_with_user_id"] is None
    updated = client.put(url, json=_profile(filing_status="hoh", other_income_minor=700))
    assert updated.status_code == 200
    assert updated.json()["filing_status"] == "hoh"
    assert updated.json()["itemized_deductions_minor"] == 0  # PUT replaces the whole profile
    assert client.get(url).json()["other_income_minor"] == 700
    assert client.delete(url).status_code == 204
    assert client.delete(url).status_code == 404
    assert client.get(url).status_code == 404


def test_profile_validation(client: TestClient) -> None:
    uid = _user(client)
    url = f"/api/tax/profiles/{uid}/2024"
    for bad in (
        {"filing_status": "widow"},
        {"filing_status": "single", "deduction_mode": "maybe"},
        {"filing_status": "single", "adjustments_minor": -1},
        {"filing_status": "single", "age65_or_blind_count": 5},
        {"filing_status": "single", "age65_or_blind_count": 3},  # one person: at most 2 boxes
        {"filing_status": "hoh", "age65_or_blind_count": 3},
        {"filing_status": "mfs", "age65_or_blind_count": 3},
        {"filing_status": "qss", "age65_or_blind_count": 4},
        {"filing_status": "mfj", "age65_or_blind_count": 5},
        # a joint profile may carry both spouses (4 boxes, 2 seniors) but no more
        {"filing_status": "mfj", "age65_count": 3, "age65_or_blind_count": 4},
        # the senior count is at most 1 for every status but mfj, and a subset of the boxes
        {"filing_status": "single", "age65_count": 2, "age65_or_blind_count": 2},
        {"filing_status": "hoh", "age65_count": 2, "age65_or_blind_count": 2},
        {"filing_status": "mfj", "age65_count": 3, "age65_or_blind_count": 2},
        {"filing_status": "mfj", "age65_count": 2, "age65_or_blind_count": 1},
        {"filing_status": "single", "age65_count": 1, "age65_or_blind_count": 0},
        {"filing_status": "single", "other_income_minor": 10**13},
        {"filing_status": "single", "other_income_minor": 1.5},
        {},
    ):
        assert client.put(url, json=bad).status_code == 422, bad
    for good in (
        {"filing_status": "mfj", "age65_or_blind_count": 3},
        {"filing_status": "mfj", "age65_or_blind_count": 4, "age65_count": 2},
        {"filing_status": "mfj", "age65_or_blind_count": 2, "age65_count": 2},
        {"filing_status": "single", "age65_or_blind_count": 2, "age65_count": 1},
    ):
        r = client.put(url, json=good)
        assert r.status_code in (200, 201), good
        assert r.json()["age65_or_blind_count"] == good["age65_or_blind_count"]
    assert client.put(f"/api/tax/profiles/{uid}/1800", json=_profile()).status_code == 400
    assert client.put("/api/tax/profiles/999/2024", json=_profile()).status_code == 404
    assert client.get("/api/tax/profiles/999/2024").status_code == 404
    assert client.delete("/api/tax/profiles/999/2024").status_code == 404


def test_profile_spouse_rules(client: TestClient) -> None:
    a, b = _user(client, "a"), _user(client, "b")
    url = f"/api/tax/profiles/{a}/2024"
    assert (
        client.put(url, json=_profile(filing_status="single", joint_with_user_id=b)).status_code
        == 422
    )
    assert (
        client.put(url, json=_profile(filing_status="mfj", joint_with_user_id=a)).status_code == 422
    )
    assert (
        client.put(url, json=_profile(filing_status="mfj", joint_with_user_id=999)).status_code
        == 404
    )
    ok = client.put(url, json=_profile(filing_status="mfj", joint_with_user_id=b))
    assert ok.status_code == 201 and ok.json()["joint_with_user_id"] == b


def test_profile_conflict_with_the_spouses_profile_is_409(client: TestClient) -> None:
    a, b, c = _user(client, "a"), _user(client, "b"), _user(client, "c")
    client.put(f"/api/tax/profiles/{b}/2024", json=_profile(filing_status="single"))
    r = client.put(
        f"/api/tax/profiles/{a}/2024", json=_profile(filing_status="mfj", joint_with_user_id=b)
    )
    assert r.status_code == 409
    client.put(
        f"/api/tax/profiles/{b}/2024", json=_profile(filing_status="mfj", joint_with_user_id=c)
    )
    r = client.put(
        f"/api/tax/profiles/{a}/2024", json=_profile(filing_status="mfj", joint_with_user_id=b)
    )
    assert r.status_code == 409


def test_deleting_a_user_removes_their_profiles(client: TestClient) -> None:
    a, b = _user(client, "a"), _user(client, "b")
    client.put(
        f"/api/tax/profiles/{a}/2024", json=_profile(filing_status="mfj", joint_with_user_id=b)
    )
    assert client.delete(f"/api/users/{b}").status_code == 204
    assert client.get(f"/api/tax/profiles/{a}/2024").json()["joint_with_user_id"] is None
    assert client.delete(f"/api/users/{a}").status_code == 204
    assert client.get(f"/api/tax/profiles/{a}/2024").status_code == 404


# --- estimate ---------------------------------------------------------------------------------------


def test_estimate_response_shape_and_figures(client: TestClient, session: Session) -> None:
    uid = _user(client)
    _seed_w2(session, uid, 80_000, 10_000)
    r = client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2024})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "user_id",
        "tax_year",
        "scope",
        "household_user_ids",
        "filing_status",
        "filing_status_source",
        "calc",
        "user",
        "cross_check",
        "profile",
        "parameters",
        "warnings",
        "not_modeled",
    }
    assert body["scope"] == "individual" and body["household_user_ids"] == [uid]
    assert (body["filing_status"], body["filing_status_source"]) == ("single", "default")
    calc = body["calc"]
    assert calc["gross_income_minor"] == 80_000 * D
    assert calc["taxable_income_minor"] == 65_400 * D
    assert calc["total_tax_minor"] == 9_447 * D
    assert calc["result_minor"] == 553 * D and calc["refund_minor"] == 553 * D
    assert calc["owed_minor"] == 0
    assert calc["marginal_rate"] == 0.22 and calc["preferential_rate"] is None
    assert body["user"] == {
        "gross_income_minor": 80_000 * D,
        "share": None,
        "total_tax_minor": 9_447 * D,
        "payments_minor": 10_000 * D,
        "result_minor": 553 * D,
        "refund_minor": 553 * D,
        "owed_minor": 0,
        "additional_medicare_withheld_minor": 0,
    }
    for field in (
        "senior_deduction_minor",
        "net_investment_income_tax_minor",
        "additional_medicare_tax_minor",
        "additional_medicare_withheld_minor",
        "excess_social_security_credit_minor",
        "nonrefundable_credits_entered_minor",
        "refundable_credits_minor",
    ):
        assert calc[field] == 0, field
    assert body["cross_check"] is None and body["profile"] is None
    assert body["parameters"]["tax_year"] == 2024 and body["parameters"]["source"] == "bundled"
    assert body["parameters"]["sources"] == ["Rev. Proc. 2023-34 (sections 3.01, 3.03, 3.15)"]
    assert body["not_modeled"] and any("No tax profile" in w for w in body["warnings"])
    # no payer names or identifiers in an estimate
    assert "payer" not in json.dumps(body).lower()


def test_estimate_uses_the_saved_profile(client: TestClient, session: Session) -> None:
    uid = _user(client)
    _seed_w2(session, uid, 100_000, 14_000)
    client.put(
        f"/api/tax/profiles/{uid}/2024",
        json=_profile(
            filing_status="hoh", deduction_mode="itemized", itemized_deductions_minor=30_000 * D
        ),
    )
    body = client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2024}).json()
    assert body["filing_status_source"] == "profile" and body["filing_status"] == "hoh"
    assert body["calc"]["deduction_minor"] == 30_000 * D
    assert body["profile"]["filing_status"] == "hoh"


def test_estimate_errors(client: TestClient) -> None:
    uid = _user(client)
    assert client.get("/api/tax/estimate", params={"user_id": uid}).status_code == 422
    assert client.get("/api/tax/estimate", params={"tax_year": 2024}).status_code == 422
    assert (
        client.get("/api/tax/estimate", params={"user_id": 999, "tax_year": 2024}).status_code
        == 404
    )
    no_params = client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2020})
    assert no_params.status_code == 422 and "2020" in no_params.json()["detail"]
    assert (
        client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 1800}).status_code
        == 400
    )


def test_estimate_conflict_is_409(client: TestClient, session: Session) -> None:
    a, b = _user(client, "a"), _user(client, "b")
    from tests.analytics.tax_seed import add_profile

    add_profile(session, a, 2024, "mfj", joint_with_user_id=b)
    add_profile(session, b, 2024, "single")
    session.commit()
    r = client.get("/api/tax/estimate", params={"user_id": a, "tax_year": 2024})
    assert r.status_code == 409


def test_joint_estimate_allocation_over_the_api(client: TestClient, session: Session) -> None:
    a, b = _user(client, "a"), _user(client, "b")
    _seed_w2(session, a, 120_000, 15_000)
    _seed_w2(session, b, 60_000, 5_000)
    client.put(
        f"/api/tax/profiles/{a}/2024", json=_profile(filing_status="mfj", joint_with_user_id=b)
    )
    ra = client.get("/api/tax/estimate", params={"user_id": a, "tax_year": 2024}).json()
    rb = client.get("/api/tax/estimate", params={"user_id": b, "tax_year": 2024}).json()
    assert ra["scope"] == rb["scope"] == "household_allocation"
    assert ra["household_user_ids"] == rb["household_user_ids"] == [a, b]
    assert ra["calc"] == rb["calc"]
    assert (
        ra["user"]["total_tax_minor"] + rb["user"]["total_tax_minor"]
        == ra["calc"]["total_tax_minor"]
    )
    assert ra["user"]["share"] == pytest.approx(2 / 3)
    assert ra["profile"] is not None and rb["profile"] is None


def test_cross_check_in_the_response(client: TestClient, session: Session) -> None:
    uid = _user(client)
    _seed_w2(session, uid, 80_000, 10_000)
    add_form(
        session,
        uid,
        2024,
        "1040",
        taxes={"total_federal_tax": 9_000 * D, "federal_income_tax_withheld": 9_900 * D},
        filing_status="single",
    )
    session.commit()
    cc = client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2024}).json()[
        "cross_check"
    ]
    assert cc["return_total_tax_minor"] == 9_000 * D
    assert cc["delta_total_tax_minor"] == 447 * D
    assert cc["delta_withheld_minor"] == 100 * D
    assert cc["return_refund_minor"] is None and cc["delta_result_minor"] is None
    assert cc["household"] is False


# --- summary headline ------------------------------------------------------------------------------


def test_summary_carries_headline_and_superseded_forms(
    client: TestClient, session: Session
) -> None:
    uid = _user(client)
    _seed_w2(session, uid, 80_000, 10_000)
    original = add_form(
        session, uid, 2024, "w2", income={"salary_wages": 1 * D}, tin="0009", account=None
    )
    corrected = add_form(
        session, uid, 2024, "w2", income={"salary_wages": 2 * D}, tin="0009", corrected=True
    )
    session.commit()
    (summary,) = client.get("/api/tax/summary", params={"user_ids": uid}).json()
    assert summary["headline"]["federal_basis"] == "estimate"
    assert summary["superseded_forms"] == [
        {
            "form_id": original.id,
            "document_id": original.document_id,
            "user_id": uid,
            "form_type": "w2",
            "superseded_by_form_id": corrected.id,
        }
    ]
    assert summary["total_income_minor"] == 80_002 * D


# --- parameters and the opt-in update ---------------------------------------------------------------


def _bundled_text() -> str:
    return (
        resources.files("spend_analyzer.core")
        .joinpath("tax_data", "federal_params.yaml")
        .read_text(encoding="utf-8")
    )


def test_parameters_endpoint(client: TestClient) -> None:
    body = client.get("/api/tax/parameters").json()
    assert body["source"] == "bundled"
    assert body["params_version"] == body["bundled_version"] >= 1
    assert body["cached_version"] is None and body["override_version"] is None
    assert body["last_checked"] is None and body["auto_update"] is False
    assert [y["tax_year"] for y in body["years"]] == [2022, 2023, 2024, 2025, 2026]
    y2025 = next(y for y in body["years"] if y["tax_year"] == 2025)
    assert y2025["standard_deduction_minor"]["single"] == 15_750 * D
    assert y2025["standard_deduction_minor"]["hoh"] == 23_625 * D
    assert y2025["sources"] and any("Rev. Proc." in s for s in y2025["sources"])
    assert y2025["ss_wage_base_minor"] == 176_100 * D
    assert y2025["senior_deduction_minor"] == 6_000 * D and y2025["senior_phaseout_rate_bp"] == 600
    assert y2025["senior_phaseout_threshold_minor"] == {
        "single": 75_000 * D,
        "mfj": 150_000 * D,
        "hoh": 75_000 * D,
        "qss": 75_000 * D,
    }
    y2024 = next(y for y in body["years"] if y["tax_year"] == 2024)
    assert y2024["senior_deduction_minor"] == 0 and y2024["senior_phaseout_threshold_minor"] == {}


def test_refresh_endpoint_runs_one_check_with_the_opt_in_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = with_next_year(tax_params.load_bundled().version + 1)
    raw["years"][2027]["ss_wage_base"] = 190_000
    calls: list[str] = []

    def fake(url: str) -> str:
        calls.append(url)
        return yaml.safe_dump(raw)

    monkeypatch.setattr(upd, "fetch_text", fake)
    r = client.post("/api/tax/parameters/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "updated" and calls == [upd.PARAMS_URL]
    assert body["parameters"]["source"] == "cached"
    assert body["parameters"]["last_check_outcome"] == "updated"
    assert body["parameters"]["last_checked"] is not None
    years = [y["tax_year"] for y in body["parameters"]["years"]]
    assert years == [2022, 2023, 2024, 2025, 2026, 2027]  # a download only adds years
    y2027 = next(y for y in body["parameters"]["years"] if y["tax_year"] == 2027)
    assert y2027["ss_wage_base_minor"] == 190_000 * D
    # a second explicit check is allowed (and finds nothing newer)
    assert client.post("/api/tax/parameters/refresh").json()["outcome"] == "current"
    assert len(calls) == 2


def test_refresh_reports_an_unreachable_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(url: str) -> str:
        raise upd.FetchError("ConnectError")

    monkeypatch.setattr(upd, "fetch_text", down)
    body = client.post("/api/tax/parameters/refresh").json()
    assert body["outcome"] == "unreachable"
    assert body["parameters"]["source"] == "bundled"


def test_estimate_and_summary_never_fetch_while_the_opt_in_is_off(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str) -> str:
        raise AssertionError("no request may be made with the opt-in off")

    monkeypatch.setattr(upd, "fetch_text", boom)
    uid = _user(client)
    _seed_w2(session, uid, 1_000, 10)
    assert (
        client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2024}).status_code
        == 200
    )
    assert client.get("/api/tax/summary").status_code == 200


def test_the_opt_in_setting_round_trips_and_older_clients_keep_it(client: TestClient) -> None:
    settings = client.get("/api/settings").json()
    assert settings["tax"] == {"auto_update_params": False}
    settings["tax"] = {"auto_update_params": True}
    assert client.put("/api/settings", json=settings).json()["tax"] == {"auto_update_params": True}
    assert client.get("/api/tax/parameters").json()["auto_update"] is True
    # a client that does not know the `tax` section does not switch it off
    legacy = {k: v for k, v in client.get("/api/settings").json().items() if k != "tax"}
    assert client.put("/api/settings", json=legacy).json()["tax"] == {"auto_update_params": True}


def test_the_yearly_check_is_triggered_lazily_when_enabled(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = client.get("/api/settings").json()
    settings["tax"] = {"auto_update_params": True}
    client.put("/api/settings", json=settings)
    started: list[object] = []

    def fake_start(s: object, **kw: object) -> bool:
        started.append(s)
        return True

    from spend_analyzer.api.routers import tax as tax_router

    monkeypatch.setattr(tax_router.tax_params_update, "maybe_check_in_background", fake_start)
    uid = _user(client)
    client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2024})
    client.get("/api/tax/summary")
    assert len(started) == 2


def test_profile_age_boxes_drive_the_senior_deduction(client: TestClient, session: Session) -> None:
    uid = _user(client)
    _seed_w2(session, uid, 60_000, 5_000, year=2025)
    r = client.put(
        f"/api/tax/profiles/{uid}/2025", json=_profile(age65_or_blind_count=1, age65_count=1)
    )
    assert r.status_code == 201 and r.json()["age65_count"] == 1
    calc = client.get("/api/tax/estimate", params={"user_id": uid, "tax_year": 2025}).json()["calc"]
    assert calc["senior_deduction_minor"] == 6_000 * D
    assert calc["deduction_minor"] == (15_750 + 2_000) * D
    assert calc["taxable_income_minor"] == (60_000 - 17_750 - 6_000) * D
    ok_joint = client.put(
        f"/api/tax/profiles/{uid}/2024",
        json=_profile(filing_status="mfj", age65_or_blind_count=2, age65_count=1),
    )
    assert ok_joint.status_code == 201
