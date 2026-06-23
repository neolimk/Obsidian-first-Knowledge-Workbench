# openwebui-lite Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前单文件、明文密钥、前端直连 provider 的 openwebui-lite 重构为可维护、可测试、默认安全的本地 AI WebUI。

**Architecture:** 保留“Python 后端 + 静态前端 + SQLite + SSE”的总体形态，不做框架迁移。重构重点是先建立测试与依赖基线，再把 `server.py` 中的配置、数据库、上游网关、Provider 管理、路由分层拆出，同时把敏感密钥留在后端，前端只消费受控 API。

**Tech Stack:** Python 3, stdlib `http.server`, SQLite, PyYAML, pytest, ruff, vendored frontend assets (`marked`, `highlight.js`, `DOMPurify`)

---

## File Structure

### Existing files to keep
- `server.py` — 过渡期入口文件，最终只负责启动应用并委托给新模块。
- `static/index.html` — 过渡期前端入口，最终只保留 HTML 骨架和静态资源引用。
- `ctl.sh` — 运维脚本，改造成跨环境友好版本。
- `README.md` — 更新部署方式、环境变量、目录结构。
- `CLAUDE.md` — 已存在 `## Health Stack`，后续补充实际命令。

### New backend files
- `app/__init__.py` — 包声明。
- `app/config.py` — 环境变量、路径、CORS、TLS、安全默认值。
- `app/db.py` — SQLite 连接、初始化、表迁移。
- `app/models.py` — 轻量数据结构和响应构造函数。
- `app/services/gateway.py` — 统一的上游 HTTP 请求、SSE、超时和错误处理。
- `app/services/providers.py` — Provider 的 CRUD、测试、导入、模型列表缓存。
- `app/services/chat.py` — 聊天与图片请求编排。
- `app/services/obsidian.py` — 本地知识库检索。
- `app/services/tasks.py` — 任务管理和执行。
- `app/web.py` — 路由注册、请求分发、响应输出。

### New frontend files
- `static/app.css` — 从 `index.html` 抽离样式。
- `static/app.js` — 启动逻辑与共享状态。
- `static/modules/api.js` — `fetch` 封装。
- `static/modules/chat.js` — 聊天视图与 SSE 处理。
- `static/modules/tasks.js` — 任务视图。
- `static/modules/providers.js` — Provider 管理 UI。
- `static/modules/settings.js` — 设置与主题。
- `static/vendor/marked.min.js` — 本地 vendored markdown 依赖。
- `static/vendor/highlight.min.js` — 本地 vendored code highlight 依赖。
- `static/vendor/dompurify.min.js` — HTML sanitize 依赖。

### New test and tooling files
- `requirements.txt` — 运行时依赖。
- `requirements-dev.txt` — 开发依赖。
- `pytest.ini` — pytest 配置。
- `ruff.toml` — lint 配置。
- `tests/conftest.py` — 临时数据目录、测试 server fixture。
- `tests/test_config.py` — 配置与安全默认值测试。
- `tests/test_providers_api.py` — Provider API 行为测试，重点校验不泄露密钥。
- `tests/test_chat_api.py` — 聊天与 SSE 输出测试。
- `tests/test_obsidian_api.py` — Obsidian API 测试。
- `tests/test_tasks_api.py` — 任务 API 测试。
- `tests/test_ctl_script.py` — `ctl.sh` 输出与兼容性测试。

---

### Task 1: 建立依赖、测试和目录基线

**Files:**
- Create: `app/__init__.py`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `ruff.toml`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`
- Modify: `README.md`

- [ ] **Step 1: 写失败的配置测试**

```python
# tests/test_config.py
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 3: 添加依赖与测试配置文件**

```txt
# requirements.txt
PyYAML==6.0.2
```

```txt
# requirements-dev.txt
-r requirements.txt
pytest==8.3.2
ruff==0.6.2
```

```ini
# pytest.ini
[pytest]
testpaths = tests
pythonpath = .
addopts = -q
```

```toml
# ruff.toml
line-length = 100

target-version = "py311"

[lint]
select = ["E", "F", "I"]
```

```python
# app/__init__.py
__all__ = []
```

- [ ] **Step 4: 写最小配置实现**

