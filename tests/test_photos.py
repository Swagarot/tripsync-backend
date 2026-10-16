"""Your photos of a stop (photos.py), with storage kept in memory."""
import io
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from tripsync import photos, storage


@pytest.fixture
def store(app):
    memory = storage.MemoryStorage()
    app.config["PHOTO_STORAGE"] = memory
    yield memory
    app.config["PHOTO_STORAGE"] = None


@pytest.fixture
def stop(client, alice):
    trip = client.post("/trips", json={"name": "T"}, headers=alice).json
    day = client.post(f"/trips/{trip['id']}/days", json={}, headers=alice).json
    s = client.post(f"/days/{day['id']}/stops", json={"name": "Senso-ji"}, headers=alice).json
    return {"trip": trip["id"], "stop": s["id"]}


def image_bytes(size=(3000, 1500), fmt="JPEG", gps=False):
    img = Image.new("RGB", size, (200, 80, 40))
    out = io.BytesIO()
    if gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (35.0, 42.0, 53.0)}  # GPSInfo: where the photo was taken
        img.save(out, fmt, exif=exif)
    else:
        img.save(out, fmt)
    return out.getvalue()


def upload(client, headers, stop_id, data, name="photo.jpg"):
    return client.post(
        f"/stops/{stop_id}/photos", data={"file": (io.BytesIO(data), name)},
        headers=headers, content_type="multipart/form-data",
    )


def get_stop(client, headers, ids):
    trip = client.get(f"/trips/{ids['trip']}", headers=headers).json
    return trip["days"][0]["stops"][0]


def test_upload_resizes_makes_thumb_and_strips_location(client, alice, stop, store):
    res = upload(client, alice, stop["stop"], image_bytes(gps=True))
    assert res.status_code == 201
    assert (res.json["width"], res.json["height"]) == (2048, 1024)
    full = Image.open(io.BytesIO(store.objects[f"photos/{res.json['id']}/full.jpg"][0]))
    thumb = Image.open(io.BytesIO(store.objects[f"photos/{res.json['id']}/thumb.jpg"][0]))
    assert max(full.size) == 2048 and max(thumb.size) == 480
    assert not full.getexif()  # no GPS position (or any metadata) left
    assert [p["id"] for p in get_stop(client, alice, stop)["photos"]] == [res.json["id"]]


def test_png_is_stored_as_jpeg(client, alice, stop, store):
    res = upload(client, alice, stop["stop"], image_bytes((800, 600), "PNG"), "shot.png")
    assert res.status_code == 201
    assert store.objects[f"photos/{res.json['id']}/full.jpg"][1] == "image/jpeg"


def test_not_an_image_is_refused(client, alice, stop, store):
    res = upload(client, alice, stop["stop"], b"%PDF-1.7 definitely not a photo", "fake.jpg")
    assert res.status_code == 400
    assert store.objects == {}


def test_without_storage_uploads_are_off(client, alice, stop, app, monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    app.config["PHOTO_STORAGE"] = None
    assert upload(client, alice, stop["stop"], image_bytes((100, 100))).status_code == 503


def test_signed_links_work_and_cannot_be_forged(app, client, alice, stop, store):
    photo = upload(client, alice, stop["stop"], image_bytes((600, 400))).json
    ok = client.get(photo["thumb_url"].removeprefix("/api"))  # no login needed: the signature is the key
    assert ok.status_code == 200 and ok.mimetype == "image/jpeg"
    assert "immutable" in ok.headers["Cache-Control"]

    url = urlparse(photo["thumb_url"].removeprefix("/api"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    tampered = client.get(f"{url.path}?exp={q['exp']}&sig={'0' * 32}")
    other_size = client.get(url.path.replace("/thumb", "/full") + f"?exp={q['exp']}&sig={q['sig']}")
    with app.app_context():  # the signature uses the app's secret
        old_sig = photos._signature(photo["id"], "thumb", 1000)
    expired = client.get(f"{url.path}?exp=1000&sig={old_sig}")
    assert (tampered.status_code, other_size.status_code, expired.status_code) == (403, 403, 403)


def test_signed_link_is_stable_for_weeks():
    import datetime as dt
    a = photos._expiry(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc))
    b = photos._expiry(dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc))
    assert a == b == int(dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc).timestamp())
    assert photos._expiry(dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)) == int(
        dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc).timestamp())


def test_cover_photo_must_be_own_and_resets_on_delete(client, alice, stop, store):
    photo = upload(client, alice, stop["stop"], image_bytes((300, 300))).json
    res = client.patch(f"/stops/{stop['stop']}", json={"cover_photo_id": photo["id"]}, headers=alice)
    assert res.status_code == 200
    bad = client.patch(f"/stops/{stop['stop']}", json={"cover_photo_id": "00000000-0000-0000-0000-000000000000"},
                       headers=alice)
    assert bad.status_code == 400
    assert client.delete(f"/photos/{photo['id']}", headers=alice).status_code == 204
    s = get_stop(client, alice, stop)
    assert s["cover_photo_id"] is None and s["photos"] == []
    assert store.objects == {}  # the files are gone too
    assert client.get(photo["thumb_url"].removeprefix("/api")).status_code == 404


@pytest.mark.parametrize("body, ok", [
    ({"rating": 5}, True), ({"rating": 1, "visited": True}, True),
    ({"rating": 6}, False), ({"rating": 4.5}, False), ({"rating": True}, False), ({"visited": "yes"}, False),
])
def test_rating_and_visited(client, alice, stop, body, ok):
    res = client.patch(f"/stops/{stop['stop']}", json=body, headers=alice)
    assert (res.status_code == 200) is ok
    if ok:
        assert {k: res.json[k] for k in body} == body


def test_other_users_cannot_touch_photos(client, alice, bob, stop, store):
    photo = upload(client, alice, stop["stop"], image_bytes((300, 300))).json
    assert upload(client, bob, stop["stop"], image_bytes((300, 300))).status_code == 404
    assert client.delete(f"/photos/{photo['id']}", headers=bob).status_code == 404
    assert len(store.objects) == 2


def test_storage_bootstrap_is_repeatable(monkeypatch):
    calls = []
    answers = {
        "/v2/GetClusterStatus": (200, {"nodes": [{"id": "n1", "role": None}], "layoutVersion": 0}),
        "/v2/CreateBucket": (409, None),  # already there from an earlier run
        "/v2/ImportKey": (409, None),
        "/v2/GetBucketInfo?globalAlias=tripsync-photos": (200, {"id": "b1"}),
    }

    def fake_admin(method, path, body=None):
        calls.append((path, body))
        return answers.get(path, (200, None))

    monkeypatch.setattr(storage, "_admin", fake_admin)
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "GK00")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "s")
    steps = storage.bootstrap(4 * 1024**3)
    assert steps == ["layout applied (200)", "bucket exists", "key exists", "key allowed on bucket (200)"]
    assert ("/v2/ApplyClusterLayout", {"version": 1}) in calls
    assert ("/v2/AllowBucketKey", {"bucketId": "b1", "accessKeyId": "GK00",
                                   "permissions": {"read": True, "write": True, "owner": False}}) in calls
