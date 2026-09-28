"""Save alerts and their telemetry as one folder per incident."""

import json
import re
from datetime import datetime, timezone

from incident_response.collector import parse_time


def incident_id(alert):
    """Stable per alert occurrence, so Grafana's repeat notifications land in the same folder."""
    started = parse_time(alert.get("startsAt")) or datetime.now(timezone.utc)
    labels = alert.get("labels", {})
    endpoint = " ".join(filter(None, [labels.get("http_request_method"), labels.get("http_route")]))
    slug = re.sub(r"[^a-z0-9]+", "-", (endpoint or labels.get("alertname", "alert")).lower()).strip("-")
    fingerprint = alert.get("fingerprint") or "nofingerprint"
    return f"{started:%Y%m%dT%H%M%SZ}-{slug[:60]}-{fingerprint[:8]}"


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def save_incident(folder, alert, payload, context):
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "alert.json", {"alert": alert, "notification": payload})
    write_json(folder / "metrics.json", context.get("metrics"))
    write_json(folder / "logs.json", context["logs"])
    traces_dir = folder / "traces"
    traces_dir.mkdir(exist_ok=True)
    for trace in context["traces"]:
        write_json(traces_dir / f"{trace['trace_id']}.json", trace)
    (folder / "incident.md").write_text(render_markdown(folder.name, alert, context))


def record_update(folder, alert):
    """Append a later notification (repeat or resolved) for an existing incident."""
    with (folder / "updates.jsonl").open("a") as updates:
        updates.write(json.dumps({
            "received_at": datetime.now(timezone.utc).isoformat(),
            "status": alert.get("status"),
            "endsAt": alert.get("endsAt"),
            "values": alert.get("values"),
        }) + "\n")


def render_markdown(incident, alert, context):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    target = context["target"]
    endpoint = annotations.get("endpoint") or " ".join(filter(None, [target["method"], target["route"]]))
    lines = [
        f"# Incident {incident}",
        "",
        f"- **Alert:** {labels.get('alertname', '?')} ({alert.get('status', '?')}, severity {labels.get('severity', '?')})",
        f"- **Service:** {target['service']}",
        f"- **Endpoint:** `{endpoint or 'unknown'}`",
        f"- **Started:** {alert.get('startsAt', '?')}",
        f"- **Summary:** {annotations.get('summary', '')}",
        f"- **Description:** {annotations.get('description', '')}",
        f"- **Alert values:** {json.dumps(alert.get('values'))}",
        f"- **Dashboard:** {annotations.get('dashboard_url') or alert.get('dashboardURL') or '-'}",
        f"- **Alert rule:** {alert.get('generatorURL') or '-'}",
        f"- **Telemetry window:** {context['window']['start']} to {context['window']['end']}",
        "",
    ]
    if context["errors"]:
        lines += ["## Collection problems", ""]
        lines += [f"- {error}" for error in context["errors"]]
        lines.append("")

    metrics = context.get("metrics")
    lines += ["## Requests to this endpoint by status code (last 15 minutes)", ""]
    if metrics and metrics["requests_by_status_last_15m"]:
        lines += ["| Status | Requests |", "| --- | --- |"]
        for status, count in sorted(metrics["requests_by_status_last_15m"].items()):
            lines.append(f"| {status} | {count:.0f} |")
    else:
        lines.append("No data.")
    lines.append("")

    lines += [f"## Logs ({len(context['logs'])})", ""]
    if context["logs"]:
        lines.append("```")
        for entry in context["logs"]:
            meta = entry["metadata"]
            level = meta.get("severity_text") or meta.get("detected_level", "?")
            trace = f"  trace_id={meta['trace_id']}" if meta.get("trace_id") else ""
            lines.append(f"{entry['timestamp']} {level.upper():7} {entry['line']}{trace}")
        lines += ["```", ""]
        exceptions = {}
        for entry in context["logs"]:
            meta = entry["metadata"]
            if meta.get("exception_stacktrace"):
                key = (meta.get("exception_type"), meta.get("exception_message"))
                exceptions.setdefault(key, meta["exception_stacktrace"])
        for (exc_type, message), stacktrace in exceptions.items():
            lines += [f"### Exception: {exc_type}: {message}", "", "```", stacktrace.rstrip(), "```", ""]
    else:
        lines += ["No logs found.", ""]

    lines += [f"## Traces ({len(context['traces'])})", ""]
    for trace in context["traces"]:
        lines += [f"### Trace {trace['trace_id']}", "", "```"]
        lines += render_span_tree(trace["spans"])
        lines += ["```", ""]
    if not context["traces"]:
        lines += ["No error traces found.", ""]

    lines += [
        "## Files",
        "",
        "- `alert.json`: the alert and the full Grafana notification",
        "- `metrics.json`: the Prometheus query and result",
        "- `logs.json`: log lines with all their metadata",
        "- `traces/<trace_id>.json`: every span with attributes and events",
        "",
    ]
    return "\n".join(lines)


def render_span_tree(spans):
    ids = {span["span_id"] for span in spans}
    children = {}
    for span in spans:
        parent = span["parent_span_id"] if span["parent_span_id"] in ids else None
        children.setdefault(parent, []).append(span)

    lines = []

    def walk(parent, depth):
        for span in children.get(parent, []):
            status = span["status"].get("code", "")
            status = " ERROR" if "ERROR" in str(status) else ""
            attributes = ", ".join(f"{k}={v}" for k, v in span["attributes"].items())
            lines.append(f"{'  ' * depth}{span['name']} [{span['duration_ms']} ms]{status} {{{attributes}}}")
            for event in span["events"]:
                a = event["attributes"]
                if event["name"] == "exception":
                    lines.append(f"{'  ' * depth}  ! {a.get('exception.type')}: {a.get('exception.message')}")
                else:
                    lines.append(f"{'  ' * depth}  - event {event['name']}")
            walk(span["span_id"], depth + 1)

    walk(None, 0)
    return lines
