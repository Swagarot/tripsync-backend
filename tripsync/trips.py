"""Trips, days and stops: create, read, update, delete, reorder.

Security rule for this whole file: a row is only ever loaded through one of the
find_*() helpers, which only see trips the logged-in user owns or is a member
of, and then owned() checks the user's role is enough for the action (see
access.py). Someone asking for a trip they're not on gets 404, exactly as if it
didn't exist, so IDs that belong to someone else can't even be confirmed. A
viewer trying to change something gets 403.
"""
from flask import Blueprint, abort, jsonify
from flask_jwt_extended import jwt_required
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .access import EDITOR, OWNER, VIEWER, members_of, require, role_in, trip_of, visible_trips
from .auth import current_user_id
from .extensions import db
from .models import Day, Stop, Trip, TripMember, User, utcnow
from .validation import (
    Bool, Choice, Currency, Date, Int, Number, Text, Time, Url, ValidationError, json_body, load, optional_id,
    parse_uuid,
)

bp = Blueprint("trips", __name__)


# Every route in this blueprint needs a valid token. Without one,
# flask-jwt-extended answers 401 before the view runs.
@bp.before_request
@jwt_required()
def require_login():
    pass


TRIP_FIELDS = {
    "name": Text(200, required=True),
    "start_date": Date(),
    "end_date": Date(),
    "notes": Text(5000),
    "destination": Text(200),
    "dest_lat": Number(-90, 90),
    "dest_lng": Number(-180, 180),
    "dest_zoom": Number(1, 18),
    "home_currency": Currency(),
}
DAY_FIELDS = {
    "date": Date(),
    "title": Text(200),
    "notes": Text(5000),
    "travel_mode": Choice(("walk", "bike", "drive", "transit")),
}
STOP_FIELDS = {
    "name": Text(200, required=True),
    "lat": Number(-90, 90),
    "lng": Number(-180, 180),
    "address": Text(500),
    "time": Time(),
    "notes": Text(5000),
    "osm_ref": Text(32),
    "category": Text(64),
    "photo_url": Url(1000),
    "website": Url(500),
    "opening_hours": Text(500),
    "rating": Int(1, 5),
    "visited": Bool(nullable=False),
}


# --- Ownership-checked lookups ---------------------------------------------

# Everything the trip page shows, loaded up front in a fixed number of queries
# ("eager loading"). Without it, SQLAlchemy loads each day's stops and each
# stop's photos on first access: one query per day plus one per stop (the
# "N+1 query" problem), 40 queries for a 5-day trip, hundreds for a long one,
# each a network round trip to Postgres. selectinload fetches each level for
# all parents at once (WHERE day_id IN (...)): 7 queries, whatever the size.
FULL_TRIP = (
    selectinload(Trip.days).selectinload(Day.stops).selectinload(Stop.photos),
    selectinload(Trip.ideas),
    selectinload(Trip.expenses),
    selectinload(Trip.checklist),
    selectinload(Trip.lodgings),
)


def find_full_trip(trip_id):
    return db.session.scalar(
        select(Trip).options(*FULL_TRIP).where(
            Trip.id == trip_id,
            visible_trips(),
            Trip.deleted_at.is_(None),
        )
    )


def find_trip(trip_id):
    return db.session.scalar(
        select(Trip).where(
            Trip.id == trip_id,
            visible_trips(),
            Trip.deleted_at.is_(None),
        )
    )


def find_day(day_id):
    # A day has no owner of its own; access comes from its trip.
    return db.session.scalar(
        select(Day).join(Day.trip).where(
            Day.id == day_id,
            Day.deleted_at.is_(None),
            visible_trips(),
            Trip.deleted_at.is_(None),
        )
    )


def find_stop(stop_id):
    return db.session.scalar(
        select(Stop).join(Stop.day).join(Day.trip).where(
            Stop.id == stop_id,
            Stop.deleted_at.is_(None),
            Day.deleted_at.is_(None),
            visible_trips(),
            Trip.deleted_at.is_(None),
        )
    )


def owned(find, row_id, need=EDITOR):
    """Load a row the user may act on: 404 if they can't see it, 403 if
    their role on its trip is lower than `need`. Changing things is the
    common case, so `need` defaults to editor; reads pass VIEWER."""
    row = find(row_id)
    if row is None:
        abort(404)
    require(trip_of(row), need)
    return row


