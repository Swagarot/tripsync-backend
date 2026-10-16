import jwt as pyjwt
import pytest


def register(client, email="roye@example.com", password="correct-horse", **kwargs):
    return client.post("/auth/register", json={"email": email, "password": password}, **kwargs)


def login(client, email="roye@example.com", password="correct-horse", **kwargs):
    return client.post("/auth/login", json={"email": email, "password": password}, **kwargs)


def test_register_returns_token_and_lowercases_email(client):
    res = register(client, email="  Roye@Example.COM ")
    assert res.status_code == 201
    assert res.json["user"]["email"] == "roye@example.com"
    assert res.json["access_token"]
    assert "password" not in str(res.json)


def test_register_duplicate_email_ignores_case(client):
    register(client)
    res = register(client, email="ROYE@example.com")
    assert res.status_code == 409


@pytest.mark.parametrize("body, message", [
    ({"email": "roye@example.com", "password": "short"}, "at least 8"),
    ({"email": "not-an-email", "password": "correct-horse"}, "valid address"),
    ({"email": "roye@example.com"}, "password is required"),
    ({"password": "correct-horse"}, "email is required"),
    ({"email": 42, "password": "correct-horse"}, "must be a string"),
])
def test_register_validation(client, body, message):
    res = client.post("/auth/register", json=body)
    assert res.status_code == 400
    assert message in res.json["error"]


def test_register_rejects_non_json(client):
    res = client.post("/auth/register", data="email=x", content_type="application/x-www-form-urlencoded")
    assert res.status_code == 400


def test_login_success(client):
    register(client)
    res = login(client, email="Roye@example.com")
    assert res.status_code == 200
    assert res.json["user"]["email"] == "roye@example.com"


def test_login_failures_look_the_same(client):
    register(client)
    wrong_password = login(client, password="wrong-password")
    unknown_email = login(client, email="nobody@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json == unknown_email.json


def test_me_requires_a_valid_token(client):
    token = register(client).json["access_token"]
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    res = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json["email"] == "roye@example.com"


def test_token_signed_with_another_key_is_rejected(client):
    claims = {"sub": "00000000-0000-0000-0000-000000000000", "type": "access"}
    forged = pyjwt.encode(claims, "wrong-key-wrong-key-wrong-key-123")
    res = client.get("/trips", headers={"Authorization": f"Bearer {forged}"})
    assert res.status_code == 401


def test_login_is_rate_limited_per_client_ip(client):
    register(client)
    ip_a = {"X-Forwarded-For": "203.0.113.1"}
    ip_b = {"X-Forwarded-For": "203.0.113.2"}
    for _ in range(10):
        assert login(client, password="wrong-password", headers=ip_a).status_code == 401
    # The 11th try in the same minute from the same IP is refused...
    assert login(client, headers=ip_a).status_code == 429
    # ...but another client IP (as seen through the proxy) is unaffected.
    # This proves ProxyFix works: without it every request would come from the
    # proxy's address and one attacker could lock everyone out.
    assert login(client, headers=ip_b).status_code == 200
