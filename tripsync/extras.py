"""Ideas, expenses and checklist items: the side panel's lists.

All three are "a list of things belonging to a trip", so one set of endpoints
serves them, with the kind in the URL:

  POST   /trips/<trip_id>/<kind>     create (client may choose the id)
  PATCH  /<kind>/<id>                change some fields
  DELETE /<kind>/<id>                soft delete

kind = ideas | expenses | checklist. They come back inside GET /trips/<id>.
Same security rule as trips.py: rows are only found through the logged-in
user's own trips, so anyone else's id is a 404.
"""
from decimal import Decimal

from flask import Blueprint, abort, jsonify
from flask_jwt_extended import jwt_required
from sqlalchemy import func, select

from .extensions import db
from .models import ChecklistItem, Expense, SavedPlace, Trip, utcnow
from .trips import already_created, find_trip, owned
from .auth import current_user_id
from .validation import Bool, Choice, Currency, Date, Number, Text, Url, ValidationError, load

bp = Blueprint("extras", __name__)


@bp.before_request
@jwt_required()
def require_login():
    pass


EXPENSE_CATEGORIES = ("food", "transport", "lodging", "activities", "shopping", "other")

KINDS = {
    "ideas": (SavedPlace, {
        "name": Text(200, required=True),
        "lat": Number(-90, 90),
        "lng": Number(-180, 180),
        "address": Text(500),
        "notes": Text(5000),
        "osm_ref": Text(32),
        "category": Text(64),
        "photo_url": Url(1000),
        "website": Url(500),
        "opening_hours": Text(500),
    }),
    "expenses": (Expense, {
        "amount": Number(0, 10_000_000, required=True),
        "currency": Currency(required=True),
        "category": Choice(EXPENSE_CATEGORIES, required=True),
        "note": Text(300),
        "date": Date(),
    }),
    "checklist": (ChecklistItem, {
        "text": Text(300, required=True),
        "done": Bool(nullable=False),
    }),
}
KIND_PATTERN = "<any(ideas, expenses, checklist):kind>"


def find_child(model, row_id):
    return db.session.scalar(
        select(model).join(Trip, Trip.id == model.trip_id).where(
            model.id == row_id,
            model.deleted_at.is_(None),
            Trip.user_id == current_user_id(),
            Trip.deleted_at.is_(None),
        )
    )


def prepare(data):
    # Money is stored as an exact decimal, rounded to cents.
    if "amount" in data and data["amount"] is not None:
        data["amount"] = Decimal(str(round(data["amount"], 2)))
    return data


@bp.post(f"/trips/<uuid:trip_id>/{KIND_PATTERN}")
def create(trip_id, kind):
    model, fields = KINDS[kind]
    trip = owned(find_trip, trip_id)
    new_id, existing = already_created(model, lambda i: find_child(model, i))
    if existing:
        return jsonify(existing.to_dict())
    data = prepare(load(fields))
    if model is ChecklistItem:
        highest = db.session.scalar(
            select(func.max(ChecklistItem.position)).where(
                ChecklistItem.trip_id == trip.id, ChecklistItem.deleted_at.is_(None)
            )
        )
        data.setdefault("done", False)
        data["position"] = 0 if highest is None else highest + 1
    row = model(id=new_id, trip_id=trip.id, **data)
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@bp.patch(f"/{KIND_PATTERN}/<uuid:row_id>")
def update(kind, row_id):
    model, fields = KINDS[kind]
    row = find_child(model, row_id) or abort(404)
    for key, value in prepare(load(fields, partial=True)).items():
        if value is None and fields[key].required:
            raise ValidationError(f"{key} is required")
        setattr(row, key, value)
    db.session.commit()
    return jsonify(row.to_dict())


@bp.delete(f"/{KIND_PATTERN}/<uuid:row_id>")
def delete(kind, row_id):
    model, _ = KINDS[kind]
    row = find_child(model, row_id) or abort(404)
    row.deleted_at = utcnow()
    db.session.commit()
    return "", 204
