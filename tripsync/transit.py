"""Public-transport journeys between a day's stops, from Transitous.

Transitous (transitous.org) is a free, community-run journey planner built on
the open-source MOTIS router and public timetable data (GTFS) from transport
agencies worldwide. No API key; fair use expected, so answers are cached.
Coverage depends on which cities publish open timetables (Tokyo and Lisbon
do; some places don't, and then the app falls back to straight lines).

Unlike driving, a transit journey depends on WHEN you travel, so each pair of
consecutive stops is planned separately for the day's date at about 10:00
local time.
"""
import datetime as dt
import re
import threading

from cachetools import TTLCache

from .places import UpstreamError, fetch_json

TRANSITOUS = "https://api.transitous.org/api/v5/plan"
MAX_PAIRS = 12  # one request per pair: keep a single day's planning polite

_cache = TTLCache(maxsize=3000, ttl=12 * 3600)  # timetables change rarely
_lock = threading.Lock()


def decode_polyline(encoded, precision=6):
    """Google's "encoded polyline" format -> [[lat, lng], ...].

    Each coordinate is stored as the difference from the previous one, as a
    variable-length base-64-ish number: 5 bits per character, +63 to keep it
    printable, and the 0x20 bit meaning "more characters follow".
    """
    points, index, lat, lng = [], 0, 0, 0
    factor = 10 ** precision
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lng += delta
        points.append([round(lat / factor, 5), round(lng / factor, 5)])
    return points


def travel_date(date, today=None):
    """Timetables are only published a few weeks ahead. For a trip further
    out (or in the past), plan on the next same weekday instead: the typical
    weekday service is what matters for planning."""
    today = today or dt.date.today()
    if today <= date <= today + dt.timedelta(days=30):
        return date
    days_ahead = (date.weekday() - today.weekday()) % 7 or 7
    return today + dt.timedelta(days=days_ahead)


def departure_time(date, lng):
    """About 10:00 local time, in UTC. The time zone is estimated from the
    longitude (15° per hour): off by an hour here and there, which is fine for
    finding normal daytime service."""
    offset = round(lng / 15)
    local = dt.datetime.combine(date, dt.time(10, 0))
    return (local - dt.timedelta(hours=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def line_label(leg):
    """A short name for a ride's badge. Timetable data varies a lot by
    agency: Keikyu sends "KK", Toei sends an internal number "2966211" with no
    long name, the Oedo line sends only "大江戸線". So: a short code if it really
    is short, else the line's long name (its Latin-script part when it has
    one, like "Keikyū Airport Line"), else the operator."""
    short = (leg.get("routeShortName") or "").strip()
    if short and len(short) <= 5:
        return short
    name = (leg.get("routeLongName") or "").strip() or (leg.get("agencyName") or "").strip()
    latin = [run.strip() for run in re.findall(r"[A-Za-z][A-Za-z0-9 .'&À-ſ-]*", name)]
    text = max(latin, key=len) if latin and len(max(latin, key=len)) >= 3 else name
    return text if len(text) <= 22 else text[:21] + "…"


def summarize(itinerary):
    """One journey -> what the app shows and draws."""
    steps, parts = [], []
    for leg in itinerary.get("legs", []):
        geom = leg.get("legGeometry") or {}
        points = decode_polyline(geom.get("points", ""), geom.get("precision", 6)) if geom.get("points") else []
        mode = leg.get("mode", "WALK")
        color = f"#{leg['routeColor']}" if leg.get("routeColor") else None
        parts.append({"mode": mode, "color": color, "points": points})
        if mode != "WALK":
            steps.append({
                "mode": mode,
                "line": line_label(leg),
                # Where you get on and off, and the train's direction, in the
                # user's language when the agency publishes translations.
                "from": (leg.get("from") or {}).get("name"),
                "to": (leg.get("to") or {}).get("name"),
                "headsign": leg.get("headsign"),
                # Full name + direction, for the badge's tooltip.
                "title": " · ".join(x for x in (leg.get("routeLongName"), leg.get("headsign")) if x),
                "color": color,
                "minutes": round(leg.get("duration", 0) / 60),
            })
    return {
        "duration_s": itinerary.get("duration", 0),
        "transfers": itinerary.get("transfers", 0),
        "steps": steps,
        "parts": parts,
    }


def plan_pair(a, b, date, lang="en"):
    key = (a, b, date, lang)
    with _lock:
        if key in _cache:
            return _cache[key]
    data = fetch_json("transitous", TRANSITOUS, {
        "fromPlace": f"{a[0]},{a[1]}",
        "toPlace": f"{b[0]},{b[1]}",
        "time": departure_time(date, a[1]),
        "numItineraries": 3,
        # Allow up to 45 min of walking to the first stop / from the last one:
        # a big place's pin can be far from its entrance or the nearest station.
        "maxPreTransitTime": 2700,
        "maxPostTransitTime": 2700,
        # Names of lines, stations and directions in this language, where the
        # timetable data has translations (Japan's has English).
        "language": lang,
    })
    itineraries = data.get("itineraries") or []
    best = min(itineraries, key=lambda it: it.get("duration", 1e9)) if itineraries else None
    result = summarize(best) if best else None
    with _lock:
        _cache[key] = result
    return result


def plan_day(points, date, lang="en"):
    """Same shape as an OpenRouteService answer (geometry, legs, totals), plus
    `parts` (to draw each line in its own colour). A leg is None when no
    journey was found; the app shows a straight line for that one."""
    date = travel_date(date)
    legs, parts, geometry, total = [], [], [], 0
    for a, b in list(zip(points, points[1:]))[:MAX_PAIRS]:
        try:
            leg = plan_pair(a, b, date, lang)
        except UpstreamError:
            leg = None
        if leg is None:
            legs.append(None)
            parts.append({"mode": "NONE", "color": None, "points": [list(a), list(b)]})
            geometry += [list(a), list(b)]
            continue
        legs.append({k: leg[k] for k in ("duration_s", "transfers", "steps")} | {"distance_m": 0})
        parts += leg["parts"]
        geometry += [p for part in leg["parts"] for p in part["points"]]
        total += leg["duration_s"]
    return {
        "mode": "transit",
        "planned_for": date.isoformat(),
        "legs": legs,
        "parts": parts,
        "geometry": geometry,
        "duration_s": total,
        "distance_m": 0,
    }
