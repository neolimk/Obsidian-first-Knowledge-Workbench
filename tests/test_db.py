from app.config import build_config
from app.db import init_db, open_db


def test_init_db_creates_expected_tables(app_env):
    config = build_config()
    init_db(config)

    with open_db(config) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert {"sessions", "messages", "providers", "tasks", "obsidian_categories"}.issubset(names)
