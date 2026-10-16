import os
import uuid

import pytest

from tripsync import create_app
from tripsync.extensions import db, limiter


@pytest.fixture(scope="session")
def app():
    # Locally: an in-memory SQLite database, nothing to install.
    # In Jenkins: TEST_DATABASE_URL points at a throwaway Postgres container,
    # migrated with `flask db upgrade` first, so the tests run on the exact
    # schema production gets (create_all() then finds the tables and does nothing).
    # One app per test session, because the Prometheus counters are process-wide
    # and can't be registered twice.
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": os.environ.get("TEST_DATABASE_URL", "sqlite://"),
        "JWT_SECRET_KEY": "test-secret-key-at-least-32-bytes-long",
    })
    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture(autouse=True)
def clean_state(app):
    # Every test starts with empty tables and fresh rate-limit counters.
    yield
    with app.app_context():
        db.session.rollback()
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()
        limiter.reset()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_user(client):
    """Register a new user and return the Authorization header for them."""
    def make(email=None, password="correct-horse"):
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        res = client.post("/auth/register", json={"email": email, "password": password})
        assert res.status_code == 201, res.json
        return {"Authorization": f"Bearer {res.json['access_token']}"}
    return make


@pytest.fixture
def alice(make_user):
    return make_user("alice@example.com")


@pytest.fixture
def bob(make_user):
    return make_user("bob@example.com")
