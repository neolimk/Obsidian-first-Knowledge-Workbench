# Knowledge Workbench Repositioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 openwebui-lite 从“聊天中心型原型”改造成以 Obsidian 为核心数据源的个人知识工作台，并按 `Knowledge Core → Chat thinning → Tasks → Writing/Image integration` 的顺序建立长期可演进架构。

**Architecture:** 先建立 Knowledge Core，把 Obsidian 索引、搜索、RAG context 和分类从 `server.py` 中抽成独立服务，让 chat/search/writing/tasks 都消费同一套知识内核。然后继续压薄 chat，把 tasks 变成知识执行层，最后再把 writing 和 image 升级为正式知识能力入口。

**Tech Stack:** Python 3, stdlib `http.server`, SQLite, pytest, ruff

---

## File Structure

### Existing files to modify
- `server.py` — 继续变薄，逐步只保留 HTTP 入口与协调。
- `app/services/chat_flow.py` — 继续收口聊天业务状态。
- `app/services/chat_gateway.py` — 保留为上游聊天网关。
- `app/services/sse.py` — 继续作为所有流式能力共享层。
- `README.md` — 更新产品定位和架构说明。

### New backend files
- `app/services/chat_store.py` — 聊天相关 DB 读写 helper。
- `app/services/task_store.py` — task 表读写 helper。
- `app/services/task_flow.py` — task CRUD、状态迁移和展示整形。
- `app/services/task_runner.py` — task prompt 执行与流式产出。
- `app/services/obsidian_index.py` — vault 扫描、缓存、增量刷新。
- `app/services/obsidian_search.py` — tokenization、评分、top-k 检索。
- `app/services/obsidian_classify.py` — 分类与 summary 逻辑。
- `app/services/rag_context.py` — refs 格式化、context/system prompt 构造。
- `app/services/writing_flow.py` — 文档写作编排的最小骨架。

### New tests
- `tests/test_chat_store.py`
- `tests/test_task_store.py`
- `tests/test_task_flow.py`
- `tests/test_task_runner.py`
- `tests/test_obsidian_index.py`
- `tests/test_obsidian_search.py`
- `tests/test_rag_context.py`
- `tests/test_obsidian_classify.py`
- `tests/test_writing_flow.py`

---

### Task 1: 完成 Chat 最后一轮压薄

**Files:**
- Create: `app/services/chat_store.py`
- Create: `tests/test_chat_store.py`
- Modify: `app/services/chat_flow.py`
- Modify: `server.py:773-1850`

- [ ] **Step 1: 写失败的 chat_store 测试**

```python
# tests/test_chat_store.py
from app.config import build_config
from app.db import init_db, open_db
from app.services.chat_store import save_assistant_message


def test_save_assistant_message_persists_meta(app_env):
    config = build_config()
    init_db(config)

    with open_db(config) as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, model, system, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("sid1", "标题", "litellm:gpt-5", "", 1.0, 1.0),
        )
        conn.commit()

    save_assistant_message(
        config,
        "sid1",
        "你好",
        {"model": "litellm:gpt-5", "provider": "litellm", "finish_reason": "stop"},
    )

    with open_db(config) as conn:
        row = conn.execute(
            "SELECT role, content, meta FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
            ("sid1",),
        ).fetchone()

    assert row["role"] == "assistant"
    assert row["content"] == "你好"
    assert '"provider": "litellm"' in row["meta"]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_chat_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.chat_store'`

- [ ] **Step 3: 写最小 chat_store 实现**

```python
# app/services/chat_store.py
import json
import time

from app.db import open_db



def save_assistant_message(config, sid, text, meta):
    with open_db(config) as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (sid, "assistant", text, json.dumps(meta), time.time()),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))
        conn.commit()
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_chat_store.py -q`
Expected: PASS

- [ ] **Step 5: 把 `chat_flow` 扩成真正的聊天收口层**

```python
# app/services/chat_flow.py
from app.services.chat_store import save_assistant_message



def finalize_chat_success(config, sid, text, model, provider, finish_reason, direct_err=None):
    meta = {"model": model, "provider": provider, "finish_reason": finish_reason}
    if direct_err:
        meta["direct_err"] = direct_err[:200]
    save_assistant_message(config, sid, text, meta)



def finalize_chat_error(config, sid, text):
    save_assistant_message(config, sid, text, {"kind": "error"})
```

- [ ] **Step 6: 把 `_handle_sse(...)` 压成很薄的协调层**

```python
# server.py target shape
config = build_config()
result = stream_chat_with_fallback(body, route, LITELLM_BASE, LITELLM_MASTER_KEY)
for event in result["events"]:
    self.wfile.write(event)
    self.wfile.flush()
if result["full_text"].strip():
    finalize_chat_success(
        config,
        sid,
        result["full_text"],
        model_full,
        result["provider"],
        result["finish_reason"],
        result.get("direct_err"),
    )
```

