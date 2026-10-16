import logging

from flask import Flask, jsonify
from prometheus_flask_exporter import PrometheusMetrics
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import db, jwt, limiter, migrate
from .validation import ValidationError

log = logging.getLogger(__name__)


def path(request):
    """Metric label for a request: the route template, not the real URL.

    The exporter's default label is the literal path, so /trips/<every trip id>
    would each become a separate Prometheus time series, growing forever
    ("high cardinality"). The template (/trips/<uuid:trip_id>) is a fixed set.
    The function is named `path` because the exporter names the label after it,
    so the alert rules and dashboard that filter on `path` keep working.
    """
    return request.url_rule.rule if request.url_rule else "unmatched"


# One exporter for the whole process (its counters are process-global).
metrics = PrometheusMetrics.for_app_factory(group_by=path)


def create_app(overrides=None):
    # Application factory: tests and the Flask CLI each build an app with
    # their own settings instead of sharing one created at import time.
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config.update(overrides or {})

    if not app.config["JWT_SECRET_KEY"]:
        if not str(app.config["SQLALCHEMY_DATABASE_URI"]).startswith("sqlite"):
            raise RuntimeError("JWT_SECRET_KEY must be set when running against Postgres")
        # Laptop-only convenience. Tokens stop working whenever this restarts.
        log.warning("JWT_SECRET_KEY not set, using an insecure development key")
        app.config["JWT_SECRET_KEY"] = "dev-only-insecure-key-do-not-use-in-production"

    # Trust exactly one proxy hop (ingress-nginx, or the compose gateway): take
    # the client IP from the last X-Forwarded-For entry it added. Trusting more
    # hops would let a client fake its IP by sending its own X-Forwarded-For,
    # and dodge the login rate limit.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    db.init_app(app)
    # render_as_batch lets Alembic alter columns on SQLite too (it rebuilds the
    # table), so migrations also run against the laptop's SQLite file.
    migrate.init_app(app, db, render_as_batch=True)
    jwt.init_app(app)
    limiter.init_app(app)
    metrics.init_app(app)

    # (importing models registers the tables)
    from . import auth, extras, health, models, places, routes, travelinfo, trips  # noqa: F401
    app.register_blueprint(health.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(trips.bp)
    app.register_blueprint(places.bp)
    app.register_blueprint(routes.bp)
    app.register_blueprint(extras.bp)
    app.register_blueprint(travelinfo.bp)

    register_error_handlers(app)
    return app


def register_error_handlers(app):
    # Every error is JSON shaped {"error": "..."}, so the frontend can show
    # error messages without guessing the format.

    @app.errorhandler(ValidationError)
    def bad_request(e):
        return jsonify({"error": str(e)}), 400

    @app.errorhandler(IntegrityError)
    def conflict(e):
        # A database constraint caught something the code didn't, e.g. two
        # sign-ups with the same email at the same moment.
        db.session.rollback()
        return jsonify({"error": "conflicts with existing data"}), 409

    @app.errorhandler(429)
    def too_many_requests(e):
        # Flask-Limiter's own text is the rule ("10 per 1 minute").
        return jsonify({"error": "Too many attempts. Wait a minute and try again."}), 429

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify({"error": e.description}), e.code

    # flask-jwt-extended's own responses use {"msg": ...}; align them.
    @jwt.unauthorized_loader
    def missing_token(reason):
        return jsonify({"error": "login required"}), 401

    @jwt.invalid_token_loader
    def bad_token(reason):
        return jsonify({"error": "invalid token"}), 401

    @jwt.expired_token_loader
    def expired_token(header, payload):
        return jsonify({"error": "session expired, log in again"}), 401
