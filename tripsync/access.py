"""Who may do what with a trip (shared trips).

Three roles, from least to most:
  viewer  sees the trip
  editor  also changes anything in it (days, stops, ideas, expenses, photos...)
  owner   also renames or deletes it and decides who else is on it

The owner is trips.user_id; everyone else is a row in trip_members.

Every lookup in trips.py / extras.py / photos.py goes through two steps:
  1. find the row among trips you can SEE (visible_trips()); not found = 404,
     exactly like an ID that doesn't exist, so outsiders can't even confirm an
     ID (same rule as before sharing);
  2. check your role is enough for what you're doing (require()); too low = 403.
     403, not 404, here: a viewer already knows the trip exists, and "you can't
     edit this" is a more useful answer than "not found".
"""
from flask import abort
from sqlalchemy import or_, select

from .auth import current_user_id
from .extensions import db
from .models import Day, Stop, StopPhoto, Trip, TripChild, TripMember, User

VIEWER, EDITOR, OWNER = "viewer", "editor", "owner"
RANK = {VIEWER: 0, EDITOR: 1, OWNER: 2}
MEMBER_ROLES = (EDITOR, VIEWER)  # what an invite or a role change can give


def visible_trips():
    """SQL condition on Trip: the logged-in user owns it or is a member."""
    me = current_user_id()
    return or_(
        Trip.user_id == me,
        Trip.id.in_(select(TripMember.trip_id).where(TripMember.user_id == me)),
    )


def role_in(trip):
    """owner / editor / viewer, or None if the user has no access."""
    me = current_user_id()
    if trip.user_id == me:
        return OWNER
    return db.session.scalar(
        select(TripMember.role).where(TripMember.trip_id == trip.id, TripMember.user_id == me)
    )


def trip_of(row):
    """The trip a row belongs to (rows get their access through their trip)."""
    if isinstance(row, Trip):
        return row
    if isinstance(row, Day):
        return row.trip
    if isinstance(row, Stop):
        return row.day.trip
    if isinstance(row, StopPhoto):
        return db.session.get(Stop, row.stop_id).day.trip
    if isinstance(row, TripChild):
        return db.session.get(Trip, row.trip_id)
    raise TypeError(f"no trip for {type(row).__name__}")


MESSAGES = {
    EDITOR: "You can only view this trip. Ask its owner to make you an editor.",
    OWNER: "Only the trip's owner can do that.",
}


def require(trip, need):
    """Abort unless the logged-in user has at least role `need` on the trip."""
    role = role_in(trip)
    if role is None:
        abort(404)
    if RANK[role] < RANK[need]:
        abort(403, MESSAGES[need])
    return role


def members_of(trip):
    """Everyone on the trip, owner first: for the sharing panel and for
    choosing who paid / who shares an expense."""
    owner = db.session.get(User, trip.user_id)
    people = [{"user_id": str(owner.id), "email": owner.email, "role": OWNER}]
    rows = db.session.execute(
        select(TripMember, User).join(User, User.id == TripMember.user_id)
        .where(TripMember.trip_id == trip.id).order_by(TripMember.created_at)
    )
    people += [{"user_id": str(u.id), "email": u.email, "role": m.role} for m, u in rows]
    return people


def member_ids(trip):
    """The user ids allowed in an expense's paid_by / split_between."""
    ids = {trip.user_id}
    ids.update(db.session.scalars(select(TripMember.user_id).where(TripMember.trip_id == trip.id)))
    return ids
