# agent-runtime — 极简 Agent Runtime

一个从零手写的最小 Agent 运行时：不依赖 LangChain / LangGraph 等框架，只用 OpenAI SDK 的 chat + function calling，靠「模型调用 → 工具执行 → 结果回喂」的循环跑通 Agent 核心机制，并逐版本叠加权限控制、Skill 装配、MCP 协议接入与 HITL 人工审批。

依赖只有 `openai` 与 `python-dotenv`，其余全是标准库；`src/runtime` 十个文件、不足 1000 行，适合用来搞清楚框架到底替你封装掉了什么。

## 版本演进

每一版只解决上一版暴露出来的问题。

| 版本 | 新增能力                                                                      |
| ---- | ------------------------------------------------------------------------------- |
| v0   | 50 行单文件：chat + tool_calls 循环，验证「模型驱动循环」最小可行             |
| v1   | Agent 主循环（chat + function calling）                                       |
| v2   | 工具注册表、risk 声明（readonly / write）                                     |
| v3   | 权限拦截：write 工具直接 deny 并把原因回喂                                    |
| v4   | Skill 装配：system prompt + 工具白名单 + plan_mode                            |
| v5   | MCP 支持：stdio 子进程 JSON-RPC，远程工具透明注册                             |
| v6   | HITL 暂停与恢复：write 工具挂起落库，`--resume` 人工审批后续跑                |
| v7   | Trace 按 session_id 聚合：一次业务生命周期（含审批恢复）写入同一个 trace 文件 |

## 目录结构

```
src/runtime/
├── main.py            # 入口：CLI 参数、Agent 循环、HITL 挂起/恢复
├── tools.py           # ToolSpec 与工具注册表（name -> ToolSpec）
├── tools_impl.py      # 本地演示工具（get_service_status / get_recent_logs / restart_service）
├── permissions.py     # check_permission：按 risk + plan_mode 判定 allow/deny
├── parsing.py         # parse_tool_args：参数串安全解析，绝不抛异常
├── skill.py           # Skill 加载：system prompt + allowed_tools + plan_mode
├── skills/            # Skill 定义（json）
├── mcp_client.py      # MCP 客户端：以子进程 stdio 收发 JSON-RPC 2.0
├── mcp_server.py      # 演示用 MCP server（暴露 get_pod_events）
├── session_store.py   # HITL 会话状态存储：标准库 sqlite3
└── runtime_trace.py   # jsonl 事件 trace，按 session_id 聚合文件名
tests/                 # 离线单测：mock 模型客户端，全程不发网络请求
```

运行时产物不进仓库：`sessions.db`（HITL 会话状态）、`traces/*.jsonl`（事件日志）、`.env`（模型配置）都已在 `.gitignore` 中。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置模型：复制 .env.example 为 .env 后填写
cp .env.example .env

