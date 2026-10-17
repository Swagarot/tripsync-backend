"""Routes and travel times between a day's stops.

walk / bike / drive: OpenRouteService (one request for the whole day).
transit: Transitous (see transit.py), one journey per pair of stops.

POST /routes {"mode": "walk"|"bike"|"drive"|"transit", "points": [[lat, lng], ...], "date"?: "2026-10-17"}
  -> {"geometry": [[lat, lng], ...], "legs": [{"distance_m", "duration_s"}],
      "distance_m", "duration_s"}

The free OpenRouteService key allows about 200 route requests a day, so:
  - answers are cached for a week, keyed by mode + points: the same day's route
    is only ever computed once unless its stops change;
  - the frontend asks only when a day's stops or mode change, and falls back
    to straight lines when this endpoint can't answer.
The API key comes from the k8s Secret `openrouteservice` (env ORS_API_KEY). Without
it (EKS, local dev) the endpoint answers 503 and the app shows straight lines.
"""
import datetime as dt
import os
import threading

import requests
from cachetools import TTLCache
from flask import Blueprint, abort, jsonify
from flask_jwt_extended import jwt_required
from prometheus_client import Counter

from .extensions import limiter
from .places import LANGUAGES, session
from .transit import plan_day
from .validation import Choice, Date, ValidationError, json_body

bp = Blueprint("routes", __name__)

ORS_URL = "https://api.openrouteservice.org/v2/directions/{profile}/geojson"
PROFILES = {"walk": "foot-walking", "bike": "cycling-regular", "drive": "driving-car"}
MAX_POINTS = 25  # a very full day; keeps one request small

ROUTE_CALLS = Counter("tripsync_routes_upstream_requests_total", "Calls to OpenRouteService", ["outcome"])
ROUTE_CACHE = Counter("tripsync_routes_cache_total", "Route cache lookups", ["result"])

_cache = TTLCache(maxsize=2000, ttl=7 * 24 * 3600)
_lock = threading.Lock()


def parse_points(value):
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_POINTS:
        raise ValidationError(f"points must be a list of 2 to {MAX_POINTS} [lat, lng] pairs")
    points = []
    for p in value:
        ok = (
            isinstance(p, list) and len(p) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in p)
            and abs(p[0]) <= 90 and abs(p[1]) <= 180
        )
        if not ok:
            raise ValidationError("each point must be [lat, lng]")
        # ~1 m precision: enough for routing, and tiny GPS-level differences
        # still hit the same cache entry.
        points.append((round(p[0], 5), round(p[1], 5)))
    return points


def simplify(line, tolerance=0.00005):
    """Drop points that don't change the line's shape (Douglas-Peucker).

    A long route comes back with tens of thousands of points (Tokyo to
    Kumamoto on foot: 35,000, ~700 kB), most of them on straight stretches.
    Keeping only points that bend the line by more than ~5 m (0.00005 degrees)
    looks the same on the map at any zoom and is a fraction of the size, for
    the browser, its offline cache and our own 2000-entry cache.
    Returns the indexes of the points kept (first and last always are).
    """
    keep = {0, len(line) - 1}
    stack = [(0, len(line) - 1)]
    while stack:
        a, b = stack.pop()
        (ay, ax), (by, bx) = line[a], line[b]
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        far, far_d = None, tolerance
        for i in range(a + 1, b):
            py, px = line[i]
            # Distance from the point to the straight line a-b.
            d = abs(dy * (px - ax) - dx * (py - ay)) / length if length else ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if d > far_d:
                far, far_d = i, d
        if far is not None:
            keep.add(far)
            stack += [(a, far), (far, b)]
    return sorted(keep)


def call_ors(mode, points, key, avoid_ferries):
    body = {
        # ORS wants [lng, lat]; we use [lat, lng] everywhere else.
        "coordinates": [[lng, lat] for lat, lng in points],
        # How far from each stop to look for a road or path. The default,
        # 350 m, fails for big places whose pin sits in the middle (an
        # airport's is on the airfield, a park's on the grass).
        "radiuses": [2000] * len(points),
    }
    if avoid_ferries:
        body["options"] = {"avoid_features": ["ferries"]}
    return session.post(
        ORS_URL.format(profile=PROFILES[mode]),
        json=body,
        headers={"Authorization": key},
        timeout=20,
    )


def fetch_route(mode, points):
    key = os.environ.get("ORS_API_KEY")
    if not key:
        abort(503, "Routing isn't set up on this server.")
    try:
        # Ferries first avoided: in OpenStreetMap a ferry line is often one
        # straight segment, and on long trips ORS happily picks one (by bike
        # from Tokyo to Kumamoto it took the overnight ferry, drawn as a
        # straight line across the sea). Only when there's no other way (an
        # island) ask again with ferries allowed; that costs a second request.
        res = call_ors(mode, points, key, avoid_ferries=True)
        if res.status_code == 404:
            res = call_ors(mode, points, key, avoid_ferries=False)
    except requests.RequestException:
        ROUTE_CALLS.labels("error").inc()
        abort(502, "The routing service can't be reached right now.")
    if res.status_code == 429:
        ROUTE_CALLS.labels("rate_limited").inc()
        abort(503, "Daily routing limit reached. Showing straight lines until tomorrow.")
    if res.status_code == 404 or res.status_code == 400:
        # e.g. a point in the sea, or no road/path between two stops.
        ROUTE_CALLS.labels("no_route").inc()
        return None
    if not res.ok:
        ROUTE_CALLS.labels("error").inc()
        abort(502, "The routing service had a problem.")
    ROUTE_CALLS.labels("ok").inc()

    feature = res.json()["features"][0]
    props = feature["properties"]
    full = [[round(lat, 5), round(lng, 5)] for lng, lat, *_ in feature["geometry"]["coordinates"]]
    geometry = [full[i] for i in simplify(full)] if len(full) > 2 else full
    return {
        "geometry": geometry,
        # Where each stop joined the road network (way_points = indexes into
        # the full geometry, so read before simplifying). With the 2 km search
        # radius this can be far from the stop's pin; the map draws a dotted
        # line between the two.
        "snapped": [full[i] for i in props.get("way_points", []) if i < len(full)],
        # One leg per pair of consecutive stops.
        "legs": [
            {"distance_m": round(s.get("distance", 0)), "duration_s": round(s.get("duration", 0))}
            for s in props.get("segments", [])
        ],
        "distance_m": round(props["summary"].get("distance", 0)),
        "duration_s": round(props["summary"].get("duration", 0)),
    }


@bp.post("/routes")
@jwt_required()
@limiter.limit("30 per minute")
def route():
    body = json_body()
    mode = Choice((*PROFILES, "transit"), required=True).parse("mode", body.get("mode"))
    points = parse_points(body.get("points"))
    if mode == "transit":
        date = Date().parse("date", body.get("date")) or dt.date.today()
        lang = body.get("lang") if body.get("lang") in LANGUAGES else "en"
        return jsonify(plan_day(points, date, lang))
    cache_key = (mode, tuple(points))
    with _lock:
        if cache_key in _cache:
            ROUTE_CACHE.labels("hit").inc()
            return jsonify(_cache[cache_key])
    ROUTE_CACHE.labels("miss").inc()
    result = fetch_route(mode, points)
    if result is None:
        abort(422, "No route found between these stops.")
    with _lock:
        _cache[cache_key] = result
    return jsonify(result)
