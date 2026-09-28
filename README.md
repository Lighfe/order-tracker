# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop everything with `docker compose down`. Add `-v` only if you also want to delete the order data and stored telemetry.

## Telemetry

The app emits OpenTelemetry traces, metrics, and logs over OTLP/HTTP to an OpenTelemetry Collector. `docker compose up` starts the whole stack:

| Service | Role | Local URL |
| --- | --- | --- |
| `otel-collector` | Receives OTLP from the app and routes each signal to its backend | <http://127.0.0.1:4318> (HTTP), `127.0.0.1:4317` (gRPC) |
| `prometheus` | Metrics, received on its OTLP endpoint | <http://127.0.0.1:9090> |
| `loki` | Logs, received on its OTLP endpoint | <http://127.0.0.1:3100> |
| `tempo` | Traces | <http://127.0.0.1:3200> |
| `grafana` | Dashboards and Explore, with the three data sources provisioned | <http://127.0.0.1:3000> |

Open Grafana at <http://127.0.0.1:3000>. The home dashboard is **Order Tracker: Requests and Errors**. It shows request counts, 5xx and 4xx errors, the error rate, rates by route and status code, order lookup outcomes, and recent warning and error logs. Health checks (`/healthz`) are excluded. You can view it without logging in; sign in as `admin` / `admin` (`GRAFANA_ADMIN_PASSWORD`) to edit. In Explore, log lines link to their trace in Tempo, and spans link back to their logs in Loki.

Metrics are exported every 10 seconds (`OTEL_METRIC_EXPORT_INTERVAL`, in milliseconds). Loki needs about 15 seconds after startup before it accepts logs; the Collector retries until then. The `*_PORT` variables (`GRAFANA_PORT`, `PROMETHEUS_PORT`, `LOKI_PORT`, `TEMPO_PORT`, `OTLP_HTTP_PORT`, `OTLP_GRPC_PORT`) change the host ports.

All configuration is in [`observability/`](observability/): the Collector pipeline, the Prometheus, Loki, and Tempo configs, and Grafana provisioning. The dashboard is [`observability/grafana/dashboards/order-tracker.json`](observability/grafana/dashboards/order-tracker.json). Grafana loads it read-only, so to change it, edit the file (or export an edited copy from the UI) and save it there.

### Alerts

Grafana provisions one alert rule, **Order Tracker 5xx responses**, from [`observability/grafana/provisioning/alerting/order-tracker-alerts.yaml`](observability/grafana/provisioning/alerting/order-tracker-alerts.yaml). Every minute, it counts 5xx responses per endpoint (method + route, excluding `/healthz`) over the last 5 minutes. It fires as soon as an endpoint has at least one 5xx, with no pending period, and resolves after 5 minutes without one. Each firing alert carries the endpoint, the count, the 5-minute window, and a link to the dashboard filtered to that route (`dashboard_url`). It is also linked to the dashboard's **Server errors (5xx)** panel. When there are no 5xx series at all, for example on a fresh stack, the rule is Normal rather than No Data.

Check its state under **Alerting → Alert rules** in Grafana, or with `curl -s localhost:3000/api/prometheus/grafana/api/v1/rules`. Grafana sends alerts to the **incident-response** contact point, a webhook to the [incident-response service](incident-response/README.md) on port 8001 of the host ([`observability/grafana/provisioning/alerting/incident-response.yaml`](observability/grafana/provisioning/alerting/incident-response.yaml)). That service saves the endpoint, logs, and traces behind each alert and starts Claude Code in headless mode to investigate. Run it with `cd incident-response && uv run --frozen python -m incident_response`. To try it, request `GET /api/orders/express-1002`, which currently returns 500 at month end. Like the dashboard, the rule is read-only in the UI; edit the file and restart Grafana (`docker compose restart grafana`).

To print signals to stdout instead of sending them to the Collector, set the endpoint to an empty string:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT= docker compose up --build -d --wait app
docker compose logs app
```

| Signal | Name | Details |
| --- | --- | --- |
| Metric | `http.server.request.duration` (Prometheus: `http_server_request_duration_seconds`) | Histogram for every request, with `http.request.method`, `http.route` (route template, or `unmatched`), and `http.response.status_code` |
| Metric | `order_tracker.order.lookups` (Prometheus: `order_tracker_order_lookups_total`) | Counter with `outcome` = `found`, `not_found`, or `error` |
| Trace | `GET /api/orders/{order_id}` → `order.lookup` | Server span plus a lookup span with `order.id`, `order.found`, `order.priority`, and `order.status`. Unhandled exceptions are recorded on the span. |
| Log | `order_tracker` logger | `Order … found` (INFO), `Order … not found` (WARN), and `Unhandled error …` (ERROR, with stack trace), linked to the trace |

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
