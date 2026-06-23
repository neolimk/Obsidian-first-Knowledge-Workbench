from app.config import AppConfig, build_config


def test_build_config_requires_master_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENWEBUI_LITE_MASTER_KEY", raising=False)
    monkeypatch.setenv("OPENWEBUI_LITE_BASE_DIR", str(tmp_path))

    try:
        build_config()
    except RuntimeError as exc:
        assert "OPENWEBUI_LITE_MASTER_KEY" in str(exc)
    else:
        raise AssertionError("build_config() should fail without master key")


def test_build_config_uses_secure_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWEBUI_LITE_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("OPENWEBUI_LITE_BASE_DIR", str(tmp_path))

    config = build_config()

    assert isinstance(config, AppConfig)
    assert config.litellm_master_key == "test-master-key"
    assert config.verify_tls is True
    assert config.cors_allowed_origins == ["http://127.0.0.1:8899"]
