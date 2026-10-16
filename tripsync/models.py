import uuid
import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, Time, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db

# Design notes (these matter for offline sync in Week 8):
# - IDs are UUIDs, and the client may choose them. A phone that creates a trip
#   offline can give it its final ID right away and send it later; with
#   database-assigned integers it would need a temporary ID and a remapping step.
# - updated_at changes on every write, so a client can later ask "what changed
#   since my last sync?".
# - Trips, days and stops are soft-deleted (deleted_at is set, the row stays).
#   A hard delete leaves nothing behind to tell an offline phone the row is gone.


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(value):
    return value.isoformat() if value is not None else None


def _photo_dict(photo):
    from .photos import photo_dict  # here, not at the top: photos.py imports this module
    return photo_dict(photo)


class SyncColumns:
    """created_at / updated_at / deleted_at, shared by trips, days and stops."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class User(db.Model):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Stored lowercased, so Roye@x.com and roye@x.com are the same account.
    email: Mapped[str] = mapped_column(String(254), unique=True)
    # Never the password itself: Werkzeug's salted scrypt hash (see auth.py).
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {"id": str(self.id), "email": self.email, "created_at": iso(self.created_at)}


class Trip(SyncColumns, db.Model):
    __tablename__ = "trips"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The owner. Every trip query filters on this (see trips.py).
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    # "Where to?": picked from place search when the trip is created. Centres
    # the map before there are any stops and biases place search towards it.
    destination: Mapped[str | None] = mapped_column(String(200))
    dest_lat: Mapped[float | None] = mapped_column(Float)
    dest_lng: Mapped[float | None] = mapped_column(Float)
    dest_zoom: Mapped[float | None] = mapped_column(Float)  # country ~5, city ~11
    # Expenses are converted into this currency for the totals (ISO code, ILS...).
    home_currency: Mapped[str | None] = mapped_column(String(3))

    days: Mapped[list["Day"]] = relationship(back_populates="trip", order_by="Day.position")
    ideas: Mapped[list["SavedPlace"]] = relationship(order_by="SavedPlace.created_at")
    expenses: Mapped[list["Expense"]] = relationship(order_by="Expense.created_at")
    checklist: Mapped[list["ChecklistItem"]] = relationship(order_by="ChecklistItem.position")
    lodgings: Mapped[list["Lodging"]] = relationship(order_by="Lodging.check_in")

    def to_dict(self, include_days=False):
        data = {
            "id": str(self.id),
            "name": self.name,
            "start_date": iso(self.start_date),
            "end_date": iso(self.end_date),
            "notes": self.notes,
            "destination": self.destination,
            "dest_lat": self.dest_lat,
            "dest_lng": self.dest_lng,
            "dest_zoom": self.dest_zoom,
            "home_currency": self.home_currency,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }
        if include_days:
            # The full trip, as the trip page (and its offline copy) needs it.
            live = lambda rows: [r.to_dict() for r in rows if r.deleted_at is None]  # noqa: E731
            data["days"] = [d.to_dict(include_stops=True) for d in self.days if d.deleted_at is None]
            data["ideas"] = live(self.ideas)
            data["expenses"] = live(self.expenses)
            data["checklist"] = live(self.checklist)
            data["lodgings"] = live(self.lodgings)
        return data


class Day(SyncColumns, db.Model):
    __tablename__ = "days"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)
    # Order within the trip: 0, 1, 2... Rewritten by the reorder endpoint.
    position: Mapped[int] = mapped_column(Integer)
    date: Mapped[dt.date | None] = mapped_column(Date)
    title: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    # How you get between this day's stops: walk / bike / drive / transit. Used for the
    # route and travel times.
    travel_mode: Mapped[str | None] = mapped_column(String(16))

    trip: Mapped[Trip] = relationship(back_populates="days")
    stops: Mapped[list["Stop"]] = relationship(back_populates="day", order_by="Stop.position")

    def to_dict(self, include_stops=False):
        data = {
            "id": str(self.id),
            "trip_id": str(self.trip_id),
            "position": self.position,
            "date": iso(self.date),
            "title": self.title,
            "notes": self.notes,
            "travel_mode": self.travel_mode,
            "updated_at": iso(self.updated_at),
        }
        if include_stops:
            data["stops"] = [s.to_dict() for s in self.stops if s.deleted_at is None]
        return data


class Stop(SyncColumns, db.Model):
    __tablename__ = "stops"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    day_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("days.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    # Entered by hand or picked on the map (Week 7). No Places API (scope).
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(String(500))
    # Planned arrival time on that day, local time, no timezone.
    time: Mapped[dt.time | None] = mapped_column(Time)
    notes: Mapped[str | None] = mapped_column(Text)
    # Filled when the stop was added from place search (places.py). A copy is
    # kept on the stop, so the photo/hours/website still show offline, and the
    # trip doesn't change if OpenStreetMap does.
    osm_ref: Mapped[str | None] = mapped_column(String(32))  # e.g. W24341353
    category: Mapped[str | None] = mapped_column(String(64))
    photo_url: Mapped[str | None] = mapped_column(String(1000))
    website: Mapped[str | None] = mapped_column(String(500))
    opening_hours: Mapped[str | None] = mapped_column(String(500))
    # Your own memories of the place.
    rating: Mapped[int | None] = mapped_column(Integer)  # 1-5 stars, after visiting
    visited: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Which of your photos is the stop's picture; none = the famous photo (or
    # your first photo when there's no famous one).
    cover_photo_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    day: Mapped[Day] = relationship(back_populates="stops")
    photos: Mapped[list["StopPhoto"]] = relationship(order_by="StopPhoto.created_at")

    def to_dict(self):
        return {
            "id": str(self.id),
            "day_id": str(self.day_id),
            "position": self.position,
            "name": self.name,
            "lat": self.lat,
            "lng": self.lng,
            "address": self.address,
            "time": self.time.strftime("%H:%M") if self.time else None,
            "notes": self.notes,
            "osm_ref": self.osm_ref,
            "category": self.category,
            "photo_url": self.photo_url,
            "website": self.website,
            "opening_hours": self.opening_hours,
            "rating": self.rating,
            "visited": bool(self.visited),
            "cover_photo_id": str(self.cover_photo_id) if self.cover_photo_id else None,
            # Signed links to your photos (see photos.py). Imported here to
            # avoid a circular import (photos.py imports the models).
            "photos": [_photo_dict(p) for p in self.photos if p.deleted_at is None],
            "updated_at": iso(self.updated_at),
        }


# --- Things that hang directly off a trip (the side panel's tabs) ---------------
# Same rules as days and stops: client-chosen UUIDs, updated_at, soft delete,
# and ownership through the trip (see extras.py).

class TripChild(SyncColumns):
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trips.id", ondelete="CASCADE"), index=True)

    def base_dict(self):
        return {"id": str(self.id), "trip_id": str(self.trip_id), "updated_at": iso(self.updated_at)}


class SavedPlace(TripChild, db.Model):
    """'Ideas': places you might visit, not on any day yet."""
    __tablename__ = "saved_places"

    name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    osm_ref: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(64))
    photo_url: Mapped[str | None] = mapped_column(String(1000))
    website: Mapped[str | None] = mapped_column(String(500))
    opening_hours: Mapped[str | None] = mapped_column(String(500))

    def to_dict(self):
        return {**self.base_dict(), **{k: getattr(self, k) for k in (
            "name", "lat", "lng", "address", "notes", "osm_ref", "category", "photo_url", "website", "opening_hours",
        )}}


class Expense(TripChild, db.Model):
    __tablename__ = "expenses"

    # Numeric, not Float: money must add up exactly (0.1 + 0.2 != 0.3 in floats).
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))  # what was actually paid in
    category: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(String(300))
    date: Mapped[dt.date | None] = mapped_column(Date)

    def to_dict(self):
        return {
            **self.base_dict(), "amount": float(self.amount), "currency": self.currency,
            "category": self.category, "note": self.note, "date": iso(self.date),
        }


class ChecklistItem(TripChild, db.Model):
    __tablename__ = "checklist_items"

    text: Mapped[str] = mapped_column(String(300))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer)

    def to_dict(self):
        return {**self.base_dict(), "text": self.text, "done": self.done, "position": self.position}


class StopPhoto(SyncColumns, db.Model):
    """One of your photos of a stop. The image itself is in object storage
    (storage.py) under photos/<id>/full.jpg and thumb.jpg; this row says whose
    it is and where it belongs."""
    __tablename__ = "stop_photos"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stops.id", ondelete="CASCADE"), index=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)


class Lodging(TripChild, db.Model):
    """Where you sleep from check_in to check_out (a hotel, an apartment...).
    With `bookends`, every day in that range starts and ends there: the app adds
    it to the day's route and travel times."""
    __tablename__ = "lodgings"

    name: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    address: Mapped[str | None] = mapped_column(String(500))
    osm_ref: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(64))
    photo_url: Mapped[str | None] = mapped_column(String(1000))
    website: Mapped[str | None] = mapped_column(String(500))
    check_in: Mapped[dt.date | None] = mapped_column(Date)
    check_out: Mapped[dt.date | None] = mapped_column(Date)
    bookends: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def to_dict(self):
        return {
            **self.base_dict(),
            **{k: getattr(self, k) for k in (
                "name", "lat", "lng", "address", "osm_ref", "category", "photo_url", "website",
            )},
            "check_in": iso(self.check_in),
            "check_out": iso(self.check_out),
            "bookends": bool(self.bookends),
        }
