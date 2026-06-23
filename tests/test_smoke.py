from app.config import build_config
from app.db import init_db


def test_app_bootstrap_creates_db(app_env):
    config = build_config()
    init_db(config)
    assert config.db_path.exists()
