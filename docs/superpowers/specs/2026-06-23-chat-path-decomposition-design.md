# Chat Path Decomposition Design

**Date:** 2026-06-23
**Project:** openwebui-lite
**Scope:** 深拆 `server.py` 的聊天主链路，统一 SSE 输出、provider/LiteLLM fallback、聊天业务编排边界。

## Goal

把当前集中在 `server.py` 内的聊天主链路拆成清晰的服务边界，同时保持现有前端接口和主要事件语义兼容。重构完成后，`server.py` 主要承担 HTTP 入口职责，聊天业务编排、上游网关调用、SSE 事件编码分别落在独立模块中。

## Non-Goals

本次不做以下事项：

- 不重构前端聊天 UI 结构
- 不彻底拆 task 链路
- 不彻底拆 obsidian / RAG 全链路，只在聊天入口需要时调用
- 不做全量 `server.py` 风格清理
- 不修改外部 API 路径和主要返回语义

## Recommended Approach

采用“聊天编排服务化”方案：

1. 新增 `app/services/sse.py`，统一 SSE 事件编码
2. 新增 `app/services/chat_gateway.py`，统一 provider / LiteLLM 流式调用与 fallback
3. 新增 `app/services/chat_flow.py`，负责聊天业务编排和消息落库协作
4. 修改 `server.py`，让它只做请求接收、调用 flow/gateway、写出事件

这个方案比“只抽 SSE/gateway”更有收益，也比“后端前端一起大改”风险更低，适合当前项目的连续演进。

## Architecture

### 1. app/services/sse.py

职责：统一构造 SSE 事件。

提供的 helper：

- `build_sse_event(payload)`
- `build_done_event()`
- `build_delta_event(text)`
- `build_error_event(message, extra=None)`
- `build_log_event(text)`
- `build_refs_event(refs, hit_count=None)`

约束：

- 输出仍保持当前前端可识别的 `delta / error / refs / log / done` 语义
- 统一编码为 `data: <json>\n\n`
- 所有流式链路复用，不再在各个 handler 中手写 JSON 字符串

### 2. app/services/chat_gateway.py

职责：统一上游聊天请求和 fallback。

负责：

- provider 直连 URL 拼接
- LiteLLM 请求构造
- 流式 chunk 解析
- `finish_reason` 提取
- provider 失败后 fallback LiteLLM
- 错误归一化

不负责：

- session 查找
- user message 入库
- 标题生成
- RAG prompt 业务规则
- HTTP header / socket flush

建议接口：

- `stream_via_provider(...)`
- `stream_via_litellm(...)`
- `stream_chat_with_fallback(...)`

输出建议统一成内部事件迭代器或统一结果对象，让 `server.py` 只做事件转发。

### 3. app/services/chat_flow.py

职责：聊天业务编排。

负责：

- 校验 `sid` / `message`
- 读取 session
- 写入 user message
- 自动生成标题
- 基于 RAG 开关决定是否检索 Obsidian
- 构造最终发送给 gateway 的 `messages/system/route/meta`
- 成功或失败时协助落库 assistant 回复及元数据

不负责：

- provider 请求细节
- HTTP 层 SSE 写出
- 原始 socket 操作

建议接口：

- `prepare_chat_request(...)`
- `finalize_chat_success(...)`
- `finalize_chat_error(...)`

### 4. server.py

保留为整合入口，但职责缩小：

- `api_messages_send(...)` 只做 HTTP body 解析和调用 `chat_flow.prepare_chat_request(...)`
- `_handle_sse(...)` 只负责驱动 gateway 并把事件写给客户端
- `_handle_sse_demo(...)` 复用 `sse.py`
- `_handle_sse_task(...)` 暂时保留逻辑，但统一改用 `sse.py` 输出事件

## Data Flow

### A. 普通聊天请求

入口仍为 `POST /api/messages`。

1. `server.py` 接收 HTTP 请求
2. `chat_flow.prepare_chat_request(...)`：
   - 校验输入
   - 读取 session
   - 写入 user message
   - 自动标题
   - RAG 拼接（如启用）
   - 生成标准化请求对象
