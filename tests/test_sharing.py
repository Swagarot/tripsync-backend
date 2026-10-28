"""Shared trips: invite links, what each role may do, and that people who
aren't on a trip still can't see or touch it (test_isolation.py, extended to
the sharing endpoints and to members who were removed)."""
import time

import pytest

from tests.test_isolation import ENDPOINTS, fill


@pytest.fixture
def carol(make_user):
    return make_user("carol@example.com")


def invite(client, owner, trip_id, role):
    res = client.post(f"/trips/{trip_id}/invites", json={"role": role}, headers=owner)
    assert res.status_code == 201, res.json
    return res.json["token"]


def join(client, who, token):
    return client.post(f"/invites/{token}/accept", headers=who)


@pytest.fixture
def shared(client, alice, bob, alices_data):
    """Alice's trip with Bob on it; returns a function to set Bob's role."""
    def as_role(role):
        assert join(client, bob, invite(client, alice, alices_data["trip"], role)).status_code == 200
        if role == "viewer":  # a link never moves anyone down: set it directly
            client.patch(f"/trips/{alices_data['trip']}/members/{user_id(client, bob)}",
                         json={"role": "viewer"}, headers=alice)
        return alices_data
    return as_role


def user_id(client, who):
    return client.get("/auth/me", headers=who).json["id"]


# --- Joining -------------------------------------------------------------------

def test_invite_link_lets_someone_join(client, alice, bob, alices_data):
    token = invite(client, alice, alices_data["trip"], "editor")
    preview = client.get(f"/invites/{token}", headers=bob).json
    assert preview["trip_name"] == "Alice's trip"
    assert preview["owner_email"] == "alice@example.com"
    assert preview["role"] == "editor" and preview["your_role"] is None

    assert join(client, bob, token).json == {"trip_id": alices_data["trip"], "role": "editor"}
    trips = client.get("/trips", headers=bob).json
    assert [(t["name"], t["role"], t["owner_email"]) for t in trips] == [
        ("Alice's trip", "editor", "alice@example.com")]
    # Both see the same member list, owner first.
    trip = client.get(f"/trips/{alices_data['trip']}", headers=bob).json
    assert trip["role"] == "editor"
    assert [(m["email"], m["role"]) for m in trip["members"]] == [
        ("alice@example.com", "owner"), ("bob@example.com", "editor")]


def test_joining_twice_is_harmless_and_never_downgrades(client, alice, bob, alices_data):
    editor_link = invite(client, alice, alices_data["trip"], "editor")
    viewer_link = invite(client, alice, alices_data["trip"], "viewer")
    join(client, bob, viewer_link)
    assert join(client, bob, editor_link).json["role"] == "editor"  # upgraded
    assert join(client, bob, viewer_link).json["role"] == "editor"  # not downgraded
    assert join(client, alice, editor_link).json["role"] == "owner"  # owner stays owner
    assert len(client.get(f"/trips/{alices_data['trip']}", headers=alice).json["members"]) == 2


def test_broken_tampered_or_cancelled_links_dont_work(client, alice, bob, alices_data):
    token = invite(client, alice, alices_data["trip"], "editor")
    assert client.get("/invites/not-a-token", headers=bob).status_code == 404
    assert join(client, bob, token[:-2] + ("AA" if not token.endswith("AA") else "BB")).status_code == 404
    # "Cancel all links": the old one stops working, a new one works.
    assert client.post(f"/trips/{alices_data['trip']}/invites/reset", headers=alice).status_code == 204
    assert join(client, bob, token).status_code == 404
    assert join(client, bob, invite(client, alice, alices_data["trip"], "editor")).status_code == 200


def test_links_expire_after_7_days(client, alice, bob, alices_data, monkeypatch):
    token = invite(client, alice, alices_data["trip"], "editor")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 8 * 24 * 3600)
    assert join(client, bob, token).status_code == 404


def test_link_to_a_deleted_trip_doesnt_work(client, alice, bob, alices_data):
    token = invite(client, alice, alices_data["trip"], "editor")
    client.delete(f"/trips/{alices_data['trip']}", headers=alice)
    assert join(client, bob, token).status_code == 404


def test_invite_needs_login_and_a_valid_role(client, alice, alices_data):
    assert client.post(f"/trips/{alices_data['trip']}/invites", json={"role": "editor"}).status_code == 401
    assert client.post(f"/trips/{alices_data['trip']}/invites", json={"role": "owner"},
                       headers=alice).status_code == 400


# --- What each role may do -------------------------------------------------------

def test_editor_changes_everything_but_owner_things(client, alice, bob, shared):
    ids = shared("editor")
    assert client.patch(f"/days/{ids['day']}", json={"title": "Bob's edit"}, headers=bob).status_code == 200
    assert client.post(f"/days/{ids['day']}/stops", json={"name": "Bob's stop"}, headers=bob).status_code == 201
    assert client.patch(f"/trips/{ids['trip']}", json={"notes": "bring cash"}, headers=bob).status_code == 200
    # Owner-only: rename, delete, invite, cancel links, manage people.
    assert client.patch(f"/trips/{ids['trip']}", json={"name": "Bob's now"}, headers=bob).status_code == 403
    assert client.delete(f"/trips/{ids['trip']}", headers=bob).status_code == 403
    assert client.post(f"/trips/{ids['trip']}/invites", json={"role": "viewer"}, headers=bob).status_code == 403
    assert client.post(f"/trips/{ids['trip']}/invites/reset", headers=bob).status_code == 403
    alice_id = user_id(client, alice)
    assert client.delete(f"/trips/{ids['trip']}/members/{alice_id}", headers=bob).status_code == 403
    trip = client.get(f"/trips/{ids['trip']}", headers=alice).json
    assert trip["name"] == "Alice's trip" and trip["days"][0]["title"] == "Bob's edit"


