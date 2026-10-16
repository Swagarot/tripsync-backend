"""Transit planning (transit.py) with Transitous faked."""
import datetime as dt

import pytest

from tripsync import routes, transit


def test_decode_polyline_matches_googles_documented_example():
    # From Google's "Encoded Polyline Algorithm Format" page (precision 5).
    points = transit.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert points == [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]


def test_travel_date_keeps_near_dates_and_maps_far_ones_to_same_weekday():
    today = dt.date(2026, 10, 16)  # a Friday
    assert transit.travel_date(dt.date(2026, 10, 20), today) == dt.date(2026, 10, 20)
    far = transit.travel_date(dt.date(2027, 4, 6), today)  # a Tuesday, months away
    assert far.weekday() == dt.date(2027, 4, 6).weekday() and today < far <= today + dt.timedelta(days=7)
    past = transit.travel_date(dt.date(2026, 1, 2), today)  # a Friday in the past
    assert past == dt.date(2026, 10, 23)


@pytest.mark.parametrize("leg, label", [
    ({"routeShortName": "KK", "routeLongName": "京急空港線 Keikyū Airport Line"}, "KK"),
    ({"routeShortName": "2966211", "routeLongName": "", "agencyName": "都営地下鉄"}, "都営地下鉄"),
    ({"routeShortName": "", "routeLongName": "大江戸線"}, "大江戸線"),
    ({"routeShortName": "", "routeLongName": "京急空港線 Keikyū Airport Line"}, "Keikyū Airport Line"),
    ({"routeShortName": "", "routeLongName": "東京モノレール羽田空港線 Tokyo Monorail Haneda Airport Line"},
     "Tokyo Monorail Haneda…"),
])
def test_line_label(leg, label):
    assert transit.line_label(leg) == label


def test_departure_time_is_about_ten_local():
    assert transit.departure_time(dt.date(2026, 10, 17), 139.7) == "2026-10-17T01:00:00Z"  # Tokyo, UTC+9
    assert transit.departure_time(dt.date(2026, 10, 17), -9.2) == "2026-10-17T11:00:00Z"   # Lisbon, ~UTC-1 by longitude


ITINERARY = {
    "duration": 1980, "transfers": 0,
    "legs": [
        {"mode": "WALK", "duration": 780, "legGeometry": {"points": "_p~iF~ps|U_ulLnnqC", "precision": 5}},
        {"mode": "SUBWAY", "duration": 1020, "routeShortName": "G", "routeColor": "ff9500",
         "legGeometry": {"points": "_p~iF~ps|U_ulLnnqC", "precision": 5}},
    ],
}


@pytest.fixture
def fake_transitous(monkeypatch):
    calls = []

    def fake(service, url, params=None):
        calls.append(params)
        if params["fromPlace"].startswith("0.0"):
            return {"itineraries": []}
        return {"itineraries": [dict(ITINERARY, duration=2400), ITINERARY]}

    monkeypatch.setattr(transit, "fetch_json", fake)
    transit._cache.clear()
    return calls


def test_transit_route_picks_fastest_and_keeps_line_colours(client, alice, fake_transitous):
    body = {"mode": "transit", "points": [[35.68, 139.76], [35.65, 139.70]], "date": "2026-10-17"}
    res = client.post("/routes", json=body, headers=alice)
    assert res.status_code == 200
    leg = res.json["legs"][0]
    assert leg["duration_s"] == 1980
    assert leg["steps"] == [{"mode": "SUBWAY", "line": "G", "from": None, "to": None, "headsign": None,
                             "title": "", "color": "#ff9500", "minutes": 17}]
    assert [p["mode"] for p in res.json["parts"]] == ["WALK", "SUBWAY"]
    assert fake_transitous[0]["maxPreTransitTime"] == 2700
    assert fake_transitous[0]["language"] == "en"


def test_transit_names_in_the_users_language(client, alice, fake_transitous):
    body = {"mode": "transit", "points": [[35.68, 139.76], [35.65, 139.70]], "lang": "fr"}
    client.post("/routes", json=body, headers=alice)
    assert fake_transitous[0]["language"] == "fr"


def test_transit_leg_without_journey_is_none(client, alice, fake_transitous):
    body = {"mode": "transit", "points": [[35.68, 139.76], [0.0, 0.0], [35.65, 139.70]]}
    res = client.post("/routes", json=body, headers=alice).json
    assert res["legs"][0] is not None and res["legs"][1] is None
    assert res["parts"][-1]["mode"] == "NONE"  # drawn as a straight line


def test_transit_is_cached_per_pair_and_date(client, alice, fake_transitous):
    body = {"mode": "transit", "points": [[35.68, 139.76], [35.65, 139.70]], "date": "2026-10-17"}
    client.post("/routes", json=body, headers=alice)
    client.post("/routes", json=body, headers=alice)
    assert len(fake_transitous) == 1


def test_road_routes_ask_for_a_wide_snap_radius(client, alice, monkeypatch):
    sent = {}

    class Res:
        status_code, ok = 404, False

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.update(json)
        return Res()

    monkeypatch.setattr(routes.session, "post", fake_post)
    monkeypatch.setenv("ORS_API_KEY", "k")
    routes._cache.clear()
    client.post("/routes", json={"mode": "drive", "points": [[35.5494, 139.7798], [35.6586, 139.7454]]}, headers=alice)
    assert sent["radiuses"] == [2000, 2000]


def test_day_accepts_transit_mode(client, alice):
    trip = client.post("/trips", json={"name": "T"}, headers=alice).json
    day = client.post(f"/trips/{trip['id']}/days", json={"travel_mode": "transit"}, headers=alice)
    assert day.status_code == 201 and day.json["travel_mode"] == "transit"
