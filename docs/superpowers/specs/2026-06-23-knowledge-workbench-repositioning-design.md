# Obsidian-First Knowledge Workbench Repositioning Design

**Date:** 2026-06-23
**Project:** openwebui-lite
**Scope:** 重新定义项目目标，从“轻量 OpenWebUI 替代品”转向“以 Obsidian 为核心数据源的个人知识工作台”，并给出分阶段重构路线。

## Goal

把项目重新定位为一个 Obsidian-first 的个人知识工作台：平台核心是从本地 Obsidian 知识库持续获取最新内容，经过检索、组织、引用和大模型润色后，为用户提供知识问答、知识写作、知识搜索、知识任务和知识内容视觉生成能力。

## Non-Goals

本次设计不直接进入代码实现，也不一次性重写整个项目。当前目标是明确产品定位、核心架构和后续重构顺序，为继续改造而不是推倒重写提供依据。

## Product Repositioning

当前项目不应再被理解为“聊天系统加一点 RAG”，而应明确为：

> **Obsidian-first Personal Knowledge Workbench**

在这个定位下：

- Obsidian 是第一数据源，不是聊天插件
- 大模型的角色是理解、润色、扩写、整理和生成，而不是脱离知识库独立回答
- 聊天、搜索、写作、任务、图片都是对知识库的不同操作入口

## Why Continue Refactoring Instead of Rewriting

继续改造比重写更合适，原因如下：

1. 现有项目已经具备本地运行、SQLite、SSE、Provider 路由、chat/tasks/image/obsidian 原型等有价值资产
2. 当前真实需求已经通过现有功能验证出方向，重写只会丢掉这些验证结果
3. 现在的问题主要是架构中心错位和模块边界不清，而不是技术路线完全错误

因此推荐路线是：

> 保留现有产品外壳和能力原型，逐步把内部架构替换为 Knowledge-first 模式。

## Architecture

### 1. Knowledge Core

这是平台内核，也是最先要成型的部分。它负责：

- Obsidian 数据接入
- 最新数据同步 / 索引刷新
- 检索
- 引用与可追溯性
- RAG context 构造
- 分类 / 摘要 / 标签扩展基础

Knowledge Core 必须成为：

- Chat 的底层支撑
- Search QA 的底层支撑
- Writing 的底层支撑
- Tasks 的底层支撑

它不再服务于聊天，而是所有能力都服务于它。

### 2. Capability Layer

这是能力入口层，每个能力都是对 Knowledge Core 的不同消费方式：

- Chat
- Search QA
- Writing
- Image Generation
- Tasks

这些能力未来都应该共享统一知识上下文，而不是各自复制一套知识接入逻辑。

### 3. Workflow Layer

这是把能力连接成工作台的层。目标是支持：

- 搜索 → 问答
- 搜索 → 写作
- 搜索 → 任务
- 问答 → 文档沉淀
- 文档 → 图片生成
- 任务执行 → 知识更新

如果没有这一层，项目仍然只是功能集合；有了这一层，项目才是知识工作台。

## Capability Priorities

### 1. Knowledge Search

平台首先要能基于 Obsidian 提供可靠搜索，而不只是“把一些片段塞给模型”。

它应该逐步具备：

- 新内容及时进入索引
- 命中有排序依据
- 返回可追溯引用
- 能说明回答依据来自哪些笔记

### 2. Knowledge QA

问答是知识库消费入口之一，而不是系统中心。问答结果应明确区分：

- 来自本地知识库的依据
- 模型的润色与补充

### 3. Knowledge Writing

写作要成为正式能力，而不是聊天输出的副产品。平台应支持：

- 基于已有知识生成草稿
- 组织笔记/总结/文章/文档
- 润色并保留引用意识

### 4. Knowledge Tasks

任务能力应升级为知识执行层，而不是单纯待办列表。它未来的价值是：

- 从知识内容提炼任务
- 让任务执行结果回流知识库
- 把知识整理过程变成可推进的行动流

