# tripsync-backend

Flask API for TripSync: accounts (JWT), trips → days → stops, Postgres via
SQLAlchemy + Alembic migrations. Deployed by this repo's Jenkinsfile with the
Helm chart from `tripsync-devops`.

## Layout

| Path | What |
|---|---|
| `app.py` | Entry point (`gunicorn app:app`, `flask ...`) |
| `tripsync/__init__.py` | `create_app()`: config, extensions, error handlers, metrics |
| `tripsync/models.py` | Tables: users, trips, days, stops (UUID ids, soft delete) |
| `tripsync/auth.py` | `/auth/register`, `/auth/login`, `/auth/me` |
| `tripsync/trips.py` | Trip/day/stop CRUD + reorder; every lookup is owner-checked |
| `tripsync/validation.py` | Request body validation |
| `tripsync/places.py` | Place search/details via OpenStreetMap services + Wikipedia, cached |
| `migrations/` | Alembic migrations (`flask db ...`) |
| `tests/` | pytest; `test_isolation.py` proves users can't reach each other's data |

## API (paths as Flask sees them; the Ingress adds `/api` in front)

```
POST   /auth/register  /auth/login     {email, password} -> {access_token, user}
GET    /auth/me
GET    /trips                    POST /trips             {name, start_date?, end_date?, notes?, id?}
GET    /trips/<id>               PATCH / DELETE /trips/<id>     (GET includes days and stops)
POST   /trips/<id>/days          PUT /trips/<id>/days/order     {ids: [...]}
PATCH  /days/<id>                DELETE /days/<id>
POST   /days/<id>/stops          PUT /days/<id>/stops/order     {ids: [...]}
PATCH  /stops/<id>               DELETE /stops/<id>             (PATCH {day_id} moves a stop)
GET    /places/search?q=&lat=&lng=   place search (Photon / OpenStreetMap)
GET    /places/<N|W|R><osm id>       details: hours, website, photo, description
GET    /health  /health/live  /health/ready  /metrics   (/metrics is not on the Ingress)
```

## Local development

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # (bin/ on Linux/macOS)
.venv/Scripts/python -m pytest          # in-memory SQLite, nothing else needed
.venv/Scripts/flask --app app db upgrade && .venv/Scripts/python app.py      # SQLite file, port 5000
```

Full stack with Postgres: `docker compose up --build` in `tripsync-devops`, then
open http://localhost:8080.

## Changing the database schema

1. Edit `tripsync/models.py`.
2. Against a database at the current schema: `flask db migrate -m "what changed"`.
3. Read the generated file in `migrations/versions/`, then commit it.

Deploys run `flask db upgrade` in an init container before the new pod starts,
while the old pod still serves. So a migration must keep the old code working:
add columns as nullable, and drop columns only in a later release.
