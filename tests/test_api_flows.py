from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import AuthHeaders


def test_create_and_get_flow(
    client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any]
) -> None:
    created = client.post("/flows", json=example_flow, headers=auth())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "archive-dataset"
    assert body["definition"]["steps"][0]["id"] == "validate"

    fetched = client.get(f"/flows/{body['id']}", headers=auth())
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_invalid_flow_is_422(client: TestClient, auth: AuthHeaders) -> None:
    resp = client.post(
        "/flows",
        json={"name": "x", "definition": {"steps": [{"id": "a", "action": "rm_rf", "params": {}}]}},
        headers=auth(),
    )
    assert resp.status_code == 422


def test_users_only_see_their_own_flows(
    client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any]
) -> None:
    flow_id = client.post("/flows", json=example_flow, headers=auth(subject="alice")).json()["id"]

    assert client.get(f"/flows/{flow_id}", headers=auth(subject="bob")).status_code == 404
    assert client.get("/flows", headers=auth(subject="bob")).json()["items"] == []
    assert len(client.get("/flows", headers=auth(subject="alice")).json()["items"]) == 1


def test_list_flows_is_paginated_newest_first(
    client: TestClient, auth: AuthHeaders, example_flow: dict[str, Any]
) -> None:
    names = [f"flow-{i}" for i in range(5)]
    for name in names:
        client.post("/flows", json={**example_flow, "name": name}, headers=auth())

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/flows", params=params, headers=auth()).json()
        seen += [f["name"] for f in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert seen == list(reversed(names))


def test_invalid_cursor_is_400(client: TestClient, auth: AuthHeaders) -> None:
    resp = client.get("/flows", params={"cursor": "%%%"}, headers=auth())
    assert resp.status_code == 400


def test_unknown_flow_is_404(client: TestClient, auth: AuthHeaders) -> None:
    resp = client.get("/flows/00000000-0000-0000-0000-000000000000", headers=auth())
    assert resp.status_code == 404