### 5. Knowledge-to-Image

图片能力不是独立玩具，而是知识表达层的一部分。未来应主要服务于：

- 文档配图
- 概念图示
- 知识内容视觉摘要

## Refactor Strategy

### Phase 1: Establish Knowledge Core

这是最优先阶段。目标是把 Obsidian/RAG 从附加功能升级成平台内核。

重点包括：

- 独立 `obsidian_index`
- 独立 `obsidian_search`
- 独立 `rag_context`
- 独立 `obsidian_classify`

阶段目标：所有知识消费能力都统一从这里取数据。

### Phase 2: Finish Thinning Chat

聊天继续保留，但角色转变为知识问答入口。

重点包括：

- 完成 `chat_flow` 收口
- 让 assistant 落库也通过 flow 管理
- 让 `_handle_sse(...)` 只保留协调职责
- 确保 chat 只消费 Knowledge Core，不承载知识逻辑

### Phase 3: Decompose Tasks

任务链路是下一个最适合拆的模块，因为它和 chat 同样有：

- prompt
- 执行过程
- 流式结果
- 状态落库

建议目标结构：

- `task_flow`
- `task_runner`
- `task_store`
- 复用 `sse`

### Phase 4: Promote Writing to First-Class Capability

写作能力必须从“聊天能写点东西”升级成正式入口。重点包括：

- 从知识检索结果生成结构化草稿
- 支持总结、文档、笔记、文章输出
- 明确知识依据与润色层次

### Phase 5: Integrate Image as Knowledge Expression

图片能力最后接入知识表达工作流，而不是单独存在。重点包括：

- 文档配图
- 概念图示
- 视觉化知识表达

## Target File Layout

### Shared foundation
- `app/config.py`
- `app/db.py`
- `app/web.py`
- `app/services/sse.py`

### Chat
- `app/services/chat_flow.py`
- `app/services/chat_gateway.py`
- `app/services/chat_store.py`（后续补齐）

### Tasks
- `app/services/task_flow.py`
- `app/services/task_runner.py`
- `app/services/task_store.py`

### Obsidian / RAG
- `app/services/obsidian_index.py`
- `app/services/obsidian_search.py`
- `app/services/obsidian_classify.py`
- `app/services/rag_context.py`

### Optional shared result/error layer
- `app/services/result_types.py` 或 `app/services/http_errors.py`

## Sequencing Recommendation

推荐按以下顺序实施：

1. **先完成 Knowledge Core 设计与落位**
2. **再完成 chat 最后一轮压薄**
3. **然后拆 tasks**
4. **再把 writing 升级为正式能力**
5. **最后再整合 image**

## Risks

### 1. Still treating Obsidian as a plugin

如果后续改造仍然把 Obsidian 当作聊天附加插件，那么平台定位会继续漂移，最终又回到“多功能聊天壳子”。

### 2. Capability-first instead of knowledge-first

如果先继续堆更多 UI 或功能按钮，而不是先立 Knowledge Core，后续每个能力都会重复接知识逻辑，返工成本会很高。

### 3. Over-refactoring without workflow value

如果只拆文件、不明确“搜索 → 问答 → 写作 → 任务”的知识工作流，项目虽然结构更干净，但仍不符合真实需求。

## Scope Check

这份设计不是单个实现计划的范围，而是后续多个实现周期的总路线图。它为接下来一段时间的重构提供统一目标，后续应拆成多个子设计/子计划执行。

## Ambiguity Resolution

为避免后续理解偏差，这里明确：

- 项目的第一核心能力是 **Knowledge Workbench**，不是聊天
- Chat 是知识入口之一，不是系统中心
- Obsidian / RAG 是平台核心，不是附加功能
- 继续改造优先于全量重写

## Review Summary

- 无 TBD / TODO / 未定占位
- 目标、架构、阶段路线、能力排序一致
- 已明确这是多阶段路线图，而不是单次实现计划