3. `server.py` 根据返回决定是错误 JSON 还是进入流式执行
4. `chat_gateway.stream_chat_with_fallback(...)`：
   - 优先 provider 直连
   - 失败则记录 direct error
   - 回退 LiteLLM
   - 统一产生事件流
5. `server.py` 把事件写为 SSE
6. `chat_flow.finalize_chat_success(...)` / `finalize_chat_error(...)` 负责 assistant 落库一致性

### B. Demo 聊天

`__demo__` 模式保留，但输出不再自己拼接事件字符串。demo 内容生成逻辑可以保留在单独函数中，事件输出统一走 `sse.py`。

### C. Task Run SSE

本次不是任务主拆对象，但 `_handle_sse_task(...)` 的事件输出要复用 `sse.py`。这样后续拆 task 链路时不会重复改事件层。

## File Plan

### New files

- `app/services/sse.py`
- `app/services/chat_gateway.py`
- `app/services/chat_flow.py`
- `tests/test_sse.py`
- `tests/test_chat_gateway.py`
- `tests/test_chat_flow.py`

### Modified files

- `server.py`
- 如有必要，补充 `app/services/__init__.py`

## Testing Plan

### tests/test_sse.py

验证：

- delta event 输出格式
- error event 输出格式
- refs event 输出格式
- done event 输出格式

目标：锁定 SSE 编码层，避免后续事件协议漂移。

### tests/test_chat_gateway.py

验证：

- provider 流成功
- provider 失败后 fallback LiteLLM
- 两边都失败时错误归一化
- chunk 解析可正确提取 `delta` 和 `finish_reason`

目标：把上游调用风险集中在网关层测试中。

### tests/test_chat_flow.py

验证：

- session 不存在时报错
- 标题自动生成
- RAG 开启时正确注入 refs/system
- demo 模式分流
- assistant 成功落库时元数据结构正确

目标：锁定聊天业务规则，不让拆分影响用户可见行为。

### Verification commands

- `pytest tests/test_sse.py -q`
- `pytest tests/test_chat_gateway.py -q`
- `pytest tests/test_chat_flow.py -q`
- `pytest -q`
- `ruff check app tests`

## Compatibility Rules

为控制风险，实施时必须满足：

- 前端仍能识别现有事件名：`delta / error / refs / log / done`
- `/api/messages` 路径不变
- provider fallback 顺序保持一致：先直连，后 LiteLLM
- assistant 落库语义不变：成功回复入库，错误保持可追踪

## Risks

### 1. SSE 事件兼容性风险

风险：事件名字、字段名、done 触发时机变化，导致前端流式渲染异常。

控制方式：

- 先写 `tests/test_sse.py`
- 保持事件名不变
- 统一 helper 输出

### 2. Fallback 行为偏移

风险：provider 失败后 LiteLLM 兜底与旧逻辑 subtly 不一致。

控制方式：

- 抽出 `chat_gateway.py`
- 用 gateway 测试锁定顺序和错误语义

### 3. Assistant 落库时机变化

风险：消息历史丢失、重复、元数据不一致。

控制方式：

- 在 `chat_flow.py` 中集中成功/失败收口
- 用 flow 测试覆盖落库行为

## Scope Check

这份设计聚焦在“聊天主链路深拆分”，范围足够集中，可以作为单个实现计划执行，不需要再拆成多个独立子项目。

## Ambiguity Resolution

为避免执行时歧义，这里明确：

- 本次拆分以 **后端** 为主，不同时重构前端聊天状态管理
- task SSE 只统一事件输出，不重构任务业务编排
- RAG 逻辑只在聊天入口需要的部分接入，不把整个 Obsidian 子系统搬走

## Review Summary

- 无 TBD / TODO / 待定占位
- 架构、数据流、测试、兼容性约束一致
- 范围聚焦在聊天主链路，不扩散到其他子系统
