from pathlib import Path


def test_server_uses_env_for_master_key():
    text = Path("server.py").read_text(encoding="utf-8")
    assert 'LITELLM_MASTER_KEY = "sk-lit2026"' not in text
    assert 'os.environ.get("OPENWEBUI_LITE_MASTER_KEY")' in text
