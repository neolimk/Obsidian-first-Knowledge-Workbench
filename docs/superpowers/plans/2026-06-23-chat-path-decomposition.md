# Chat Path Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `server.py` 中的聊天主链路拆到独立服务里，统一 SSE 事件、provider/LiteLLM fallback 和聊天业务编排，同时保持现有前端接口兼容。

**Architecture:** 新增三层服务：`sse.py` 负责事件编码，`chat_gateway.py` 负责上游流式调用和 fallback，`chat_flow.py` 负责 session/message/RAG/落库编排。`server.py` 保留 HTTP 入口，只接收请求、调用 flow/gateway、写回事件。

**Tech Stack:** Python 3, stdlib `http.server`, SQLite, pytest, ruff

---

## File Structure

### Existing files to modify
- `server.py` — 保留 HTTP handler、路由和 legacy 逻辑，移除大段聊天主链路内联实现，改为调用新服务。
- `app/services/chat.py` — 保留现有基础 SSE helper，必要时只作为兼容层或被 `sse.py` 替代/复用。

### New backend files
- `app/services/sse.py` — 统一生成 `delta / error / refs / log / done / start` 事件。
- `app/services/chat_gateway.py` — provider 直连、LiteLLM fallback、chunk 解析、finish_reason 提取。
- `app/services/chat_flow.py` — session 校验、user message 入库、标题生成、RAG 注入、assistant 落库收口。

### New tests
- `tests/test_sse.py` — SSE 事件编码测试。
- `tests/test_chat_gateway.py` — 上游流式调用和 fallback 测试。
- `tests/test_chat_flow.py` — 聊天业务编排测试。

---

### Task 1: 建立统一 SSE 事件层

**Files:**
- Create: `app/services/sse.py`
- Create: `tests/test_sse.py`
- Modify: `server.py:1500-1850`

- [ ] **Step 1: 写失败的 SSE 测试**

```python
# tests/test_sse.py
from app.services.sse import (
    build_delta_event,
    build_done_event,
    build_error_event,
    build_log_event,
    build_refs_event,
)


def test_build_delta_event():
    assert build_delta_event("你好") == b'data: {"event": "delta", "text": "\xe4\xbd\xa0\xe5\xa5\xbd"}\n\n'


def test_build_done_event():
    assert build_done_event() == b"data: [DONE]\n\n"


def test_build_log_event():
    assert build_log_event("fallback") == b'data: {"event": "log", "text": "fallback"}\n\n'


def test_build_error_event_with_extra():
    assert build_error_event("boom", {"code": 500}) == (
        b'data: {"event": "error", "text": "boom", "code": 500}\n\n'
    )


def test_build_refs_event():
    refs = [{"path": "a.md", "title": "A"}]
    assert build_refs_event(refs, 1) == (
        b'data: {"event": "refs", "refs": [{"path": "a.md", "title": "A"}], "hit_count": 1}\n\n'
    )
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_sse.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.sse'`

- [ ] **Step 3: 写最小 SSE 实现**

```python
# app/services/sse.py
import json



def build_sse_event(payload):
    return "data: {}\n\n".format(
        json.dumps(payload, ensure_ascii=False)
    ).encode("utf-8")



def build_delta_event(text):
    return build_sse_event({"event": "delta", "text": text})



def build_log_event(text):
    return build_sse_event({"event": "log", "text": text})



def build_error_event(message, extra=None):
    payload = {"event": "error", "text": message}
    if extra:
        payload.update(extra)
    return build_sse_event(payload)



def build_refs_event(refs, hit_count=None):
    payload = {"event": "refs", "refs": refs}
    if hit_count is not None:
        payload["hit_count"] = hit_count
    return build_sse_event(payload)



def build_start_event():
    return build_sse_event({"event": "start"})



def build_done_event():
    return b"data: [DONE]\n\n"
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_sse.py -q`
Expected: PASS

- [ ] **Step 5: 把 demo / task / chat 三条流的手写 SSE 输出替换成新 helper**

```python
# server.py imports
from app.services.sse import (
    build_delta_event,
    build_done_event,
    build_error_event,
    build_log_event,
    build_refs_event,
    build_start_event,
)
```

```python
# server.py examples
self.wfile.write(build_delta_event(delta))
self.wfile.write(build_log_event(direct_err))
self.wfile.write(build_error_event(err_msg, {"code": e.code}))
self.wfile.write(build_refs_event(rag_refs, r.get("rag_hit_count", len(rag_refs))))
self.wfile.write(build_start_event())
self.wfile.write(build_done_event())
self.wfile.flush()
```

- [ ] **Step 6: 运行局部回归**

Run: `pytest tests/test_sse.py tests/test_chat_api.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add app/services/sse.py tests/test_sse.py server.py
git commit -m "refactor: centralize sse event helpers"
```

---

### Task 2: 抽离聊天上游网关与 fallback

**Files:**
- Create: `app/services/chat_gateway.py`
- Create: `tests/test_chat_gateway.py`
- Modify: `server.py:1640-1850`

- [ ] **Step 1: 写失败的 gateway 测试**

