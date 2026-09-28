# Incident Response

This service receives Grafana alerts at `POST /alerts` on port 8001. For each new firing alert, it:

1. Saves the context needed to understand the problem in `incidents/<id>/`: the affected endpoint, request counts by status code (Prometheus), the endpoint's logs and the logs of its error traces (Loki), and the error traces (Tempo).
2. Starts Claude Code in headless mode (`claude -p`) in the repository root. It tells the assistant to find the root cause, fix it, add a test, run the tests, and write `incidents/<id>/report.md`.

## Run it

Start the order-tracker stack first (`docker compose up --build -d --wait` in the repository root). Then, with `uv` and a logged-in `claude` CLI on the host:

```bash
cd incident-response
uv run --frozen python -m incident_response
```

Grafana is provisioned with an `incident-response` contact point and a notification policy that sends all alerts to `http://host.docker.internal:8001/alerts`, one notification group per endpoint. The service runs on the host, not in Compose, because the assistant needs the repository checkout and your Claude Code login.

To try it, request `GET /api/orders/express-1002` on the app (it returns 500 at month end). The alert rule evaluates every minute and Grafana waits 10 seconds before sending, so the incident folder appears within about 1 to 2 minutes.

## Incident folder

| File | Contents |
| --- | --- |
| `incident.md` | Summary for people and the assistant: alert, endpoint, request counts, log lines, stack traces, span trees |
| `alert.json` | The alert and the full Grafana notification |
| `metrics.json`, `logs.json`, `traces/<trace_id>.json` | The raw data behind `incident.md` |
| `updates.jsonl` | Later notifications for the same alert (repeats, resolved) |
| `prompt.md` | The prompt given to the assistant |
| `agent.json` | Assistant command, start and finish time, exit code |
| `agent.out`, `agent.err` | Assistant output (stream-json) and errors |
| `report.md` | Written by the assistant: root cause, fix, and verification |

The folder name combines the alert start time, the endpoint, and Grafana's fingerprint. Repeat and resolved notifications for the same alert are added to `updates.jsonl` and do not start another assistant. Assistants run one at a time because they edit the same checkout. If Loki, Tempo, or Prometheus cannot be reached, the problem is listed in `incident.md` and the assistant still starts.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_RESPONSE_HOST`, `INCIDENT_RESPONSE_PORT` | `0.0.0.0`, `8001` | Listen address |
| `INCIDENT_ALLOWED_NETWORKS` | `127.0.0.0/8,::1/128,10.215.24.0/24` | Clients allowed to post alerts: loopback and the Compose network. Update it if you change `ORDER_TRACKER_SUBNET`. |
| `INCIDENT_AGENT_COMMAND` | `claude -p --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools '…'` | Assistant command. The prompt is passed on stdin. Set it to an empty string to only save context. |
| `LOKI_URL`, `TEMPO_URL`, `PROMETHEUS_URL` | `http://127.0.0.1:3100`, `:3200`, `:9090` | Telemetry backends |
| `INCIDENTS_DIR` | `incident-response/incidents` | Where incidents are saved (ignored by git) |
| `INCIDENT_REPO_DIR` | the repository root | Working directory of the assistant |
| `INCIDENT_RESPONSE_URL` (Compose) | `http://host.docker.internal:8001/alerts` | Webhook URL Grafana posts to |

The default assistant command accepts file edits and allows only these Bash commands: `uv run …`, `git status`, `git diff`, and `git log`. Headless mode denies everything else, and the assistant does not commit. Review its changes with `git diff` and read `report.md`.

Alert content ends up in the assistant's prompt context, so only trusted senders should reach the endpoint. The service listens on all interfaces so that Grafana's container can reach it, and it rejects clients outside `INCIDENT_ALLOWED_NETWORKS`. If a host firewall blocks the Docker network from port 8001, allow it, for example `sudo ufw allow from 10.215.24.0/24 to any port 8001`.

## Tests

```bash
uv run --frozen pytest -q
```

The tests replace Loki, Tempo, and Prometheus with fakes, and replace the assistant with a script that records its prompt.
