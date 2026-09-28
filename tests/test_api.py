from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import main


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_express_order_placed_at_month_end(client):
    # Seeded express-1002 is placed on the last day of the previous month.
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    order = response.json()
    placed_at = datetime.fromisoformat(order["created_at"])
    expected = (placed_at + timedelta(days=2)).date().isoformat()
    assert order["estimated_delivery"] == expected


def test_express_estimate_rolls_over_month_end():
    for created_at, expected in (
        ("2026-08-31T12:00:00+00:00", "2026-09-02"),
        ("2026-09-30T12:00:00+00:00", "2026-10-02"),
        ("2026-12-31T12:00:00+00:00", "2027-01-02"),
        ("2027-02-27T12:00:00+00:00", "2027-03-01"),
    ):
        order = main.order_detail({"priority": "express", "created_at": created_at})
        assert order["estimated_delivery"] == expected


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_order_lookup_telemetry(client, otel):
    response = client.get("/api/orders/standard-1001")
    assert response.status_code == 200

    lookup = next(s for s in otel.spans.get_finished_spans() if s.name == "order.lookup")
    server = next(s for s in otel.spans.get_finished_spans() if s.name == "GET /api/orders/{order_id}")
    assert lookup.parent.span_id == server.context.span_id
    assert lookup.attributes["order.id"] == "standard-1001"
    assert lookup.attributes["order.found"] is True
    assert server.attributes["http.route"] == "/api/orders/{order_id}"
    assert server.attributes["http.response.status_code"] == 200

    [log] = [r.log_record for r in otel.logs.get_finished_logs() if r.log_record.attributes.get("order.id")]
    assert log.body == "Order standard-1001 found"
    assert log.trace_id == lookup.context.trace_id

    metrics = otel.collect_metrics()
    [request] = metrics["http.server.request.duration"]
    assert dict(request.attributes) == {
        "http.request.method": "GET",
        "http.route": "/api/orders/{order_id}",
        "http.response.status_code": 200,
    }
    assert request.count == 1
    [lookups] = metrics["order_tracker.order.lookups"]
    assert dict(lookups.attributes) == {"outcome": "found"} and lookups.value == 1


def test_missing_order_telemetry(client, otel):
    assert client.get("/api/orders/missing").status_code == 404

    lookup = next(s for s in otel.spans.get_finished_spans() if s.name == "order.lookup")
    assert lookup.attributes["order.found"] is False
    [log] = [r.log_record for r in otel.logs.get_finished_logs() if r.log_record.attributes.get("order.id")]
    assert log.severity_text == "WARN"
    assert log.trace_id == lookup.context.trace_id

    metrics = otel.collect_metrics()
    [request] = metrics["http.server.request.duration"]
    assert request.attributes["http.route"] == "/api/orders/{order_id}"
    assert request.attributes["http.response.status_code"] == 404
    [lookups] = metrics["order_tracker.order.lookups"]
    assert dict(lookups.attributes) == {"outcome": "not_found"}


def test_failed_lookup_is_recorded_as_500(client, otel, monkeypatch):
    def broken(_row):
        raise ValueError("boom")

    monkeypatch.setattr(main, "order_detail", broken)
    client_no_raise = TestClient(main.app, raise_server_exceptions=False)
    assert client_no_raise.get("/api/orders/standard-1001").status_code == 500

    spans = {s.name: s for s in otel.spans.get_finished_spans()}
    assert spans["order.lookup"].status.is_ok is False
    assert spans["GET /api/orders/{order_id}"].attributes["http.response.status_code"] == 500
    assert any(r.log_record.body.startswith("Unhandled error") for r in otel.logs.get_finished_logs())

    metrics = otel.collect_metrics()
    [request] = metrics["http.server.request.duration"]
    assert request.attributes["http.response.status_code"] == 500
    [lookups] = metrics["order_tracker.order.lookups"]
    assert dict(lookups.attributes) == {"outcome": "error"}


def test_unmatched_route_is_not_high_cardinality(client, otel):
    assert client.get("/nope/123").status_code == 404
    metrics = otel.collect_metrics()
    [request] = metrics["http.server.request.duration"]
    assert request.attributes["http.route"] == "unmatched"
