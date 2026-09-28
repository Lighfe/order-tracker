import logging
import os

import uvicorn

from incident_response.main import create_app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
uvicorn.run(
    create_app(),
    # Grafana runs in Docker and reaches the host through host.docker.internal, so listen
    # beyond loopback. INCIDENT_ALLOWED_NETWORKS limits who may post alerts.
    host=os.getenv("INCIDENT_RESPONSE_HOST", "0.0.0.0"),
    port=int(os.getenv("INCIDENT_RESPONSE_PORT", "8001")),
)
