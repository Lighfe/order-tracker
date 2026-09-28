import logging
import os
import time

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode


SCOPE = "order-tracker"
UNMATCHED_ROUTE = "unmatched"

tracer = trace.get_tracer(SCOPE)
meter = metrics.get_meter(SCOPE)
logger = logging.getLogger("order_tracker")

request_duration = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of HTTP server requests.",
    explicit_bucket_boundaries_advisory=[
        0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10,
    ],
)
order_lookups = meter.create_counter(
    "order_tracker.order.lookups",
    unit="{lookup}",
    description="Order lookups by outcome (found, not_found, error).",
)


def configure_telemetry():
    """Install SDK providers for traces, metrics, and logs.

    Signals are printed to stdout (see `docker compose logs app`) unless
    OTEL_EXPORTER_OTLP_ENDPOINT is set, in which case they go over OTLP/HTTP.
    Leaves an already installed SDK tracer provider alone, so tests can install their own.
    """
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return False
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", SCOPE)})
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        span_exporter, metric_exporter, log_exporter = (
            OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter(),
        )
    else:
        span_exporter, metric_exporter, log_exporter = (
            ConsoleSpanExporter(), ConsoleMetricExporter(), ConsoleLogRecordExporter(),
        )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
    ))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    _logs.set_logger_provider(logger_provider)
    install_log_handler(logger_provider)
    return True


def install_log_handler(logger_provider):
    """Send the app's log records to OpenTelemetry, correlated with the active span."""
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    logger.setLevel(logging.INFO)


def route_template(scope):
    route = scope.get("route")
    return getattr(route, "path_format", None) or getattr(route, "path", None) or UNMATCHED_ROUTE


class TelemetryMiddleware:
    """Trace every HTTP request and record its duration by route and status code."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        status_code = None
        start = time.perf_counter()

        async def send_with_status(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        with tracer.start_as_current_span(
            method,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": scope["path"]},
        ) as span:
            try:
                await self.app(scope, receive, send_with_status)
            except Exception:
                status_code = status_code or 500
                logger.exception(
                    "Unhandled error for %s %s", method, scope["path"],
                    extra={"http.request.method": method, "url.path": scope["path"]},
                )
                raise
            finally:
                route = route_template(scope)
                attributes = {
                    "http.request.method": method,
                    "http.route": route,
                    "http.response.status_code": status_code or 500,
                }
                span.update_name(f"{method} {route}")
                span.set_attributes(attributes)
                if attributes["http.response.status_code"] >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                request_duration.record(time.perf_counter() - start, attributes)
