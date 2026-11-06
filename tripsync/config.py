import os
from datetime import timedelta

from sqlalchemy import URL


def database_url():
    # In Kubernetes the chart passes the connection as separate env vars (the
    # password comes from a Secret). Building the URL here means the password
    # never has to be URL-encoded by hand and never sits in one long string.
    if os.environ.get("POSTGRES_HOST"):
        return URL.create(
            "postgresql+psycopg",  # SQLAlchemy dialect + the psycopg 3 driver
            username=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database=os.environ["POSTGRES_DB"],
        )
    # Anything else (tests, quick local runs) can pass a full URL. The fallback
    # is a SQLite file, so `flask run` works on a laptop without Postgres.
    return os.environ.get("DATABASE_URL", "sqlite:///tripsync-dev.db")


class Config:
    SQLALCHEMY_DATABASE_URI = database_url()
    # Check a pooled connection with a cheap round trip before using it, so a
    # Postgres restart doesn't surface as one failed request per stale connection.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Signs the login tokens. Anyone who knows it can mint a token for any
    # user, so in the cluster it comes from a Secret (see the Helm chart).
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
    # One long-lived token per device instead of short access + refresh tokens:
    # the app is meant to work offline for days, and a token that expires
    # mid-trip would lock the user out of syncing. Every online start swaps it
    # for a new one (auth.py), so a stolen copy dies at the next refresh.
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=7)

    # Biggest request Flask accepts: a phone photo is 2-8 MB. (ingress-nginx has
    # its own limit, raised for /api in the chart.)
    MAX_CONTENT_LENGTH = 15 * 1024 * 1024

    # Rate-limit counters live in this process's memory. Fine with one Gunicorn
    # worker and one replica; with more replicas each pod counts separately
    # (then point this at Redis).
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_HEADERS_ENABLED = True
