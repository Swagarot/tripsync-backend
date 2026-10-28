"""Sharing a trip: invite links, the member list, roles, leaving.

  POST   /trips/<id>/invites          {"role": "editor"|"viewer"}  -> a link token (owner)
  POST   /trips/<id>/invites/reset    every earlier link stops working (owner)
  GET    /invites/<token>             what the link is for (any logged-in user)
  POST   /invites/<token>/accept      join the trip
  PATCH  /trips/<id>/members/<user>   {"role": ...} (owner)
  DELETE /trips/<id>/members/<user>   remove someone (owner), or leave (yourself)

Invite links carry a signed token, not a database row: itsdangerous (part of
Flask) signs {trip, role, invite_version} with the app's secret key and a
timestamp. Nobody can make or change one without the key, it expires after 7
days, and "cancel links" bumps the trip's invite_version so older tokens no
longer match. No email is involved (no mail provider, scope): the owner copies
the link and sends it however they like. Anyone who opens it while logged in
joins with the link's role, so treat it like a key.
"""
import datetime as dt
import time
import uuid

from flask import Blueprint, abort, current_app, jsonify
from flask_jwt_extended import jwt_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .access import MEMBER_ROLES, OWNER, RANK, VIEWER, members_of, require, role_in
from .auth import current_user_id
from .extensions import db, limiter
from .models import Trip, TripMember, User
from .trips import find_trip, owned
from .validation import Choice, load

bp = Blueprint("sharing", __name__)

INVITE_DAYS = 7


@bp.before_request
@jwt_required()
def require_login():
    pass


def serializer():
    # A salt per purpose: a token signed for invites is useless anywhere else
    # that uses the same key.
    return URLSafeTimedSerializer(current_app.config["JWT_SECRET_KEY"], salt="trip-invite")


def read_invite(token):
    """The trip and role a token is for, or 404 if it's broken, expired,
    cancelled, or its trip is gone. One message for all: no hints for guessers."""
    try:
        data = serializer().loads(token, max_age=INVITE_DAYS * 24 * 3600)
        trip = db.session.get(Trip, uuid.UUID(data["t"]))
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        abort(404, "This invite link is invalid or has expired. Ask for a new one.")
    if trip is None or trip.deleted_at is not None or trip.invite_version != data.get("v") \
            or data.get("r") not in MEMBER_ROLES:
        abort(404, "This invite link is invalid or has expired. Ask for a new one.")
    return trip, data["r"]


@bp.post("/trips/<uuid:trip_id>/invites")
@limiter.limit("30 per hour")
def create_invite(trip_id):
    trip = owned(find_trip, trip_id, need=OWNER)
    role = load({"role": Choice(MEMBER_ROLES, required=True)})["role"]
    token = serializer().dumps({"t": str(trip.id), "r": role, "v": trip.invite_version})
    expires = dt.datetime.fromtimestamp(time.time() + INVITE_DAYS * 24 * 3600, dt.timezone.utc)
    return jsonify({"token": token, "role": role, "expires_at": expires.isoformat()}), 201


@bp.post("/trips/<uuid:trip_id>/invites/reset")
def reset_invites(trip_id):
    trip = owned(find_trip, trip_id, need=OWNER)
    trip.invite_version += 1
    db.session.commit()
    return "", 204


@bp.get("/invites/<token>")
@limiter.limit("60 per hour")
def show_invite(token):
    trip, role = read_invite(token)
    owner = db.session.get(User, trip.user_id)
    return jsonify({
        "trip_id": str(trip.id), "trip_name": trip.name, "destination": trip.destination,
        "owner_email": owner.email, "role": role,
        "your_role": role_in(trip),  # already on it? (the page then just opens it)
    })


@bp.post("/invites/<token>/accept")
@limiter.limit("60 per hour")
def accept_invite(token):
    trip, role = read_invite(token)
    current = role_in(trip)
    if current is None:
        db.session.add(TripMember(trip_id=trip.id, user_id=current_user_id(), role=role))
    elif current != OWNER and RANK[role] > RANK[current]:
        # A viewer opening an editor link moves up; a link never moves anyone down.
        member = db.session.get(TripMember, (trip.id, current_user_id()))
        member.role = role
    db.session.commit()
    return jsonify({"trip_id": str(trip.id), "role": role_in(trip)})


def find_member(trip, user_id):
    return db.session.get(TripMember, (trip.id, user_id)) or abort(404, "That person isn't on this trip.")


@bp.patch("/trips/<uuid:trip_id>/members/<uuid:user_id>")
def change_role(trip_id, user_id):
    trip = owned(find_trip, trip_id, need=OWNER)
    member = find_member(trip, user_id)
    member.role = load({"role": Choice(MEMBER_ROLES, required=True)})["role"]
    db.session.commit()
    return jsonify(members_of(trip))


@bp.delete("/trips/<uuid:trip_id>/members/<uuid:user_id>")
def remove_member(trip_id, user_id):
    trip = owned(find_trip, trip_id, need=VIEWER)
    if user_id != current_user_id():
        require(trip, OWNER)  # removing someone else
    # The owner isn't a member row, so this also refuses "owner leaves"
    # (they'd delete the trip instead).
    member = find_member(trip, user_id)
    db.session.delete(member)
    db.session.commit()
    return "", 204
