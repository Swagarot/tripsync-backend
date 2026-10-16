import uuid
import datetime as dt

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, Time, Uuid
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

    days: Mapped[list["Day"]] = relationship(back_populates="trip", order_by="Day.position")

    def to_dict(self, include_days=False):
        data = {
            "id": str(self.id),
            "name": self.name,
            "start_date": iso(self.start_date),
            "end_date": iso(self.end_date),
            "notes": self.notes,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }
        if include_days:
            data["days"] = [d.to_dict(include_stops=True) for d in self.days if d.deleted_at is None]
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
    # How you get between this day's stops: walk / bike / drive. Used for the
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

    day: Mapped[Day] = relationship(back_populates="stops")

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
            "updated_at": iso(self.updated_at),
        }
