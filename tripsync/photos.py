"""Your own photos of a stop.

  POST   /stops/<id>/photos       upload one (multipart form, field "file")
  DELETE /photos/<id>             remove it
  GET    /photos/<id>/<size>?exp=..&sig=..   the image (size: full | thumb)

Uploads are checked and re-encoded with Pillow before they're stored:
  - it must really be an image (not just named .jpg);
  - it's turned upright (phones store rotation as a tag), shrunk to at most
    2048 px, and saved as a fresh JPEG. That also drops all EXIF metadata,
    including the GPS position, which you don't want to publish by accident;
  - a 480 px thumbnail is made for the lists.

Reading a photo can't use the login token: the browser loads it with a plain
<img src>, which can't send an Authorization header. So photo URLs are
*signed* (like S3's presigned URLs): the backend adds an expiry and an HMAC
signature, and only answers when the signature matches. Expiry is set to the
end of next month, so a photo's URL stays the same for weeks and the service
worker can keep it for offline use.
"""
import datetime as dt
import hashlib
import hmac
import io
import time
import uuid

from flask import Blueprint, Response, abort, current_app, jsonify, request
from flask_jwt_extended import jwt_required
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select

from .access import visible_trips
from .extensions import db, limiter
from .models import Day, Stop, StopPhoto, Trip, utcnow
from .storage import StorageUnavailable, get_storage
from .trips import find_stop, owned

bp = Blueprint("photos", __name__)

MAX_SIDE = 2048
THUMB_SIDE = 480
MAX_PHOTOS_PER_STOP = 30
# A "decompression bomb" is a tiny file that expands to a gigantic image and
# eats all memory. Refuse anything over 50 megapixels before decoding it.
Image.MAX_IMAGE_PIXELS = 50_000_000


def object_key(photo_id, size):
    return f"photos/{photo_id}/{size}.jpg"


# --- Signed URLs ------------------------------------------------------------------

def _signature(photo_id, size, exp):
    secret = (current_app.config["JWT_SECRET_KEY"] + ":photos").encode()
    return hmac.new(secret, f"{photo_id}:{size}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def _expiry(now=None):
    # The first day of the month after next, 00:00 UTC: one URL for weeks.
    today = (now or dt.datetime.now(dt.timezone.utc)).date()
    year, month = (today.year, today.month + 2) if today.month <= 10 else (today.year + 1, today.month - 10)
    return int(dt.datetime(year, month, 1, tzinfo=dt.timezone.utc).timestamp())


def signed_url(photo_id, size):
    exp = _expiry()
    return f"/api/photos/{photo_id}/{size}?exp={exp}&sig={_signature(photo_id, size, exp)}"


def photo_dict(photo):
    return {
        "id": str(photo.id),
        "url": signed_url(photo.id, "full"),
        "thumb_url": signed_url(photo.id, "thumb"),
        "width": photo.width,
        "height": photo.height,
        "created_at": photo.created_at.isoformat() if photo.created_at else None,
    }


# --- Upload -------------------------------------------------------------------------

def _encode(img, side, quality):
    copy = img.copy()
    copy.thumbnail((side, side))  # keeps the aspect ratio, never enlarges
    out = io.BytesIO()
    copy.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue(), copy.size


@bp.post("/stops/<uuid:stop_id>/photos")
@jwt_required()
@limiter.limit("60 per minute")
def upload(stop_id):
    stop = owned(find_stop, stop_id)  # adding photos = editing the trip
    try:
        storage = get_storage(current_app)
    except StorageUnavailable as e:
        abort(503, str(e))
    live = [p for p in stop.photos if p.deleted_at is None]
    if len(live) >= MAX_PHOTOS_PER_STOP:
        abort(400, f"A stop can have at most {MAX_PHOTOS_PER_STOP} photos.")
    file = request.files.get("file")
    if file is None:
        abort(400, "Send the photo as form field 'file'.")
    try:
        img = Image.open(file.stream)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        abort(400, "That file isn't a photo we can read (JPEG, PNG, WebP, HEIC not supported).")

    full, (width, height) = _encode(img, MAX_SIDE, 85)
    thumb, _ = _encode(img, THUMB_SIDE, 80)
    # The client may choose the id (offline-first, like everything else).
    try:
        photo_id = uuid.UUID(request.form.get("id")) if request.form.get("id") else uuid.uuid4()
    except ValueError:
        abort(400, "id must be a UUID")
    if db.session.get(StopPhoto, photo_id):
        abort(409, "id is already in use")
    storage.put(object_key(photo_id, "full"), full, "image/jpeg")
    storage.put(object_key(photo_id, "thumb"), thumb, "image/jpeg")
    photo = StopPhoto(id=photo_id, stop_id=stop.id, width=width, height=height, size_bytes=len(full))
    db.session.add(photo)
    db.session.commit()
    return jsonify(photo_dict(photo)), 201


def find_photo(photo_id):
    return db.session.scalar(
        select(StopPhoto).join(Stop, Stop.id == StopPhoto.stop_id).join(Day).join(Trip).where(
            StopPhoto.id == photo_id,
            StopPhoto.deleted_at.is_(None),
            Stop.deleted_at.is_(None),
            Day.deleted_at.is_(None),
            visible_trips(),
            Trip.deleted_at.is_(None),
        )
    )


@bp.delete("/photos/<uuid:photo_id>")
@jwt_required()
def delete(photo_id):
    photo = owned(find_photo, photo_id)
    photo.deleted_at = utcnow()
    stop = db.session.get(Stop, photo.stop_id)
    if stop.cover_photo_id == photo.id:
        stop.cover_photo_id = None
    db.session.commit()
    # The files go right away; the row stays (soft delete) for offline sync.
    try:
        storage = get_storage(current_app)
        for size in ("full", "thumb"):
            storage.delete(object_key(photo_id, size))
    except Exception:  # noqa: BLE001 - a leftover file is harmless; don't fail the delete
        current_app.logger.warning("could not delete photo files for %s", photo_id)
    return "", 204


# --- Serving ------------------------------------------------------------------------

@bp.get("/photos/<uuid:photo_id>/<any(full, thumb):size>")
@limiter.exempt  # a trip page loads many thumbnails at once
def serve(photo_id, size):
    exp = request.args.get("exp", "")
    sig = request.args.get("sig", "")
    if not exp.isdigit() or int(exp) < time.time():
        abort(403, "This photo link has expired.")
    if not hmac.compare_digest(sig, _signature(photo_id, size, int(exp))):
        abort(403)
    photo = db.session.get(StopPhoto, photo_id)
    if photo is None or photo.deleted_at is not None:
        abort(404)
    try:
        data, content_type = get_storage(current_app).get(object_key(photo_id, size))
    except StorageUnavailable as e:
        abort(503, str(e))
    except Exception:  # noqa: BLE001
        abort(404)
    res = Response(data, mimetype=content_type)
    # A photo never changes under its id: browsers and the service worker can
    # keep it until the link expires. "private": not for shared caches.
    res.headers["Cache-Control"] = f"private, max-age={max(0, int(exp) - int(time.time()))}, immutable"
    return res
