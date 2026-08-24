from fastapi.testclient import TestClient

from ai_server.app.main import app


def test_health_returns_non_sensitive_liveness_response() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "dependencies": {
            "ai_server": "ok",
            "model_server": "unavailable",
            "openemr_api": "unavailable",
            "ocr": "unavailable",
            "external_llm": "unavailable",
        },
    }
