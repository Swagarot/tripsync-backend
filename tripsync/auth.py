"""Accounts and login sessions.

Every login creates a session: one row per device (models.AuthSession). Its
token carries the session id ("sid") and a token id ("jti"), and is only
accepted while that jti is the session's current one. Every time the app
starts online (or comes back online), it calls POST /auth/refresh, which swaps
its token for a new 7-day one ("rotation"), so a device that is used keeps
working and a token stolen from it stops working at the next refresh.

If a replaced token is used again later, someone has a copy: the whole session
is revoked (refresh-token rotation with reuse detection, as in OAuth). The
token just replaced stays valid for a short grace, because requests already in
flight still carry it.

Why one long-lived token per device and not short access + refresh tokens:
the app works offline for days, and a token that expires mid-trip would lock
the user out of syncing. Rotation limits what a stolen token is worth without
that.
"""
import datetime as dt
import re
import uuid

from flask import Blueprint, jsonify
from flask_jwt_extended import create_access_token, get_jti, get_jwt, get_jwt_identity, jwt_required
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, jwt, limiter
from .models import AuthSession, User, utcnow
from .validation import Text, ValidationError, load

bp = Blueprint("auth", __name__, url_prefix="/auth")

# Deliberately loose: the only real proof an address works is sending mail to
# it, and there's no mail provider (scope). This just catches typos like
# "roye.gmail.com".
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CREDENTIALS = {
    "email": Text(254, required=True),
    "password": Text(128, required=True),
}

# Used when the email doesn't exist, so a failed login takes as long either
# way. Otherwise an attacker could tell registered emails apart by timing.
DUMMY_HASH = generate_password_hash("not-a-real-password")


def current_user_id():
    """The logged-in user's ID, from the token's 'sub' claim."""
    return uuid.UUID(get_jwt_identity())


# A request sent just before a rotation can still arrive with the old token.
ROTATION_GRACE = dt.timedelta(minutes=2)
# Rotation keeps a used device logged in; this caps it, so a session (and a
# stolen one nobody noticed) can't live forever.
MAX_SESSION_AGE = dt.timedelta(days=90)


def aware(value):
    # SQLite (tests, laptop) hands datetimes back without a time zone.
    return value if value is None or value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def issue_token(user, session):
    """A new token for this session; it replaces the session's current one."""
    # identity becomes the token's 'sub' claim. The token is signed, not
    # encrypted: anyone can read it, nobody can change it without the key.
    token = create_access_token(identity=str(user.id), additional_claims={"sid": str(session.id)})
    if session.jti is not None:
        session.previous_jti, session.rotated_at = session.jti, utcnow()
    session.jti = get_jti(token)
    return token


def token_response(user, session=None, status=200):
    if session is None:
        session = AuthSession(id=uuid.uuid4(), user_id=user.id)  # id now: the token needs it
        db.session.add(session)
    token = issue_token(user, session)
    db.session.commit()
    return jsonify({"access_token": token, "user": user.to_dict()}), status


def revoke(session, reason):
    session.revoked_at, session.revoked_reason = utcnow(), reason


@jwt.token_in_blocklist_loader
def token_revoked(header, payload):
    """Runs on every request with a token. True = refuse it (401)."""
    sid = payload.get("sid")
    if sid is None:
        # Issued before sessions existed: valid until it expires (7 days);
        # /auth/refresh turns it into a session.
        return False
    session = db.session.get(AuthSession, uuid.UUID(sid))
    if session is None or session.revoked_at is not None:
        return True
    if payload["jti"] == session.jti:
        return False
    if payload["jti"] == session.previous_jti and utcnow() - aware(session.rotated_at) < ROTATION_GRACE:
        return False
    # A replaced token came back: there's a copy of it somewhere.
    revoke(session, "reused")
    db.session.commit()
    return True


def read_credentials():
    data = load(CREDENTIALS)
    email = data["email"].lower()
    if not EMAIL_RE.match(email):
        raise ValidationError("email is not a valid address")
    # Note: Text() strips whitespace, so a password can't start or end with a
    # space. Accepted to keep one validator; the frontend does the same.
    return email, data["password"]


@bp.post("/register")
# Per client IP. Sign-up is rarer than login, so the limit is tighter.
@limiter.limit("10 per hour")
def register():
    email, password = read_credentials()
    if len(password) < 8:
        raise ValidationError("password must be at least 8 characters")
    if db.session.scalar(db.select(User).filter_by(email=email)):
        # This does reveal that the email is registered. Every sign-up form
        # without email verification has the same leak; accepted.
        return jsonify({"error": "email is already registered"}), 409

    # Werkzeug's default is scrypt with a random salt per password: slow on
    # purpose, so a leaked database is expensive to brute-force.
    user = User(email=email, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.flush()  # user.id, for the session
    return token_response(user, status=201)


@bp.post("/login")
@limiter.limit("10 per minute")
def login():
    email, password = read_credentials()
    user = db.session.scalar(db.select(User).filter_by(email=email))
    password_hash = user.password_hash if user else DUMMY_HASH
    if not check_password_hash(password_hash, password) or user is None:
        # Same message for "no such user" and "wrong password".
        return jsonify({"error": "invalid email or password"}), 401
    return token_response(user)


@bp.get("/me")
@jwt_required()
def me():
    user = db.session.get(User, current_user_id())
    if user is None:
        # Valid signature, but the account no longer exists.
        return jsonify({"error": "user not found"}), 401
    return jsonify(user.to_dict())


@bp.post("/refresh")
@limiter.limit("30 per minute")
@jwt_required()
def refresh():
    """Swap this device's token for a new one. Answers like login."""
    user = db.session.get(User, current_user_id())
    if user is None:
        return jsonify({"error": "user not found"}), 401
    sid = get_jwt().get("sid")
    session = db.session.get(AuthSession, uuid.UUID(sid)) if sid else None
    if session is not None and utcnow() - aware(session.created_at) > MAX_SESSION_AGE:
        revoke(session, "too_old")
        db.session.commit()
        return jsonify({"error": "session expired, log in again"}), 401
    return token_response(user, session)


@bp.post("/logout")
@jwt_required()
def logout():
    sid = get_jwt().get("sid")
    session = db.session.get(AuthSession, uuid.UUID(sid)) if sid else None
    if session is not None:
        revoke(session, "logout")
        db.session.commit()
    return "", 204


@bp.post("/logout-all")
@jwt_required()
def logout_all():
    """Every device of this user, this one included."""
    sessions = db.session.scalars(
        db.select(AuthSession).filter_by(user_id=current_user_id(), revoked_at=None)
    )
    for session in sessions:
        revoke(session, "logout_all")
    db.session.commit()
    return "", 204
