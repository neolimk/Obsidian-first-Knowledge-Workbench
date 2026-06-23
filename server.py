# -*- coding: utf-8 -*-
"""
openwebui-lite — 轻量级 Open WebUI 替代品
- 后端:Python 单文件 http server(基于 stdlib)
- 路由:走本地 LiteLLM 网关 (localhost:4000) + master_key (from env)
- 存储:SQLite(会话 + 消息)
- 流式:SSE
- 端口:8899

灵感来源:https://github.com/open-webui/open-webui
"""

import json
import os
import re as _re
import sys
import ssl
import time
import uuid
import yaml as _yaml
import sqlite3
import threading
import http.server
import socketserver
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs

from app.config import build_config
from app.services.chat import build_done_event, build_sse_event
from app.services.chat_flow import prepare_chat_request
from app.services.chat_gateway import stream_chat_with_fallback
from app.services.providers import list_providers as safe_list_providers, save_provider as safe_save_provider, get_provider_with_secret, fetch_provider_models
from app.services.sse import (
    build_delta_event,
    build_done_event as build_sse_done_event,
    build_error_event,
    build_log_event,
    build_refs_event,
    build_start_event,
)
from app.web import run_server

# ============== 路径配置 ==============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "openwebui-lite.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")
LITELLM_BASE = os.environ.get("OPENWEBUI_LITE_BASE", "http://127.0.0.1:4000")
LITELLM_MASTER_KEY = os.environ.get("OPENWEBUI_LITE_MASTER_KEY") or ""
LITELLM_CONFIG = os.environ.get("LITELLM_CONFIG", os.path.join(BASE_DIR, "config.yaml"))
PORT = 8899

# Obsidian vault 路径(RAG 数据源)
# 默认 Windows 端 Documents 下的 Obsidian Vault(通过 WSL /mnt/c 访问)
# 用户可在设置 → 偏好 改路径
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT_PATH", "/mnt/c/Users/Administrator/Documents/Obsidian Vault")
# 排除目录(.obsidian 配置 / assets 二进制)
OBSIDIAN_EXCLUDE_DIRS = {".obsidian", "assets"}
# 检索返回 top N
OBSIDIAN_TOP_K = 3
# 单篇最大字符数(喂 LLM 的 context 上限)
OBSIDIAN_MAX_CHARS_PER_DOC = 2500
# 检索最低分数阈值(低分噪声不返回)
OBSIDIAN_SCORE_THRESHOLD = 15.0

os.makedirs(DATA_DIR, exist_ok=True)

