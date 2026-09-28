import pytest
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class Telemetry:
    def __init__(self):
        self.spans = InMemorySpanExporter()
        # Delta temporality so each test only sees the measurements it produced.
        self.metrics = InMemoryMetricReader(preferred_temporality={
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
        })
        self.logs = InMemoryLogRecordExporter()

    def clear(self):
        self.spans.clear()
        self.metrics.get_metrics_data()
        self.logs.clear()

    def collect_metrics(self):
        """Collect once and return data points by metric name (delta since last collect)."""
        points = {}
        data = self.metrics.get_metrics_data()
        for resource_metrics in data.resource_metrics if data else []:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    points.setdefault(metric.name, []).extend(metric.data.data_points)
        return points


def _install_providers():
    captured = Telemetry()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(captured.spans))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(metric_readers=[captured.metrics]))
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(captured.logs))
    _logs.set_logger_provider(logger_provider)
    telemetry.install_log_handler(logger_provider)
    return captured


# Install in-memory providers before importing app.main, whose configure_telemetry()
# would otherwise install console exporters.
from app import telemetry  # noqa: E402
_TELEMETRY = _install_providers()
from app import main  # noqa: E402


@pytest.fixture
def otel():
    _TELEMETRY.clear()
    return _TELEMETRY


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client
