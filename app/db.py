import sqlite3
import threading
from contextlib import contextmanager

from app.config import AppConfig

_db_lock = threading.Lock()


@contextmanager
def open_db(config: AppConfig):
    conn = sqlite3.connect(str(config.db_path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(config: AppConfig) -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT,
        model TEXT,
        system TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        meta TEXT,
        created_at REAL NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
    CREATE TABLE IF NOT EXISTS providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key TEXT NOT NULL,
        models TEXT,
        model_type TEXT DEFAULT 'chat',
        status TEXT DEFAULT 'unknown',
        last_test REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_provider_updated ON providers(updated_at DESC);
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        model TEXT,
        skill TEXT DEFAULT 'chat',
        status TEXT DEFAULT 'pending',
        result TEXT,
        source_sid TEXT,
        source_msg_id INTEGER,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_task_source ON tasks(source_sid, source_msg_id);
    CREATE TABLE IF NOT EXISTS obsidian_categories (
        path TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        tags TEXT,
        summary TEXT,
        classified_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_obs_cat_category ON obsidian_categories(category);
    """

    with _db_lock:
        with open_db(config) as conn:
            conn.executescript(schema)
            try:
                conn.execute("SELECT model_type FROM providers LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE providers ADD COLUMN model_type TEXT DEFAULT 'chat'")
            conn.commit()
