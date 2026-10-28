"""Ideas, expenses and checklist items: the side panel's lists.

All three are "a list of things belonging to a trip", so one set of endpoints
serves them, with the kind in the URL:

  POST   /trips/<trip_id>/<kind>     create (client may choose the id)
  PATCH  /<kind>/<id>                change some fields
  DELETE /<kind>/<id>                soft delete

kind = ideas | expenses | checklist | lodgings. They come back inside GET /trips/<id>.
Same security rule as trips.py: rows are only found through trips the
logged-in user is on (404 otherwise), and changing them needs the editor role.

Expenses also say who paid (paid_by) and who shares the cost (split_between),
both people on the trip; the balances are worked out by the frontend.
"""
from decimal import Decimal

from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required
from sqlalchemy import func, select

from .access import member_ids, trip_of, visible_trips
from .extensions import db
from .models import ChecklistItem, Expense, Lodging, SavedPlace, Trip, utcnow
from .trips import already_created, find_trip, owned
from .validation import Bool, Choice, Currency, Date, Number, Text, Url, ValidationError, json_body, load, parse_uuid

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
    "lodgings": (Lodging, {
        "name": Text(200, required=True),
        "lat": Number(-90, 90),
        "lng": Number(-180, 180),
        "address": Text(500),
        "osm_ref": Text(32),
        "category": Text(64),
        "photo_url": Url(1000),
        "website": Url(500),
        "check_in": Date(),
        "check_out": Date(),
        "bookends": Bool(nullable=False),
    }),
}
KIND_PATTERN = "<any(ideas, expenses, checklist, lodgings):kind>"


def find_child(model, row_id):
    return db.session.scalar(
        select(model).join(Trip, Trip.id == model.trip_id).where(
            model.id == row_id,
            model.deleted_at.is_(None),
            visible_trips(),
            Trip.deleted_at.is_(None),
        )
    )


def check_stay(data):
    if data.get("check_in") and data.get("check_out") and data["check_out"] < data["check_in"]:
        raise ValidationError("check_out must not be before check_in")


def read_split(trip):
    """paid_by / split_between from the body, checked against the trip's
    people (someone not on the trip can't pay or owe). Only the keys that were
    sent, so an update that leaves them out keeps the old values."""
    body = json_body()
    out = {}
    people = None
    if "paid_by" in body:
        people = member_ids(trip)
        if body["paid_by"] is None:
            out["paid_by"] = None
        else:
            payer = parse_uuid("paid_by", body["paid_by"])
            if payer not in people:
                raise ValidationError("paid_by must be someone on this trip")
            out["paid_by"] = payer
    if "split_between" in body:
        people = people or member_ids(trip)
        value = body["split_between"]
        if value is None:
            out["split_between"] = None  # everyone on the trip
        else:
            if not isinstance(value, list) or not value:
                raise ValidationError("split_between must be a non-empty list of people (or null for everyone)")
            ids = [parse_uuid("split_between", v) for v in value]
            if any(i not in people for i in ids):
                raise ValidationError("split_between must only list people on this trip")
            # Stored as strings, sorted, no duplicates: JSON has no UUID type.
            out["split_between"] = sorted({str(i) for i in ids})
    return out


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
    check_stay(data)
    if model is Expense:
        data.update(read_split(trip))
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
    row = owned(lambda i: find_child(model, i), row_id)
    data = prepare(load(fields, partial=True))
    if model is Expense:
        data.update(read_split(trip_of(row)))
    if model is Lodging:
        check_stay({"check_in": data.get("check_in", row.check_in), "check_out": data.get("check_out", row.check_out)})
    for key, value in data.items():
        if key in fields and value is None and fields[key].required:
            raise ValidationError(f"{key} is required")
        setattr(row, key, value)
    db.session.commit()
    return jsonify(row.to_dict())


@bp.delete(f"/{KIND_PATTERN}/<uuid:row_id>")
def delete(kind, row_id):
    model, _ = KINDS[kind]
    row = owned(lambda i: find_child(model, i), row_id)
    row.deleted_at = utcnow()
    db.session.commit()
    return "", 204
