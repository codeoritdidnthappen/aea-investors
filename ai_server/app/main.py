from fastapi import FastAPI

app = FastAPI(title="Intake AI Server", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a non-sensitive liveness response for local development."""
    return {"status": "ok"}
