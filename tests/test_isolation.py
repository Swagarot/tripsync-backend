"""Bob must not be able to read, change or even detect Alice's data.

This is the most important test file for a multi-account API. The classic bug
("IDOR", insecure direct object reference) is an endpoint that loads a row by
its ID and forgets to check who owns it. Every endpoint that takes an ID is
listed here, and each must answer Bob with 404, the same as for an ID that
doesn't exist.
"""
import pytest


ENDPOINTS = [
    ("get", "/trips/{trip}", None),
    ("patch", "/trips/{trip}", {"name": "hacked"}),
    ("delete", "/trips/{trip}", None),
    ("post", "/trips/{trip}/days", {"title": "hacked"}),
    ("put", "/trips/{trip}/days/order", {"ids": ["{day}"]}),
    ("patch", "/days/{day}", {"title": "hacked"}),
    ("delete", "/days/{day}", None),
    ("post", "/days/{day}/stops", {"name": "hacked"}),
    ("put", "/days/{day}/stops/order", {"ids": ["{stop}"]}),
    ("patch", "/stops/{stop}", {"name": "hacked"}),
    ("delete", "/stops/{stop}", None),
    ("post", "/trips/{trip}/ideas", {"name": "hacked"}),
    ("patch", "/ideas/{idea}", {"name": "hacked"}),
    ("delete", "/ideas/{idea}", None),
    ("post", "/trips/{trip}/expenses", {"amount": 1, "currency": "EUR", "category": "food"}),
    ("patch", "/expenses/{expense}", {"amount": 999}),
    ("delete", "/expenses/{expense}", None),
    ("post", "/trips/{trip}/checklist", {"text": "hacked"}),
    ("patch", "/checklist/{item}", {"done": True}),
    ("delete", "/checklist/{item}", None),
    ("post", "/trips/{trip}/lodgings", {"name": "hacked"}),
    ("patch", "/lodgings/{stay}", {"name": "hacked"}),
    ("delete", "/lodgings/{stay}", None),
]


def fill(value, ids):
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, list):
        return [fill(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ids) for k, v in value.items()}
    return value


@pytest.mark.parametrize("method, url, body", ENDPOINTS, ids=[f"{m} {u}" for m, u, _ in ENDPOINTS])
def test_other_users_rows_are_404(client, alice, bob, alices_data, method, url, body):
    kwargs = {"headers": bob}
    if body is not None:
        kwargs["json"] = fill(body, alices_data)
    res = getattr(client, method)(fill(url, alices_data), **kwargs)
    assert res.status_code == 404

    # And Alice's data is untouched.
    trip = client.get(f"/trips/{alices_data['trip']}", headers=alice).json
    assert trip["name"] == "Alice's trip"
    assert [d["title"] for d in trip["days"]] == ["Alice's day"]
    assert [s["name"] for s in trip["days"][0]["stops"]] == ["Alice's stop"]
    assert [i["name"] for i in trip["ideas"]] == ["Alice's idea"]
    assert [e["amount"] for e in trip["expenses"]] == [12.5]
    assert [(c["text"], c["done"]) for c in trip["checklist"]] == [("Alice's passport", False)]
    assert [s["name"] for s in trip["lodgings"]] == ["Alice's hotel"]


def test_trip_list_only_shows_own_trips(client, alice, bob, alices_data):
    client.post("/trips", json={"name": "Bob's trip"}, headers=bob)
    assert [t["name"] for t in client.get("/trips", headers=bob).json] == ["Bob's trip"]
    assert [t["name"] for t in client.get("/trips", headers=alice).json] == ["Alice's trip"]


def test_cannot_move_own_stop_into_other_users_day(client, bob, alices_data):
    trip = client.post("/trips", json={"name": "Bob's trip"}, headers=bob).json
    day = client.post(f"/trips/{trip['id']}/days", json={}, headers=bob).json
    stop = client.post(f"/days/{day['id']}/stops", json={"name": "Bob's stop"}, headers=bob).json
    res = client.patch(f"/stops/{stop['id']}", json={"day_id": alices_data["day"]}, headers=bob)
    assert res.status_code == 400


@pytest.mark.parametrize("kind", ["trip", "day", "stop", "idea", "expense", "item", "stay"])
def test_cannot_take_over_an_id_by_creating_with_it(client, alice, bob, alices_data, kind):
    # Bob guesses (or sees) one of Alice's IDs and tries to create a row with
    # it. The "retry is harmless" path must not hand him Alice's row.
    trip = client.post("/trips", json={"name": "Bob's trip"}, headers=bob).json
    day = client.post(f"/trips/{trip['id']}/days", json={}, headers=bob).json
    url = {"trip": "/trips", "day": f"/trips/{trip['id']}/days", "stop": f"/days/{day['id']}/stops",
           "idea": f"/trips/{trip['id']}/ideas", "expense": f"/trips/{trip['id']}/expenses",
           "item": f"/trips/{trip['id']}/checklist", "stay": f"/trips/{trip['id']}/lodgings"}[kind]
    body = {"name": "hacked", "text": "hacked", "amount": 1, "currency": "EUR", "category": "food"}
    res = client.post(url, json={"id": alices_data[kind], **body}, headers=bob)
    assert res.status_code == 409
    assert "Alice" not in str(res.json)