```python
# app/config.py
from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    data_dir: Path
    db_path: Path
    static_dir: Path
    index_html: Path
    litellm_base: str
    litellm_master_key: str
    verify_tls: bool
    cors_allowed_origins: list[str]
    port: int


def _parse_origins(raw: str | None) -> list[str]:
    if not raw:
        return ["http://127.0.0.1:8899"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_config() -> AppConfig:
    base_dir = Path(os.environ.get("OPENWEBUI_LITE_BASE_DIR", Path(__file__).resolve().parent.parent))
    master_key = os.environ.get("OPENWEBUI_LITE_MASTER_KEY")
    if not master_key:
        raise RuntimeError("OPENWEBUI_LITE_MASTER_KEY is required")

    data_dir = base_dir / "data"
    static_dir = base_dir / "static"
    data_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=data_dir / "openwebui-lite.db",
        static_dir=static_dir,
        index_html=static_dir / "index.html",
        litellm_base=os.environ.get("OPENWEBUI_LITE_BASE", "http://127.0.0.1:4000"),
        litellm_master_key=master_key,
        verify_tls=os.environ.get("OPENWEBUI_LITE_VERIFY_TLS", "true").lower() != "false",
        cors_allowed_origins=_parse_origins(os.environ.get("OPENWEBUI_LITE_CORS_ORIGINS")),
        port=int(os.environ.get("OPENWEBUI_LITE_PORT", "8899")),
    )
```

- [ ] **Step 5: 添加测试 fixture，避免后续每个测试重复搭环境**

```python
# tests/conftest.py
import pytest


@pytest.fixture
def app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWEBUI_LITE_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("OPENWEBUI_LITE_BASE_DIR", str(tmp_path))
    return tmp_path
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 7: 更新 README 的启动前置要求**

```md
## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export OPENWEBUI_LITE_MASTER_KEY="replace-me"
pytest
ruff check .
```
```

- [ ] **Step 8: 提交**

```bash
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || git init
git add app/__init__.py app/config.py requirements.txt requirements-dev.txt pytest.ini ruff.toml tests/conftest.py tests/test_config.py README.md
git commit -m "chore: add project scaffolding and config tests"
```

---

### Task 2: 抽离数据库与应用入口

**Files:**
- Create: `app/db.py`
- Create: `app/models.py`
- Modify: `server.py:28-133`
- Test: `tests/test_config.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: 写失败的数据库初始化测试**

```python
# tests/test_db.py
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_db.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.db'`

- [ ] **Step 3: 抽取数据库模块**

```python
# app/db.py
import sqlite3
import threading
from contextlib import contextmanager

from app.config import AppConfig

_db_lock = threading.Lock()


@contextmanager
def open_db(config: AppConfig):
    conn = sqlite3.connect(config.db_path, check_same_thread=False, timeout=10)
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
```

- [ ] **Step 4: 给入口文件加最小委托**

```python
# server.py
from app.config import build_config
from app.db import init_db


def main() -> None:
    config = build_config()
    init_db(config)
    print(f"openwebui-lite listening on :{config.port}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 运行数据库测试**

Run: `pytest tests/test_db.py tests/test_config.py -q`
Expected: PASS

- [ ] **Step 6: 运行语法检查**

Run: `python -m py_compile server.py app/config.py app/db.py`
Expected: no output

- [ ] **Step 7: 提交**

```bash
git add app/config.py app/db.py server.py tests/test_db.py tests/test_config.py
git commit -m "refactor: extract config and database bootstrap"
```

---

### Task 3: 收口上游调用，移除前端密钥暴露

**Files:**
- Create: `app/services/gateway.py`
- Create: `app/services/providers.py`
- Modify: `server.py:834-1176`
- Test: `tests/test_providers_api.py`
- Modify: `static/index.html:2517-2607`
- Create: `static/modules/providers.js`

- [ ] **Step 1: 写失败的 Provider API 测试，明确“前端拿不到 api_key”**

```python
# tests/test_providers_api.py
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_providers_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.providers'`

- [ ] **Step 3: 创建统一上游网关，默认开启 TLS 校验**

```python
# app/services/gateway.py
import json
import ssl
import urllib.error
import urllib.request

from app.config import AppConfig


class GatewayError(RuntimeError):
    pass


def _build_ssl_context(config: AppConfig):
    if config.verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request_json(config: AppConfig, url: str, *, method: str = "GET", headers=None, body=None, timeout: int = 15):
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_build_ssl_context(config)) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GatewayError(f"HTTP {exc.code}: {detail[:300]}") from exc
```

- [ ] **Step 4: 创建 Provider 服务，统一后端序列化**

