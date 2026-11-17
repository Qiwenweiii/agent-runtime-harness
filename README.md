# Agent Runtime — 从零手搓的轻量级执行引擎

一个不依赖 `LangChain` / `LangGraph` 等任何框架，仅使用原生 `OpenAI SDK` 与 Python 标准库从零实现的 Agent 底层执行引擎。

本项目旨在剥离框架的黑盒，探索大模型 Tool Calling 的底层机制。代码规模极简（10 个文件，<1000 行），但完整实现了**权限拦截、MCP 协议集成、HITL 中断恢复与 Trace 审计**，具备生产级 Agent 底座的雏形。

## 💡 为什么会有这个项目？
在使用高层框架时，Agent 的执行循环、状态挂起、协议解析都被封装在底层。为了真正掌控大模型的执行边界（特别是高危工具调用的安全兜底），本项目选择“造轮子”，从 `chat + tool_calls` 的最简循环跑通，逐步叠加上真实的工程级护栏。

## ✨ 核心特性

- **极简依赖**：仅依赖 `openai` 与 `python-dotenv`，其余全部基于 Python 标准库。
- **原生 Function Calling**：从零解析工具调用、执行并回喂结果，拒绝黑盒。
- **细粒度权限管控**：基于 `ToolSpec` 的风险分级（readonly/write），结合 `plan_mode` 实现高危拦截。
- **MCP 协议无缝接入**：通过 stdio 收发 `JSON-RPC 2.0`，将外部 MCP Server 的工具透明注册为本地 `ToolSpec`，实现跨进程调度。
- **HITL 跨进程恢复**：高危写操作触发挂起并落库（SQLite），支持通过 `--resume` 恢复上下文并人工审批，防止 Agent 失控。
- **全链路 Trace 审计**：按 `session_id` 聚合生命周期日志，包含审批挂起与恢复，提供完整的执行追踪证据。

## 🏗️ 架构与模块职责

```text
src/runtime/
├── main.py            # 核心入口：Agent 循环、HITL 挂起与恢复逻辑
├── tools.py           # ToolSpec 契约定义与工具注册表
├── permissions.py     # 权限引擎：基于 risk 与 plan_mode 的 allow/deny 判定
├── mcp_client.py      # MCP 客户端：stdio 子进程与 JSON-RPC 通信
├── mcp_server.py      # 本地 MCP Server 演示：对外暴露工具
├── session_store.py   # 状态恢复：基于 sqlite3 持久化挂起会话
└── runtime_trace.py   # 审计日志：JSONL 格式的事件流记录
```

## 🚀 快速启动

```bash
# 1. 安装依赖
uv sync

# 2. 配置模型环境变量
cp .env.example .env
# 填入 OPENAI_API_KEY, OPENAI_MODEL (及可选的 OPENAI_BASE_URL)

# 3. 启动 CLI 交互
uv run --no-sync python src/runtime/main.py
```

### 高阶运行模式
```bash
# 计划模式：所有 write 工具（如重启服务）将被直接拒绝
uv run --no-sync python src/runtime/main.py --plan-mode

# 技能装配：指定 system prompt 与工具白名单
uv run --no-sync python src/runtime/main.py --skill status_check

# 人工审批恢复：高危操作挂起后，使用指定 session_id 续跑
uv run --no-sync python src/runtime/main.py --resume {session_id}
```

## 🧠 核心机制解析

### 1. 权限与 MCP 统一管控
无论是本地演示工具还是通过 MCP 动态发现的外部工具，在注册时都会统一转换为 `ToolSpec`。
`check_permission` 引擎会基于工具的 `risk` 属性进行裁决：
- `readonly`：直接放行。
- `write` + `plan_mode`：直接拒绝（deny），并将拒绝原因回喂给大模型。
- `write` + 非 `plan_mode`：触发 `needs_human_review`，进入 HITL 挂起流程。

### 2. 跨进程 HITL (Human-in-the-Loop)
由于写操作需人工确认，进程不可一直阻塞。命中审批时，引擎会将 `messages`、`step`、`pending_tool_call` 及后续未执行的 `remaining` 调用快照序列化存入 SQLite。
用户通过 CLI 恢复时，系统重建上下文，展示待审操作，批准后执行并继续主循环，确保多轮消息合法性与状态一致性。

### 3. 结构化 Trace 审计
运行事件追加写入 `traces/trace_{session_id}.jsonl`。事件节点包括 `run_start`、`model_call`、`tool_call`、`hitl_pause` 等。通过 `session_id` 聚合，使得挂起前后的执行链路被串联在同一条时间线上，方便排障与合规回溯。

## 🧪 测试保障
```bash
uv run --no-sync pytest
```
包含全链路离线单测：使用 `Mock` 模型客户端，绝不发起真实网络请求，覆盖主循环逻辑、HITL 挂起恢复、参数安全解析与权限判定。

## ⚠️ 已知局限与演进规划
- **单机单例**：当前工具注册表与状态管理为进程内单例，未来可向会话级隔离与并发安全演进。
- **传输协议**：MCP 当前仅实现 stdio 传输，计划补齐 SSE 及流式通知。
- **上下文管理**：暂未处理长上下文截断、Token 成本统计，属于 V1.0 之后的进阶功能。
