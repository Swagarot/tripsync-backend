"""Routing with OpenRouteService faked (never calls the real API: it has a
daily quota)."""
import pytest

from tripsync import routes

ORS_ANSWER = {
    "features": [{
        "geometry": {"coordinates": [[-9.21593, 38.69159], [-9.21, 38.695], [-9.2066, 38.69786]]},
        "properties": {
            "segments": [{"distance": 1353.4, "duration": 974.2}],
            "summary": {"distance": 1353.4, "duration": 974.2},
        },
    }]
}
BODY = {"mode": "walk", "points": [[38.69159, -9.21593], [38.69786, -9.2066]]}


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data
        self.ok = status < 400

    def json(self):
        return self.data


@pytest.fixture
def ors(monkeypatch):
    state = {"calls": [], "status": 200}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["calls"].append({"url": url, "json": json, "auth": headers.get("Authorization")})
        return FakeResponse(state["status"], ORS_ANSWER)

    monkeypatch.setattr(routes.session, "post", fake_post)
    monkeypatch.setenv("ORS_API_KEY", "test-key")
    routes._cache.clear()
    return state


def test_route_needs_login(client):
    assert client.post("/routes", json=BODY).status_code == 401


def test_route_returns_legs_and_lat_lng_geometry(client, alice, ors):
    res = client.post("/routes", json=BODY, headers=alice)
    assert res.status_code == 200
    assert res.json["legs"] == [{"distance_m": 1353, "duration_s": 974}]
    assert res.json["geometry"][0] == [38.69159, -9.21593]  # flipped back to [lat, lng]
    call = ors["calls"][0]
    assert call["url"].endswith("/foot-walking/geojson")
    assert call["json"]["coordinates"][0] == [-9.21593, 38.69159]  # ORS wants [lng, lat]
    assert call["auth"] == "test-key"


def test_route_is_cached(client, alice, ors):
    client.post("/routes", json=BODY, headers=alice)
    # Same stops, a few cm apart: same cache entry, no second paid call.
    nudged = {"mode": "walk", "points": [[38.691591, -9.215931], [38.69786, -9.2066]]}
    client.post("/routes", json=nudged, headers=alice)
    assert len(ors["calls"]) == 1


@pytest.mark.parametrize("body", [
    {"mode": "fly", "points": BODY["points"]},
    {"mode": "walk", "points": [[38.7, -9.2]]},
    {"mode": "walk", "points": [[38.7, -9.2], [91, 0]]},
    {"mode": "walk", "points": [[38.7, -9.2], [True, 0]]},
    {"mode": "walk", "points": [[0, 0]] * 26},
])
def test_route_validation(client, alice, ors, body):
    assert client.post("/routes", json=body, headers=alice).status_code == 400
    assert ors["calls"] == []


def test_daily_limit_is_503_with_message(client, alice, ors):
    ors["status"] = 429
    res = client.post("/routes", json=BODY, headers=alice)
    assert res.status_code == 503
    assert "limit" in res.json["error"]


def test_no_route_is_422(client, alice, ors):
    ors["status"] = 404
    assert client.post("/routes", json=BODY, headers=alice).status_code == 422


def test_without_key_routing_is_off(client, alice, ors, monkeypatch):
    monkeypatch.delenv("ORS_API_KEY")
    assert client.post("/routes", json=BODY, headers=alice).status_code == 503
    assert ors["calls"] == []