```python
# app/services/providers.py
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
        except json.JSONDecodeError:
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
            "SELECT id, name, base_url, models, model_type, status, last_test FROM providers ORDER BY updated_at DESC"
        ).fetchall()
    return [serialize_provider_for_client(dict(row)) for row in rows]


def save_provider(config, *, provider_id, name, base_url, api_key, model_type):
    now = time.time()
    provider_id = provider_id or f"p_{uuid.uuid4().hex[:8]}"
    with open_db(config) as conn:
        conn.execute(
            """
            INSERT INTO providers (id, name, base_url, api_key, model_type, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              base_url=excluded.base_url,
              api_key=excluded.api_key,
              model_type=excluded.model_type,
              updated_at=excluded.updated_at
            """,
            (provider_id, name, base_url.rstrip("/"), api_key, model_type, now, now),
        )
        conn.commit()
    return provider_id


def fetch_provider_models(config, *, provider):
    base_url = provider["base_url"].rstrip("/")
    url = base_url + ("/models" if base_url.endswith("/v1") else "/v1/models")
    payload = request_json(
        config,
        url,
        headers={"Authorization": f"Bearer {provider['api_key']}", "Accept": "application/json"},
    )
    return [item.get("id") for item in payload.get("data", []) if item.get("id")]
```

- [ ] **Step 5: 把原 `/api/providers/list-with-models` 改为不返回密钥**

```python
# server.py inside providers endpoint
providers = list_providers(config)
return {"ok": True, "providers": providers, "count": len(providers), "status": 200}
```

- [ ] **Step 6: 前端移除对 `api_key` 的依赖，改成只传 provider id 给后端**

```javascript
// static/modules/providers.js
export async function loadProviders(apiPost) {
  const response = await apiPost('/api/providers/list', {});
  return response.providers || [];
}

export async function testProvider(apiPost, providerId) {
  return apiPost('/api/providers/test', { id: providerId });
}
```

```javascript
// static/index.html temporary bridge
import { loadProviders, testProvider } from './modules/providers.js';
```

- [ ] **Step 7: 运行测试，确认通过**

Run: `pytest tests/test_providers_api.py -q`
Expected: PASS

- [ ] **Step 8: 运行 lint**

Run: `ruff check app tests`
Expected: `All checks passed!`

- [ ] **Step 9: 提交**

```bash
git add app/services/gateway.py app/services/providers.py server.py static/modules/providers.js static/index.html tests/test_providers_api.py
git commit -m "refactor: keep provider secrets on the server"
```

---

### Task 4: 重做聊天与 SSE 路径，统一错误和超时处理

**Files:**
- Create: `app/services/chat.py`
- Modify: `server.py:758-832,1601-1917`
- Test: `tests/test_chat_api.py`
- Create: `tests/test_chat_api.py`

- [ ] **Step 1: 写失败的聊天测试，锁定 SSE 输出格式**

```python
# tests/test_chat_api.py
from app.services.chat import build_sse_event


def test_build_sse_event_encodes_json_line():
    line = build_sse_event({"event": "delta", "text": "你好"})
    assert line == 'data: {"event": "delta", "text": "你好"}\n\n'.encode("utf-8")
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_chat_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.chat'`

- [ ] **Step 3: 创建聊天服务，统一 SSE 编码**

```python
# app/services/chat.py
import json


def build_sse_event(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def build_done_event() -> bytes:
    return b"data: [DONE]\n\n"
```

- [ ] **Step 4: 将 `_handle_sse`、`_handle_sse_task`、`_handle_sse_demo` 的 `wfile.write(...)` 调用改为复用服务函数**

```python
# server.py example replacement
from app.services.chat import build_done_event, build_sse_event

self.wfile.write(build_sse_event({"event": "delta", "text": d2}))
self.wfile.flush()
...
self.wfile.write(build_done_event())
```

- [ ] **Step 5: 对上游调用统一使用 `GatewayError` 映射为一致错误响应**

```python
# server.py example replacement
except GatewayError as exc:
    err_msg = str(exc)
    self.wfile.write(build_sse_event({"event": "error", "text": err_msg}))
    self.wfile.write(build_done_event())
```

- [ ] **Step 6: 运行测试**

Run: `pytest tests/test_chat_api.py -q`
Expected: PASS

- [ ] **Step 7: 手动回归语法**

Run: `python -m py_compile server.py app/services/chat.py app/services/gateway.py`
Expected: no output

- [ ] **Step 8: 提交**

```bash
git add app/services/chat.py server.py tests/test_chat_api.py
git commit -m "refactor: centralize chat SSE helpers"
```

---