@pytest.mark.parametrize("method, url, body", [e for e in ENDPOINTS if e[0] != "get"],
                         ids=[f"{m} {u}" for m, u, _ in ENDPOINTS if m != "get"])
def test_viewer_can_look_but_not_change_anything(client, alice, bob, shared, method, url, body):
    ids = shared("viewer")
    assert client.get(f"/trips/{ids['trip']}", headers=bob).json["role"] == "viewer"
    kwargs = {"headers": bob}
    if body is not None:
        kwargs["json"] = fill(body, ids)
    res = getattr(client, method)(fill(url, ids), **kwargs)
    assert res.status_code == 403, res.json
    assert "view" in res.json["error"] or "owner" in res.json["error"]


def test_viewer_cannot_add_photos(client, bob, shared):
    ids = shared("viewer")
    res = client.post(f"/stops/{ids['stop']}/photos", data={}, headers=bob)
    assert res.status_code == 403


# --- People who aren't (or are no longer) on the trip ------------------------------

SHARING_ENDPOINTS = [
    ("post", "/trips/{trip}/invites", {"role": "editor"}),
    ("post", "/trips/{trip}/invites/reset", None),
    ("patch", "/trips/{trip}/members/{bob}", {"role": "viewer"}),
    ("delete", "/trips/{trip}/members/{bob}", None),
]


@pytest.mark.parametrize("method, url, body", SHARING_ENDPOINTS + ENDPOINTS,
                         ids=[f"{m} {u}" for m, u, _ in SHARING_ENDPOINTS + ENDPOINTS])
def test_outsiders_still_get_404(client, alice, bob, carol, shared, method, url, body):
    ids = {**shared("editor"), "bob": user_id(client, bob)}
    kwargs = {"headers": carol}
    if body is not None:
        kwargs["json"] = fill(body, ids)
    assert getattr(client, method)(fill(url, ids), **kwargs).status_code == 404


def test_removed_member_loses_access_at_once(client, alice, bob, shared):
    ids = shared("editor")
    bob_id = user_id(client, bob)
    assert client.delete(f"/trips/{ids['trip']}/members/{bob_id}", headers=alice).status_code == 204
    assert client.get(f"/trips/{ids['trip']}", headers=bob).status_code == 404
    assert client.patch(f"/days/{ids['day']}", json={"title": "x"}, headers=bob).status_code == 404
    assert client.get("/trips", headers=bob).json == []


def test_member_can_leave_but_owner_cannot(client, alice, bob, shared):
    ids = shared("viewer")
    assert client.delete(f"/trips/{ids['trip']}/members/{user_id(client, bob)}", headers=bob).status_code == 204
    assert client.get(f"/trips/{ids['trip']}", headers=bob).status_code == 404
    # The owner isn't a member; they delete the trip instead.
    assert client.delete(f"/trips/{ids['trip']}/members/{user_id(client, alice)}",
                         headers=alice).status_code == 404


def test_owner_changes_roles(client, alice, bob, shared):
    ids = shared("editor")
    res = client.patch(f"/trips/{ids['trip']}/members/{user_id(client, bob)}", json={"role": "viewer"}, headers=alice)
    assert [m["role"] for m in res.json] == ["owner", "viewer"]
    assert client.patch(f"/days/{ids['day']}", json={"title": "x"}, headers=bob).status_code == 403


# --- Expense splitting (balances are computed in the frontend) ---------------------

def test_expense_split_must_name_people_on_the_trip(client, alice, bob, carol, shared):
    ids = shared("editor")
    alice_id, bob_id, carol_id = (user_id(client, u) for u in (alice, bob, carol))
    url = f"/trips/{ids['trip']}/expenses"
    base = {"amount": 30, "currency": "EUR", "category": "food"}

    res = client.post(url, json={**base, "paid_by": bob_id, "split_between": [alice_id, bob_id, bob_id]}, headers=bob)
    assert res.status_code == 201
    assert res.json["paid_by"] == bob_id
    assert res.json["split_between"] == sorted([alice_id, bob_id])  # duplicates dropped

    for bad in ({"paid_by": carol_id}, {"split_between": [alice_id, carol_id]}, {"split_between": []},
                {"paid_by": "nope"}):
        assert client.post(url, json={**base, **bad}, headers=bob).status_code == 400, bad

    # null = everyone on the trip; leaving the keys out of an update keeps them.
    everyone = client.post(url, json={**base, "paid_by": alice_id, "split_between": None}, headers=alice).json
    assert everyone["split_between"] is None
    updated = client.patch(f"/expenses/{res.json['id']}", json={"amount": 45}, headers=alice).json
    assert updated["paid_by"] == bob_id and updated["amount"] == 45


def test_expenses_without_split_still_work(client, alice, alices_data):
    # Single-user trips and older expenses: no paid_by, no split.
    trip = client.get(f"/trips/{alices_data['trip']}", headers=alice).json
    assert trip["expenses"][0]["paid_by"] is None and trip["expenses"][0]["split_between"] is None