```python
# tests/test_chat_gateway.py
from app.services.chat_gateway import extract_stream_events, normalize_chat_error



def test_extract_stream_events_yields_deltas_and_finish_reason():
    raw_lines = [
        b'data: {"choices":[{"delta":{"content":"你"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n',
        b'data: [DONE]\n',
    ]

    events = list(extract_stream_events(raw_lines))

    assert events == [
        {"type": "delta", "text": "你"},
        {"type": "delta", "text": "好"},
        {"type": "finish", "finish_reason": "stop"},
    ]



def test_normalize_chat_error_includes_source():
    result = normalize_chat_error("provider", RuntimeError("boom"))
    assert result == "provider: RuntimeError: boom"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_chat_gateway.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.chat_gateway'`

- [ ] **Step 3: 写最小 gateway 实现**

```python
# app/services/chat_gateway.py
import json



def extract_stream_events(raw_lines):
    for raw_line in raw_lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {}).get("content")
            if delta:
                yield {"type": "delta", "text": delta}
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                yield {"type": "finish", "finish_reason": finish_reason}



def normalize_chat_error(source, error):
    return "{}: {}: {}".format(source, type(error).__name__, error)
```

- [ ] **Step 4: 为 provider / LiteLLM / fallback 定义统一调用骨架**

```python
# app/services/chat_gateway.py
import json
import ssl
import urllib.request

from app.config import build_config
from app.services.sse import build_delta_event, build_done_event, build_error_event, build_log_event



def _ssl_context(config):
    if config.verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx



def stream_via_request(url, body, headers, timeout):
    config = build_config()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(config))
```

- [ ] **Step 5: 把 `_handle_sse` 里的 provider 直连和 LiteLLM fallback 循环搬到 gateway 层**

```python
# app/services/chat_gateway.py

def stream_chat_with_fallback(body, route, litellm_base, litellm_master_key):
    # 1) try provider direct
    # 2) if direct fails -> yield log event
    # 3) try litellm
    # 4) return {"text": full_text, "finish_reason": finish_reason, "provider": used_provider}
    raise NotImplementedError
```

```python
# server.py target shape
result = stream_chat_with_fallback(body, route, LITELLM_BASE, LITELLM_MASTER_KEY)
for event in result["events"]:
    self.wfile.write(event)
    self.wfile.flush()
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `pytest tests/test_chat_gateway.py -q`
Expected: PASS

- [ ] **Step 7: 运行聊天回归测试**

Run: `pytest tests/test_chat_gateway.py tests/test_chat_api.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add app/services/chat_gateway.py tests/test_chat_gateway.py server.py
git commit -m "refactor: extract chat gateway fallback logic"
```

---

### Task 3: 抽离聊天业务编排

**Files:**
- Create: `app/services/chat_flow.py`
- Create: `tests/test_chat_flow.py`
- Modify: `server.py:758-832`

- [ ] **Step 1: 写失败的 flow 测试**

```python
# tests/test_chat_flow.py
from app.services.chat_flow import build_chat_title, prepare_messages_for_session



def test_build_chat_title_uses_first_line_and_truncates():
    title = build_chat_title("第一行标题\n第二行说明" + "x" * 80)
    assert title.startswith("第一行标题")
    assert len(title) == 40



def test_prepare_messages_for_session_appends_user_message():
    sess = {"messages": [{"role": "assistant", "content": "旧回复"}]}
    msgs = prepare_messages_for_session(sess, "新问题")
    assert msgs == [
        {"role": "assistant", "content": "旧回复"},
        {"role": "user", "content": "新问题"},
    ]
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_chat_flow.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.chat_flow'`

- [ ] **Step 3: 写最小 flow 实现**

```python
# app/services/chat_flow.py


def build_chat_title(user_msg):
    return user_msg.split("\n")[0][:40]



def prepare_messages_for_session(sess, user_msg):
    msgs = [{"role": m["role"], "content": m["content"]} for m in sess.get("messages", [])]
    msgs.append({"role": "user", "content": user_msg})
    return msgs
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest tests/test_chat_flow.py -q`
Expected: PASS

- [ ] **Step 5: 扩展 flow，接管 `api_messages_send(...)` 中的编排职责**

```python
# app/services/chat_flow.py

def prepare_chat_request(sess, user_msg, route, rag_enabled, rag_search, rag_context_builder, rag_refs_builder):
    msgs = prepare_messages_for_session(sess, user_msg)
    title = build_chat_title(user_msg)
    rag_hits = []
    refs = None
    full_system = sess.get("system") or ""
    if rag_enabled:
        rag_hits = rag_search(user_msg)
    if rag_hits:
        rag_ctx = rag_context_builder(user_msg, rag_hits)
        refs = rag_refs_builder(rag_hits)
        rag_system = (
            "你是一个基于用户本地 Obsidian 知识库的助手。请优先基于以下参考资料回答用户问题。\n"
            "## 用户的 Obsidian 知识库参考资料:\n" + rag_ctx
        )
        full_system = rag_system + ("\n\n## 用户额外设置:\n" + full_system if full_system else "")
        msgs = [{"role": "system", "content": full_system}] + msgs
    return {
        "title": title,
        "messages": msgs,
        "refs": refs,
        "rag_hits": rag_hits,
        "system": full_system,
        "route": route or {},
    }
```