def already_created(model, find):
    """Handle a client-chosen ID on create.

    Returns the existing row if this user already created it (an offline phone
    retrying a request whose response it never got, so the retry is harmless),
    None if the ID is new, and 409 if the ID is taken by something else.
    """
    new_id = optional_id()
    if new_id is None or db.session.get(model, new_id) is None:
        return new_id, None
    existing = find(new_id)
    if existing is None:
        abort(409, "id is already in use")
    return new_id, existing


def next_position(column, parent_column, parent_id):
    # Append at the end: one more than the highest position of the live rows.
    highest = db.session.scalar(
        select(func.max(column)).where(parent_column == parent_id, column.class_.deleted_at.is_(None))
    )
    return 0 if highest is None else highest + 1


def check_dates(start, end):
    if start and end and end < start:
        raise ValidationError("end_date must not be before start_date")


def reorder(rows, what):
    """Set positions from the body's "ids" list, which must name every row once."""
    ids = json_body().get("ids")
    if not isinstance(ids, list):
        raise ValidationError("ids must be a list")
    ids = [parse_uuid("ids", i) for i in ids]
    by_id = {row.id: row for row in rows}
    if len(ids) != len(by_id) or set(ids) != set(by_id):
        raise ValidationError(f"ids must list every {what} exactly once")
    for position, row_id in enumerate(ids):
        by_id[row_id].position = position
    db.session.commit()


def soft_delete(row):
    row.deleted_at = utcnow()
    db.session.commit()
    return "", 204


# --- Trips -----------------------------------------------------------------

def trip_previews(trip_ids):
    """For the trips page cards: counts, the pins for a mini map, and a
    cover photo (the first stop that has one). Two queries for all trips
    together, instead of loading every trip in full."""
    previews = {tid: {"days": 0, "places": 0, "points": [], "photo_url": None} for tid in trip_ids}
    if not trip_ids:
        return previews
    day_counts = db.session.execute(
        select(Day.trip_id, func.count())
        .where(Day.trip_id.in_(trip_ids), Day.deleted_at.is_(None))
        .group_by(Day.trip_id)
    )
    for trip_id, count in day_counts:
        previews[trip_id]["days"] = count
    stops = db.session.execute(
        select(Day.trip_id, Stop.lat, Stop.lng, Stop.photo_url)
        .join(Stop.day)
        .where(Day.trip_id.in_(trip_ids), Day.deleted_at.is_(None), Stop.deleted_at.is_(None))
        .order_by(Day.trip_id, Day.position, Stop.position)
    )
    for trip_id, lat, lng, photo in stops:
        p = previews[trip_id]
        p["places"] += 1
        if lat is not None and len(p["points"]) < 50:  # enough for a thumbnail map
            p["points"].append([round(lat, 4), round(lng, 4)])
        p["photo_url"] = p["photo_url"] or photo
    return previews


@bp.get("/trips")
def list_trips():
    trips = list(db.session.scalars(
        select(Trip)
        .where(visible_trips(), Trip.deleted_at.is_(None))
        .order_by(Trip.created_at.desc())
    ))
    previews = trip_previews([t.id for t in trips])
    # Your role on each, and whose it is, in two queries for the whole list.
    me = current_user_id()
    roles = dict(db.session.execute(
        select(TripMember.trip_id, TripMember.role).where(TripMember.user_id == me)
    ).all())
    owner_ids = {t.user_id for t in trips if t.user_id != me}
    emails = dict(db.session.execute(select(User.id, User.email).where(User.id.in_(owner_ids))).all())
    return jsonify([{
        **t.to_dict(),
        "preview": previews[t.id],
        "role": OWNER if t.user_id == me else roles.get(t.id),
        "owner_email": None if t.user_id == me else emails.get(t.user_id),
    } for t in trips])


@bp.post("/trips")
def create_trip():
    new_id, existing = already_created(Trip, find_trip)
    if existing:
        return jsonify(full_trip_dict(existing))
    data = load(TRIP_FIELDS)
    check_dates(data.get("start_date"), data.get("end_date"))
    trip = Trip(id=new_id, user_id=current_user_id(), **data)
    db.session.add(trip)
    db.session.commit()
    return jsonify(full_trip_dict(trip)), 201


@bp.get("/trips/<uuid:trip_id>")
def get_trip(trip_id):
    # The whole trip in one response (days with their stops), which is what
    # the trip page and, later, the offline cache need. Plus your role (the
    # page hides editing for viewers) and who's on it (sharing, splitting).
    trip = find_full_trip(trip_id) or abort(404)
    role = require(trip, VIEWER)
    return jsonify(full_trip_dict(trip, role))