# 3. 运行（启动后输入一句用户需求）
uv run --no-sync python src/runtime/main.py
uv run --no-sync python src/runtime/main.py --plan-mode           # 计划模式：write 全部直接拒绝
uv run --no-sync python src/runtime/main.py --skill status_check  # 换一套 Skill（工具白名单 + system prompt）
uv run --no-sync python src/runtime/main.py --resume {session_id} # 审批并恢复挂起的会话
```

触发挂起后终端会打印 `session_id`；可用 sqlite3 查看会话状态：

```bash
sqlite3 sessions.db "SELECT session_id, status FROM sessions;"
```

### 环境变量

| 变量              | 必填 | 说明                                         |
| ----------------- | ---- | ---------------------------------------------- |
| `OPENAI_API_KEY`  | 是   | 模型接入密钥                                 |
| `OPENAI_BASE_URL` | 否   | OpenAI 兼容端点（换国产模型 / 自建网关时填） |
| `OPENAI_MODEL`    | 是   | 模型名，需支持 function calling              |

### CLI 参数

| 参数          | 默认                | 说明                                             |
| ------------- | ------------------- | -------------------------------------------------- |
| `--skill`     | `incident_response` | 加载 `src/runtime/skills/{skill}.json`           |
| `--plan-mode` | `false`             | 计划模式，拒绝所有 write 工具                    |
| `--max-steps` | `5`                 | Agent 循环上限，超限记 `max_steps_exceeded` 结束 |
| `--resume`    | 无                  | 传入 session_id 恢复 pending 会话并走人工审批    |

## 核心机制

### Agent 循环

`run_agent` 每轮调模型：有 `tool_calls` 就逐个执行并以 `role="tool"` 回喂结果；没有工具调用即视为最终回答并结束。`start_step` 让 HITL 恢复时从挂起步的下一步续跑，而不是从第 1 步重来。

### 权限模型

`check_permission(spec, plan_mode)` 只看两点：工具自己声明的 `risk`，和当前是否 plan_mode。

| 场景                        | 结果                                                |
| --------------------------- | ----------------------------------------------------- |
| `risk=readonly`             | allow，直接执行                                     |
| `risk=write` + plan_mode    | deny（reason=`plan_mode`），拒绝原因回喂模型        |
| `risk=write` + 非 plan_mode | deny（reason=`needs_human_review`）→ 触发 HITL 挂起 |

本地工具与 MCP 工具走同一套判定，远程工具不会因为来源不同而绕过权限。

### HITL：跨进程暂停与恢复

写操作要人审，但人不可能一直等在进程里，所以把循环状态快照落库、进程退出，由人另行发起恢复：

1. 命中 `needs_human_review` 时，把 `messages / step / pending_tool_call / skill_config / plan_mode / max_steps` 序列化存入 `sessions.db`（status=pending），进程退出。
2. 同一轮里排在后面、还没执行的 tool_calls 一并记入 `pending_tool_call.remaining`——否则恢复后 assistant 与 tool 消息配对不合法。
3. `--resume {session_id}` 读回状态，终端 y/n 审批：批准则执行挂起工具并补跑 remaining，拒绝则把「用户拒绝了该操作」回喂；随后接回主循环续跑。

### Skill 装配

Skill 是一份 JSON 声明：`system_prompt`（角色与约束）、`allowed_tools`（模型可见工具白名单）、`plan_mode`（是否强制只读）。Skill 声明 `plan_mode: true` 时强制生效，命令行参数只能加严、不能放宽。内置两个示例：`incident_response`（允许请求重启，走人审）与 `status_check`（纯只读排障）。

### MCP 接入

启动时以子进程拉起 `mcp_server.py`，通过 stdio 交换单行 JSON-RPC 2.0 消息：`tools/list` 返回的工具被包装成本地 `ToolSpec`（`func` 是闭包，调用时转发 `tools/call`），因此远程工具与本地工具在注册表、权限、trace 三条路径上完全一致。退出前用 `finally` 关闭子进程，避免工具调用打到已退出的 server。

### Trace 审计

`traces/trace_{session_id}.jsonl` 追加写，一行一个事件，统一带 ISO 时间戳。工具调用事件用 `status` 区分四种结果：`success` / `tool_error` / `denied` / `parse_error`，并记录 `risk`、拒绝 `reason`、来源 `source`。因为文件名由 session_id 决定，暂停与恢复落在同一个文件里，一次业务生命周期就是可完整审计的一条线：

```
run_start → model_call → tool_call → hitl_pause
→ run_start(resumed) → tool_call → model_call → final_answer
```

结果与内容统一截断到 200 字符，避免大段日志把 trace 文件撑爆。

## 内置工具

| 工具                 | 来源 | risk     | 说明                             |
| -------------------- | ---- | -------- | ---------------------------------- |
| `get_service_status` | 本地 | readonly | 返回模拟的服务运行状态           |
| `get_recent_logs`    | 本地 | readonly | 返回模拟的最近日志               |
| `restart_service`    | 本地 | write    | 模拟重启，专门用来触发权限与人审 |
| `get_pod_events`     | MCP  | readonly | 由 MCP server 提供，返回 Pod 事件 |

## 测试

```bash
uv run --no-sync pytest
```

单测全程离线：`tests/helpers.py` 构造 OpenAI SDK 的消息对象与按脚本返回响应的 mock 客户端，`tests/conftest.py` 把工具注册表、trace 目录、`sessions.db` 各自隔离到临时路径，因此跑测试不会污染项目根的真实产物。覆盖 Agent 主循环、HITL 挂起与恢复、参数安全解析、权限判定。

## 文档

- [docs/architecture.md](docs/architecture.md) — 数据流主线、五条撑起设计的约定、模块职责分工、一次 HITL 的跨进程时序、离线验证方法与已知短板

## 已知局限

- 工具注册表是进程内单例 dict，没有会话级隔离，也没有并发安全。
- MCP 只有 stdio 传输，未实现完整生命周期与流式通知。
- HITL 审批只有一个终端 y/n，没有多用户、审批超时与拒绝后的重试策略。
- Skill 只吃 JSON，暂不支持行业标准的 `SKILL.md` 格式与按需渐进加载。
- 未处理上下文长度管理、失败重试与成本统计；会话状态没有过期清理；权限判定只看 risk 与 plan_mode，没有资源级授权。
