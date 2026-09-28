import base64
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from incident_response.collector import Collector, route_regex
from incident_response.config import Settings, parse_networks
from incident_response.main import create_app


TRACE_ID = "f3521b74455d8fbbcfc35e3cd2a2c95d"
SERVER_SPAN, LOOKUP_SPAN = "adc8e673e23c4501", "37f2263d29cefe57"
STACKTRACE = (
    'Traceback (most recent call last):\n  File "/app/app/main.py", line 62, in order_detail\n'
    "ValueError: day is out of range for month\n"
)


def alert(status="firing", **overrides):
    return {
        "status": status,
        "labels": {
            "alertname": "Order Tracker 5xx responses",
            "http_request_method": "GET",
            "http_route": "/api/orders/{order_id}",
            "service": "order-tracker",
            "severity": "critical",
        },
        "annotations": {
            "summary": "1 5xx response(s) from GET /api/orders/{order_id} in the last 5 minutes",
            "endpoint": "GET /api/orders/{order_id}",
            "dashboard_url": "http://127.0.0.1:3000/d/order-tracker/order-tracker",
        },
        "startsAt": "2026-09-28T21:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "3c4b0f5e6d7a8b9c",
        "values": {"A": 1, "C": 1},
        **overrides,
    }


def notification(*alerts):
    return {"receiver": "incident-response", "status": alerts[0]["status"], "alerts": list(alerts)}


def b64(hex_id):
    return base64.b64encode(bytes.fromhex(hex_id)).decode()


def span(span_id, parent, name, attributes, exception=False):
    return {
        "traceId": b64(TRACE_ID), "spanId": b64(span_id), "parentSpanId": b64(parent) if parent else "",
        "name": name, "kind": "SPAN_KIND_SERVER",
        "startTimeUnixNano": "1790629264818117271", "endTimeUnixNano": "1790629264830423012",
        "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in attributes.items()],
        "status": {"code": "STATUS_CODE_ERROR"} if exception else {},
        "events": [{"name": "exception", "attributes": [
            {"key": "exception.type", "value": {"stringValue": "ValueError"}},
            {"key": "exception.message", "value": {"stringValue": "day is out of range for month"}},
        ]}] if exception else [],
    }


class FakeBackends:
    """Answers like Prometheus, Loki, and Tempo, and remembers the queries it got."""

    def __init__(self):
        self.queries = []

    def __call__(self, request):
        params = dict(request.url.params)
        self.queries.append((request.url.path, params))
        if request.url.path == "/api/v1/query":
            return httpx.Response(200, json={"data": {"result": [
                {"metric": {"http_response_status_code": "500"}, "value": [0, "3"]},
                {"metric": {"http_response_status_code": "200"}, "value": [0, "12"]},
            ]}})
        if request.url.path == "/loki/api/v1/query_range":
            if "trace_id=~" in params["query"]:
                stream = {"service_name": "order-tracker", "severity_text": "INFO", "trace_id": TRACE_ID}
                values = [["1790629264820000000", "Order express-1002 lookup started"]]
            else:
                stream = {
                    "service_name": "order-tracker", "severity_text": "ERROR", "trace_id": TRACE_ID,
                    "url_path": "/api/orders/express-1002", "exception_type": "ValueError",
                    "exception_message": "day is out of range for month",
                    "exception_stacktrace": STACKTRACE,
                }
                values = [["1790629264827053312", "Unhandled error for GET /api/orders/express-1002"]]
            return httpx.Response(200, json={"data": {"result": [{"stream": stream, "values": values}]}})
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": TRACE_ID}]})
        if request.url.path == f"/api/v2/traces/{TRACE_ID}":
            return httpx.Response(200, json={"trace": {"resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "order-tracker"}}]},
                "scopeSpans": [{"spans": [
                    span(SERVER_SPAN, None, "GET /api/orders/{order_id}",
                         {"http.route": "/api/orders/{order_id}"}, exception=True),
                    span(LOOKUP_SPAN, SERVER_SPAN, "order.lookup", {"order.id": "express-1002"}, exception=True),
                ]}],
            }]}})
        return httpx.Response(404)


class InlineExecutor:
    def submit(self, func, *args):
        func(*args)

    def shutdown(self, **_kwargs):
        pass


# Stands in for the assistant: records the prompt it gets on stdin and the directory it runs in.
FAKE_AGENT = (
    "import json, os, sys; "
    "json.dump({'prompt': sys.stdin.read(), 'cwd': os.getcwd()}, open('agent-called.json', 'w'))"
)


@pytest.fixture
def backends():
    return FakeBackends()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        incidents_dir=tmp_path / "incidents",
        repo_dir=tmp_path,
        agent_command=[sys.executable, "-c", FAKE_AGENT],
        allowed_networks=[],
    )


