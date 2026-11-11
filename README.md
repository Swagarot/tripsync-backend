# tripsync-backend

Flask API for TripSync: accounts with per-device login sessions (JWT), trips →
days → stops, shared trips with roles, ideas, expenses (split and settled),
checklist, lodgings, photos, place search, routes, exchange rates and weather.
PostgreSQL through SQLAlchemy, schema changes as Alembic migrations, photos in
S3-compatible storage (Garage in the cluster, AWS S3 on EKS). Built, tested and
deployed by this repo's Jenkinsfile with the Helm chart from `tripsync-devops`.

## Layout

| Path | What |
|---|---|
| `app.py` | Entry point (`gunicorn app:app`, `flask ...`) |
| `tripsync/__init__.py` | `create_app()`: config, extensions, blueprints, JSON error handlers, metrics |
| `tripsync/config.py` | Settings from environment variables (database, JWT key, limits) |
| `tripsync/models.py` | Tables: users, sessions, trips, members, days, stops, ideas, expenses, checklist, lodgings, photos (UUID ids, soft delete) |
| `tripsync/access.py` | Who may do what with a trip: one place for every visibility (404) and role (403) check |
| `tripsync/validation.py` | Request body validation (types, lengths, ranges, required fields) |
| `tripsync/auth.py` | Register, login, sessions with token rotation and reuse detection, logout |
| `tripsync/trips.py` | Trip / day / stop CRUD and reordering |
| `tripsync/extras.py` | Ideas, expenses, checklist and lodgings (one generic CRUD) |
| `tripsync/sharing.py` | Signed invite links, members, roles, leaving |
| `tripsync/photos.py`, `storage.py` | Photo upload (resized, thumbnails), HMAC-signed photo URLs, S3 storage, `flask storage-init` |
| `tripsync/places.py` | Place search and details via OpenStreetMap services + Wikipedia, cached |
| `tripsync/routes.py`, `transit.py` | Walk/bike/drive routes (OpenRouteService, key from `ORS_API_KEY`) and public transit (Transitous), cached |
| `tripsync/travelinfo.py` | Exchange rates (ECB via Frankfurter) and weather (Open-Meteo), cached |
| `tripsync/health.py` | Liveness and readiness endpoints for the Kubernetes probes |
| `migrations/` | Alembic migrations (`flask db ...`) |
| `tests/` | pytest (235 tests); `test_isolation.py` proves users can't reach each other's data |

## API (paths as Flask sees them; the Ingress adds `/api` in front)

Every endpoint except register, login and health needs `Authorization: Bearer
<token>`. Errors are JSON `{"error": "..."}` with a fitting status: 400
validation, 401 not logged in, 403 role too low, 404 not found (also for other
users' trips), 409 conflict, 413 too large, 429 rate limited.

```
Accounts
POST   /auth/register  /auth/login     {email, password} -> {access_token, user}
GET    /auth/me
POST   /auth/refresh                   new token for this device, old one stops working
POST   /auth/logout    /auth/logout-all

Trips, days, stops
GET    /trips                    POST /trips             {name, start_date?, end_date?, notes?, id?}
GET    /trips/<id>               PATCH / DELETE /trips/<id>     (GET includes everything in the trip)
POST   /trips/<id>/days          PUT /trips/<id>/days/order     {ids: [...]}
PATCH  /days/<id>                DELETE /days/<id>
POST   /days/<id>/stops          PUT /days/<id>/stops/order     {ids: [...]}
PATCH  /stops/<id>               DELETE /stops/<id>             (PATCH {day_id} moves a stop)

Ideas, expenses, checklist, lodgings   (<kind> = ideas | expenses | checklist | lodgings)
POST   /trips/<id>/<kind>        PATCH / DELETE /<kind>/<id>

Sharing
POST   /trips/<id>/invites       {role: editor|viewer} -> link token (owner)
POST   /trips/<id>/invites/reset every earlier link stops working (owner)
GET    /invites/<token>          POST /invites/<token>/accept
PATCH  /trips/<id>/members/<user>   {role}     DELETE /trips/<id>/members/<user>   (remove, or leave)

Photos
POST   /stops/<id>/photos        multipart field "file"; resized + thumbnail
GET    /photos/<id>/full|thumb?exp=&sig=      signed URL, works in <img src>
DELETE /photos/<id>

Places, routes, travel info
GET    /places/search?q=&lat=&lng=   place search (Photon / OpenStreetMap)
GET    /places/<N|W|R><osm id>       details: hours, website, photo, description
GET    /places/photo?url=            Wikimedia photo through the backend
POST   /routes  {mode: walk|bike|drive|transit, points: [[lat, lng], ...], date?}
GET    /rates?base=ILS           GET /weather?lat=&lng=

Health and metrics
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