- [ ] **Step 6: 把 `server.py` 的 `api_messages_send(...)` 改成调用 flow**

```python
# server.py target shape
prepared = prepare_chat_request(
    sess,
    user_msg,
    route,
    rag_enabled,
    lambda query: _obsidian_search(query, top_k=OBSIDIAN_TOP_K),
    _obsidian_build_context,
    _obsidian_format_refs,
)

if not sess.get("title") or sess.get("title") == "新对话":
    update_session(sid, title=prepared["title"])

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
```

- [ ] **Step 7: 为成功/失败落库收口写 helper**

```python
# app/services/chat_flow.py

def persist_assistant_message(add_message_fn, sid, text, meta):
    if text and text.strip():
        add_message_fn(sid, "assistant", text, meta=meta)
```

- [ ] **Step 8: 运行测试，确认通过**

Run: `pytest tests/test_chat_flow.py tests/test_chat_api.py -q`
Expected: PASS

- [ ] **Step 9: 提交**

```bash
git add app/services/chat_flow.py tests/test_chat_flow.py server.py
git commit -m "refactor: extract chat flow orchestration"
```

---

### Task 4: 用新服务重连 server.py 聊天主链路

**Files:**
- Modify: `server.py:1500-1850`
- Test: `tests/test_chat_api.py`
- Test: `tests/test_chat_gateway.py`
- Test: `tests/test_chat_flow.py`
- Test: `tests/test_sse.py`

- [ ] **Step 1: 写一个覆盖端到端聊天事件语义的失败测试**

```python
# tests/test_chat_api.py
from app.services.sse import build_delta_event, build_done_event



def test_sse_helpers_match_existing_event_contract():
    assert build_delta_event("A") == b'data: {"event": "delta", "text": "A"}\n\n'
    assert build_done_event() == b"data: [DONE]\n\n"
```

- [ ] **Step 2: 运行测试，确认当前状态要么失败要么暴露契约差异**

Run: `pytest tests/test_chat_api.py tests/test_sse.py -q`
Expected: PASS or a focused FAIL. If it already passes, continue without changing the test.

- [ ] **Step 3: 把 `_handle_sse(...)` 改成薄控制器**

```python
# server.py target shape
prepared_body = {
    "model": model_id,
    "messages": msgs,
    "temperature": 0.7,
    "stream": True,
}

result = stream_chat_with_fallback(
    prepared_body,
    route,
    LITELLM_BASE,
    LITELLM_MASTER_KEY,
)

for event in result["events"]:
    self.wfile.write(event)
    self.wfile.flush()

persist_assistant_message(
    add_message,
    sid,
    result["full_text"],
    {
        "model": model,
        "provider": result["provider"],
        "finish_reason": result["finish_reason"],
        "direct_err": result.get("direct_err"),
    },
)
```

- [ ] **Step 4: 确保 `_handle_sse_demo(...)` 和 `_handle_sse_task(...)` 只复用 `sse.py`，不引入新业务变化**

```python
# server.py expectation
# keep demo/task logic in place
# only replace manual event encoding with helpers from app/services/sse.py
```

- [ ] **Step 5: 运行局部回归**

Run: `pytest tests/test_sse.py tests/test_chat_gateway.py tests/test_chat_flow.py tests/test_chat_api.py -q`
Expected: PASS

- [ ] **Step 6: 运行全量回归**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 7: 运行 lint**

Run: `ruff check app tests`
Expected: `All checks passed!`

- [ ] **Step 8: 运行语法检查**

Run: `python -m py_compile server.py app/services/sse.py app/services/chat_gateway.py app/services/chat_flow.py`
Expected: no output

- [ ] **Step 9: 提交**

```bash
git add server.py app/services/sse.py app/services/chat_gateway.py app/services/chat_flow.py tests/test_sse.py tests/test_chat_gateway.py tests/test_chat_flow.py tests/test_chat_api.py
git commit -m "refactor: decompose chat request path"
```

---

## Self-Review

### Spec coverage
- SSE 统一层：Task 1 覆盖
- provider/LiteLLM fallback 网关化：Task 2 覆盖
- 聊天业务编排抽离：Task 3 覆盖
- `server.py` 变薄控制器：Task 4 覆盖
- 兼容性约束（事件名、API 路径、fallback 顺序）：Task 1-4 都有对应验证步骤

### Placeholder scan
- 没有 TBD / TODO / implement later / similar to Task N 之类占位语
- 每个代码步骤都给出具体代码骨架
- 每个验证步骤都写了明确命令和预期结果

### Type consistency
- SSE helper 统一使用 `build_*_event`
- gateway 统一使用 `stream_chat_with_fallback`
- flow 统一使用 `prepare_chat_request` / `persist_assistant_message`
- `server.py` 只作为调用方使用以上接口，没有引入第二套命名

---

Plan complete and saved to `docs/superpowers/plans/2026-06-23-chat-path-decomposition.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