- [ ] **Step 7: 运行回归**

Run: `pytest tests/test_chat_store.py tests/test_chat_flow.py tests/test_chat_gateway.py tests/test_chat_api.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add app/services/chat_store.py app/services/chat_flow.py tests/test_chat_store.py server.py
git commit -m "refactor: finish thinning chat request path"
```

---

### Task 2: 建立 Obsidian 索引服务

**Files:**
- Create: `app/services/obsidian_index.py`
- Create: `tests/test_obsidian_index.py`
- Modify: `server.py:1938-2160`

- [ ] **Step 1: 写失败的索引测试**

```python
# tests/test_obsidian_index.py
from pathlib import Path

from app.services.obsidian_index import parse_markdown_file



def test_parse_markdown_file_uses_heading_as_title(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# 标题\n\n正文", encoding="utf-8")

    title, body = parse_markdown_file(path)

    assert title == "标题"
    assert body == "正文"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_obsidian_index.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.obsidian_index'`

- [ ] **Step 3: 写最小索引实现**

```python
# app/services/obsidian_index.py
from pathlib import Path



def parse_markdown_file(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    title = ""
    body_lines = []
    for line in text.splitlines():
        if not title and line.strip().startswith("# "):
            title = line.strip()[2:].strip()
        else:
            body_lines.append(line)
    if not title:
        title = Path(path).stem
    return title, "\n".join(body_lines).strip()
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_obsidian_index.py -q`
Expected: PASS

- [ ] **Step 5: 把 `_obsidian_parse_md`、扫描、mtime 复用逻辑迁到 `obsidian_index.py`**

```python
# app/services/obsidian_index.py target functions
# - tokenize_text(...)
# - parse_markdown_file(...)
# - build_index(vault_path, exclude_dirs, old_index=None, force=False)
# - ensure_index_loaded(...)
```

- [ ] **Step 6: 运行回归**

Run: `pytest tests/test_obsidian_index.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/services/obsidian_index.py tests/test_obsidian_index.py server.py
git commit -m "refactor: extract obsidian index service"
```

---

### Task 3: 建立 Obsidian 搜索与 RAG context 服务

**Files:**
- Create: `app/services/obsidian_search.py`
- Create: `app/services/rag_context.py`
- Create: `tests/test_obsidian_search.py`
- Create: `tests/test_rag_context.py`
- Modify: `server.py:1967-2217`

- [ ] **Step 1: 写失败的搜索测试**

```python
# tests/test_obsidian_search.py
from app.services.obsidian_search import score_document_match



def test_score_document_match_prefers_title_hits():
    score = score_document_match(title_hits=2, body_hits=3)
    assert score == 9.0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_obsidian_search.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.obsidian_search'`

- [ ] **Step 3: 写最小搜索实现**

```python
# app/services/obsidian_search.py


def score_document_match(title_hits, body_hits):
    return title_hits * 3.0 + body_hits * 1.0
```

- [ ] **Step 4: 写失败的 RAG context 测试**

```python
# tests/test_rag_context.py
from app.services.rag_context import build_rag_system_prompt



def test_build_rag_system_prompt_includes_user_system():
    text = build_rag_system_prompt("参考内容", "用户系统提示")
    assert "参考内容" in text
    assert "用户系统提示" in text
```

- [ ] **Step 5: 运行测试，确认失败后实现最小 context 逻辑**

```python
# app/services/rag_context.py


def build_rag_system_prompt(rag_ctx, user_system):
    text = (
        "你是一个基于用户本地 Obsidian 知识库的助手。请优先基于以下参考资料回答用户问题。\n"
        "## 用户的 Obsidian 知识库参考资料:\n" + rag_ctx
    )
    if user_system:
        text += "\n\n## 用户额外设置:\n" + user_system
    return text
```

- [ ] **Step 6: 把 `_obsidian_search`、refs/context 拼装迁到 `obsidian_search.py` 与 `rag_context.py`**

```python
# target functions
# obsidian_search.py
# - search_index(query, index, top_k, min_score)
# - build_suggestions(index, n)
# - build_stats(index, vault_exists)

# rag_context.py
# - format_refs(hits)
# - build_context(query, hits, max_chars_per_doc)
# - build_rag_system_prompt(...)
```

- [ ] **Step 7: 运行回归**

Run: `pytest tests/test_obsidian_search.py tests/test_rag_context.py tests/test_chat_flow.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add app/services/obsidian_search.py app/services/rag_context.py tests/test_obsidian_search.py tests/test_rag_context.py server.py
git commit -m "refactor: extract obsidian search and rag context"
```

