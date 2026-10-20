"""Explicitly offline browser fixture: python verification/baseline/serve_offline.py."""
from pathlib import Path
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
import uvicorn
from travel_agent.app import create_app
from travel_agent.runtime import Conversation
from fixtures import NOW, model_responses, supplier

if __name__ == "__main__":
    os.environ["TUNIU_API_KEY"] = "synthetic"
    os.environ["TUNIU_FLIGHT_URL"] = "https://supplier.invalid/flight"
    with tempfile.TemporaryDirectory(prefix="travel-browser-offline-") as folder:
        app = create_app(Conversation(clock=lambda: NOW, fake=True, responses=model_responses,
            transport_http=httpx.MockTransport(supplier), evidence_root=folder))
        uvicorn.run(app, host="127.0.0.1", port=8766, timeout_graceful_shutdown=3)
