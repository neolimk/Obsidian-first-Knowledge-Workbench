import pytest


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWEBUI_LITE_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("OPENWEBUI_LITE_BASE_DIR", str(tmp_path))
    return tmp_path