---

### Task 4: 建立 Task Store 与 Task Flow

**Files:**
- Create: `app/services/task_store.py`
- Create: `app/services/task_flow.py`
- Create: `tests/test_task_store.py`
- Create: `tests/test_task_flow.py`
- Modify: `server.py:1288-1398`

- [ ] **Step 1: 写失败的 task_store 测试**

```python
# tests/test_task_store.py
from app.config import build_config
from app.db import init_db
from app.services.task_store import create_task_record



def test_create_task_record_returns_task_id(app_env):
    config = build_config()
    init_db(config)

    task_id = create_task_record(config, "标题", "内容", "litellm:gpt-5", "chat", None, None)

    assert isinstance(task_id, int)
    assert task_id > 0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_task_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.task_store'`

- [ ] **Step 3: 写最小 task_store 实现**

```python
# app/services/task_store.py
import time

from app.db import open_db



def create_task_record(config, title, prompt, model, skill, source_sid, source_msg_id):
    now = time.time()
    with open_db(config) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, prompt, model, skill, status, source_sid, source_msg_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (title, prompt, model, skill, "pending", source_sid, source_msg_id, now, now),
        )
        conn.commit()
        return cur.lastrowid
```

- [ ] **Step 4: 写失败的 task_flow 测试**

```python
# tests/test_task_flow.py
from app.services.task_flow import build_task_preview



def test_build_task_preview_truncates_prompt():
    preview = build_task_preview("x" * 120)
    assert len(preview) == 80
```

- [ ] **Step 5: 运行测试，确认失败后实现最小 task_flow**

```python
# app/services/task_flow.py


def build_task_preview(prompt):
    return (prompt or "")[:80]
```

- [ ] **Step 6: 把 `api_tasks_*` 的数据整形与状态迁移迁到 `task_store.py` / `task_flow.py`**

```python
# target responsibilities
# task_store.py
# - list_task_rows(...)
# - get_task_row(...)
# - create_task_record(...)
# - update_task_record(...)
# - delete_task_record(...)
# - mark_task_status(...)

# task_flow.py
# - serialize_task_list_item(...)
# - serialize_task_detail(...)
# - validate_task_create(...)
# - validate_task_update(...)
```

- [ ] **Step 7: 运行回归**

Run: `pytest tests/test_task_store.py tests/test_task_flow.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add app/services/task_store.py app/services/task_flow.py tests/test_task_store.py tests/test_task_flow.py server.py
git commit -m "refactor: extract task store and flow"
```

---

### Task 5: 建立 Task Runner 并压薄 `_handle_sse_task(...)`

**Files:**
- Create: `app/services/task_runner.py`
- Create: `tests/test_task_runner.py`
- Modify: `server.py:1521-1641`

- [ ] **Step 1: 写失败的 task_runner 测试**

```python
# tests/test_task_runner.py
from app.services.task_runner import normalize_task_model_id



def test_normalize_task_model_id_strips_source_prefix():
    assert normalize_task_model_id("litellm:glm-4-flash") == "glm-4-flash"
    assert normalize_task_model_id("p_xxx:glm-4-flash") == "glm-4-flash"
    assert normalize_task_model_id("plain-model") == "plain-model"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_task_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.task_runner'`

- [ ] **Step 3: 写最小 task_runner 实现**

```python
# app/services/task_runner.py


def normalize_task_model_id(model_id):
    if ":" in model_id and not model_id.startswith("http"):
        source, value = model_id.split(":", 1)
        if source == "litellm" or source.startswith("p_"):
            return value
    return model_id
```

- [ ] **Step 4: 把 `_handle_sse_task(...)` 的 URL 拼接、执行与结果收口迁入 `task_runner.py`**

```python
# target functions
# - normalize_task_model_id(...)
# - build_task_body(...)
# - run_task_with_fallback(...)
# - persist_task_result(...)
```

- [ ] **Step 5: `_handle_sse_task(...)` 变成薄协调层**

```python
# server.py target shape
result = run_task_with_fallback(task, route, LITELLM_BASE, LITELLM_MASTER_KEY)
for event in result["events"]:
    self.wfile.write(event)
    self.wfile.flush()
persist_task_result(...)
```

- [ ] **Step 6: 运行回归**

Run: `pytest tests/test_task_runner.py tests/test_task_flow.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/services/task_runner.py tests/test_task_runner.py server.py
git commit -m "refactor: extract task runner"
```

---

### Task 6: 建立 Obsidian 分类服务

**Files:**
- Create: `app/services/obsidian_classify.py`
- Create: `tests/test_obsidian_classify.py`
- Modify: `server.py:1468-1482,2180-2217`

- [ ] **Step 1: 写失败的分类测试**

