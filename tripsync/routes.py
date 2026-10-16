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


def fetch_route(mode, points):
    key = os.environ.get("ORS_API_KEY")
    if not key:
        abort(503, "Routing isn't set up on this server.")
    try:
        res = session.post(
            ORS_URL.format(profile=PROFILES[mode]),
            # ORS wants [lng, lat]; we use [lat, lng] everywhere else.
            json={
                "coordinates": [[lng, lat] for lat, lng in points],
                "instructions": True,
                # How far from each stop to look for a road or path. The
                # default, 350 m, fails for big places whose pin sits in the
                # middle (an airport's is on the airfield, a park's on the grass).
                "radiuses": [2000] * len(points),
            },
            headers={"Authorization": key},
            timeout=10,
        )
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
    geometry = [[round(lat, 5), round(lng, 5)] for lng, lat, *_ in feature["geometry"]["coordinates"]]
    return {
        "geometry": geometry,
        # Where each stop joined the road network (way_points = indexes into
        # the geometry). With the 2 km search radius this can be far from the
        # stop's pin; the map draws a dotted line between the two.
        "snapped": [geometry[i] for i in props.get("way_points", []) if i < len(geometry)],
        # One leg per pair of consecutive stops (turn-by-turn steps dropped).
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
