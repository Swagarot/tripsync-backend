"""Exchange rates and weather forecasts, from free services with no API key.

  GET /rates?base=ILS
      Frankfurter (European Central Bank reference rates, updated once per
      working day): how much 1 unit of `base` is in ~30 other currencies.
      The frontend keeps the last answer, so expense totals work offline too.

  GET /weather?lat=..&lng=..
      Open-Meteo: a daily forecast for the next 16 days at that spot (rain
      probability and amount, min/max temperature, wind, weather code). The
      frontend turns it into warnings for the trip's days ("storm forecast,
      2 outdoor stops"). Further than 16 days ahead there is no forecast.

Both through the backend for the same reasons as place search: one shared
cache, an identifying User-Agent, and control over how often we call out.
"""
from cachetools import TTLCache
from flask import Blueprint, abort, jsonify, request
from flask_jwt_extended import jwt_required

from .extensions import limiter
from .places import UpstreamError, cached, fetch_json, optional_coord

bp = Blueprint("travelinfo", __name__)

FRANKFURTER = "https://api.frankfurter.dev/v1/latest"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

_rates_cache = TTLCache(maxsize=100, ttl=6 * 3600)  # rates change once a day
_weather_cache = TTLCache(maxsize=2000, ttl=3600)  # forecasts update hourly


@bp.get("/rates")
@jwt_required()
@limiter.limit("30 per minute")
def rates():
    base = request.args.get("base", "").upper()
    if not (len(base) == 3 and base.isalpha()):
        abort(400, "base must be a currency code like ILS")
    try:
        data = cached(_rates_cache, "rates", base, lambda: fetch_json("frankfurter", FRANKFURTER, {"base": base}))
    except UpstreamError:
        abort(502, "Exchange rates are unavailable right now.")
    return jsonify({"base": base, "date": data.get("date"), "rates": {base: 1.0, **data.get("rates", {})}})


DAILY = ",".join([
    "weather_code", "temperature_2m_max", "temperature_2m_min",
    "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max",
])


def load_forecast(lat, lng):
    data = fetch_json("open-meteo", OPEN_METEO, {
        "latitude": lat, "longitude": lng, "daily": DAILY, "timezone": "auto", "forecast_days": 16,
    })
    daily = data.get("daily", {})
    keys = [k for k in daily if k != "time"]
    # Columns -> one object per day: [{"date": "2026-10-16", "weather_code": 3, ...}, ...]
    return [{"date": day, **{k: daily[k][i] for k in keys}} for i, day in enumerate(daily.get("time", []))]


@bp.get("/weather")
@jwt_required()
@limiter.limit("60 per minute")
def weather():
    lat, lng = optional_coord("lat", 90), optional_coord("lng", 180)
    if lat is None or lng is None:
        abort(400, "lat and lng are required")
    try:
        # optional_coord rounds to 0.1° (~10 km): plenty for a forecast, and
        # stops in the same city share one cache entry.
        days = cached(_weather_cache, "weather", (lat, lng), lambda: load_forecast(lat, lng))
    except UpstreamError:
        abort(502, "The weather forecast is unavailable right now.")
    return jsonify({"lat": lat, "lng": lng, "days": days})
