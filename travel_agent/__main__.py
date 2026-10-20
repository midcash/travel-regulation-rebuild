"""Run the single-user local service: python -m travel_agent."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("travel_agent.app:app", host="127.0.0.1", port=8765,
                timeout_graceful_shutdown=3)
