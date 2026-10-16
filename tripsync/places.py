"""Place search and place details, from free OpenStreetMap-based services.

  search   Photon (photon.komoot.io): search-as-you-type over OpenStreetMap.
  details  Nominatim (nominatim.openstreetmap.org) looks up one place's OSM tags
           (opening hours, website, wikidata link...). Wikidata + Wikipedia
           then give a short description and a free photo.

Why through the backend and not straight from the browser:
  - one cache for all users: the same search or place is fetched once, which
    keeps us inside the free services' fair-use rules;
  - we control the request rate (Nominatim allows at most 1 request/second);
  - every request carries a User-Agent naming the app, as the services' usage
    policies require (a browser's fetch() can't set it);
  - if we ever add a keyed service, the key stays on the server.

No ratings: free, legitimate sources for star ratings don't exist (Google /
TripAdvisor / Yelp data is proprietary). OSM's practical details are shown
instead.
"""
import difflib
import re
import threading
import time
import unicodedata
from urllib.parse import quote

import requests
from cachetools import TTLCache
from flask import Blueprint, abort, jsonify, request
from flask_jwt_extended import jwt_required
from prometheus_client import Counter

from .extensions import limiter

bp = Blueprint("places", __name__, url_prefix="/places")

USER_AGENT = "TripSync/1.0 (+https://tripsync.royerandomnonsense.uk)"
TIMEOUT = 6  # seconds per outside call; the user is waiting on it

PHOTON = "https://photon.komoot.io/api/"
NOMINATIM = "https://nominatim.openstreetmap.org/lookup"
WIKIDATA = "https://www.wikidata.org/w/api.php"

# Shows up in Prometheus next to the request metrics: how often each outside
# service is called and how often the cache saves a call.
UPSTREAM = Counter(
    "tripsync_places_upstream_requests_total", "Calls to outside place services", ["service", "outcome"]
)
CACHE = Counter("tripsync_places_cache_total", "Place cache lookups", ["kind", "result"])

# In-process caches (one backend pod; Gunicorn threads share them, hence the
# lock). Search results change rarely, place details even less.
_search_cache = TTLCache(maxsize=5000, ttl=24 * 3600)
_details_cache = TTLCache(maxsize=5000, ttl=7 * 24 * 3600)
_cache_lock = threading.Lock()

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


class UpstreamError(Exception):
    pass


