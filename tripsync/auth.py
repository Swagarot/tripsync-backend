import re
import uuid

from flask import Blueprint, jsonify
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, limiter
from .models import User
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


def token_response(user, status=200):
    # identity becomes the token's 'sub' claim. The token is signed, not
    # encrypted: anyone can read it, nobody can change it without the key.
    token = create_access_token(identity=str(user.id))
    return jsonify({"access_token": token, "user": user.to_dict()}), status


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
    db.session.commit()
    return token_response(user, 201)


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
