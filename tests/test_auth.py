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


def test_rate_limit_uses_cloudflare_client_ip(client):
    # Through the tunnel, all requests arrive from the same cloudflared pod;
    # only CF-Connecting-IP tells visitors apart.
    register(client)
    same_proxy = {"X-Forwarded-For": "10.42.1.50"}
    for _ in range(10):
        login(client, password="wrong-password", headers={**same_proxy, "CF-Connecting-IP": "198.51.100.7"})
    assert login(client, headers={**same_proxy, "CF-Connecting-IP": "198.51.100.7"}).status_code == 429
    assert login(client, headers={**same_proxy, "CF-Connecting-IP": "198.51.100.8"}).status_code == 200


# --- Sessions and token rotation ------------------------------------------------

def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def refresh(client, token):
    return client.post("/auth/refresh", headers=bearer(token))


def test_refresh_rotates_the_token(client):
    old = register(client).json["access_token"]
    res = refresh(client, old)
    assert res.status_code == 200
    new = res.json["access_token"]
    assert new != old and res.json["user"]["email"] == "roye@example.com"
    assert client.get("/auth/me", headers=bearer(new)).status_code == 200


def test_replaced_token_works_during_the_grace(client):
    # A request sent just before the refresh still carries the old token.
    old = register(client).json["access_token"]
    new = refresh(client, old).json["access_token"]
    assert client.get("/auth/me", headers=bearer(old)).status_code == 200
    assert client.get("/auth/me", headers=bearer(new)).status_code == 200


def test_reused_token_after_the_grace_revokes_the_session(client, app):
    from tripsync.auth import ROTATION_GRACE
    from tripsync.extensions import db
    from tripsync.models import AuthSession

    stolen = register(client).json["access_token"]
    new = refresh(client, stolen).json["access_token"]
    with app.app_context():
        session = db.session.scalars(db.select(AuthSession)).one()
        session.rotated_at = session.rotated_at - ROTATION_GRACE
        db.session.commit()
    res = client.get("/trips", headers=bearer(stolen))
    assert res.status_code == 401
    assert res.json == {"error": "session ended, log in again"}
    # The whole session is gone, the owner's new token included.
    assert client.get("/trips", headers=bearer(new)).status_code == 401
    with app.app_context():
        assert db.session.scalars(db.select(AuthSession)).one().revoked_reason == "reused"


def test_two_rotations_back_is_reuse(client):
    t1 = register(client).json["access_token"]
    t2 = refresh(client, t1).json["access_token"]
    t3 = refresh(client, t2).json["access_token"]
    # t1 is neither current nor the one just replaced.
    assert client.get("/auth/me", headers=bearer(t1)).status_code == 401
    assert client.get("/auth/me", headers=bearer(t3)).status_code == 401


def test_each_login_is_its_own_session(client):
    phone = register(client).json["access_token"]
    laptop = login(client).json["access_token"]
    refresh(client, laptop)
    assert client.get("/auth/me", headers=bearer(phone)).status_code == 200


def test_logout_ends_only_this_device(client):
    phone = register(client).json["access_token"]
    laptop = login(client).json["access_token"]
    assert client.post("/auth/logout", headers=bearer(phone)).status_code == 204
    assert client.get("/auth/me", headers=bearer(phone)).status_code == 401
    assert client.get("/auth/me", headers=bearer(laptop)).status_code == 200


def test_logout_all_ends_every_device(client):
    phone = register(client).json["access_token"]
    laptop = login(client).json["access_token"]
    assert client.post("/auth/logout-all", headers=bearer(laptop)).status_code == 204
    assert client.get("/auth/me", headers=bearer(phone)).status_code == 401
    assert client.get("/auth/me", headers=bearer(laptop)).status_code == 401
    # Logging in again still works.
    assert client.get("/auth/me", headers=bearer(login(client).json["access_token"])).status_code == 200


def test_session_older_than_the_cap_cannot_refresh(client, app):
    from tripsync.auth import MAX_SESSION_AGE
    from tripsync.extensions import db
    from tripsync.models import AuthSession

    token = register(client).json["access_token"]
    with app.app_context():
        session = db.session.scalars(db.select(AuthSession)).one()
        session.created_at = session.created_at - MAX_SESSION_AGE
        db.session.commit()
    assert refresh(client, token).status_code == 401
    assert client.get("/auth/me", headers=bearer(token)).status_code == 401


def test_token_from_before_sessions_still_works_and_refreshes(client, app):
    from flask_jwt_extended import create_access_token

    register(client)
    user_id = client.get("/auth/me", headers=bearer(login(client).json["access_token"])).json["id"]
    with app.app_context():
        legacy = create_access_token(identity=user_id)  # no "sid" claim
    assert client.get("/auth/me", headers=bearer(legacy)).status_code == 200
    res = refresh(client, legacy)
    assert res.status_code == 200
    assert client.get("/auth/me", headers=bearer(res.json["access_token"])).status_code == 200