def fetch_json(service, url, params=None):
    try:
        res = session.get(url, params=params, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except (requests.RequestException, ValueError) as e:
        UPSTREAM.labels(service, "error").inc()
        raise UpstreamError(f"{service}: {e}") from e
    UPSTREAM.labels(service, "ok").inc()
    return data


# Nominatim's policy: at most one request per second for the whole app.
_nominatim_lock = threading.Lock()
_nominatim_last = 0.0


def nominatim_json(params):
    global _nominatim_last
    with _nominatim_lock:
        wait = 1.0 - (time.monotonic() - _nominatim_last)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last = time.monotonic()
    return fetch_json("nominatim", NOMINATIM, params)


def cached(cache, kind, key, compute):
    with _cache_lock:
        if key in cache:
            CACHE.labels(kind, "hit").inc()
            return cache[key]
    CACHE.labels(kind, "miss").inc()
    value = compute()
    with _cache_lock:
        cache[key] = value
    return value


# --- Search ----------------------------------------------------------------------

# The languages the app offers for names (see the frontend's settings.js).
LANGUAGES = {"en", "he", "es", "fr", "de", "it", "ja", "zh", "ko"}
# Photon only knows a few; for the others its English names are the most useful.
PHOTON_LANGUAGES = {"en", "de", "fr"}


def request_language():
    lang = (request.args.get("lang") or "en").lower()
    return lang if lang in LANGUAGES else "en"


# "W24341353": OSM element type (Node / Way / Relation) + its numeric ID.
OSM_REF = re.compile(r"^[NWR]\d{1,15}$")


def label(osm_value):
    return osm_value.replace("_", " ").capitalize() if osm_value and osm_value != "yes" else None


def format_address(p):
    street = " ".join(x for x in (p.get("street"), p.get("housenumber")) if x)
    parts = [street, p.get("city") or p.get("town") or p.get("village") or p.get("county"), p.get("country")]
    return ", ".join(x for x in parts if x) or None


# How far to zoom the map to show a place: a country needs a wide view, a
# café a close one. Used when a search result becomes a trip's destination.
ZOOM_BY_TYPE = {
    "continent": 3, "country": 5, "state": 7, "region": 7, "province": 7, "county": 9,
    "city": 11, "town": 13, "municipality": 11, "village": 14, "suburb": 14, "island": 10,
}


def to_result(feature):
    p = feature.get("properties", {})
    lng, lat = feature["geometry"]["coordinates"]
    place_type = p.get("type") if p.get("osm_key") == "place" else p.get("osm_value")
    return {
        "osm_ref": f"{p['osm_type']}{p['osm_id']}",
        "name": p["name"],
        "category": label(p.get("osm_value")),
        "address": format_address(p),
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "zoom": ZOOM_BY_TYPE.get(place_type, ZOOM_BY_TYPE.get(p.get("osm_value"), 15)),
    }


def search_photon(q, lat, lng, lang="en"):
    params = {"q": q, "limit": 10, "lang": lang if lang in PHOTON_LANGUAGES else "en"}
    if lat is not None:
        # Bias towards the trip (the map's centre or the trip's stops), so
        # "cathedral" finds the one in the city you're planning, not anywhere.
        params.update(lat=lat, lon=lng)
    data = fetch_json("photon", PHOTON, params)
    results, seen = [], set()
    for f in data.get("features", []):
        p = f.get("properties", {})
        if not p.get("name") or p.get("osm_type") not in ("N", "W", "R"):
            continue
        r = to_result(f)
        if r["osm_ref"] not in seen:
            seen.add(r["osm_ref"])
            results.append(r)
    return results[:8]


def optional_coord(name, limit):
    value = request.args.get(name)
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        abort(400, f"{name} must be a number")
    if abs(number) > limit:
        abort(400, f"{name} out of range")
    # Rounded to ~10 km: the bias doesn't need more, and nearby users then
    # share cache entries.
    return round(number, 1)


@bp.get("/search")
@jwt_required()
# Type-ahead sends a request every few keystrokes; generous, but stops a
# script from using us as a free proxy to Photon.
@limiter.limit("60 per minute")
def search():
    q = " ".join(request.args.get("q", "").split())
    if len(q) < 2:
        return jsonify([])
    if len(q) > 200:
        abort(400, "q is too long")
    lat, lng = optional_coord("lat", 90), optional_coord("lng", 180)
    lang = request_language()
    key = (q.lower(), lat, lng, lang if lang in PHOTON_LANGUAGES else "en")
    try:
        return jsonify(cached(_search_cache, "search", key, lambda: search_photon(q, lat, lng, lang)))
    except UpstreamError:
        abort(502, "Place search is unavailable right now. Try again in a moment.")


# --- Details ------------------------------------------------------------------------

def _simplify(text):
    # "Torre de Belém" -> "torre de belem", for comparing names loosely.
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def wikipedia_summary(lang, title):
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}"
    data = fetch_json("wikipedia", url)
    if data.get("type") == "disambiguation":
        return None
    return {
        "description": data.get("extract"),
        "photo_url": (data.get("thumbnail") or {}).get("source"),
        "source_url": (data.get("content_urls") or {}).get("desktop", {}).get("page"),
    }


def wikidata_article(qid, lang="en"):
    """(language, Wikipedia title) for a Wikidata item, in the user's language
    when that Wikipedia has an article, else English; plus its image (P18)."""
    sites = f"{lang}wiki|enwiki" if lang != "en" else "enwiki"
    data = fetch_json("wikidata", WIKIDATA, {
        "action": "wbgetentities", "ids": qid, "props": "claims|sitelinks",
        "sitefilter": sites, "format": "json",
    })
    entity = data.get("entities", {}).get(qid, {})
    links = entity.get("sitelinks", {})
    if f"{lang}wiki" in links:
        title = (lang, links[f"{lang}wiki"]["title"])
    elif "enwiki" in links:
        title = ("en", links["enwiki"]["title"])
    else:
        title = None
    image = None
    for claim in entity.get("claims", {}).get("P18", []):
        image = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        break
    return title, image


# Small words that don't help tell places apart, in a few common languages.
STOPWORDS = {"the", "of", "a", "an", "and", "de", "da", "do", "del", "la", "le", "les", "el", "di", "der", "die", "das"}


