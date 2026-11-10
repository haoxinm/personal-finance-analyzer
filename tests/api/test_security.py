"""A31/I8: Host allowlist and per-launch token enforcement."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_missing_token_is_403(client: TestClient) -> None:
    response = client.get("/api/users", headers={"X-PFA-Token": ""})
    assert response.status_code == 403


def test_wrong_host_is_403(client: TestClient) -> None:
    response = client.get("/api/users", headers={"Host": "evil.example"})
    assert response.status_code == 403


def test_multipart_upload_without_token_is_403(client: TestClient) -> None:
    response = client.post(
        "/api/statements",
        headers={"X-PFA-Token": ""},
        data={"user_id": "1"},
        files={"file": ("s.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert response.status_code == 403


def test_valid_host_and_token_passes(client: TestClient) -> None:
    response = client.get("/api/users")
    assert response.status_code == 200


def test_localhost_hostname_also_allowed(client: TestClient) -> None:
    response = client.get("/api/users", headers={"Host": "localhost"})
    assert response.status_code == 200


_TAX_GET_ROUTES = (
    "/api/tax/documents",
    "/api/tax/documents/1",
    "/api/tax/years",
    "/api/tax/summary",
    "/api/tax/income-by-payer",
)


@pytest.mark.parametrize("route", _TAX_GET_ROUTES)
def test_tax_get_routes_require_token_and_host(client: TestClient, route: str) -> None:
    assert client.get(route, headers={"X-PFA-Token": ""}).status_code == 403
    assert client.get(route, headers={"X-PFA-Token": "wrong"}).status_code == 403
    assert client.get(route, headers={"Host": "evil.example"}).status_code == 403


def test_tax_delete_requires_token(client: TestClient) -> None:
    assert client.delete("/api/tax/documents/1", headers={"X-PFA-Token": ""}).status_code == 403


def test_tax_upload_requires_token_and_host(client: TestClient) -> None:
    kwargs: dict[str, Any] = {
        "data": {"user_id": "1"},
        "files": {"file": ("w2.pdf", b"%PDF-1.4 fake", "application/pdf")},
    }
    assert (
        client.post("/api/tax/documents", headers={"X-PFA-Token": ""}, **kwargs).status_code == 403
    )
    assert (
        client.post("/api/tax/documents", headers={"Host": "evil.example"}, **kwargs).status_code
        == 403
    )