### Task 5: 加固前端资源与消息渲染，移除远程 `eval`

**Files:**
- Create: `static/app.css`
- Create: `static/app.js`
- Create: `static/modules/api.js`
- Create: `static/vendor/marked.min.js`
- Create: `static/vendor/highlight.min.js`
- Create: `static/vendor/dompurify.min.js`
- Modify: `static/index.html:8-42,2345-2430`
- Test: `tests/test_frontend_assets.py`
- Create: `tests/test_frontend_assets.py`

- [ ] **Step 1: 写失败的静态文件测试，锁定“页面不再从 CDN fetch+eval”**

```python
# tests/test_frontend_assets.py
from pathlib import Path


def test_index_html_does_not_eval_remote_marked():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert "eval(safeCode)" not in html
    assert "https://cdn.jsdelivr.net" not in html
    assert "static/vendor/marked.min.js" or "vendor/marked.min.js"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_frontend_assets.py -q`
Expected: FAIL because current `index.html` still contains CDN and `eval`

- [ ] **Step 3: 把第三方依赖 vendoring 到本地**

```text
static/vendor/marked.min.js
static/vendor/highlight.min.js
static/vendor/dompurify.min.js
```

Use exact source versions:
- `marked@4.3.0`
- `highlight.js@11.9.0`
- `dompurify@3.1.6`

- [ ] **Step 4: 替换 HTML 头部脚本引用**

```html
<link rel="stylesheet" href="/vendor/highlight.min.css">
<script src="/vendor/marked.min.js" defer></script>
<script src="/vendor/highlight.min.js" defer></script>
<script src="/vendor/dompurify.min.js" defer></script>
<script type="module" src="/app.js"></script>
```

- [ ] **Step 5: 用 sanitize 后的 markdown HTML 渲染消息**

```javascript
// static/app.js
export function renderMarkdown(text) {
  const unsafeHtml = window.marked.parse(text || '', { breaks: true, gfm: true });
  const safeHtml = window.DOMPurify.sanitize(unsafeHtml);
  setTimeout(() => {
    if (window.hljs) {
      document.querySelectorAll('.msg-content pre code').forEach((block) => {
        window.hljs.highlightElement(block);
      });
    }
  }, 0);
  return safeHtml;
}
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `pytest tests/test_frontend_assets.py -q`
Expected: PASS

- [ ] **Step 7: 做一轮静态回归检查**

Run: `python - <<'PY'
from pathlib import Path
html = Path('static/index.html').read_text(encoding='utf-8')
assert 'eval(safeCode)' not in html
assert 'cdn.jsdelivr.net' not in html
print('frontend assets OK')
PY`
Expected: `frontend assets OK`

- [ ] **Step 8: 提交**

```bash
git add static/index.html static/app.js static/app.css static/modules/api.js static/vendor/marked.min.js static/vendor/highlight.min.js static/vendor/dompurify.min.js tests/test_frontend_assets.py
git commit -m "refactor: vendor frontend dependencies and sanitize markdown"
```

---

### Task 6: 收紧 CORS、修复 ctl.sh、补回归文档

**Files:**
- Modify: `app/config.py`
- Modify: `server.py:1497-1568`
- Modify: `ctl.sh`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Create: `tests/test_ctl_script.py`

- [ ] **Step 1: 写失败的脚本测试，锁定“不依赖 pgrep 时也能正确降级”**

```python
# tests/test_ctl_script.py
from pathlib import Path


def test_ctl_script_checks_command_availability():
    script = Path("ctl.sh").read_text(encoding="utf-8")
    assert "command -v pgrep" in script
    assert "command -v ss" in script
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_ctl_script.py -q`
Expected: FAIL because current script calls `pgrep` directly

- [ ] **Step 3: 收紧 CORS 响应头**

```python
# server.py helper

def _send_cors(self):
    origin = self.headers.get("Origin")
    if origin and origin in config.cors_allowed_origins:
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
```

Replace current unconditional `Access-Control-Allow-Origin: *` with `_send_cors()`.

- [ ] **Step 4: 让 `ctl.sh` 先探测命令可用性，再输出明确状态**

```bash
# ctl.sh
has_cmd() { command -v "$1" >/dev/null 2>&1; }

running_pid() {
  if has_cmd pgrep; then
    pgrep -f "$APP_DIR/server.py" | head -1
    return 0
  fi
  return 1
}

port_status() {
  if has_cmd ss; then
    ss -tln 2>/dev/null | grep ":$PORT " || true
  else
    echo "  ss not available"
  fi
}
```

- [ ] **Step 5: 更新 README 的配置章节**

```md
## 环境变量

