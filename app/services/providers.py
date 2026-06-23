import json
import time
import uuid

from app.db import open_db
from app.services.gateway import request_json


def serialize_provider_for_client(row):
    models = []
    if row.get("models"):
        try:
            models = json.loads(row["models"])
        except Exception:
            models = []
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "models": models,
        "model_type": row.get("model_type") or "chat",
        "status": row.get("status") or "unknown",
        "last_test": row.get("last_test"),
        "models_count": len(models),
    }



def list_providers(config):
    with open_db(config) as conn:
        rows = conn.execute(
            "SELECT id, name, base_url, models, model_type, status, "
            "last_test FROM providers ORDER BY updated_at DESC"
        ).fetchall()
    return [serialize_provider_for_client(dict(row)) for row in rows]



def save_provider(config, provider_id, name, base_url, api_key, model_type):
    now = time.time()
    provider_id = provider_id or "p_{}".format(uuid.uuid4().hex[:8])
    with open_db(config) as conn:
        existing = conn.execute("SELECT id FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE providers SET name=?, base_url=?, api_key=?, "
                "model_type=?, updated_at=? WHERE id=?",
                (name, base_url.rstrip("/"), api_key, model_type, now, provider_id),
            )
        else:
            conn.execute(
                "INSERT INTO providers (id, name, base_url, api_key, model_type, "
                "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (provider_id, name, base_url.rstrip("/"), api_key, model_type, "unknown", now, now),
            )
        conn.commit()
    return provider_id



def get_provider_with_secret(config, provider_id):
    with open_db(config) as conn:
        row = conn.execute(
            "SELECT id, name, base_url, api_key, models, model_type, status, "
            "last_test FROM providers WHERE id = ?",
            (provider_id,),
        ).fetchone()
    return dict(row) if row else None



def fetch_provider_models(config, provider):
    base_url = provider["base_url"].rstrip("/")
    url = base_url + ("/models" if base_url.endswith("/v1") else "/v1/models")
    payload = request_json(
        config,
        url,
        headers={
            "Authorization": "Bearer {}".format(provider["api_key"]),
            "Accept": "application/json",
        },
    )
    return [item.get("id") for item in payload.get("data", []) if item.get("id")]
