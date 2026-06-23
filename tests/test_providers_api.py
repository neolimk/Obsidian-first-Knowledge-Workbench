import json

from app.services.providers import serialize_provider_for_client


def test_provider_payload_omits_api_key():
    row = {
        "id": "p_demo",
        "name": "Demo",
        "base_url": "https://example.com",
        "api_key": "sk-secret",
        "models": json.dumps(["gpt-4o-mini"]),
        "model_type": "chat",
        "status": "ok",
        "last_test": 123.0,
    }

    payload = serialize_provider_for_client(row)

    assert payload["id"] == "p_demo"
    assert "api_key" not in payload
    assert payload["models"] == ["gpt-4o-mini"]