def name_similarity(a, b):
    """0..1. The best of: character similarity, and word-set overlap (so
    "Tower of Belém" matches "Belém Tower" despite the different word order)."""
    a, b = _simplify(a), _simplify(b)
    if not a or not b:
        return 0.0
    chars = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()) - STOPWORDS, set(b.split()) - STOPWORDS
    words = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    return max(chars, words)


def nearby_article(names, lat, lng):
    """Roye's idea: find the place's Wikipedia article by name, but only among
    articles within 150 m of it, so a common name can't match a different place
    in another city. `names`: the English and the local name."""
    data = fetch_json("wikipedia", "https://en.wikipedia.org/w/api.php", {
        "action": "query", "list": "geosearch", "gscoord": f"{lat}|{lng}",
        "gsradius": 150, "gslimit": 10, "format": "json",
    })
    best, best_score = None, 0.0
    for page in data.get("query", {}).get("geosearch", []):
        score = max(name_similarity(n, page["title"]) for n in names)
        if score > best_score:
            best, best_score = page["title"], score
    return best if best_score >= 0.75 else None


def safe_url(value):
    # Only http(s) links ever reach the page: a "javascript:" URL in OSM data
    # would otherwise run code when clicked.
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def load_details(osm_ref, lang="en"):
    rows = nominatim_json({
        "osm_ids": osm_ref, "format": "json", "extratags": 1, "namedetails": 1,
        "addressdetails": 1, "accept-language": f"{lang},en",
    })
    if not rows:
        return None
    row = rows[0]
    tags = row.get("extratags") or {}
    names = row.get("namedetails") or {}
    name = (names.get(f"name:{lang}") or names.get("name:en") or names.get("name") or row.get("name")
            or row.get("display_name", "").split(",")[0])
    lat, lng = float(row["lat"]), float(row["lon"])
    address = row.get("address") or {}

    details = {
        "osm_ref": osm_ref,
        "name": name,
        "category": label(row.get("type")),
        "address": format_address({
            "street": address.get("road"), "housenumber": address.get("house_number"),
            "city": address.get("city") or address.get("town") or address.get("village"),
            "country": address.get("country"),
        }),
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "opening_hours": tags.get("opening_hours"),
        "website": safe_url(tags.get("website") or tags.get("contact:website")),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "wheelchair": tags.get("wheelchair"),
        "fee": tags.get("fee"),
        "cuisine": tags.get("cuisine", "").replace(";", ", ").replace("_", " ") or None,
        "description": None,
        "photo_url": None,
        "source_url": None,
    }

    # Description and photo are extras: if Wikipedia is down, the OSM details
    # are still worth returning.
    try:
        summary, image = None, None
        if tags.get("wikidata"):
            title, image = wikidata_article(tags["wikidata"], lang)
            if title:
                summary = wikipedia_summary(*title)
        if not summary and ":" in tags.get("wikipedia", ""):
            lang, title = tags["wikipedia"].split(":", 1)
            if re.fullmatch(r"[a-z-]{2,12}", lang):
                summary = wikipedia_summary(lang, title)
        if not summary and not tags.get("wikidata"):
            local = names.get("name") or name
            title = nearby_article({name, local}, lat, lng)
            if title:
                summary = wikipedia_summary("en", title)
        if summary:
            details.update({k: v for k, v in summary.items() if v})
        if image:
            # Wikidata's "image" (P18) is chosen to show the place itself, so it
            # wins over Wikipedia's lead image, which is sometimes a logo or a
            # map (the Eiffel Tower's is its logo).
            details["photo_url"] = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(image)}?width=640"
    except UpstreamError:
        pass
    details["photo_url"] = safe_url(details["photo_url"])
    details["source_url"] = safe_url(details["source_url"])
    return details


@bp.get("/<osm_ref>")
@jwt_required()
@limiter.limit("30 per minute")
def details(osm_ref):
    if not OSM_REF.match(osm_ref):
        abort(404)
    lang = request_language()
    try:
        result = cached(_details_cache, "details", (osm_ref, lang), lambda: load_details(osm_ref, lang))
    except UpstreamError:
        abort(502, "Place details are unavailable right now. Try again in a moment.")
    if result is None:
        abort(404)
    return jsonify(result)
