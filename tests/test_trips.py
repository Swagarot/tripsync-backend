import uuid

import pytest


@pytest.fixture
def trip(client, alice):
    res = client.post("/trips", json={"name": "Japan", "start_date": "2027-04-01", "end_date": "2027-04-10"},
                      headers=alice)
    assert res.status_code == 201
    return res.json


def add_day(client, headers, trip_id, **body):
    res = client.post(f"/trips/{trip_id}/days", json=body, headers=headers)
    assert res.status_code == 201, res.json
    return res.json


def add_stop(client, headers, day_id, name="Stop", **body):
    res = client.post(f"/days/{day_id}/stops", json={"name": name, **body}, headers=headers)
    assert res.status_code == 201, res.json
    return res.json


# --- Trips -------------------------------------------------------------------

def test_trips_require_login(client):
    assert client.get("/trips").status_code == 401
    assert client.post("/trips", json={"name": "x"}).status_code == 401


def test_create_list_get_trip(client, alice, trip):
    assert trip["name"] == "Japan"
    assert trip["start_date"] == "2027-04-01"
    assert trip["days"] == []
    listed = client.get("/trips", headers=alice).json
    assert [t["id"] for t in listed] == [trip["id"]]
    assert client.get(f"/trips/{trip['id']}", headers=alice).json["name"] == "Japan"


def test_update_trip_only_changes_sent_fields(client, alice, trip):
    res = client.patch(f"/trips/{trip['id']}", json={"notes": "  bring adapter  "}, headers=alice)
    assert res.status_code == 200
    assert res.json["notes"] == "bring adapter"
    assert res.json["name"] == "Japan"
    assert res.json["updated_at"] >= trip["updated_at"]


def test_destination_is_stored(client, alice):
    body = {"name": "Trip to Japan", "destination": "Japan", "dest_lat": 36.5, "dest_lng": 139.0, "dest_zoom": 5}
    res = client.post("/trips", json=body, headers=alice)
    assert res.status_code == 201
    assert res.json["destination"] == "Japan" and res.json["dest_zoom"] == 5
    assert client.post("/trips", json={**body, "dest_zoom": 40}, headers=alice).status_code == 400


def test_empty_optional_text_becomes_null(client, alice, trip):
    res = client.patch(f"/trips/{trip['id']}", json={"notes": "   "}, headers=alice)
    assert res.json["notes"] is None


@pytest.mark.parametrize("body, message", [
    ({}, "name is required"),
    ({"name": "   "}, "must not be empty"),
    ({"name": None}, "name is required"),
    ({"name": "x" * 201}, "at most 200"),
    ({"name": "x", "start_date": "16/10/2026"}, "must be a date"),
    ({"name": "x", "start_date": "2027-04-10", "end_date": "2027-04-01"}, "end_date must not be before"),
    ({"name": "x", "id": "not-a-uuid"}, "id must be a UUID"),
])
def test_create_trip_validation(client, alice, body, message):
    res = client.post("/trips", json=body, headers=alice)
    assert res.status_code == 400
    assert message in res.json["error"]


def test_patch_checks_dates_against_stored_values(client, alice, trip):
    # Only end_date is sent; it must still be compared with the stored start_date.
    res = client.patch(f"/trips/{trip['id']}", json={"end_date": "2027-03-01"}, headers=alice)
    assert res.status_code == 400


def test_delete_trip_hides_it_and_its_children(client, alice, trip):
    day = add_day(client, alice, trip["id"])
    stop = add_stop(client, alice, day["id"])
    assert client.delete(f"/trips/{trip['id']}", headers=alice).status_code == 204
    assert client.get(f"/trips/{trip['id']}", headers=alice).status_code == 404
    assert client.get("/trips", headers=alice).json == []
    assert client.patch(f"/days/{day['id']}", json={"title": "x"}, headers=alice).status_code == 404
    assert client.patch(f"/stops/{stop['id']}", json={"name": "x"}, headers=alice).status_code == 404


def test_unknown_or_malformed_ids_are_404(client, alice):
    assert client.get(f"/trips/{uuid.uuid4()}", headers=alice).status_code == 404
    assert client.get("/trips/not-a-uuid", headers=alice).status_code == 404


# --- Client-chosen IDs (offline-first) ---------------------------------------

def test_client_chosen_id_is_used_and_retry_is_harmless(client, alice):
    trip_id = str(uuid.uuid4())
    first = client.post("/trips", json={"id": trip_id, "name": "Offline trip"}, headers=alice)
    assert first.status_code == 201
    assert first.json["id"] == trip_id
    # The phone never got the response and sends the same request again.
    retry = client.post("/trips", json={"id": trip_id, "name": "Offline trip"}, headers=alice)
    assert retry.status_code == 200
    assert len(client.get("/trips", headers=alice).json) == 1


# --- Days --------------------------------------------------------------------

def test_days_are_appended_in_order_and_nested_in_trip(client, alice, trip):
    d1 = add_day(client, alice, trip["id"], date="2027-04-01", title="Tokyo")
    d2 = add_day(client, alice, trip["id"], title="Kyoto")
    assert (d1["position"], d2["position"]) == (0, 1)
    days = client.get(f"/trips/{trip['id']}", headers=alice).json["days"]
    assert [d["title"] for d in days] == ["Tokyo", "Kyoto"]


