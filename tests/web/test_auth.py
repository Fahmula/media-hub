from __future__ import annotations

from fastapi.testclient import TestClient

from mediahub.web.routes.auth import login_limiter, safe_next

from .conftest import PASSWORD, csrf_from, setup_admin


def test_redirects_to_setup_when_no_users(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_setup_creates_admin_and_logs_in(client: TestClient) -> None:
    setup_admin(client)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    assert "Dashboard" in resp.text
    # Setup is closed afterwards.
    again = client.get("/setup", follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/login"


def test_setup_validation(client: TestClient) -> None:
    page = client.get("/setup")
    resp = client.post(
        "/setup",
        data={
            "username": "a",
            "password": "short",
            "password_confirm": "different",
            "csrf_token": csrf_from(page.text),
        },
    )
    assert resp.status_code == 422
    assert "at least" in resp.text


def test_setup_requires_csrf(client: TestClient) -> None:
    client.get("/setup")
    resp = client.post(
        "/setup",
        data={"username": "admin", "password": PASSWORD, "password_confirm": PASSWORD},
    )
    assert resp.status_code == 403


def test_login_logout_flow(client: TestClient) -> None:
    setup_admin(client)
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"

    page = client.get("/login")
    bad = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": csrf_from(page.text)},
    )
    assert bad.status_code == 401

    ok = client.post(
        "/login",
        data={
            "username": "admin",
            "password": PASSWORD,
            "csrf_token": csrf_from(page.text),
            "next": "/activity",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == "/activity"

    dash = client.get("/")
    token = csrf_from(dash.text)
    # Logout without the session CSRF token is refused.
    assert client.post("/logout").status_code == 403
    out = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    assert out.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_rate_limit(client: TestClient) -> None:
    setup_admin(client)
    client.cookies.clear()
    login_limiter.reset("testclient")
    page = client.get("/login")
    token = csrf_from(page.text)
    statuses = [
        client.post(
            "/login", data={"username": "admin", "password": "nope", "csrf_token": token}
        ).status_code
        for _ in range(6)
    ]
    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429
    login_limiter.reset("testclient")


def test_htmx_unauthenticated_gets_hx_redirect(client: TestClient) -> None:
    setup_admin(client)
    client.cookies.clear()
    resp = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert resp.status_code == 204
    assert resp.headers["hx-redirect"] == "/login"


def test_security_headers(client: TestClient) -> None:
    resp = client.get("/login", follow_redirects=True)
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"


def test_safe_next() -> None:
    assert safe_next("/emby") == "/emby"
    assert safe_next("//evil.com") == "/"
    assert safe_next("https://evil.com") == "/"
    assert safe_next("/\\evil.com") == "/"
    assert safe_next(None) == "/"


def test_session_cookie_flags(client: TestClient) -> None:
    page = client.get("/setup")
    resp = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "csrf_token": csrf_from(page.text),
        },
        follow_redirects=False,
    )
    cookie = resp.headers["set-cookie"]
    assert "mh_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("samesite", "SameSite")