# ============== 数据库 ==============
_db_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with _db_lock:
        c = db()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            model       TEXT,
            system      TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            meta        TEXT,
            created_at  REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);

        CREATE TABLE IF NOT EXISTS providers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            base_url    TEXT NOT NULL,
            api_key     TEXT NOT NULL,
            models      TEXT,                       -- JSON 缓存
            model_type  TEXT DEFAULT 'chat',         -- chat / image
            status      TEXT DEFAULT 'unknown',      -- ok/error/unknown
            last_test   REAL,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_provider_updated ON providers(updated_at DESC);

        CREATE TABLE IF NOT EXISTS tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            prompt          TEXT NOT NULL,
            model           TEXT,                       -- 完整 select value (e.g. "p_xxx:glm-4-flash" 或 "litellm:gpt-5.4")
            skill           TEXT DEFAULT 'chat',         -- 第一版只跑 chat,留扩展点(web_search/code_exec/...)
            status          TEXT DEFAULT 'pending',      -- pending/running/done/failed
            result          TEXT,                       -- 完成后存 assistant 响应
            source_sid      TEXT,                       -- 关联 session_id(消息标记任务时)
            source_msg_id   INTEGER,                    -- 关联 message.id
            created_at      REAL NOT NULL,
            updated_at      REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_task_source ON tasks(source_sid, source_msg_id);

        CREATE TABLE IF NOT EXISTS obsidian_categories (
            path        TEXT PRIMARY KEY,            -- vault 内相对路径
            category    TEXT NOT NULL,               -- 学习/运动/习惯/教育/其他
            tags        TEXT,                        -- JSON 数组字符串
            summary     TEXT,                        -- 一句话总结
            classified_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_obs_cat_category ON obsidian_categories(category);
        """)
        c.commit()
        # 迁移:给旧 providers 表加 model_type 列(如果不存在)
        try:
            c.execute("SELECT model_type FROM providers LIMIT 1")
        except Exception:
            c.execute("ALTER TABLE providers ADD COLUMN model_type TEXT DEFAULT 'chat'")
            c.commit()
        c.close()

# ============== Sessions ==============
def list_sessions():
    with _db_lock:
        c = db()
        rows = c.execute(
            "SELECT id, title, model, system, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        c.close()
        return [dict(r) for r in rows]

def get_session(sid):
    with _db_lock:
        c = db()
        row = c.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if not row:
            c.close()
            return None
        msgs = c.execute(
            "SELECT id, role, content, meta, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (sid,)
        ).fetchall()
        c.close()
        out = dict(row)
        out["messages"] = []
        for m in msgs:
            d = dict(m)
            # 反序列化 meta(JSON string → dict)
            if d.get("meta") and isinstance(d["meta"], str):
                try:
                    d["meta"] = json.loads(d["meta"])
                except Exception:
                    pass
            out["messages"].append(d)
        return out

def create_session(model, system="", title=""):
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    with _db_lock:
        c = db()
        c.execute(
            "INSERT INTO sessions (id, title, model, system, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (sid, title or "新对话", model, system, now, now)
        )
        c.commit()
        c.close()
    return sid

def update_session(sid, **kw):
    sets, params = [], []
    for k, v in kw.items():
        if k in ("title", "model", "system"):
            sets.append(f"{k}=?"); params.append(v)
    if not sets:
        return
    sets.append("updated_at=?"); params.append(time.time())
    params.append(sid)
    with _db_lock:
        c = db()
        c.execute(f"UPDATE sessions SET {','.join(sets)} WHERE id=?", params)
        c.commit(); c.close()

def delete_session(sid):
    with _db_lock:
        c = db()
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        c.commit(); c.close()

def delete_message(sid, msg_id, include_following=True):
    """删某条消息(及后续消息,如果 include_following)。
    返回被删的 message ids。
    """
    with _db_lock:
        c = db()
        # 找到目标 msg 在 session 里的 id 序号(按 id 顺序)
        target = c.execute(
            "SELECT id FROM messages WHERE id=? AND session_id=?",
            (msg_id, sid)
        ).fetchone()
        if not target:
            c.close()
            return []
        target_pk = target['id']  # 用 rowid/PK
        if include_following:
            ids = [r['id'] for r in c.execute(
                "SELECT id FROM messages WHERE session_id=? AND id>=? ORDER BY id",
                (sid, target_pk)
            ).fetchall()]
        else:
            ids = [target_pk]
        if ids:
            qmarks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM messages WHERE id IN ({qmarks})", ids)
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
        c.commit(); c.close()
        return ids

def list_messages(sid):
    """返 session 全部 messages 简化(不含 meta)"""
    with _db_lock:
        c = db()
        rows = c.execute(
            "SELECT id, role, content, meta, created_at FROM messages WHERE session_id=? ORDER BY id",
            (sid,)
        ).fetchall()
        c.close()
        out = []
        for m in rows:
            meta = m['meta']
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except: meta = None
            out.append({"id": m['id'], "role": m['role'], "content": m['content'], "meta": meta})
        return out

def add_message(sid, role, content, meta=None):
    with _db_lock:
        c = db()
        cur = c.execute(
            "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (sid, role, content, json.dumps(meta) if meta else None, time.time())
        )
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
        c.commit()
        mid = cur.lastrowid
        c.close()
    return mid

# ============== LiteLLM 集成 ==============
def list_models():
    """从 LiteLLM 拉模型列表"""
    try:
        req = urllib.request.Request(
            LITELLM_BASE + "/v1/models",
            headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}
        )
        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            d = json.loads(r.read())
            return d.get("data", [])
    except Exception as e:
        print(f"[WARN] list_models: {e}", file=sys.stderr)
        return []

# 缓存:auto-test 结果(避免每次都全跑)
_AUTO_TEST_CACHE = {"ts": 0, "results": {}}  # {model_id: {ok, status, error, duration_ms}}
_AUTO_TEST_TTL = 300  # 5 分钟

def auto_test_models(model_ids, timeout_per=8):
    """对每个 model 发一个最小 chat 请求,检测是否可用。
    返回 {model_id: {ok: bool, status: int, error: str, duration_ms: int}}
    用 5 分钟缓存。
    """
    import time as _t
    now = _t.time()
    # 缓存命中
    if now - _AUTO_TEST_CACHE["ts"] < _AUTO_TEST_TTL and _AUTO_TEST_CACHE["results"]:
        cached_ids = set(_AUTO_TEST_CACHE["results"].keys())
        requested = set(model_ids)
        if requested.issubset(cached_ids):
            return {k: v for k, v in _AUTO_TEST_CACHE["results"].items() if k in requested}
    results = {}
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    for mid in model_ids:
        body = json.dumps({
            "model": mid,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            LITELLM_BASE + "/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        t0 = _t.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout_per, context=ctx) as r:
                r.read()
                results[mid] = {"ok": True, "status": r.status, "duration_ms": int((_t.time()-t0)*1000)}
        except urllib.error.HTTPError as e:
            err = ""
            try: err = json.loads(e.read().decode("utf-8") or "{}").get("error", {}).get("message", "")
            except: pass
            results[mid] = {"ok": False, "status": e.code, "error": err, "duration_ms": int((_t.time()-t0)*1000)}
        except Exception as e:
            results[mid] = {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}", "duration_ms": int((_t.time()-t0)*1000)}
    _AUTO_TEST_CACHE["ts"] = now
    _AUTO_TEST_CACHE["results"] = results
    return results


# 缓存:image 模型的能力检查(对每个 image model 发最小 image 请求)
_IMAGE_TEST_CACHE = {"ts": 0, "results": {}}
_IMAGE_TEST_TTL = 300

def auto_test_image_models(model_ids, timeout_per=15):
    """对每个 image model 发最小图片请求"""
    import time as _t
    now = _t.time()
    if now - _IMAGE_TEST_CACHE["ts"] < _IMAGE_TEST_TTL and _IMAGE_TEST_CACHE["results"]:
        cached = set(_IMAGE_TEST_CACHE["results"].keys())
        if set(model_ids).issubset(cached):
            return {k: v for k, v in _IMAGE_TEST_CACHE["results"].items() if k in model_ids}
    results = {}
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    for mid in model_ids:
        body = json.dumps({"model": mid, "prompt": "test", "n": 1, "size": "256x256"}).encode("utf-8")
        req = urllib.request.Request(
            LITELLM_BASE + "/v1/images/generations",
            data=body,
            headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        t0 = _t.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout_per, context=ctx) as r:
                r.read()
                results[mid] = {"ok": True, "status": r.status, "duration_ms": int((_t.time()-t0)*1000)}
        except urllib.error.HTTPError as e:
            err = ""
            try: err = json.loads(e.read().decode("utf-8") or "{}").get("error", {}).get("message", "")
            except: pass
            results[mid] = {"ok": False, "status": e.code, "error": err, "duration_ms": int((_t.time()-t0)*1000)}
        except Exception as e:
            results[mid] = {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}", "duration_ms": int((_t.time()-t0)*1000)}
    _IMAGE_TEST_CACHE["ts"] = now
    _IMAGE_TEST_CACHE["results"] = results
    return results

# 从 config.yaml 读 image model → 中转站 映射(用于直连 gxair 等)
def get_image_model_route(model_name):
    """从 litellm config 找 image model 的 api_base + api_key。
    返回 (api_base, api_key) 或 None。"""
    try:
        import yaml
        with open(LITELLM_CONFIG) as f:
            cfg = yaml.safe_load(f) or {}
        for m in cfg.get("model_list", []):
            if m.get("model_name") == model_name:
                lp = m.get("litellm_params", {})
                return lp.get("api_base"), lp.get("api_key")
    except Exception as e:
        print(f"[WARN] get_image_model_route: {e}", file=sys.stderr)
    return None

def chat_stream(messages, model, system=None, temperature=0.7, max_tokens=None):
    """SSE 流式调 LiteLLM"""
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
    }
    if system:
        body["messages"] = [{"role": "system", "content": system}] + [m for m in messages if m["role"] != "system"]
    if max_tokens:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(
        LITELLM_BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    resp = urllib.request.urlopen(req, timeout=120, context=ctx)
    return resp  # SSE 流

def parse_sse_chunk(raw):
    """解析 SSE chunk: data: {...}"""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return {"done": True}
    try:
        return json.loads(payload)
    except Exception:
        return None

# ============== HTTP ==============
class APIHandler:
    def __init__(self):
        self.routes_get = {
            "/": self.serve_html,
            "/index.html": self.serve_html,
            "/favicon.ico": self.serve_favicon,
            "/favicon-32.png": self.serve_favicon,
            "/logo.png": self.serve_logo,
            "/api/models": self.api_models,
            "/api/sessions": self.api_sessions_list,
            "/api/config": self.api_config,
            "/health": lambda d: {"ok": True, "ts": time.time()},
        }
        self.routes_post = {
            "/api/sessions": self.api_sessions_create,
            "/api/sessions/get": self.api_sessions_get,
            "/api/sessions/delete": self.api_sessions_delete,
            "/api/sessions/rename": self.api_sessions_rename,
            "/api/sessions/update": self.api_sessions_update,
            "/api/sessions/messages/delete": self.api_sessions_messages_delete,
            "/api/sessions/search": self.api_sessions_search,
            "/api/sessions/compare": self.api_sessions_compare,
            "/api/sessions/export": self.api_sessions_export,
            "/api/messages": self.api_messages_send,   # 流式 SSE
            "/api/images/generations": self.api_images_generate,
            # 直接 API 配置(绕过 LiteLLM,直连上游拉模型)
            "/api/direct/test": self.api_direct_test,
            "/api/direct/import": self.api_direct_import,
            "/api/direct/list": self.api_direct_list,
            # Settings tab: Provider 管理(5 tab 设置页面之 Models)
            "/api/providers/list": self.api_providers_list,
            "/api/providers/list-with-models": self.api_providers_list_with_models,
            "/api/providers/save": self.api_providers_save,
            "/api/providers/delete": self.api_providers_delete,
            "/api/providers/test": self.api_providers_test,
            "/api/providers/fetch": self.api_providers_fetch,
            "/api/providers/import": self.api_providers_import,
            # 任务(替代 history 页)
            "/api/tasks/list": self.api_tasks_list,
            "/api/tasks/get": self.api_tasks_get,
            "/api/tasks/create": self.api_tasks_create,
            "/api/tasks/update": self.api_tasks_update,
            "/api/tasks/delete": self.api_tasks_delete,
            "/api/tasks/run": self.api_tasks_run,
            # Obsidian RAG
            "/api/obsidian/search": self.api_obsidian_search,
            "/api/obsidian/suggestions": self.api_obsidian_suggestions,
            "/api/obsidian/stats": self.api_obsidian_stats,
            "/api/obsidian/reindex": self.api_obsidian_reindex,
            "/api/obsidian/get": self.api_obsidian_get,
            "/api/obsidian/set-vault": self.api_obsidian_set_vault,
            "/api/obsidian/categorize": self.api_obsidian_categorize,
            "/api/obsidian/categories": self.api_obsidian_categories,
        }

    # ---------- HTML ----------
    def serve_html(self, data):
        if not os.path.exists(INDEX_HTML):
            return {"_raw": f"<h1>index.html not found at {INDEX_HTML}</h1>", "content_type": "text/html; charset=utf-8", "status": 200}
        with open(INDEX_HTML, "r", encoding="utf-8") as f:
            body = f.read()
        # 注入版本 banner(让用户立刻判断看到的是新还是旧)
        import time as _t
        version_banner = f'\n<!-- DoDox v2.0 build-time:{_t.strftime("%Y-%m-%d %H:%M:%S")} 5-tab-modal 0-iframe api-key-field -->\n'
        # 在 </head> 前插入
        body = body.replace("</head>", version_banner + "</head>", 1)
        return {"_raw": body, "content_type": "text/html; charset=utf-8", "status": 200}

    def serve_favicon(self, data):
        """返 favicon-32.png"""
        path = os.path.join(STATIC_DIR, 'favicon-32.png')
        if not os.path.exists(path):
            return {"error": "favicon not found", "status": 404}
        with open(path, 'rb') as f:
            return {"_raw_bytes": f.read(), "content_type": "image/png", "status": 200}

    def serve_logo(self, data):
        """返 logo.png"""
        path = os.path.join(STATIC_DIR, 'logo.png')
        if not os.path.exists(path):
            return {"error": "logo not found", "status": 404}
        with open(path, 'rb') as f:
            return {"_raw_bytes": f.read(), "content_type": "image/png", "status": 200}

    def api_models(self, data):
        """返 LiteLLM 模型列表 + auto-test 状态。
        加 ?ok_only=1 参数只返 ok 的(走一遍 6s/模型的可达性测试,有 5min 缓存)。
        image 类 model 用 image endpoint 测试(而不是 chat)。"""
        d = data or {}
        ok_only = d.get("ok_only") in (1, "1", True, "true")
        skip_test = d.get("skip_test") in (1, "1", True, "true")
        all_models = list_models()
        test_results = {}
        if ok_only and not skip_test and all_models:
            import re as _re
            mids = [m["id"] for m in all_models]
            # image 类 model 走 image endpoint,其他走 chat
            chat_mids = [m for m in mids if not _re.search(r'image|dalle|flux|midjourney', m, _re.I)]
            image_mids = [m for m in mids if _re.search(r'image|dalle|flux|midjourney', m, _re.I)]
            if chat_mids:
                test_results.update(auto_test_models(chat_mids, timeout_per=6))
            if image_mids:
                test_results.update(auto_test_image_models(image_mids, timeout_per=15))
            for m in all_models:
                r = test_results.get(m["id"], {})
                m["ok"] = r.get("ok", False)
                m["status"] = r.get("status", 0)
                m["error"] = r.get("error", "")
            models = [m for m in all_models if m.get("ok")]
        else:
            models = all_models
        return {
            "ok": True, "models": models, "count": len(models),
            "total": len(all_models), "tested": bool(test_results),
            "tests": test_results, "status": 200
        }

    def api_config(self, data):
        return {"ok": True, "litellm_base": LITELLM_BASE, "port": PORT, "status": 200}

    # ---------- Sessions ----------
    def api_sessions_list(self, data):
        return {"ok": True, "sessions": list_sessions(), "status": 200}

    def api_sessions_create(self, data):
        d = data or {}
        model = (d.get("model") or "").strip()
        system = (d.get("system") or "").strip()
        title = (d.get("title") or "").strip()
        if not model:
            return {"error": "model 必填", "status": 400}
        sid = create_session(model, system, title)
        return {"ok": True, "id": sid, "status": 200}

    def api_sessions_delete(self, data):
        d = data or {}
        sid = d.get("id")
        if not sid:
            return {"error": "id 必填", "status": 400}
        delete_session(sid)
        return {"ok": True, "id": sid, "status": 200}

    def api_sessions_rename(self, data):
        d = data or {}
        sid = d.get("id")
        title = (d.get("title") or "").strip()
        if not sid:
            return {"error": "id 必填", "status": 400}
        if title:
            update_session(sid, title=title)
        return {"ok": True, "id": sid, "status": 200}

    def api_sessions_get(self, data):
        d = data or {}
        sid = d.get("id")
        if not sid:
            return {"error": "id 必填", "status": 400}
        sess = get_session(sid)
        if not sess:
            return {"error": "session 不存在", "status": 404}
        return {"ok": True, "session": sess, "status": 200}

    def api_sessions_update(self, data):
        d = data or {}
        sid = d.get("id")
        if not sid:
            return {"error": "id 必填", "status": 400}
        kw = {}
        if "title" in d: kw["title"] = d["title"]
        if "model" in d: kw["model"] = d["model"]
        if "system" in d: kw["system"] = d["system"]
        update_session(sid, **kw)
        return {"ok": True, "id": sid, "status": 200}

    def api_sessions_messages_delete(self, data):
        """删某条消息(及后续)
        body: {sid, msg_id, include_following: true}
        """
        d = data or {}
        sid = d.get("sid") or d.get("id")  # 兼容两种叫法
        msg_id = d.get("msg_id")
        if not sid or msg_id is None:
            return {"error": "sid 和 msg_id 必填", "status": 400}
        include = d.get("include_following", True)
        deleted = delete_message(sid, msg_id, include)
        return {"ok": True, "sid": sid, "deleted_ids": deleted, "deleted_count": len(deleted), "status": 200}

    def api_sessions_search(self, data):
        """全消息搜索
        body: {q: "keyword", limit?: 50}
        → {ok, hits: [{session_id, session_title, role, content, created_at, msg_id}]}
        """
        d = data or {}
        q = (d.get("q") or "").strip()
        if not q:
            return {"ok": True, "hits": [], "count": 0, "status": 200}
        limit = int(d.get("limit") or 50)
        with _db_lock:
            c = db()
            # 用 LIKE,大小写不敏感(FTS 没启用)
            like = f"%{q}%"
            rows = c.execute(
                """SELECT m.id AS msg_id, m.session_id, m.role, m.content, m.created_at,
                          s.title AS session_title
                   FROM messages m LEFT JOIN sessions s ON m.session_id = s.id
                   WHERE m.content LIKE ? COLLATE NOCASE
                   ORDER BY m.created_at DESC LIMIT ?""",
                (like, limit)
            ).fetchall()
            c.close()
        hits = []
        for r in rows:
            # 截取 content 命中片段(±30 字符)
            content = r["content"]
            idx = content.lower().find(q.lower())
            if idx >= 0:
                start = max(0, idx - 30)
                end = min(len(content), idx + len(q) + 30)
                snippet = content[start:end]
                if start > 0: snippet = "..." + snippet
                if end < len(content): snippet = snippet + "..."
            else:
                snippet = content[:80] + ("..." if len(content) > 80 else "")
            hits.append({
                "msg_id": r["msg_id"],
                "session_id": r["session_id"],
                "session_title": r["session_title"] or "新对话",
                "role": r["role"],
                "content": content,
                "snippet": snippet,
                "created_at": r["created_at"],
            })
        return {"ok": True, "q": q, "hits": hits, "count": len(hits), "status": 200}

    def api_sessions_compare(self, data):
        """跨会话对比
        body: {ids: [id1, id2, ...]} (前 2 个生效)
        算法:对每对相同 user prompt(user content 前 80 字符当 key),比 assistant 回复状态。
        → {ok, sessions: [...], buckets: {RECOVERED/REGRESSED/STILL_OK/STILL_FAIL/NEW: [...]}}
        """
        d = data or {}
        ids = (d.get("ids") or [])[:2]
        if len(ids) < 2:
            return {"error": "至少需要 2 个 id", "status": 400}
        recs = []
        for sid in ids:
            sess = get_session(sid)
            if sess: recs.append(sess)
        if len(recs) < 2:
            return {"error": "会话不存在", "status": 404}
        recs.sort(key=lambda r: (r.get("ts", 0), r["id"]))
        earlier, later = recs

        def get_user_state(sess):
            """返 {(prompt 前 80 字符): status} status ∈ 'ok'|'fail'|'unknown'"""
            out = {}
            msgs = sess.get("messages", [])
            for i, m in enumerate(msgs):
                if m["role"] != "user": continue
                key = m["content"][:80]
                # 找紧跟的 assistant
                for j in range(i+1, len(msgs)):
                    if msgs[j]["role"] == "assistant":
                        am = msgs[j]
                        meta = am.get("meta")
                        if isinstance(meta, str):
                            try: meta = json.loads(meta)
                            except: meta = {}
                        if not isinstance(meta, dict): meta = {}
                        if meta.get("error"):
                            out[key] = "fail"
                        else:
                            out[key] = "ok" if (am.get("content") or "").strip() else "unknown"
                        break
                else:
                    out[key] = "unknown"
            return out

        e_state = get_user_state(earlier)
        l_state = get_user_state(later)
        buckets = {"RECOVERED": [], "REGRESSED": [], "STILL_OK": [], "STILL_FAIL": [], "NEW_OK": [], "NEW_FAIL": []}
        for k, l_status in l_state.items():
            e_status = e_state.get(k)
            if e_status is None:
                if l_status == "ok": buckets["NEW_OK"].append({"q": k})
                elif l_status == "fail": buckets["NEW_FAIL"].append({"q": k})
            else:
                if e_status == l_status:
                    if l_status == "ok": buckets["STILL_OK"].append({"q": k})
                    elif l_status == "fail": buckets["STILL_FAIL"].append({"q": k})
                elif e_status == "fail" and l_status == "ok": buckets["RECOVERED"].append({"q": k})
                elif e_status == "ok" and l_status == "fail": buckets["REGRESSED"].append({"q": k})
        return {"ok": True, "sessions": [
            {"id": r["id"], "title": r.get("title", ""), "ts": r.get("ts", 0),
             "ts_label": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("ts", 0))),
             "summary": r.get("summary", {})}
            for r in recs
        ], "buckets": buckets, "status": 200}

    def api_sessions_export(self, data):
        """导出单个会话为 Markdown
        body: {id}
        → 返回 Markdown 文本
        """
        d = data or {}
        sid = d.get("id")
        if not sid:
            return {"error": "id 必填", "status": 400}
        sess = get_session(sid)
        if not sess:
            return {"error": "session 不存在", "status": 404}
        lines = [
            f"# {sess.get('title') or '新对话'}",
            "",
            f"- **模型**: `{sess.get('model', '')}`",
            f"- **时间**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(sess.get('ts', 0)))}",
            f"- **消息数**: {len(sess.get('messages', []))}",
            "",
            "---",
            "",
        ]
        if sess.get("system"):
            lines += [f"**系统提示**: {sess['system']}", "", "---", ""]
        for m in sess.get("messages", []):
            role = "🧑 你" if m["role"] == "user" else "🤖 AI"
            lines.append(f"## {role}")
            lines.append("")
            lines.append(m["content"])
            lines.append("")
        md = "\n".join(lines)
        return {"ok": True, "id": sid, "title": sess.get("title", ""), "markdown": md, "status": 200}

    # ---------- Messages (SSE 流式) ----------
    def api_messages_send(self, data):
        """发消息 + 流式响应(SSE)。
        这是关键端点:接收用户消息, 存 DB, 调 LiteLLM(直连 provider), 把 SSE 流透传给前端。
        demo 模式:model="__demo__" → 返 echo(用于 UI 测试)
        RAG 模式(d.get("rag") != False):先搜 obsidian vault,拼 context 喂 LLM,SSE 流先发 event:refs
        """
        d = data or {}
        sid = d.get("id") or d.get("session_id")
        user_msg = (d.get("message") or "").strip()
        route = d.get("_route") or {}
        if not sid or not user_msg:
            return {"error": "id + message 必填", "status": 400}
        sess = get_session(sid)
        if not sess:
            return {"error": "session 不存在", "status": 404}
        model = sess.get("model") or ""
        system = sess.get("system") or ""
        if not model:
            return {"error": "session 没选 model", "status": 400}
        # 存 user 消息
        add_message(sid, "user", user_msg)
        # demo 模式:返 echo(用于 UI 测试,无上游依赖)
        if model == "__demo__":
            msgs = [{"role": m["role"], "content": m["content"]} for m in sess.get("messages", [])]
            msgs.append({"role": "user", "content": user_msg})
            return self._demo_response(sid, msgs, user_msg)
        # 构造完整 messages
        prepared = prepare_chat_request(
            sess,
            user_msg,
            route,
            d.get("rag", True),
            lambda query: _obsidian_search(query, top_k=OBSIDIAN_TOP_K),
            _obsidian_build_context,
            _obsidian_format_refs,
        )
        if not sess.get("title") or sess.get("title") == "新对话":
            update_session(sid, title=prepared["title"])
        if prepared["rag_hits"]:
            return {
                "_sse": True,
                "sid": sid,
                "msgs": prepared["messages"],
                "model": model,
                "system": prepared["system"],
                "route": prepared["route"],
                "rag_refs": prepared["refs"],
                "rag_hit_count": len(prepared["rag_hits"]),
            }
        return self._sse_response(sid, prepared["messages"], model, prepared["system"], route=prepared["route"])

    def _demo_response(self, sid, msgs, user_msg):
        """demo echo 响应(用于 UI 测试,无上游依赖)
        返 SSE 流,显示上游 ban 提示 + echo 模式可用
        """
        return {"_sse_demo": True, "sid": sid, "msgs": msgs, "user_msg": user_msg}

    def _sse_response(self, sid, msgs, model, system, route=None, model_id=None, model_full=None):
        """构造一个 _sse_stream 对象,handler 检测到它就走流式输出
        route: dict {source, modelId, baseUrl, apiKey, providerName} - 直连 provider 模式
        model_id: 已 strip prefix 的纯名(用于调 LiteLLM)
        model_full: 完整路径(用于存 db 区分)
        """
        return {"_sse": True, "sid": sid, "msgs": msgs, "model": model_id or model, "model_full": model_full or model, "system": system, "route": route or {}}

    # ---------- Direct API(直连上游)----------
    def api_direct_test(self, data):
        """测试直连上游 API 联通性
        body: {base: 'https://api.gxair.cn/v1', key: 'sk-xxx'}
        → 调 GET {base}/models,返 {ok, models: [{id}], count}
        """
        import time as _t
        d = data or {}
        base = (d.get("base") or "").strip().rstrip("/")
        key = (d.get("key") or "").strip()
        if not base or not key:
            return {"error": "base 和 key 必填", "status": 400}
        # 智能判断:base 含 /v1 的话只加 /models
        if base.endswith("/v1"):
            url = base + "/models"
        else:
            url = base + "/v1/models"
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                method="GET",
            )
            ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
            t0 = _t.time()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                resp = json.loads(r.read())
                ms = resp.get("data", [])
                # 标 image 类
                chat, image = [], []
                for m in ms:
                    mid = m.get("id", "")
                    if not mid: continue
                    if _re.search(r'image|dalle|flux|midjourney', mid, _re.I):
                        image.append(mid)
                    else:
                        chat.append(mid)
                return {
                    "ok": True, "base": base, "count": len(ms),
                    "models": ms,
                    "chat_count": len(chat), "image_count": len(image),
                    "chat_models": chat, "image_models": image,
                    "duration_ms": int((_t.time()-t0)*1000),
                    "status": 200,
                }
        except urllib.error.HTTPError as e:
            try: err_body = json.loads(e.read().decode("utf-8") or "{}")
            except: err_body = {}
            return {"ok": False, "status": e.code,
                    "error": (err_body.get("error") or {}).get("message") or json.dumps(err_body)[:200],
                    "via": "direct"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "via": "direct", "status": 500}

    def api_direct_import(self, data):
        """把直连 API 拿到的模型写入 LiteLLM config.yaml
        body: {base, key, model_ids: [...], prefix: 'gxair', overwrite: true}
        → 写 litellm/config.yaml model_list,返 {ok, added, total}
        """
        d = data or {}
        base = (d.get("base") or "").strip().rstrip("/")
        key = (d.get("key") or "").strip()
        model_ids = d.get("model_ids") or []
        prefix = (d.get("prefix") or "direct").strip().lower()
        overwrite = d.get("overwrite", True)
        if not base or not key or not model_ids:
            return {"error": "base / key / model_ids 必填", "status": 400}
        try:
            import yaml as _yaml
            with open(LITELLM_CONFIG) as f:
                cfg = _yaml.safe_load(f) or {}
            cfg.setdefault("model_list", [])
            existing = {m.get("model_name") for m in cfg["model_list"]}
            added, skipped, replaced = 0, 0, 0
            for mid in model_ids:
                # 完整 model_name 加前缀(防止多中转站重名)
                full_name = f"{prefix}/{mid}" if prefix and not mid.startswith(f"{prefix}/") else mid
                litellm_model = f"openai/{mid}"
                new_entry = {
                    "model_name": full_name,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_base": base,
                        "api_key": key,
                        "headers": {"User-Agent": "curl/7.88.1"},  # 绕开 WAF
                    },
                }
                if full_name in existing and overwrite:
                    # 替换
                    cfg["model_list"] = [m for m in cfg["model_list"] if m.get("model_name") != full_name]
                    cfg["model_list"].append(new_entry)
                    replaced += 1
                elif full_name in existing:
                    skipped += 1
                else:
                    cfg["model_list"].append(new_entry)
                    added += 1
            with open(LITELLM_CONFIG, 'w') as f:
                _yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
            return {
                "ok": True, "added": added, "replaced": replaced, "skipped": skipped,
                "total": len(cfg["model_list"]),
                "config_path": LITELLM_CONFIG,
                "status": 200,
            }
        except Exception as e:
            import traceback
            return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(), "status": 500}

    def api_direct_list(self, data):
        """列已保存的 model_names(供前端比对)"""
        try:
            import yaml as _yaml
            with open(LITELLM_CONFIG) as f:
                cfg = _yaml.safe_load(f) or {}
            return {
                "ok": True,
                "models": [m.get("model_name") for m in cfg.get("model_list", []) if m.get("model_name")],
                "config_path": LITELLM_CONFIG,
                "status": 200,
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "status": 500}

    # ============== Providers (Settings > Models) ==============
    def api_providers_list(self, data):
        """列已保存的 API providers"""
        config = build_config()
        result = safe_list_providers(config)
        return {"ok": True, "providers": result, "count": len(result), "status": 200}

    def api_providers_list_with_models(self, data):
        """列 providers + 每个 provider 的完整 model 列表(不返回 api_key)"""
        config = build_config()
        result = safe_list_providers(config)
        return {"ok": True, "providers": result, "count": len(result), "status": 200}

    def api_providers_save(self, data):
        """保存/更新 provider(name, base_url, api_key, model_type)"""
        d = data or {}
        name = (d.get("name") or "").strip()
        base_url = (d.get("base_url") or "").strip().rstrip("/")
        api_key = (d.get("api_key") or "").strip()
        model_type = (d.get("model_type") or "chat").strip()
        if model_type not in ("chat", "image"):
            model_type = "chat"
        pid = d.get("id")
        if not name or not base_url or not api_key:
            return {"error": "name / base_url / api_key 必填", "status": 400}
        config = build_config()
        provider_id = safe_save_provider(config, pid, name, base_url, api_key, model_type)
        return {"ok": True, "id": provider_id, "name": name, "model_type": model_type, "status": 200}

    def api_providers_delete(self, data):
        """删除 provider"""
        d = data or {}
        pid = d.get("id")
        if not pid:
            return {"error": "id 必填", "status": 400}
        with _db_lock:
            c = db()
            c.execute("DELETE FROM providers WHERE id = ?", (pid,))
            c.commit()
            c.close()
        return {"ok": True, "id": pid, "status": 200}

    def api_providers_test(self, data):
        """测试 provider 连通性(GET {base}/models)"""
        d = data or {}
        pid = d.get("id")
        base_url = (d.get("base_url") or "").strip().rstrip("/")
        api_key = (d.get("api_key") or "").strip()
        config = build_config()
        provider = None
        if pid and (not base_url or not api_key):
            provider = get_provider_with_secret(config, pid)
            if provider:
                base_url = provider["base_url"]
                api_key = provider["api_key"]
        if not base_url or not api_key:
            return {"error": "id 必填,或 base_url + api_key", "status": 400}
        if provider is None:
            provider = {
                "id": pid or "__adhoc__",
                "base_url": base_url,
                "api_key": api_key,
            }
        try:
            models = fetch_provider_models(config, provider)
            duration_ms = 0
            if pid:
                with _db_lock:
                    c = db()
                    c.execute(
                        "UPDATE providers SET status=?, models=?, last_test=?, updated_at=? WHERE id=?",
                        ("ok", json.dumps(models), time.time(), time.time(), pid)
                    )
                    c.commit()
                    c.close()
            return {
                "ok": True,
                "result": "ok",
                "base_url": base_url,
                "count": len(models),
                "models": [{"id": model_id, "object": "model"} for model_id in models],
                "duration_ms": duration_ms,
            }
        except Exception as e:
            if pid:
                with _db_lock:
                    c = db()
                    c.execute(
                        "UPDATE providers SET status=?, last_test=?, updated_at=? WHERE id=?",
                        ("error", time.time(), time.time(), pid)
                    )
                    c.commit()
                    c.close()
            return {"ok": False, "result": "error", "error": "{}: {}".format(type(e).__name__, e)}

    def api_providers_fetch(self, data):
        """拉取 provider 全部模型(GET /v1/models)
        body: {id}
        → 缓存到 db.models 字段
        """
        d = data or {}
        pid = d.get("id")
        if not pid:
            return {"error": "id 必填", "status": 400}
        # 直接调 test(它会拉 + 缓存)
        r = self.api_providers_test({"id": pid})
        return r

    def api_providers_import(self, data):
        """把 provider 选中的模型写入 LiteLLM config.yaml
        body: {id, model_ids: [...], prefix?: 'gxair'}
        """
        d = data or {}
        pid = d.get("id")
        model_ids = d.get("model_ids") or []
        if not pid:
            return {"error": "id 必填", "status": 400}
        if not model_ids:
            return {"error": "model_ids 必填", "status": 400}
        with _db_lock:
            c = db()
            row = c.execute("SELECT name, base_url, api_key FROM providers WHERE id = ?", (pid,)).fetchone()
            c.close()
        if not row:
            return {"error": "provider 不存在", "status": 404}
        prefix = (d.get("prefix") or row["name"].lower().replace(" ", "-") or "provider").strip()
        # 复用 api_direct_import
        return self.api_direct_import({
            "base": row["base_url"],
            "key": row["api_key"],
            "model_ids": model_ids,
            "prefix": prefix,
            "overwrite": True,
        })

    # ---------- Images ----------
    def api_images_generate(self, data):
        """图片生成:优先直连中转站(从 config.yaml 读 api_base + api_key),
        绕开 LiteLLM 的 120s 内部 proxy read timeout。
        回退方案:用 LiteLLM 转发(18s timeout,失败快速返错)。
        支持 provider ID (p_xxx) 自动解析为 base_url + api_key + model。"""
        d = data or {}
        prompt = (d.get("prompt") or "").strip()
        model = (d.get("model") or "gpt-image-2").strip()
        n = int(d.get("n") or 1)
        size = d.get("size") or "1024x1024"
        if not prompt:
            return {"error": "prompt 必填", "status": 400}
        # 如果 model 是 provider ID (p_xxx),从 DB 解析为 base_url + api_key + 实际 model name
        if model.startswith("p_"):
            with _db_lock:
                c = db()
                row = c.execute("SELECT name, base_url, api_key, models, model_type FROM providers WHERE id = ?", (model,)).fetchone()
                c.close()
            if row:
                models_list = []
                if row["models"]:
                    try: models_list = json.loads(row["models"])
                    except: pass
                # 选第一个模型,或用默认图片模型名
                actual_model = models_list[0] if models_list else "gpt-image-2"
                # 去掉可能的 provider_id: 前缀
                if ":" in actual_model:
                    actual_model = actual_model.split(":", 1)[1]
                body = {"model": actual_model, "prompt": prompt, "n": n, "size": size}
                # 直连上游
                api_base = row["base_url"].rstrip("/")
                url = api_base + ("/images/generations" if api_base.endswith("/v1") else "/v1/images/generations")
                try:
                    req = urllib.request.Request(
                        url,
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Authorization": f"Bearer {row['api_key']}", "Content-Type": "application/json"},
                        method="POST",
                    )
                    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
                    with urllib.request.urlopen(req, timeout=600, context=ctx) as r:
                        resp = json.loads(r.read())
                        imgs = []
                        for d_item in resp.get("data", []):
                            if d_item.get("b64_json"):
                                imgs.append({"data": f"data:image/png;base64,{d_item['b64_json']}"})
                            elif d_item.get("url"):
                                imgs.append({"data": d_item["url"]})
                        return {"ok": True, "images": imgs, "model": actual_model, "via": "provider-direct", "status": 200}
                except urllib.error.HTTPError as e:
                    try:
                        err_body = json.loads(e.read().decode("utf-8") or "{}")
                    except Exception:
                        err_body = {}
                    return {
                        "ok": False, "status": e.code,
                        "error": (err_body.get("error") or {}).get("message") or json.dumps(err_body)[:300],
                        "via": "provider-direct",
                    }
                except Exception as e:
                    print(f"[WARN] provider-direct image failed ({type(e).__name__}: {e})", file=sys.stderr)
                    # 不回退到 config.yaml 路径(可能用旧 key),直接返错
                    return {
                        "ok": False, "status": 504,
                        "error": f"图片生成超时或失败: {type(e).__name__}: {e}",
                        "via": "provider-direct",
                    }
            else:
                return {"error": f"provider {model} 不存在", "status": 404}
        else:
            return {"error": f"model 必须是 provider ID (p_xxx), 收到: {model}", "status": 400}

    # ---------- Router ----------
    def handle(self, method, path, data):
        # 简单剥离 query(避免 urllib.parse 命名空间诡异)
        q = '?'
        if q in path:
            path_only = path.split(q, 1)[0]
            query_str = path.split(q, 1)[1]
        else:
            path_only = path
            query_str = ''
        if not path_only:
            path_only = '/'

        # GET + query 解析到 data(让 GET 端点支持 ?ok_only=1 这种)
        merged_data = dict(data or {})
        if query_str and method == "GET":
            for kv in query_str.split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    merged_data[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)
                else:
                    merged_data[urllib.parse.unquote_plus(kv)] = True

        if method == "GET":
            h = self.routes_get.get(path_only)
        else:
            h = self.routes_post.get(path_only)
        if h is None:
            if method == "GET" and not path_only.startswith("/api/") and not path_only.startswith("/favicon"):
                return self.serve_html(None)
            return {"error": f"Not Found: {method} {path_only}", "status": 404}
        try:
            return h(merged_data)
        except Exception as e:
            import traceback
            return {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(), "status": 500}

    # ============== Tasks (替代 history 页) ==============
    def api_tasks_list(self, data):
        """列任务,可选 status 过滤。body: {status?: pending/running/done/failed}"""
        d = data or {}
        status = (d.get("status") or "").strip()
        with _db_lock:
            c = db()
            if status:
                rows = c.execute("SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC LIMIT 200", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 200").fetchall()
            c.close()
        result = []
        for r in rows:
            d2 = dict(r)
            # 不返 prompt 全文给 list(详情接口才返),节省带宽
            d2["prompt_preview"] = (d2.get("prompt") or "")[:80]
            d2.pop("prompt", None)
            d2.pop("result", None)
            result.append(d2)
        return {"ok": True, "tasks": result, "count": len(result), "status": 200}

    def api_tasks_get(self, data):
        """任务详情,含完整 prompt + result。body: {id}"""
        d = data or {}
        tid = d.get("id")
        if not tid:
            return {"error": "id 必填", "status": 400}
        with _db_lock:
            c = db()
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            c.close()
        if not row:
            return {"error": "任务不存在", "status": 404}
        return {"ok": True, "task": dict(row), "status": 200}

    def api_tasks_create(self, data):
        """新建任务。body: {title, prompt, model?, skill?, source_sid?, source_msg_id?}"""
        d = data or {}
        title = (d.get("title") or "").strip()
        prompt = (d.get("prompt") or "").strip()
        if not title or not prompt:
            return {"error": "title 和 prompt 必填", "status": 400}
        model = (d.get("model") or "").strip() or None
        skill = (d.get("skill") or "chat").strip()
        source_sid = (d.get("source_sid") or "").strip() or None
        source_msg_id = d.get("source_msg_id")
        import time as _t
        now = _t.time()
        with _db_lock:
            c = db()
            cur = c.execute(
                "INSERT INTO tasks (title, prompt, model, skill, status, source_sid, source_msg_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (title, prompt, model, skill, "pending", source_sid, source_msg_id, now, now)
            )
            tid = cur.lastrowid
            c.commit()
            c.close()
        return {"ok": True, "id": tid, "title": title, "status": 200}

    def api_tasks_update(self, data):
        """改任务字段。body: {id, title?, prompt?, model?, status?}"""
        d = data or {}
        tid = d.get("id")
        if not tid:
            return {"error": "id 必填", "status": 400}
        import time as _t
        fields, vals = [], []
        for k in ("title", "prompt", "model", "status"):
            if k in d and d[k] is not None:
                fields.append(f"{k}=?")
                vals.append(d[k])
        if not fields:
            return {"error": "没有要更新的字段", "status": 400}
        fields.append("updated_at=?"); vals.append(_t.time())
        vals.append(tid)
        with _db_lock:
            c = db()
            c.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", vals)
            c.commit()
            c.close()
        return {"ok": True, "id": tid, "status": 200}

    def api_tasks_delete(self, data):
        """删除任务。body: {id}"""
        d = data or {}
        tid = d.get("id")
        if not tid:
            return {"error": "id 必填", "status": 400}
        with _db_lock:
            c = db()
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))
            c.commit()
            c.close()
        return {"ok": True, "id": tid, "status": 200}

    def api_tasks_run(self, data):
        """执行任务 — 返 _sse_task 标志 + task/route 数据,让 Req._handle_sse_task 写 SSE 流。
        body: {id, _route?: {source, modelId, baseUrl, apiKey, providerName}}"""
        d = data or {}
        tid = d.get("id")
        if not tid:
            return {"error": "id 必填", "status": 400}
        with _db_lock:
            c = db()
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            c.close()
        if not row:
            return {"error": "任务不存在", "status": 404}
        route = d.get("_route") or {}
        return {"_sse_task": True, "task": dict(row), "route": route, "status": 200}

    # ============== Obsidian RAG ==============
    def api_obsidian_search(self, data):
        """关键词检索 vault。body: {q: query, k?: int}"""
        d = data or {}
        q = (d.get("q") or "").strip()
        if not q:
            return {"error": "q 必填", "status": 400}
        k = d.get("k") or OBSIDIAN_TOP_K
        try:
            k = int(k)
        except Exception:
            k = OBSIDIAN_TOP_K
        hits = _obsidian_search(q, top_k=k)
        return {
            "ok": True, "query": q, "count": len(hits),
            "hits": _obsidian_format_refs(hits),
            "context": _obsidian_build_context(q, hits),
            "status": 200,
        }

    def api_obsidian_suggestions(self, data):
        """返 n 个基于 vault 标题的快捷问题。body: {n?: int}"""
        d = data or {}
        n = d.get("n") or 4
        try: n = int(n)
        except Exception: n = 4
        return {"ok": True, "suggestions": _obsidian_suggestions(n), "status": 200}

    def api_obsidian_stats(self, data):
        """vault 统计"""
        return {"ok": True, **_obsidian_stats(), "status": 200}

    def api_obsidian_reindex(self, data):
        """强制重建索引"""
        try:
            _obsidian_index_vault(force=True)
            return {"ok": True, "count": len(_obsidian_index), "status": 200}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "status": 500}

    def api_obsidian_get(self, data):
        """读 vault 单篇原文。body: {path: rel_path}"""
        d = data or {}
        rel = (d.get("path") or "").strip()
        if not rel:
            return {"error": "path 必填", "status": 400}
        # 安全:只允许 vault 内
        full = os.path.normpath(os.path.join(OBSIDIAN_VAULT, rel))
        if not full.startswith(os.path.normpath(OBSIDIAN_VAULT)):
            return {"error": "非法路径", "status": 400}
        if not os.path.isfile(full):
            return {"error": "文件不存在", "status": 404}
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"ok": True, "path": rel, "title": os.path.splitext(os.path.basename(rel))[0], "content": content, "status": 200}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "status": 500}

    def api_obsidian_set_vault(self, data):
        """改 vault 路径(空字符串恢复默认)。body: {path: string}"""
        d = data or {}
        new_path = d.get("path") or ""
        result_path, ok, err = _obsidian_set_vault(new_path)
        if not ok:
            return {"error": err, "vault_path": result_path, "status": 500}
        return {"ok": True, "vault_path": result_path, "count": len(_obsidian_index), "status": 200}

    def api_obsidian_categorize(self, data):
        """用 LLM 给所有 .md 打 category(学习/运动/习惯/教育/其他) + 1 句 summary"""
        ok, msg, stats = _obsidian_categorize()
        if not ok:
            return {"error": msg, "status": 500}
        return {"ok": True, "message": msg, "stats": stats, "by_category": _obsidian_list_categories(), "status": 200}

    def api_obsidian_categories(self, data):
        """返所有分类 + 计数,以及单篇(可选 ?path=)"""
        d = data or {}
        path = d.get("path")
        if path:
            cat = _obsidian_get_doc_category(path)
            return {"ok": True, "category": cat, "by_category": _obsidian_list_categories(), "status": 200}
        return {"ok": True, "by_category": _obsidian_list_categories(), "status": 200}

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    api_handler = None

def make_handler(api):
    class Req(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _dispatch(self, method):
            data = None
            if method == "POST":
                n = int(self.headers.get("Content-Length", 0))
                if n > 0:
                    raw = self.rfile.read(n).decode("utf-8", errors="replace")
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {"_raw": raw}

            r = self.server.api_handler.handle(method, self.path, data)
            status = r.pop("status", 200)

            # SSE 流式响应
            if r.get("_sse"):
                self._handle_sse(r)
                return
            # demo echo 模式(无上游依赖,纯 UI 测试)
            if r.get("_sse_demo"):
                self._handle_sse_demo(r)
                return
            # 任务执行模式(直连 provider + 写 tasks.result,不存 messages)
            if r.get("_sse_task"):
                self._handle_sse_task(r)
                return

            # HTML
            if "_raw" in r and r.get("content_type", "").startswith("text/html"):
                body = r["_raw"].encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", r["content_type"])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                # 最强力防 cache:no-store + ETag(每次内容变 ETag 也变,强 cache 也失效)
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                # ETag 基于内容 hash(内容变 → ETag 变 → 浏览器必须重拉)
                import hashlib
                etag = hashlib.md5(body).hexdigest()[:16]
                self.send_header("ETag", f'"{etag}"')
                self.end_headers()
                self.wfile.write(body)
                return

            # 二进制(如 png/favicon)
            if "_raw_bytes" in r:
                body = r["_raw_bytes"]
                self.send_response(status)
                self.send_header("Content-Type", r.get("content_type", "application/octet-stream"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=3600")  # 1h 缓存
                self.end_headers()
                self.wfile.write(body)
                return

            body = json.dumps(r, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _handle_sse_demo(self, r):
            """demo echo:无上游依赖,模拟 AI 回复(用于 UI 测试)"""
            import time as _t
            sid = r["sid"]
            user_msg = r.get("user_msg", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            tip = "🎭 **Demo Echo 模式**(无上游依赖)。\n\n你看到这条消息说明:\n- ✅ UI 渲染正常\n- ✅ SSE 流式正常\n- ✅ 消息收发正常\n\n"
            try: self.wfile.write(f"data: {json.dumps({'event':'log','text':tip.strip()}, ensure_ascii=False)}\n\n".encode("utf-8")); self.wfile.flush()
            except: pass
            reply = f"你说: **{user_msg}**\n\n"
            reply += "📋 **当前所有中转站状态**:\n"
            reply += "- ❌ gxair: 401 Invalid token\n"
            reply += "- ❌ kaudex: 余额 0\n"
            reply += "- ❌ aiapi1: 1010 CF ban\n"
            reply += "- ❌ newapi: 401\n\n"
            reply += "💡 **恢复方法**:\n"
            reply += "1. 去 gxair 后台 → 重新生成 token\n"
            reply += "2. 或充值 kaudex\n"
            reply += "3. 等待 1-2 小时 gxair 限流恢复"
            for i, ch in enumerate(reply):
                if i % 3 == 0: _t.sleep(0.01)
                try: self.wfile.write(build_delta_event(ch)); self.wfile.flush()
                except: break
            add_message(sid, "assistant", reply, meta=json.dumps({"model": "__demo__", "demo": True}))
            self.wfile.write(build_sse_done_event())
            self.wfile.flush()

        def _handle_sse_task(self, r):
            """任务执行 SSE:直连 provider + 写 tasks.result(不存 messages)。
            流程: 标记 running → 启动 SSE → 智能拼接 URL → 直连 → 兜底 LiteLLM → 存 result + done。
            """
            import time as _t
            task = r["task"]
            route = r.get("route") or {}
            tid = task["id"]
            # 1) 标记 running
            with _db_lock:
                c = db()
                c.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (_t.time(), tid))
                c.commit()
                c.close()
            # 2) 启动 SSE
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # 3) 准备 body
            model_id = route.get("modelId") or task.get("model") or "glm-4-flash"
            # 如果 model_id 形如 "p_xxx:glm-4-flash" 或 "litellm:glm-4-flash",strip 前缀
            if ":" in model_id and not model_id.startswith("http"):
                # 大概率是 "source:model_id" 格式
                parts = model_id.split(":", 1)
                # 只在 source 是 "litellm" 或 "p_xxx" 时 strip
                if parts[0] == "litellm" or parts[0].startswith("p_"):
                    model_id = parts[1]
            body = {
                "model": model_id,
                "messages": [{"role": "user", "content": task["prompt"]}],
                "temperature": 0.7,
                "stream": True,
            }
            full_text_parts = []
            direct_err = None
            # 4) 直连 provider
            if route.get("source") and route.get("source") != "litellm" and route.get("baseUrl"):
                base = route["baseUrl"].rstrip("/")
                if base.endswith(("/v1", "/v4", "/v2")) or "bigmodel" in base.lower() or "zhipu" in base.lower():
                    url = base + "/chat/completions"
                else:
                    url = base + "/v1/chat/completions"
                api_key = route.get("apiKey") or ""
                try:
                    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
                    req = urllib.request.Request(
                        url, data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
                        for raw_line in resp:
                            try: line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                            except: continue
                            if not line.startswith("data:"): continue
                            payload = line[5:].strip()
                            if payload == "[DONE]": break
                            try: j = json.loads(payload)
                            except: continue
                            for ch in j.get("choices", []):
                                d2 = ch.get("delta", {}).get("content")
                                if d2:
                                    full_text_parts.append(d2)
                                    self.wfile.write(build_delta_event(d2))
                                    self.wfile.flush()
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode("utf-8", errors="replace")[:300]
                    direct_err = f"❌ 直连 {route.get('source')} 失败: {e.code} · {err_body[:200]}"
                except Exception as e:
                    direct_err = f"❌ 直连出错: {type(e).__name__}: {e}"
            # 5) 兜底:LiteLLM
            if not full_text_parts:
                if direct_err:
                    try: self.wfile.write(build_log_event(direct_err)); self.wfile.flush()
                    except: pass
                try:
                    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
                    req = urllib.request.Request(
                        LITELLM_BASE + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=180, context=ctx) as resp:
                        for raw_line in resp:
                            try: line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                            except: continue
                            if not line.startswith("data:"): continue
                            payload = line[5:].strip()
                            if payload == "[DONE]": break
                            try: j = json.loads(payload)
                            except: continue
                            for ch in j.get("choices", []):
                                d2 = ch.get("delta", {}).get("content")
                                if d2:
                                    full_text_parts.append(d2)
                                    self.wfile.write(build_delta_event(d2))
                                    self.wfile.flush()
                except Exception as e:
                    err_msg = f"❌ 直连 + LiteLLM 都失败: {direct_err or ''} | LiteLLM: {type(e).__name__}: {e}"
                    self.wfile.write(build_error_event(err_msg))
                    self.wfile.flush()
                    with _db_lock:
                        c = db()
                        c.execute("UPDATE tasks SET status='failed', updated_at=? WHERE id=?", (_t.time(), tid))
                        c.commit()
                        c.close()
                    self.wfile.write(build_sse_done_event())
                    self.wfile.flush()
                    return
            # 6) 完成:存 result + done
            full_text = "".join(full_text_parts)
            with _db_lock:
                c = db()
                c.execute("UPDATE tasks SET status='done', result=?, updated_at=? WHERE id=?",
                    (full_text, _t.time(), tid))
                c.commit()
                c.close()
            self.wfile.write(build_sse_done_event())
            self.wfile.flush()

        def _handle_sse(self, r):
            """SSE 流:把 LiteLLM(或直连 provider)的响应逐 chunk 推给前端,同时存最终 assistant 回复。
            r["msgs"] 已包含完整 messages(可能首条是 system, 由 api_messages_send 构造)"""
            sid = r["sid"]
            msgs = r["msgs"]
            model = r["model"]
            route = r.get("route") or {}
            rag_refs = r.get("rag_refs")  # 可能有
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # 先发 RAG 引用(若有)— 前端在流式文本前收到引用区
            if rag_refs:
                try:
                    self.wfile.write(build_refs_event(rag_refs, r.get('rag_hit_count', len(rag_refs))))
                    self.wfile.flush()
                except Exception: pass
            # 关键:model_id 是已 strip prefix 的纯名(gpt-5.4-mini 或 gxair/gpt-5.4-mini)
            # 用 _route.modelId(从 select value 拆出)而非 session.model(完整 litellm:xxx)
            model_id = route.get("modelId") or model
            # msgs 已含完整 system + history + user,直接用
            body = {
                "model": model_id,
                "messages": msgs,
                "temperature": 0.7,
                "stream": True,
            }
            # 完整路径(含 litellm: 前缀)记 meta
            model_full = model
            result = stream_chat_with_fallback(body, route, LITELLM_BASE, LITELLM_MASTER_KEY)
            for event in result["events"]:
                self.wfile.write(event)
                self.wfile.flush()
            if result["full_text"].strip():
                meta = {
                    "model": model_full,
                    "provider": result["provider"],
                    "finish_reason": result["finish_reason"],
                }
                if result.get("direct_err"):
                    meta["direct_err"] = result["direct_err"][:200]
                add_message(sid, "assistant", result["full_text"], meta=meta)
            return

        def log_message(self, fmt, *args):
            msg = fmt % args if args else fmt
            sys.stderr.write(f"[{self.command} {self.path}] {msg}\n")
            sys.stderr.flush()
    return Req

# ============================================================
# Obsidian vault 索引 (RAG 数据源)
# ============================================================
_obsidian_lock = threading.Lock()
_obsidian_index = []         # [{path, rel_path, title, headings, body, mtime, keywords}]
_obsidian_loaded_at = 0
_obsidian_vault_exists = False

# 简单中文分词:连续 CJK 字符单字切分 + 英文/数字按词切分
def _obsidian_tokenize(text):
    text = (text or "").lower()
    # 提取连续 CJK 段,每段单字切;其余按 \W+ 切
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        # CJK Unified Ideographs + 扩展 A
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            j = i
            while j < n and (('\u4e00' <= text[j] <= '\u9fff') or ('\u3400' <= text[j] <= '\u4dbf')):
                j += 1
            # CJK 段:bigram + 单字
            seg = text[i:j]
            out.append(seg)
            for k in range(len(seg) - 1):
                out.append(seg[k:k+2])
            for ch2 in seg:
                out.append(ch2)
            i = j
        else:
            j = i
            while j < n and not (('\u4e00' <= text[j] <= '\u9fff') or ('\u3400' <= text[j] <= '\u4dbf')):
                j += 1
            seg = text[i:j]
            # 切分:按空白 + 标点
            for w in _re.split(r"[\s!-/:-@\[-`{-~]+", seg):
                if w and len(w) >= 2:
                    out.append(w)
            i = j
    return out

def _obsidian_parse_md(path):
    """读 md:返 title (首个 # 或文件名) + 全文 body"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception:
        return None, ""
    title = ""
    body_lines = []
    for line in text.splitlines():
        s = line.strip()
        if not title and s.startswith("# "):
            title = s[2:].strip()
        else:
            body_lines.append(line)
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]
    return title, "\n".join(body_lines).strip()

def _obsidian_index_vault(force=False):
    """扫 vault 建索引。mtime 缓存:文件 mtime 变才重建。"""
    global _obsidian_index, _obsidian_loaded_at, _obsidian_vault_exists
    if not os.path.isdir(OBSIDIAN_VAULT):
        _obsidian_vault_exists = False
        _obsidian_index = []
        return
    _obsidian_vault_exists = True
    with _obsidian_lock:
        # 扫所有 .md
        paths = []
        for root, dirs, files in os.walk(OBSIDIAN_VAULT):
            # 排除目录
            dirs[:] = [d for d in dirs if d not in OBSIDIAN_EXCLUDE_DIRS]
            for f in files:
                if f.endswith(".md"):
                    paths.append(os.path.join(root, f))
        # 已有索引 → 只更新 mtime 变化的文件
        old_by_path = {d["path"]: d for d in _obsidian_index}
        new_index = []
        for p in paths:
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            old = old_by_path.get(p)
            if old and not force and abs(old["mtime"] - mtime) < 0.5:
                new_index.append(old)  # 复用
                continue
            title, body = _obsidian_parse_md(p)
            if title is None:
                continue
            rel = os.path.relpath(p, OBSIDIAN_VAULT)
            # 标题 + 前 1500 字 + 全文关键词(限 8000 字)
            head = body[:1500]
            full_for_kw = (title + "\n" + head + "\n" + body[:8000])
            keywords = set(_obsidian_tokenize(title + " " + head))
            new_index.append({
                "path": p, "rel_path": rel, "title": title,
                "head": head, "body": body,
                "mtime": mtime, "keywords": keywords,
            })
        # 排序: 最近 mtime 优先(用于 suggestions)
        new_index.sort(key=lambda d: d["mtime"], reverse=True)
        _obsidian_index = new_index
        _obsidian_loaded_at = time.time()

def _obsidian_ensure_loaded():
    if not _obsidian_index and not _obsidian_loaded_at and _obsidian_vault_exists is False:
        _obsidian_index_vault()

def _obsidian_search(query, top_k=None, min_score=OBSIDIAN_SCORE_THRESHOLD):
    """纯文本检索：关键字分词 + must_token 硬性过滤 + 按 score 降序,过滤低于 min_score"""
    _obsidian_ensure_loaded()
    if top_k is None:
        top_k = OBSIDIAN_TOP_K
    if not _obsidian_index or not query or not query.strip():
        return []
    stop = {"的", "了", "是", "在", "有", "和", "与", "也", "都", "就", "到", "为", "可以", "如何", "什么", "怎么", "要", "使用", "怎么用", "推荐", "介绍", "我", "你", "他", "它", "那", "这"}
    q_tokens = set(_obsidian_tokenize(query))
    must_tokens = [t for t in q_tokens if t not in stop and len(t) >= 2]
    if not must_tokens:
        must_tokens = list(q_tokens)
    results = []
    for doc in _obsidian_index:
        title_hits = sum(1 for t in _obsidian_tokenize(doc["title"]) if t in q_tokens)
        body_hits = sum(1 for t in q_tokens if t in doc["keywords"])
        must_hit = any(t in doc["keywords"] or t in _obsidian_tokenize(doc["title"]) for t in must_tokens)
        if not must_hit and (title_hits == 0 and body_hits == 0):
            continue
        score = title_hits * 3.0 + body_hits * 1.0
        if score < min_score:
            continue
        snippet = ""
        for line in doc["body"].splitlines()[:50]:
            low = line.lower()
            if any(t in low for t in q_tokens):
                snippet = line.strip()[:160]
                break
        if not snippet:
            snippet = doc["head"][:160].replace("\n", " ")
        results.append((doc, score, snippet))
    results.sort(key=lambda r: (r[1], r[0]["mtime"]), reverse=True)
    return results[:top_k]

def _obsidian_build_context(query, hits):
    """把 hits 拼成 LLM 用的 context 文本"""
    parts = []
    for i, (doc, score, snippet) in enumerate(hits, 1):
        body = doc["body"][:OBSIDIAN_MAX_CHARS_PER_DOC]
        parts.append(f"### 参考资料 {i}: {doc['rel_path']} ({doc['title']})\n{body}\n")
    return "\n".join(parts)

def _obsidian_format_refs(hits):
    """返 [{path, rel_path, title, score, snippet}] 给前端引用区"""
    return [{
        "path": d["rel_path"], "title": d["title"],
        "score": round(s, 2), "snippet": sn,
    } for d, s, sn in hits]

def _obsidian_suggestions(n=4):
    """基于 vault 标题生成 n 个快捷问题。空 vault 用静态 fallback。"""
    _obsidian_ensure_loaded()
    out = []
    if _obsidian_index:
        # 最近更新的 top 标题 → 模板化提问
        templates = ["介绍 {t}", "{t} 是什么", "{t} 的关键点", "如何理解 {t}"]
        for i, doc in enumerate(_obsidian_index[:n*2]):  # 候选 2 倍
            tpl = templates[i % len(templates)]
            out.append({"q": tpl.format(t=doc["title"]), "src": doc["rel_path"]})
            if len(out) >= n:
                break
    else:
        # Fallback(空 vault 或没 vault)
        for q in [
            "智能家居 设备推荐",
            "Hermes Agent 是什么",
            "AI 工具 怎么选",
            "印特软件 功能介绍",
        ][:n]:
            out.append({"q": q, "src": None})
    return out

def _obsidian_stats():
    """vault 统计:篇数 + 最近 mtime"""
    _obsidian_ensure_loaded()
    if not _obsidian_vault_exists or not _obsidian_index:
        return {"exists": _obsidian_vault_exists, "count": 0, "last_updated": None, "vault_path": OBSIDIAN_VAULT}
    last = max(d["mtime"] for d in _obsidian_index)
    return {
        "exists": True, "count": len(_obsidian_index),
        "last_updated": last, "vault_path": OBSIDIAN_VAULT,
    }

def _obsidian_set_vault(new_path):
    """runtime 改 vault 路径(空字符串恢复默认)。返回 (新路径, ok)。"""
    global OBSIDIAN_VAULT
    if not new_path or not str(new_path).strip():
        new_path = os.environ.get("OBSIDIAN_VAULT_PATH", "/mnt/c/Users/Administrator/Documents/Obsidian Vault")
    new_path = os.path.expanduser(str(new_path).strip())
    OBSIDIAN_VAULT = new_path
    # 路径变了 → 重置索引
    _obsidian_index = []
    _obsidian_loaded_at = 0
    _obsidian_vault_exists = False
    # 重新扫
    try:
        _obsidian_index_vault(force=True)
    except Exception as e:
        return new_path, False, f"{type(e).__name__}: {e}"
    return new_path, True, None

def _obsidian_categorize():
    """用 LLM 给所有 .md 打 category(学习/运动/习惯/教育/其他) + 1 句 summary。
    分批处理,每批 40 篇(避免单次 prompt 超 token)。
    返回 (ok, message, stats)
    """
    _obsidian_ensure_loaded()
    if not _obsidian_index:
        return False, "vault 为空,无法分类", None
    BATCH_SIZE = 40
    # 从 db 拿可用 provider 列表
    providers_list = []
    try:
        with _db_lock:
            c = db()
            for r in c.execute("SELECT id, name, base_url, api_key FROM providers").fetchall():
                if r["api_key"] and r["base_url"]:
                    providers_list.append({"id": r["id"], "name": r["name"],
                                           "base": r["base_url"].rstrip("/"),
                                           "key": r["api_key"]})
            c.close()
    except Exception:
        pass
    providers_list.sort(key=lambda p: 0 if "zhipu" in p["name"].lower() or "bigmodel" in p["base"] else 1)
    if not providers_list:
        return False, "无可用 provider(请先在设置里加 API)", None

    sys_prompt = "你是笔记分类助手,只返 JSON。"
    all_results = []
    last_err = None
    # 分批
    for batch_start in range(0, len(_obsidian_index), BATCH_SIZE):
        batch = _obsidian_index[batch_start:batch_start + BATCH_SIZE]
        items = []
        for doc in batch:
            head = doc.get("head", "")[:400].replace("\n", " ")
            items.append(f"- {doc['rel_path']} | 标题:{doc['title']} | 摘要:{head}")
        docs_text = "\n".join(items)
        user_prompt = (
            "分析以下 Obsidian 知识库中的笔记,给每篇分配 1-2 个 category(从以下 5 个选:学习/运动/习惯/教育/其他)"
            "和 1 句话 summary(<= 30 字)。\n\n"
            "返 JSON 数组,每个元素格式: {\"path\": \"相对路径\", \"category\": \"分类\", \"tags\": [\"标签1\", \"标签2\"], \"summary\": \"一句话\"}\n"
            "只返 JSON 数组,不要其他文字。\n\n"
            f"## 笔记列表:\n{docs_text}"
        )
        # 试每个 provider 直到成功
        resp = None
        for p in providers_list:
            if p["base"].endswith(("/v1", "/v4", "/v2")) or "bigmodel" in p["base"].lower():
                url = p["base"] + "/chat/completions"
            else:
                url = p["base"] + "/v1/chat/completions"
            try:
                with _db_lock:
                    c = db()
                    m = c.execute("SELECT model_id FROM provider_models WHERE provider_id=? AND status='ok' LIMIT 1", (p["id"],)).fetchone()
                    c.close()
                model_id = m["model_id"] if m else "gpt-4o-mini"
            except Exception:
                model_id = "gpt-4o-mini"
            body = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "stream": False,
            }
            try:
                req = urllib.request.Request(url, method="POST",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {p['key']}"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                print(f"[categorize] batch {batch_start//BATCH_SIZE+1} 成功: provider={p['name']} model={model_id}", flush=True)
                break
            except Exception as e:
                last_err = f"{p['name']} {type(e).__name__}: {e}"
                print(f"[categorize] batch {batch_start//BATCH_SIZE+1} 失败: {last_err}", flush=True)
                import time as _t; _t.sleep(1)
                continue
        if resp is None:
            return False, f"LLM 调失败(所有 provider): {last_err}", None
        txt = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        txt = txt.strip()
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.M).strip()
        try:
            results = json.loads(txt)
        except Exception as e:
            return False, f"LLM 输出非 JSON:{e}; raw={txt[:200]}", None
        if isinstance(results, list):
            all_results.extend(results)
        # 批次间短延迟(避免限流)
        import time as _t
        _t.sleep(1)
    # 写入 obsidian_categories
    now = time.time()
    written = 0
    with _db_lock:
        c = db()
        for r0 in all_results:
            if not isinstance(r0, dict): continue
            p = r0.get("path") or ""
            cat = (r0.get("category") or "其他").strip()
            if cat not in ("学习", "运动", "习惯", "教育", "其他"):
                cat = "其他"
            tags = r0.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            summary = (r0.get("summary") or "").strip()[:120]
            c.execute(
                "INSERT OR REPLACE INTO obsidian_categories (path, category, tags, summary, classified_at) VALUES (?, ?, ?, ?, ?)",
                (p, cat, json.dumps(tags, ensure_ascii=False), summary, now)
            )
            written += 1
        c.commit()
        c.close()
    return True, "ok", {"written": written, "total": len(_obsidian_index), "batches": (len(_obsidian_index) + BATCH_SIZE - 1) // BATCH_SIZE}

def _obsidian_list_categories():
    """返所有分类 + 计数 + 标签"""
    with _db_lock:
        c = db()
        rows = c.execute(
            "SELECT category, COUNT(*) as n FROM obsidian_categories GROUP BY category ORDER BY n DESC"
        ).fetchall()
        c.close()
    by_cat = {r["category"]: r["n"] for r in rows}
    return by_cat

def _obsidian_get_doc_category(path):
    """返单篇的分类信息,无则 None"""
    with _db_lock:
        c = db()
        row = c.execute(
            "SELECT * FROM obsidian_categories WHERE path=?", (path,)
        ).fetchone()
        c.close()
    if not row:
        return None
    return {
        "path": row["path"],
        "category": row["category"],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "summary": row["summary"],
        "classified_at": row["classified_at"],
    }

def main():
    init_db()
    # 启动时建 obsidian 索引(失败不阻塞 server)
    try:
        _obsidian_index_vault()
        if _obsidian_vault_exists:
            print(f"[openwebui-lite] obsidian vault: {OBSIDIAN_VAULT} ({len(_obsidian_index)} 篇)", flush=True)
        else:
            print(f"[openwebui-lite] obsidian vault NOT FOUND: {OBSIDIAN_VAULT}", flush=True)
    except Exception as e:
        print(f"[openwebui-lite] obsidian index failed: {e}", flush=True)
    api = APIHandler()
    httpd = run_server(PORT, api, make_handler)
    httpd.api_handler = api
    print(f"[openwebui-lite] http://0.0.0.0:{PORT}", flush=True)
    print(f"[openwebui-lite] db: {DB_PATH}", flush=True)
    print(f"[openwebui-lite] litellm: {LITELLM_BASE} (master_key)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