@pytest.fixture
def client(settings, backends):
    collector = Collector(settings, httpx.Client(transport=httpx.MockTransport(backends)))
    with TestClient(create_app(settings, collector, InlineExecutor())) as test_client:
        yield test_client


def only_incident(settings):
    [folder] = settings.incidents_dir.iterdir()
    return folder


def test_firing_alert_saves_context_and_starts_agent(client, settings, backends):
    response = client.post("/alerts", json=notification(alert()))

    assert response.status_code == 202
    [result] = response.json()["incidents"]
    assert result["action"] == "created"
    folder = only_incident(settings)
    assert folder.name == result["incident"] == "20260928T210000Z-get-api-orders-order-id-3c4b0f5e"

    report = (folder / "incident.md").read_text()
    assert "`GET /api/orders/{order_id}`" in report
    assert "Unhandled error for GET /api/orders/express-1002" in report
    assert "ValueError: day is out of range for month" in report
    assert 'line 62, in order_detail' in report
    assert "| 500 | 3 |" in report
    assert f"### Trace {TRACE_ID}" in report
    assert "  order.lookup [" in report  # nested under the server span
    assert "Order express-1002 lookup started" in report  # found through the trace ID

    trace = json.loads((folder / "traces" / f"{TRACE_ID}.json").read_text())
    assert [s["span_id"] for s in trace["spans"]] == [SERVER_SPAN, LOOKUP_SPAN]
    assert json.loads((folder / "alert.json").read_text())["alert"]["fingerprint"] == "3c4b0f5e6d7a8b9c"
    assert len(json.loads((folder / "logs.json").read_text())) == 2

    loki_query = next(p["query"] for path, p in backends.queries if path == "/loki/api/v1/query_range")
    assert loki_query == (
        '{service_name="order-tracker"} | http_request_method="GET" '
        "| url_path=~`^/api/orders/[^/]+$`"
    )
    tempo_query = next(p["q"] for path, p in backends.queries if path == "/api/search")
    assert 'span.http.route="/api/orders/{order_id}"' in tempo_query and "status=error" in tempo_query

    called = json.loads((settings.repo_dir / "agent-called.json").read_text())
    assert called["cwd"] == str(settings.repo_dir)
    assert f"incidents/{folder.name}" in called["prompt"]
    status = json.loads((folder / "agent.json").read_text())
    assert status["exit_code"] == 0


def test_repeat_and_resolved_notifications_do_not_start_another_agent(client, settings):
    client.post("/alerts", json=notification(alert()))
    (settings.repo_dir / "agent-called.json").unlink()

    repeat = client.post("/alerts", json=notification(alert()))
    resolved = client.post("/alerts", json=notification(alert("resolved", endsAt="2026-09-28T21:10:00Z")))

    assert repeat.json()["incidents"][0]["action"] == "updated"
    assert resolved.json()["incidents"][0]["action"] == "updated"
    assert not (settings.repo_dir / "agent-called.json").exists()
    updates = (only_incident(settings) / "updates.jsonl").read_text().splitlines()
    assert [json.loads(u)["status"] for u in updates] == ["firing", "resolved"]


def test_resolved_alert_without_incident_is_ignored(client, settings):
    response = client.post("/alerts", json=notification(alert("resolved")))
    assert response.json()["incidents"][0]["action"] == "ignored"
    assert list(settings.incidents_dir.iterdir()) == []


def test_unreachable_backends_still_save_alert_and_start_agent(settings):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    collector = Collector(settings, httpx.Client(transport=httpx.MockTransport(down)))
    with TestClient(create_app(settings, collector, InlineExecutor())) as client:
        client.post("/alerts", json=notification(alert()))

    report = (only_incident(settings) / "incident.md").read_text()
    assert "## Collection problems" in report and "ConnectError" in report
    assert (settings.repo_dir / "agent-called.json").exists()


def test_no_agent_command_only_saves(settings, backends):
    settings.agent_command = []
    collector = Collector(settings, httpx.Client(transport=httpx.MockTransport(backends)))
    with TestClient(create_app(settings, collector, InlineExecutor())) as client:
        client.post("/alerts", json=notification(alert()))
    assert (only_incident(settings) / "incident.md").exists()
    assert not (settings.repo_dir / "agent-called.json").exists()


def test_rejects_clients_outside_allowed_networks(client, settings):
    settings.allowed_networks = parse_networks("127.0.0.0/8,10.215.24.0/24")
    assert client.post("/alerts", json=notification(alert())).status_code == 403  # "testclient"
    assert settings.client_allowed("10.215.24.5")
    assert not settings.client_allowed("192.168.1.20")


def test_rejects_payloads_that_are_not_grafana_notifications(client):
    assert client.post("/alerts", content=b"nope").status_code == 400
    assert client.post("/alerts", json={"foo": 1}).status_code == 422


def test_route_regex():
    assert route_regex("/api/orders/{order_id}") == r"^/api/orders/[^/]+$"
    assert route_regex("/healthz") == "^/healthz$"
