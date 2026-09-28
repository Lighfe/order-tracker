"""Collect the telemetry behind an alert from Prometheus, Loki, and Tempo."""

import base64
import re
from datetime import datetime, timedelta, timezone

import httpx


def parse_time(value):
    """Parse a Grafana timestamp. Grafana uses year 0001 for "not set"."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.year > 1 else None


def route_regex(route):
    """/api/orders/{order_id} -> ^/api/orders/[^/]+$"""
    parts = re.split(r"\{[^}/]+\}", route)
    return "^" + "[^/]+".join(re.escape(part) for part in parts) + "$"


def to_hex_id(value):
    """Tempo's v2 JSON encodes trace and span IDs as base64."""
    if not value:
        return ""
    if re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", value):
        return value
    return base64.b64decode(value).hex()


def attribute_value(value):
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in value:
            return value[kind]
    if "arrayValue" in value:
        return [attribute_value(v) for v in value["arrayValue"].get("values", [])]
    return value


def attributes_dict(attributes):
    return {a["key"]: attribute_value(a.get("value", {})) for a in attributes or []}


class Collector:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=10)

    def collect(self, alert, now=None):
        """Return everything we could find about one alert. Failures are recorded, not raised."""
        now = now or datetime.now(timezone.utc)
        labels = alert.get("labels", {})
        started = parse_time(alert.get("startsAt")) or now
        start = started - timedelta(minutes=self.settings.lookback_minutes)
        end = max(now, started) + timedelta(minutes=1)
        target = {
            "service": labels.get("service") or self.settings.default_service,
            "method": labels.get("http_request_method"),
            "route": labels.get("http_route"),
        }
        context = {
            "target": target,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "errors": [],
        }

        context["metrics"] = self._safe(context, "prometheus", self.request_counts, target, end)
        logs = self._safe(context, "loki", self.endpoint_logs, target, start, end) or []
        trace_ids = self._safe(context, "tempo", self.error_trace_ids, target, start, end) or []
        for entry in logs:
            trace_id = entry["metadata"].get("trace_id")
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
        trace_ids = trace_ids[: self.settings.max_traces]

        context["traces"] = []
        for trace_id in trace_ids:
            trace = self._safe(context, f"tempo trace {trace_id}", self.trace, trace_id)
            if trace:
                context["traces"].append(trace)
        if trace_ids:
            trace_logs = self._safe(context, "loki", self.trace_logs, target, trace_ids, start, end) or []
            seen = {(e["timestamp"], e["line"]) for e in logs}
            logs += [e for e in trace_logs if (e["timestamp"], e["line"]) not in seen]
        context["logs"] = sorted(logs, key=lambda e: e["timestamp"])
        return context

    def _safe(self, context, source, func, *args):
        try:
            return func(*args)
        except (httpx.HTTPError, ValueError, KeyError) as error:
            context["errors"].append(f"{source}: {type(error).__name__}: {error}")
            return None

    # Prometheus

    def request_counts(self, target, at):
        matchers = [f'job="{target["service"]}"']
        if target["route"]:
            matchers.append(f'http_route="{target["route"]}"')
        if target["method"]:
            matchers.append(f'http_request_method="{target["method"]}"')
        query = (
            "round(sum by (http_response_status_code) (increase("
            f'http_server_request_duration_seconds_count{{{", ".join(matchers)}}}[15m])))'
        )
        response = self.client.get(
            f"{self.settings.prometheus_url}/api/v1/query",
            params={"query": query, "time": at.timestamp()},
        )
        response.raise_for_status()
        result = response.json()["data"]["result"]
        return {
            "query": query,
            "requests_by_status_last_15m": {
                r["metric"].get("http_response_status_code", "?"): float(r["value"][1]) for r in result
            },
        }

    # Loki

    def endpoint_logs(self, target, start, end):
        query = f'{{service_name="{target["service"]}"}}'
        if target["method"]:
            query += f' | http_request_method="{target["method"]}"'
        if target["route"] and target["route"] != "unmatched":
            query += f" | url_path=~`{route_regex(target['route'])}`"
        else:
            query += ' | severity_text=~"ERROR|WARN.*|FATAL|CRITICAL"'
        return self.loki_query(query, start, end)

    def trace_logs(self, target, trace_ids, start, end):
        query = f'{{service_name="{target["service"]}"}} | trace_id=~"{"|".join(trace_ids)}"'
        return self.loki_query(query, start, end)

    def loki_query(self, query, start, end):
        response = self.client.get(
            f"{self.settings.loki_url}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": int(start.timestamp() * 1e9),
                "end": int(end.timestamp() * 1e9),
                "limit": self.settings.max_logs,
                "direction": "backward",
            },
        )
        response.raise_for_status()
        entries = []
        for stream in response.json()["data"]["result"]:
            for timestamp, line in stream["values"]:
                entries.append({
                    "timestamp": datetime.fromtimestamp(int(timestamp) / 1e9, timezone.utc).isoformat(),
                    "line": line,
                    "metadata": stream["stream"],
                })
        return entries

    # Tempo

    def error_trace_ids(self, target, start, end):
        conditions = [f'resource.service.name="{target["service"]}"', "status=error"]
        if target["route"]:
            conditions.append(f'span.http.route="{target["route"]}"')
        if target["method"]:
            conditions.append(f'span.http.request.method="{target["method"]}"')
        response = self.client.get(
            f"{self.settings.tempo_url}/api/search",
            params={
                "q": "{ " + " && ".join(conditions) + " }",
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "limit": self.settings.max_traces,
            },
        )
        response.raise_for_status()
        return [t["traceID"] for t in response.json().get("traces", [])]

    def trace(self, trace_id):
        response = self.client.get(f"{self.settings.tempo_url}/api/v2/traces/{trace_id}")
        response.raise_for_status()
        data = response.json()
        spans = []
        for resource_spans in data.get("trace", data).get("resourceSpans", []):
            resource = attributes_dict(resource_spans.get("resource", {}).get("attributes"))
            for scope_spans in resource_spans.get("scopeSpans", []):
                for span in scope_spans.get("spans", []):
                    start_ns, end_ns = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                    spans.append({
                        "span_id": to_hex_id(span.get("spanId")),
                        "parent_span_id": to_hex_id(span.get("parentSpanId")),
                        "name": span.get("name"),
                        "kind": span.get("kind"),
                        "service": resource.get("service.name"),
                        "start": datetime.fromtimestamp(start_ns / 1e9, timezone.utc).isoformat(),
                        "duration_ms": round((end_ns - start_ns) / 1e6, 3),
                        "status": span.get("status", {}),
                        "attributes": attributes_dict(span.get("attributes")),
                        "events": [
                            {"name": e.get("name"), "attributes": attributes_dict(e.get("attributes"))}
                            for e in span.get("events", [])
                        ],
                    })
        spans.sort(key=lambda s: s["start"])
        return {"trace_id": trace_id, "spans": spans}
