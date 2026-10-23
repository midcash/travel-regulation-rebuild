"""Explicitly offline browser fixture: python verification/baseline/serve_offline.py."""
from pathlib import Path
import argparse
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
import uvicorn
from travel_agent.app import create_app
from travel_agent.runtime import Conversation, NodeWorker
from fixtures import NOW, model_responses, supplier

def create_offline_app(evidence_root):
    # Cumulative for this fixture process, including explicit new conversations.
    # Only count observed events/HTTP dispatches, never inferred UI messages.
    counts = {"model_calls": 0, "supplier_requests": 0}

    class CountingWorker(NodeWorker):
        async def receive(self):
            event = await super().receive()
            if event["type"] == "model_start":
                counts["model_calls"] += 1
            return event

    def counted_supplier(request):
        counts["supplier_requests"] += 1
        return supplier(request)

    app = create_app(Conversation(clock=lambda: NOW, fake=True, responses=model_responses,
        transport_http=httpx.MockTransport(counted_supplier), evidence_root=evidence_root,
        worker_factory=CountingWorker))

    @app.get("/__verification__/counts")
    async def observed_counts():
        return {"mode": "synthetic", "session_id": app.state.conversation.session_id, **counts}

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766, help="Local fixture port (default: 8766)")
    args = parser.parse_args()
    os.environ["TUNIU_API_KEY"] = "synthetic"
    os.environ["TUNIU_FLIGHT_URL"] = "https://supplier.invalid/flight"
    with tempfile.TemporaryDirectory(prefix="travel-browser-offline-") as folder:
        app = create_offline_app(folder)
        uvicorn.run(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=3)
