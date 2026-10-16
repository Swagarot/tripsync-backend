import uuid


def test_health_endpoints(client):
    assert client.get("/health").json == {"status": "ok"}
    assert client.get("/health/live").json == {"status": "alive"}
    res = client.get("/health/ready")
    assert res.status_code == 200
    assert res.json == {"status": "ready"}


def test_metrics_use_route_templates_not_ids(client, alice):
    # Two different trip IDs must land in ONE time series (see path() in
    # tripsync/__init__.py), or every trip would create new series forever.
    for _ in range(2):
        client.get(f"/trips/{uuid.uuid4()}", headers=alice)
    body = client.get("/metrics").get_data(as_text=True)
    # (Only the duration histogram carries the path label; the plain request
    # counter is labeled by method and status only.)
    series = 'flask_http_request_duration_seconds_count{method="GET",path="/trips/<uuid:trip_id>",status="404"}'
    assert f"{series} 2.0" in body
