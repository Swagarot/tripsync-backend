"""Ideas, expenses, checklist (extras.py), trip previews, rates and weather."""
import pytest

from tripsync import places, travelinfo


@pytest.fixture
def trip_id(client, alice):
    return client.post("/trips", json={"name": "Japan", "home_currency": "ils"}, headers=alice).json["id"]


def get_trip(client, alice, trip_id):
    return client.get(f"/trips/{trip_id}", headers=alice).json


def test_home_currency_is_upper_cased(client, alice, trip_id):
    assert get_trip(client, alice, trip_id)["home_currency"] == "ILS"


def test_expenses_crud_and_exact_cents(client, alice, trip_id):
    res = client.post(f"/trips/{trip_id}/expenses", json={
        "amount": 1250.456, "currency": "jpy", "category": "food", "note": "Ramen", "date": "2027-04-01",
    }, headers=alice)
    assert res.status_code == 201
    assert res.json["amount"] == 1250.46 and res.json["currency"] == "JPY"
    eid = res.json["id"]
    assert client.patch(f"/expenses/{eid}", json={"category": "other"}, headers=alice).json["category"] == "other"
    assert client.delete(f"/expenses/{eid}", headers=alice).status_code == 204
    assert get_trip(client, alice, trip_id)["expenses"] == []


@pytest.mark.parametrize("body, message", [
    ({"currency": "EUR", "category": "food"}, "amount is required"),
    ({"amount": -5, "currency": "EUR", "category": "food"}, "between"),
    ({"amount": 5, "currency": "EURO", "category": "food"}, "at most 3"),
    ({"amount": 5, "currency": "E1R", "category": "food"}, "currency code"),
    ({"amount": 5, "currency": "EUR", "category": "jewels"}, "must be one of"),
])
def test_expense_validation(client, alice, trip_id, body, message):
    res = client.post(f"/trips/{trip_id}/expenses", json=body, headers=alice)
    assert res.status_code == 400
    assert message in res.json["error"]


def test_checklist_appends_and_toggles(client, alice, trip_id):
    a = client.post(f"/trips/{trip_id}/checklist", json={"text": "Passport"}, headers=alice).json
    b = client.post(f"/trips/{trip_id}/checklist", json={"text": "Adapter"}, headers=alice).json
    assert (a["position"], b["position"], a["done"]) == (0, 1, False)
    assert client.patch(f"/checklist/{a['id']}", json={"done": True}, headers=alice).json["done"] is True
    assert client.patch(f"/checklist/{a['id']}", json={"done": "yes"}, headers=alice).status_code == 400
    assert client.patch(f"/checklist/{a['id']}", json={"text": None}, headers=alice).status_code == 400
    assert [c["text"] for c in get_trip(client, alice, trip_id)["checklist"]] == ["Passport", "Adapter"]


def test_ideas_keep_place_details_and_refuse_bad_urls(client, alice, trip_id):
    idea = {"name": "teamLab", "lat": 35.6, "lng": 139.8, "photo_url": "https://upload.wikimedia.org/x.jpg"}
    assert client.post(f"/trips/{trip_id}/ideas", json=idea, headers=alice).status_code == 201
    bad = {**idea, "website": "javascript:alert(1)"}
    assert client.post(f"/trips/{trip_id}/ideas", json=bad, headers=alice).status_code == 400


def test_unknown_kind_is_404(client, alice, trip_id):
    assert client.post(f"/trips/{trip_id}/secrets", json={}, headers=alice).status_code == 404


def test_trip_list_preview(client, alice, trip_id):
    day = client.post(f"/trips/{trip_id}/days", json={}, headers=alice).json
    client.post(f"/days/{day['id']}/stops", json={"name": "No pin"}, headers=alice)
    client.post(f"/days/{day['id']}/stops", json={
        "name": "Senso-ji", "lat": 35.71480, "lng": 139.79671, "photo_url": "https://upload.wikimedia.org/s.jpg",
    }, headers=alice)
    preview = client.get("/trips", headers=alice).json[0]["preview"]
    assert preview == {"days": 1, "places": 2, "points": [[35.7148, 139.7967]],
                       "photo_url": "https://upload.wikimedia.org/s.jpg"}


# --- Rates and weather, outside services faked --------------------------------------

@pytest.fixture
def fake_outside(monkeypatch):
    calls = []

    def fake_fetch(service, url, params=None):
        calls.append(service)
        if service == "frankfurter":
            return {"date": "2026-10-16", "rates": {"JPY": 51.065, "EUR": 0.287}}
        return {"daily": {"time": ["2026-10-16", "2026-10-17"], "weather_code": [3, 95],
                          "precipitation_probability_max": [10, 90]}}

    monkeypatch.setattr(travelinfo, "fetch_json", fake_fetch)
    travelinfo._rates_cache.clear()
    travelinfo._weather_cache.clear()
    return calls


def test_rates_include_base_and_are_cached(client, alice, fake_outside):
    res = client.get("/rates?base=ils", headers=alice).json
    assert res["rates"]["ILS"] == 1.0 and res["rates"]["JPY"] == 51.065
    client.get("/rates?base=ILS", headers=alice)
    assert fake_outside == ["frankfurter"]
    assert client.get("/rates?base=shekel", headers=alice).status_code == 400


def test_weather_rows_per_day(client, alice, fake_outside):
    res = client.get("/weather?lat=35.68&lng=139.76", headers=alice).json
    assert res["days"][1] == {"date": "2026-10-17", "weather_code": 95, "precipitation_probability_max": 90}
    client.get("/weather?lat=35.71&lng=139.79", headers=alice)  # same ~10 km cell: cached
    assert fake_outside == ["open-meteo"]
    assert client.get("/weather?lat=35.68", headers=alice).status_code == 400


def test_rates_down_is_502(client, alice, monkeypatch):
    def down(*args, **kwargs):
        raise places.UpstreamError("down")
    monkeypatch.setattr(travelinfo, "fetch_json", down)
    travelinfo._rates_cache.clear()
    assert client.get("/rates?base=EUR", headers=alice).status_code == 502


def test_lodging_dates_and_bookends(client, alice, trip_id):
    body = {"name": "Hotel Gracery", "lat": 35.69, "lng": 139.70, "check_in": "2027-04-01", "check_out": "2027-04-05"}
    res = client.post(f"/trips/{trip_id}/lodgings", json=body, headers=alice)
    assert res.status_code == 201
    assert res.json["bookends"] is True  # on by default
    lid = res.json["id"]
    assert client.patch(f"/lodgings/{lid}", json={"bookends": False}, headers=alice).json["bookends"] is False
    # check_out before check_in is refused, on create and on update (against the stored check_in).
    assert client.post(f"/trips/{trip_id}/lodgings", json={**body, "check_out": "2027-03-30"},
                       headers=alice).status_code == 400
    assert client.patch(f"/lodgings/{lid}", json={"check_out": "2027-03-01"}, headers=alice).status_code == 400
    assert [s["name"] for s in get_trip(client, alice, trip_id)["lodgings"]] == ["Hotel Gracery"]
