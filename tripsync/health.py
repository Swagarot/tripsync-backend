from flask import Blueprint, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .extensions import db, limiter

bp = Blueprint("health", __name__)
# Kubernetes probes these every few seconds; never rate-limit them.
limiter.exempt(bp)


@bp.get("/health")
def health():
    # What the frontend calls to show "Backend status".
    return jsonify({"status": "ok"})


# Liveness probe: "is the process stuck?" Keep it shallow. A failure makes
# Kubernetes restart the pod, so it must NOT depend on the database: a DB
# outage would otherwise restart every backend pod in a loop, fixing nothing.
@bp.get("/health/live")
def live():
    return jsonify({"status": "alive"})


# Readiness probe: "can this pod serve traffic right now?" A failure only takes
# the pod out of the Service (no restart) until the database is back.
@bp.get("/health/ready")
def ready():
    try:
        db.session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"status": "database unavailable"}), 503
    return jsonify({"status": "ready"})