def test_update_and_delete_day(client, alice, trip):
    day = add_day(client, alice, trip["id"])
    res = client.patch(f"/days/{day['id']}", json={"title": "Osaka", "date": "2027-04-03"}, headers=alice)
    assert res.json["title"] == "Osaka"
    assert res.json["date"] == "2027-04-03"
    assert client.delete(f"/days/{day['id']}", headers=alice).status_code == 204
    assert client.get(f"/trips/{trip['id']}", headers=alice).json["days"] == []


def test_reorder_days(client, alice, trip):
    ids = [add_day(client, alice, trip["id"], title=t)["id"] for t in ("A", "B", "C")]
    res = client.put(f"/trips/{trip['id']}/days/order", json={"ids": [ids[2], ids[0], ids[1]]}, headers=alice)
    assert res.status_code == 200
    assert [d["title"] for d in res.json["days"]] == ["C", "A", "B"]


@pytest.mark.parametrize("pick", [
    lambda ids: ids[:2],                      # one missing
    lambda ids: ids + [ids[0]],               # duplicate
    lambda ids: ids[:2] + [str(uuid.uuid4())],  # a stranger
])
def test_reorder_days_must_list_each_day_once(client, alice, trip, pick):
    ids = [add_day(client, alice, trip["id"])["id"] for _ in range(3)]
    res = client.put(f"/trips/{trip['id']}/days/order", json={"ids": pick(ids)}, headers=alice)
    assert res.status_code == 400


# --- Stops -------------------------------------------------------------------

def test_create_stop_with_all_fields(client, alice, trip):
    day = add_day(client, alice, trip["id"])
    stop = add_stop(client, alice, day["id"], name="Senso-ji", lat=35.7148, lng=139.7967,
                    address="2-3-1 Asakusa", time="09:30", notes="early")
    assert stop["lat"] == 35.7148
    assert stop["time"] == "09:30"
    nested = client.get(f"/trips/{trip['id']}", headers=alice).json["days"][0]["stops"]
    assert [s["name"] for s in nested] == ["Senso-ji"]


@pytest.mark.parametrize("body, message", [
    ({"name": "x", "lat": 91}, "between -90 and 90"),
    ({"name": "x", "lng": "139.7"}, "must be a number"),
    ({"name": "x", "lat": True}, "must be a number"),
    ({"name": "x", "time": "9:30pm"}, "must be a time"),
    ({"lat": 1}, "name is required"),
])
def test_create_stop_validation(client, alice, trip, body, message):
    day = add_day(client, alice, trip["id"])
    res = client.post(f"/days/{day['id']}/stops", json=body, headers=alice)
    assert res.status_code == 400
    assert message in res.json["error"]


def test_move_stop_to_another_day_of_same_trip(client, alice, trip):
    d1 = add_day(client, alice, trip["id"])
    d2 = add_day(client, alice, trip["id"])
    add_stop(client, alice, d2["id"], name="already there")
    stop = add_stop(client, alice, d1["id"], name="mover")
    res = client.patch(f"/stops/{stop['id']}", json={"day_id": d2["id"]}, headers=alice)
    assert res.status_code == 200
    assert res.json["day_id"] == d2["id"]
    assert res.json["position"] == 1  # appended after the existing stop


def test_cannot_move_stop_to_another_trip(client, alice, trip):
    other = client.post("/trips", json={"name": "Other"}, headers=alice).json
    d1 = add_day(client, alice, trip["id"])
    d_other = add_day(client, alice, other["id"])
    stop = add_stop(client, alice, d1["id"])
    res = client.patch(f"/stops/{stop['id']}", json={"day_id": d_other["id"]}, headers=alice)
    assert res.status_code == 400


def test_reorder_and_delete_stops(client, alice, trip):
    day = add_day(client, alice, trip["id"])
    ids = [add_stop(client, alice, day["id"], name=n)["id"] for n in ("A", "B", "C")]
    res = client.put(f"/days/{day['id']}/stops/order", json={"ids": ids[::-1]}, headers=alice)
    assert [s["name"] for s in res.json["stops"]] == ["C", "B", "A"]
    assert client.delete(f"/stops/{ids[1]}", headers=alice).status_code == 204
    stops = client.get(f"/trips/{trip['id']}", headers=alice).json["days"][0]["stops"]
    assert [s["name"] for s in stops] == ["C", "A"]


def test_trip_page_query_count_does_not_grow_with_the_trip(app, client, alice):
    # Guards against the N+1 query problem: loading a trip must cost the same
    # number of queries for 1 stop or 30.
    from sqlalchemy import event

    from tripsync.extensions import db

    def queries_for(trip_id):
        count = {"n": 0}

        def bump(*args, **kwargs):
            count["n"] += 1

        with app.app_context():
            event.listen(db.engine, "before_cursor_execute", bump)
            try:
                client.get(f"/trips/{trip_id}", headers=alice)
            finally:
                event.remove(db.engine, "before_cursor_execute", bump)
        return count["n"]

    def make_trip(days, stops):
        trip = client.post("/trips", json={"name": "T"}, headers=alice).json["id"]
        for _ in range(days):
            day = client.post(f"/trips/{trip}/days", json={}, headers=alice).json["id"]
            for i in range(stops):
                client.post(f"/days/{day}/stops", json={"name": f"s{i}"}, headers=alice)
        return trip

    small, big = queries_for(make_trip(1, 1)), queries_for(make_trip(5, 6))
    assert small == big <= 8
