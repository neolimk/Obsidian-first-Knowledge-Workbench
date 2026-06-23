# openwebui-lite

> 轻量级 Open WebUI 替代品 · 通过 LiteLLM 网关路由多模型
> 灵感:[open-webui/open-webui](https://github.com/open-webui/open-webui)

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export OPENWEBUI_LITE_MASTER_KEY="replace-me"
pytest
ruff check .
```

## 环境变量

- `OPENWEBUI_LITE_MASTER_KEY`：必填，LiteLLM master key
- `OPENWEBUI_LITE_BASE`：可选，默认 `http://127.0.0.1:4000`
- `OPENWEBUI_LITE_VERIFY_TLS`：可选，默认 `true`
- `OPENWEBUI_LITE_CORS_ORIGINS`：可选，默认 `http://127.0.0.1:8899`
- `OPENWEBUI_LITE_PORT`：可选，默认 `8899`

## 架构

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  浏览器          │ ──> │ openwebui-lite   │ ──> │  LiteLLM 4000    │
│  http://:8899    │     │  (Python 单文件)  │     │  (master_key)   │
│  (前端 SPA)      │     │  (本项目)        │     │                 │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                                              ┌───────────┼───────────┐
                                              ▼           ▼           ▼
                                         newapi       kaudex      aiapi1
                                         (gpt-5.x)    (claude)    (grok/image)
```

## 文件结构

```
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

## API 端点

| 端点 | 方法 | 用途 |
|---|---|---|
| `/` | GET | 静态 SPA |
| `/health` | GET | 健康检查 |
| `/api/config` | GET | 服务配置 |
| `/api/models` | GET | 从 LiteLLM 拉模型列表 |
| `/api/sessions` | POST | 新建会话 `{model, title?, system?}` |
| `/api/sessions` | GET | 列会话(不含消息) |
| `/api/sessions/get` | POST | 会话详情 `{id}` → `{session: {messages: [...]}}` |
| `/api/sessions/delete` | POST | 删 `{id}` |
| `/api/sessions/rename` | POST | 改标题 `{id, title}` |
| `/api/sessions/update` | POST | 改 title/model/system `{id, title?, model?, system?}` |
| `/api/messages` | POST | **SSE 流式 chat** `{id, message}` |
| `/api/images/generations` | POST | 图片生成 `{prompt, model, n, size}` |

## 运维

```bash
~/workspace/openwebui-lite/ctl.sh start     # 启
~/workspace/openwebui-lite/ctl.sh stop      # 停
~/workspace/openwebui-lite/ctl.sh restart   # 重启
~/workspace/openwebui-lite/ctl.sh status    # 状态
~/workspace/openwebui-lite/ctl.sh enable    # 开机自启
~/workspace/openwebui-lite/ctl.sh disable   # 关自启
```

日志:`~/logs/openwebui-lite.log`

## 关键设计

1. **流式响应 (SSE)**:浏览器 `fetch` + `ReadableStream` 解析 SSE events,逐 token 渲染
2. **SQLite WAL 模式**:并发安全
3. **CJK 友好**:Fira Sans + Noto Sans SC + `marked.js` 渲染 Markdown
4. **代码高亮**:highlight.js 自动应用
5. **多会话管理**:侧栏会话列表 + 切换 / 重命名 / 删除
6. **图片生成**:复用 LiteLLM 4000 的 `/v1/images/generations` 端点

## 前端功能(借鉴 open-webui)

- ✅ 左侧栏:模型选择器 + 会话列表 + 新建按钮
- ✅ 主区:消息流(用户/AI 头像区分)
- ✅ Markdown 渲染 + 代码高亮
- ✅ 流式响应(逐 token 显示)
- ✅ 系统提示词(每会话可设)
- ✅ 图片生成模式(独立 panel)
- ✅ 模态框设置
- ✅ 示例问题卡片(空状态)
- ✅ AbortController(停止生成)
- ✅ 主题:深色 OLED(后续 UI/UX 重构时调优)

## 后续可加

- ⏳ 历史搜索 / 标签
- ⏳ 多用户 / 鉴权
- ⏳ 文档 / 图片附件(走 LiteLLM `/v1/files`)
- ⏳ Agent / Function calling
- ⏳ RAG 知识库
- ⏳ UI/UX 升级(用 ui-ux-pro-max + impeccable skill)
