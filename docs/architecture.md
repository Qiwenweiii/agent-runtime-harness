# agent-runtime 架构说明

本文档讲三件事：一次请求在代码里怎么流动、每个模块为什么存在、以及 HITL 跨进程恢复的设计取舍。逐行细节请直接读 `src/runtime/`，代码量小到可以通读。

## 一、数据流主线

```
CLI 参数 / Skill 装配
   ↓
run_agent  ── 调模型 ──→ 有 tool_calls?  ──否──→ final_answer，结束
   │                        │是
   │                        ↓
   │                  parse_tool_args（永不抛异常）
   │                        ↓
   │                  tools.get(name) → spec
   │                        ↓
   │                  check_permission(spec, plan_mode)
   │                        ├ allow ────→ 执行工具 → trace(success/tool_error) → 回喂
   │                        ├ deny(plan_mode) ──→ trace(denied) → 回喂拒绝原因
   │                        └ deny(needs_human_review) → 快照落库 → trace(hitl_pause) → 进程退出
   ↓
新进程 --resume：读快照 → 终端 y/n → 执行/拒绝 → 补账 remaining → 从 step+1 接回 run_agent
```

全程只有两类副作用：往 `messages` 追加消息，往 trace 追加一行 JSON。

## 二、撑起整个设计的五条约定

1. **状态必须可序列化**。assistant 消息用 `json.loads(msg.model_dump_json())` 转成纯 dict 再入栈，整个 `messages` 才能 `json.dumps` 进 sqlite。这是「落库 → 退出 → 新进程恢复」的前提，也是很多框架难以做真 HITL 的原因。
2. **挂起决策权上移**。`execute_tool_call` 认出「这个调用要人审」后什么都不写、直接 `return "pause"`——因为快照需要整个循环的上下文（messages、step、同轮剩余调用），单工具层拿不到。
3. **assistant/tool 配对是硬约束**。挂起点之前已执行的结果、被挂起的那个调用、同轮排在它后面还没跑的 `remaining`，恢复后三者都必须各有对应的 `role="tool"` 消息，否则下一条请求直接被 API 拒绝。这就是快照里要存 `remaining` 的唯一理由。
4. **拒绝原因用返回值传递，不用异常**。`PermissionDecision.reason`（`plan_mode` / `needs_human_review`）既是 trace 字段，也是分岔信号。语义编码进结构化返回值，才让 v3「write 一律 deny」升级到 v6「部分转人审」时不必改判定函数的签名。
5. **白名单拦在可见性层**。`to_openai_tools(allowed_tools)` 过滤的是「模型能看见什么」，幻觉出名单外的工具会走 `unknown tool` 分支。可见性过滤早于执行拦截，也更省 token。

## 三、模块职责

| 模块                | 为什么存在                                                                                   |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| `tools.py`          | 全局唯一工具漏斗。重名即抛错（静默覆盖等于权限体系的洞）；`risk` 是数据不是逻辑，加工具不改权限代码 |
| `tools_impl.py`     | 三个演示工具，其中 `restart_service` 标 `write`，专门用来触发拦截与人审                       |
| `permissions.py`    | 无副作用的判定纯函数，输入只有 `risk` 与 `plan_mode`                                         |
| `parsing.py`        | 边界守卫：模型输出是不可信外部输入，解析失败是业务分支而非程序错误，故返回二元组且吞掉一切异常  |
| `skill.py`/`skills` | 场景装配。Skill 声明的 `plan_mode` 强制生效，CLI 参数只能加严不能放宽——场景约束高于操作者偏好   |
| `mcp_client.py`     | 以子进程 stdio 收发单行 JSON-RPC 2.0；`tools/list` 的工具包装成本地 `ToolSpec`（闭包转发 `tools/call`） |
| `mcp_server.py`     | 演示用对端，暴露 `get_pod_events`                                                            |
| `session_store.py`  | 三列一张表（`session_id / state_json / status`）。快照永远整体读写，拆表只增加一致性负担      |
| `runtime_trace.py`  | jsonl 事件日志。trace 记过程，sessions 表记结论，排查时联合看                                |

MCP 工具与本地工具在注册表、权限判定、trace 三条路径上完全同构——这是 v5 想验证的结论：远程接入不该成为绕过治理的后门。

## 四、一次 HITL 的跨进程时序

```
进程 A（启动）                              进程 B（--resume）
──────────────                              ──────────────────
init_trace(sid) → trace_{sid}.jsonl
run_start {session_id}
step1 model_call → assistant(tool_calls) 入栈
readonly 工具 → 执行 + 回喂
write 工具  → deny(needs_human_review)
save_session(sid, state) ──── sqlite ────▶  load_session(sid) 校验 status=pending
trace hitl_pause                             init_trace(sid) 续写同一文件
打印 --resume {sid}                           run_start {resumed: true}
finally 关 MCP 子进程，进程退出               打印待审批操作 + input(y/n)
                                              y → status=approved，执行工具（不再走权限）
                                              n → status=rejected，回喂拒绝文案
                                              补账 remaining（保证消息配对）
                                              run_agent(start_step=step+1)
                                              final_answer → 终态
```

恢复时重建执行环境的顺序照抄启动路径：`register_all()` → `register_mcp_tools()` → `to_openai_tools(白名单)`；`skill_config` 从磁盘 `load_skill(name)` 重建，文件是唯一事实源。审批通过后绕过 `check_permission` 是刻意的——人工决策已完成，再判一次会把同一调用再挂起一次形成死循环；但绕过权限不绕过审计，trace 事件结构与自动执行完全一致。

## 五、验证方法

单测全程离线，不花 API 费用：`tests/helpers.py` 用脚本化响应的 mock client 和 `SimpleNamespace` 替掉 OpenAI SDK 与 MCP 子进程，完整跑真实逻辑；`tests/conftest.py` 把注册表、trace 目录、`sessions.db` 各自隔离到临时路径。

断言对象是状态机与协议，不是模型回复文本：DB 的 status 流转、messages 的 assistant/tool 配对、trace 事件序列逐元素相等。测试要有判别性——HITL 用例在加入挂起逻辑之前的行为上必须先跑红（期望 pause 却拿到 denied），在新实现上才跑绿。

## 六、已知短板

1. 并发 resume 无 CAS：两个进程可同时通过 pending 校验，工具被双重执行。
2. 恢复不校验环境漂移：挂起期间 skill 文件或 MCP 工具集发生变更，快照与重建环境会不一致。
3. 会话无过期清理：永不审批的记录以 pending 永久留存。
4. MCP 未走 `initialize` 握手，且远程工具一律标 `readonly`——接真实 server 前必须补，否则远程写工具会漏判权限。
5. 审批粒度是整个调用，没有参数级授权（例如限定可重启的服务名单）。
6. `skill_config` 在快照与磁盘各存一份，事实源存在冗余。
7. 工具注册表是进程内单例 dict，无会话级隔离与并发安全；stdio 一问一答也就不支持并发请求。
