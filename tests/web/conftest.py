from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct horse battery"


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(
        r'"X-CSRF-Token": "([^"]+)"', html
    )
    assert match, "no csrf token in page"
    return match.group(1)


def setup_admin(client: TestClient, username: str = "admin") -> None:
    page = client.get("/setup")
    resp = client.post(
        "/setup",
        data={
            "username": username,
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture
def authed(client: TestClient) -> TestClient:
    setup_admin(client)
    return client
