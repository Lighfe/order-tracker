import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from incident_response.agent import AgentRunner
from incident_response.collector import Collector
from incident_response.config import Settings
from incident_response.incidents import incident_id, record_update, save_incident


logger = logging.getLogger("incident_response")


def create_app(settings=None, collector=None, executor=None):
    settings = settings or Settings.from_env()
    collector = collector or Collector(settings)
    runner = AgentRunner(settings)
    # One worker: assistants edit the same checkout, so they run one after another.
    executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent")

    @asynccontextmanager
    async def lifespan(_app):
        settings.incidents_dir.mkdir(parents=True, exist_ok=True)
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Incident Response", lifespan=lifespan)
    app.state.settings = settings

    def handle_incident(folder, alert, payload):
        """Save the context right away, while it is fresh, then queue the assistant."""
        try:
            context = collector.collect(alert)
            save_incident(folder, alert, payload, context)
            logger.info("Saved %s (%d logs, %d traces, problems: %s)", folder.name,
                        len(context["logs"]), len(context["traces"]), context["errors"] or "none")
        except Exception:
            logger.exception("Could not save context for %s", folder.name)
        executor.submit(runner.run, folder)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/alerts", status_code=202)
    async def receive_alerts(request: Request, background: BackgroundTasks):
        client = request.client.host if request.client else None
        if not settings.client_allowed(client):
            logger.warning("Rejected alert from %s", client)
            raise HTTPException(403, "Client not allowed")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON")
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list):
            raise HTTPException(422, "Expected a Grafana webhook payload with an 'alerts' list")

        results = []
        for alert in alerts:
            if not isinstance(alert, dict):
                continue
            folder = settings.incidents_dir / incident_id(alert)
            status = alert.get("status", "firing")
            if folder.exists():
                record_update(folder, alert)
                action = "updated"
            elif status != "firing":
                action = "ignored"
            else:
                folder.mkdir(parents=True)
                background.add_task(handle_incident, folder, alert, payload)
                action = "created"
            logger.info("Alert %s (%s): %s", folder.name, status, action)
            results.append({"incident": folder.name, "status": status, "action": action})
        return {"incidents": results}

    return app
