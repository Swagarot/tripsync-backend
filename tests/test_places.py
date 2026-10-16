"""Place search/details with the outside services faked: tests never touch
the internet (they'd be slow, flaky, and would hammer free services)."""
import pytest
import requests

from tripsync import places

PHOTON_RESULT = {
    "features": [
        {
            "geometry": {"type": "Point", "coordinates": [-9.2159288, 38.691586]},
            "properties": {
                "osm_type": "W", "osm_id": 24341353, "osm_value": "attraction", "name": "Tower of Belém",
                "street": "Avenida de Brasília", "city": "Lisbon", "country": "Portugal",
            },
        },
        # Duplicates and nameless results are dropped.
        {
            "geometry": {"type": "Point", "coordinates": [-9.2159288, 38.691586]},
            "properties": {"osm_type": "W", "osm_id": 24341353, "osm_value": "attraction", "name": "Tower of Belém"},
        },
        {"geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {"osm_type": "N", "osm_id": 1}},
    ]
}

NOMINATIM_ROW = [{
    "lat": "38.691586", "lon": "-9.2159288", "type": "attraction", "name": "Torre de Belém",
    "namedetails": {"name": "Torre de Belém", "name:en": "Tower of Belém"},
    "address": {"road": "Avenida de Brasília", "city": "Lisbon", "country": "Portugal"},
    "extratags": {
        "wikidata": "Q215003", "opening_hours": "Tu-Su 10:00-17:30", "fee": "yes",
        "website": "javascript:alert(1)",
    },
}]


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


@pytest.fixture
def upstream(monkeypatch):
    """Answers by URL; records every call. Set .down to make all calls fail."""
    state = {"calls": [], "down": False, "wikidata": True}

    def fake_get(url, params=None, timeout=None):
        state["calls"].append(url)
        if state["down"]:
            raise requests.ConnectionError("down")
        if url == places.PHOTON:
            return FakeResponse(PHOTON_RESULT)
        if url == places.NOMINATIM:
            row = [dict(NOMINATIM_ROW[0], extratags=dict(NOMINATIM_ROW[0]["extratags"]))]
            if not state["wikidata"]:
                del row[0]["extratags"]["wikidata"]
            return FakeResponse(row)
        if url == places.WIKIDATA:
            state.setdefault("wikidata_sites", []).append(params["sitefilter"])
            return FakeResponse({"entities": {"Q215003": {
                "sitelinks": {"enwiki": {"title": "Belém Tower"}},
                "claims": {"P18": [{"mainsnak": {"datavalue": {"value": "Belem Tower.jpg"}}}]},
            }}})
        if "geosearch" in str(params):
            return FakeResponse({"query": {"geosearch": [{"title": "Monument to the Discoveries"},
                                                         {"title": "Belém Tower"}]}})
        if "/page/summary/" in url:
            return FakeResponse({
                "type": "standard", "extract": "A 16th-century fortification in Lisbon.",
                "thumbnail": {"source": "https://upload.wikimedia.org/belem.jpg"},
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Bel%C3%A9m_Tower"}},
            })
        raise AssertionError(f"unexpected outside call: {url}")

    monkeypatch.setattr(places.session, "get", fake_get)
    monkeypatch.setattr(places.time, "sleep", lambda s: None)  # skip the 1 req/s wait
    places._search_cache.clear()
    places._details_cache.clear()
    return state


def test_search_needs_login(client):
    assert client.get("/places/search?q=belem").status_code == 401


def test_search_returns_clean_results(client, alice, upstream):
    res = client.get("/places/search?q=belem%20tower&lat=38.7&lng=-9.2", headers=alice)
    assert res.status_code == 200
    assert res.json == [{
        "osm_ref": "W24341353", "name": "Tower of Belém", "category": "Attraction",
        "address": "Avenida de Brasília, Lisbon, Portugal", "lat": 38.691586, "lng": -9.215929, "zoom": 15,
    }]


def test_search_is_cached(client, alice, upstream):
    for q in ("Belem  tower", "belem tower"):  # same query after normalising
        client.get(f"/places/search?q={q}", headers=alice)
    assert upstream["calls"].count(places.PHOTON) == 1


def test_short_query_skips_upstream(client, alice, upstream):
    assert client.get("/places/search?q=b", headers=alice).json == []
    assert upstream["calls"] == []


def test_search_upstream_down_is_502(client, alice, upstream):
    upstream["down"] = True
    res = client.get("/places/search?q=belem", headers=alice)
    assert res.status_code == 502
    assert "unavailable" in res.json["error"]


@pytest.mark.parametrize("query", ["lat=abc&lng=1", "lat=91&lng=0"])
def test_search_bad_coordinates(client, alice, upstream, query):
    assert client.get(f"/places/search?q=belem&{query}", headers=alice).status_code == 400


def test_details_combine_osm_and_wikipedia(client, alice, upstream):
    res = client.get("/places/W24341353", headers=alice)
    assert res.status_code == 200
    d = res.json
    assert d["name"] == "Tower of Belém"
    assert d["opening_hours"] == "Tu-Su 10:00-17:30"
    assert d["description"].startswith("A 16th-century")
    # Wikidata's own image wins over Wikipedia's lead image.
    assert d["photo_url"] == "https://commons.wikimedia.org/wiki/Special:FilePath/Belem%20Tower.jpg?width=640"
    # A "javascript:" link in the map data must never reach the page.
    assert d["website"] is None


def test_details_ask_wikipedia_in_the_users_language(client, alice, upstream):
    d = client.get("/places/W24341353?lang=he", headers=alice).json
    assert upstream["wikidata_sites"] == ["hewiki|enwiki"]
    # No Hebrew article in the fake: falls back to the English description.
    assert d["description"].startswith("A 16th-century")


def test_unknown_language_falls_back_to_english(client, alice, upstream):
    client.get("/places/W24341353?lang=xx", headers=alice)
    assert upstream["wikidata_sites"] == ["enwiki"]


def test_details_without_wikidata_match_by_name_nearby(client, alice, upstream):
    upstream["wikidata"] = False
    d = client.get("/places/W24341353", headers=alice).json
    # "Tower of Belém" vs nearby articles: the close match is used...
    assert d["description"].startswith("A 16th-century")


def test_details_survive_wikipedia_outage(client, alice, upstream, monkeypatch):
    real = places.fetch_json

    def no_wiki(service, url, params=None):
        if service in ("wikidata", "wikipedia"):
            raise places.UpstreamError("down")
        return real(service, url, params)

    monkeypatch.setattr(places, "fetch_json", no_wiki)
    d = client.get("/places/W24341353", headers=alice).json
    assert d["opening_hours"] == "Tu-Su 10:00-17:30"
    assert d["description"] is None


def test_details_bad_ref_is_404_without_upstream_call(client, alice, upstream):
    assert client.get("/places/X123", headers=alice).status_code == 404
    assert upstream["calls"] == []


@pytest.mark.parametrize("a, b, match", [
    ("Tower of Belém", "Belém Tower", True),      # word order differs
    ("Café Central", "Cafe Central", True),       # accents
    ("Jerónimos Monastery", "Jeronimos Monastery", True),
    ("Belém Tower", "Monument to the Discoveries", False),
    ("Central Park", "Central Station", False),
])
def test_name_similarity(a, b, match):
    assert (places.name_similarity(a, b) >= 0.75) is match