def full_trip_dict(trip, role=None):
    return {**trip.to_dict(include_days=True), "role": role or role_in(trip), "members": members_of(trip)}


@bp.patch("/trips/<uuid:trip_id>")
def update_trip(trip_id):
    trip = owned(find_trip, trip_id)
    data = load(TRIP_FIELDS, partial=True)
    if "name" in data and data["name"] != trip.name:
        require(trip, OWNER)  # editors change everything else
    check_dates(data.get("start_date", trip.start_date), data.get("end_date", trip.end_date))
    for key, value in data.items():
        setattr(trip, key, value)
    db.session.commit()
    return jsonify(full_trip_dict(trip))


@bp.delete("/trips/<uuid:trip_id>")
def delete_trip(trip_id):
    # Its days and stops stay as they are: every lookup goes through the trip,
    # so they become unreachable together with it (for every member too).
    return soft_delete(owned(find_trip, trip_id, need=OWNER))


# --- Days ------------------------------------------------------------------

@bp.post("/trips/<uuid:trip_id>/days")
def create_day(trip_id):
    trip = owned(find_trip, trip_id)
    new_id, existing = already_created(Day, find_day)
    if existing:
        return jsonify(existing.to_dict(include_stops=True))
    data = load(DAY_FIELDS)
    day = Day(id=new_id, trip_id=trip.id, position=next_position(Day.position, Day.trip_id, trip.id), **data)
    db.session.add(day)
    db.session.commit()
    return jsonify(day.to_dict(include_stops=True)), 201


@bp.patch("/days/<uuid:day_id>")
def update_day(day_id):
    day = owned(find_day, day_id)
    for key, value in load(DAY_FIELDS, partial=True).items():
        setattr(day, key, value)
    db.session.commit()
    return jsonify(day.to_dict(include_stops=True))


@bp.delete("/days/<uuid:day_id>")
def delete_day(day_id):
    return soft_delete(owned(find_day, day_id))


@bp.put("/trips/<uuid:trip_id>/days/order")
def reorder_days(trip_id):
    trip = owned(find_trip, trip_id)
    reorder([d for d in trip.days if d.deleted_at is None], "day of the trip")
    return jsonify(full_trip_dict(trip))


# --- Stops -----------------------------------------------------------------

@bp.post("/days/<uuid:day_id>/stops")
def create_stop(day_id):
    day = owned(find_day, day_id)
    new_id, existing = already_created(Stop, find_stop)
    if existing:
        return jsonify(existing.to_dict())
    data = load(STOP_FIELDS)
    stop = Stop(id=new_id, day_id=day.id, position=next_position(Stop.position, Stop.day_id, day.id), **data)
    db.session.add(stop)
    db.session.commit()
    return jsonify(stop.to_dict()), 201


@bp.patch("/stops/<uuid:stop_id>")
def update_stop(stop_id):
    stop = owned(find_stop, stop_id)
    data = load(STOP_FIELDS, partial=True)
    body = json_body()
    if body.get("day_id") is not None:
        # Moving a stop to another day. The target must be a day of the same
        # trip; otherwise the move would let someone attach their stop to (and
        # so learn about) another trip's day.
        target = find_day(parse_uuid("day_id", body["day_id"]))
        if target is None or target.trip_id != stop.day.trip_id:
            raise ValidationError("day_id must be a day of the same trip")
        if target.id != stop.day_id:
            # Position first, while the stop still belongs to its old day.
            stop.position = next_position(Stop.position, Stop.day_id, target.id)
            stop.day_id = target.id
    if "cover_photo_id" in body:
        # Must be one of this stop's own photos (or null = back to the default).
        cover = body["cover_photo_id"]
        if cover is not None:
            cover = parse_uuid("cover_photo_id", cover)
            if not any(p.id == cover and p.deleted_at is None for p in stop.photos):
                raise ValidationError("cover_photo_id must be one of this stop's photos")
        stop.cover_photo_id = cover
    for key, value in data.items():
        setattr(stop, key, value)
    db.session.commit()
    return jsonify(stop.to_dict())


@bp.delete("/stops/<uuid:stop_id>")
def delete_stop(stop_id):
    return soft_delete(owned(find_stop, stop_id))


@bp.put("/days/<uuid:day_id>/stops/order")
def reorder_stops(day_id):
    day = owned(find_day, day_id)
    reorder([s for s in day.stops if s.deleted_at is None], "stop of the day")
    return jsonify(day.to_dict(include_stops=True))
