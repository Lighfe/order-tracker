import ipaddress
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
SERVICE_DIR = PACKAGE_DIR.parent

# Headless Claude Code: edits are accepted, and Bash is limited to running the project
# (tests, the app) and reading git state. Anything else is denied, because -p cannot ask.
DEFAULT_AGENT_COMMAND = (
    "claude -p --output-format stream-json --verbose --permission-mode acceptEdits "
    "--allowedTools 'Read Edit Write Glob Grep Bash(uv run *) Bash(git status *) "
    "Bash(git diff *) Bash(git log *)'"
)
# Loopback plus the Compose network (ORDER_TRACKER_SUBNET), where Grafana runs.
DEFAULT_ALLOWED_NETWORKS = "127.0.0.0/8,::1/128,10.215.24.0/24"


@dataclass
class Settings:
    loki_url: str = "http://127.0.0.1:3100"
    tempo_url: str = "http://127.0.0.1:3200"
    prometheus_url: str = "http://127.0.0.1:9090"
    default_service: str = "order-tracker"
    incidents_dir: Path = SERVICE_DIR / "incidents"
    repo_dir: Path = SERVICE_DIR.parent
    # Empty list: save the context, but do not start an assistant.
    agent_command: list[str] = field(default_factory=lambda: shlex.split(DEFAULT_AGENT_COMMAND))
    allowed_networks: list = field(default_factory=lambda: parse_networks(DEFAULT_ALLOWED_NETWORKS))
    # Minutes of telemetry to collect before the alert started.
    lookback_minutes: int = 10
    max_traces: int = 5
    max_logs: int = 50

    @classmethod
    def from_env(cls):
        env = os.environ
        settings = cls()
        settings.loki_url = env.get("LOKI_URL", settings.loki_url)
        settings.tempo_url = env.get("TEMPO_URL", settings.tempo_url)
        settings.prometheus_url = env.get("PROMETHEUS_URL", settings.prometheus_url)
        settings.default_service = env.get("INCIDENT_SERVICE_NAME", settings.default_service)
        if "INCIDENTS_DIR" in env:
            settings.incidents_dir = Path(env["INCIDENTS_DIR"]).resolve()
        if "INCIDENT_REPO_DIR" in env:
            settings.repo_dir = Path(env["INCIDENT_REPO_DIR"]).resolve()
        if "INCIDENT_AGENT_COMMAND" in env:
            settings.agent_command = shlex.split(env["INCIDENT_AGENT_COMMAND"])
        if "INCIDENT_ALLOWED_NETWORKS" in env:
            settings.allowed_networks = parse_networks(env["INCIDENT_ALLOWED_NETWORKS"])
        return settings

    def client_allowed(self, host):
        if not self.allowed_networks:
            return True
        try:
            address = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            return False
        return any(address in network for network in self.allowed_networks)


def parse_networks(value):
    return [ipaddress.ip_network(part.strip()) for part in value.split(",") if part.strip()]