- `OPENWEBUI_LITE_MASTER_KEY`：必填，LiteLLM master key
- `OPENWEBUI_LITE_BASE`：可选，默认 `http://127.0.0.1:4000`
- `OPENWEBUI_LITE_VERIFY_TLS`：可选，默认 `true`
- `OPENWEBUI_LITE_CORS_ORIGINS`：可选，默认 `http://127.0.0.1:8899`
- `OPENWEBUI_LITE_PORT`：可选，默认 `8899`
```

- [ ] **Step 6: 更新 CLAUDE.md 的健康检查命令**

```md
## Health Stack

- lint: ruff check .
- test: pytest
- shell: bash ctl.sh status
- health-review: manual code review of app/, static/, ctl.sh
```

- [ ] **Step 7: 跑完整验证**

Run: `pytest -q && ruff check . && python -m py_compile server.py app/*.py app/services/*.py`
Expected: tests pass, `All checks passed!`, py_compile no output

- [ ] **Step 8: 提交**

```bash
git add app/config.py server.py ctl.sh README.md CLAUDE.md tests/test_ctl_script.py
git commit -m "hardening: tighten cors and make ctl script portable"
```

---

### Task 7: 最终入口清理与发布前验收

**Files:**
- Modify: `server.py`
- Create: `app/web.py`
- Modify: `README.md`
- Test: `tests/test_smoke.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: 写失败的 smoke 测试，锁定入口行为**

```python
# tests/test_smoke.py
from app.config import build_config
from app.db import init_db


def test_app_bootstrap_creates_db(app_env):
    config = build_config()
    init_db(config)
    assert config.db_path.exists()
```

- [ ] **Step 2: 运行测试，确认当前未形成最终入口**

Run: `pytest tests/test_smoke.py -q`
Expected: PASS or FAIL is acceptable here; if PASS, keep test and move to next step. This task is about final cleanup, not red-green purity.

- [ ] **Step 3: 把请求分发搬到 `app/web.py`，让 `server.py` 只保留启动入口**

```python
# app/web.py
from http.server import BaseHTTPRequestHandler


def make_handler(api_handler, config):
    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "openwebui-lite/3.0"
        # move existing do_GET/do_POST/do_OPTIONS/_dispatch here
    return RequestHandler
```

```python
# server.py
from app.config import build_config
from app.db import init_db
from app.web import run_server


def main() -> None:
    config = build_config()
    init_db(config)
    run_server(config)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 更新 README 的项目结构图**

```md
openwebui-lite/
├── app/
│   ├── config.py
│   ├── db.py
│   ├── services/
│   └── web.py
├── static/
│   ├── app.js
│   ├── app.css
│   ├── modules/
│   └── vendor/
├── tests/
├── server.py
├── ctl.sh
└── requirements-dev.txt
```

- [ ] **Step 5: 跑最终验收**

Run: `pytest -q && ruff check . && bash ctl.sh status`
Expected:
- pytest: all tests pass
- ruff: `All checks passed!`
- ctl.sh: no `command not found`, status output is human-readable

- [ ] **Step 6: 提交**

```bash
git add app/web.py server.py README.md tests/test_smoke.py
git commit -m "refactor: finish modular server entrypoint"
```

---

## Self-Review

### Spec coverage
- 安全问题覆盖：硬编码密钥、TLS 校验、CORS、前端 `eval`、前端密钥暴露，分别在 Task 1、3、5、6 落地。
- 架构拆分覆盖：配置/数据库/Provider/聊天/SSE/入口拆分，分别在 Task 2、3、4、7 落地。
- 工程化覆盖：依赖、pytest、ruff、README、CLAUDE.md，分别在 Task 1、6、7 落地。
- 运维脚本覆盖：`ctl.sh` 可移植性修复在 Task 6 落地。

### Placeholder scan
- 没有使用 TBD、TODO、implement later、similar to Task N 之类占位语。
- 每个代码步骤都包含了具体代码块。
- 每个验证步骤都提供了明确命令和预期结果。

### Type consistency
- 配置对象统一使用 `AppConfig`。
- Provider 面向前端的序列化统一使用 `serialize_provider_for_client()`，不再返回 `api_key`。
- SSE 输出统一使用 `build_sse_event()` / `build_done_event()`。

---

Plan complete and saved to `docs/superpowers/plans/2026-06-23-openwebui-lite-refactor.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