```python
# tests/test_obsidian_classify.py
from app.services.obsidian_classify import normalize_category_name



def test_normalize_category_name_falls_back_to_other():
    assert normalize_category_name("学习") == "学习"
    assert normalize_category_name("未知") == "其他"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_obsidian_classify.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.obsidian_classify'`

- [ ] **Step 3: 写最小分类实现**

```python
# app/services/obsidian_classify.py
VALID_CATEGORIES = {"学习", "运动", "习惯", "教育", "其他"}



def normalize_category_name(name):
    return name if name in VALID_CATEGORIES else "其他"
```

- [ ] **Step 4: 把分类、summary 和分类持久化相关逻辑迁到 `obsidian_classify.py`**

```python
# target functions
# - normalize_category_name(...)
# - classify_document(...)
# - save_document_category(...)
# - list_categories(...)
# - get_document_category(...)
```

- [ ] **Step 5: 运行回归**

Run: `pytest tests/test_obsidian_classify.py tests/test_obsidian_index.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/services/obsidian_classify.py tests/test_obsidian_classify.py server.py
git commit -m "refactor: extract obsidian classification service"
```

---

### Task 7: 建立 Writing Flow 最小骨架并更新定位文档

**Files:**
- Create: `app/services/writing_flow.py`
- Create: `tests/test_writing_flow.py`
- Modify: `README.md`

- [ ] **Step 1: 写失败的 writing_flow 测试**

```python
# tests/test_writing_flow.py
from app.services.writing_flow import build_writing_prompt



def test_build_writing_prompt_includes_goal_and_context():
    text = build_writing_prompt("写摘要", "这是知识上下文")
    assert "写摘要" in text
    assert "这是知识上下文" in text
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_writing_flow.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.writing_flow'`

- [ ] **Step 3: 写最小 writing_flow 实现**

```python
# app/services/writing_flow.py


def build_writing_prompt(goal, context):
    return "写作目标:\n{}\n\n知识上下文:\n{}".format(goal, context)
```

- [ ] **Step 4: 更新 README 的产品定位说明**

```md
## Product Direction

openwebui-lite 正在重构为一个 Obsidian-first 的个人知识工作台：

- Knowledge Search
- Knowledge QA
- Knowledge Writing
- Knowledge Tasks
- Knowledge-to-Image
```

- [ ] **Step 5: 运行回归**

Run: `pytest tests/test_writing_flow.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/services/writing_flow.py tests/test_writing_flow.py README.md
git commit -m "docs: establish knowledge workbench direction"
```

---

### Task 8: 全量验证新路线图第一阶段

**Files:**
- Modify: `server.py`
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Test: `tests/*.py`

- [ ] **Step 1: 跑第一阶段全量测试**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 2: 跑作用域 lint**

Run: `ruff check app tests`
Expected: `All checks passed!`

- [ ] **Step 3: 跑语法检查**

Run: `python -m py_compile server.py app/services/chat_store.py app/services/task_store.py app/services/task_flow.py app/services/task_runner.py app/services/obsidian_index.py app/services/obsidian_search.py app/services/obsidian_classify.py app/services/rag_context.py app/services/writing_flow.py`
Expected: no output

- [ ] **Step 4: 人工验证服务仍能启动**

Run: `bash ctl.sh status`
Expected: no `command not found`,输出可读

- [ ] **Step 5: 更新 CLAUDE.md 的当前重点**

```md
## Health Stack

- lint: ruff check app tests
- test: pytest
- shell: bash ctl.sh status
- health-review: manual code review of app/services/, static/, ctl.sh, README.md
```

- [ ] **Step 6: 提交**

```bash
git add app server.py tests README.md CLAUDE.md
git commit -m "refactor: establish knowledge-core-first architecture"
```

---

## Self-Review

### Spec coverage
- Knowledge Core 优先：Task 2、3、6 覆盖
- Chat 最后一轮压薄：Task 1 覆盖
- Tasks 作为知识执行层：Task 4、5 覆盖
- Writing 作为正式能力入口骨架：Task 7 覆盖
- README/CLAUDE.md 中产品重定位：Task 7、8 覆盖

### Placeholder scan
- 无 TBD / TODO / implement later / fill in details
- 每个代码步骤都给了具体代码块
- 每个验证步骤都写了明确命令与预期输出

### Type consistency
- Chat 收口统一使用 `finalize_chat_success` / `finalize_chat_error`
- Tasks 统一使用 `task_store` / `task_flow` / `task_runner`
- Obsidian 统一使用 `obsidian_index` / `obsidian_search` / `obsidian_classify` / `rag_context`
- Writing 统一使用 `writing_flow`

---

Plan complete and saved to `docs/superpowers/plans/2026-06-23-knowledge-workbench-repositioning.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
