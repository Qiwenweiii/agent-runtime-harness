import argparse
import json
import os
import sys
import uuid

from dotenv import load_dotenv
from openai import OpenAI
from parsing import parse_tool_args
from permissions import check_permission
from session_store import load_session, save_session, update_status
from skill import load_skill

from mcp_client import MCPClient
from runtime_trace import clip, init_trace, trace
from tools import ToolSpec, register, to_openai_tools
from tools import get as get_tool_spec
from tools_impl import register_all

load_dotenv()


# 把 MCP server 暴露的工具包装成 ToolSpec 注册进注册表，func 闭包转发到子进程
def register_mcp_tools(mcp_client: MCPClient) -> None:
    for tool in mcp_client.list_tools():

        def make_func(tool_name: str):
            def call(**arguments):
                return mcp_client.call_tool(tool_name, arguments)

            return call

        register(
            ToolSpec(
                name=tool["name"],
                description=tool["description"],
                parameters=tool["inputSchema"],
                func=make_func(tool["name"]),
                risk="readonly",
                source="mcp",
            )
        )


class PausedSession:
    def __init__(self, session_id, step, messages, pending_tool_call, tool_name):
        self.session_id = session_id
        self.step = step
        self.messages = messages
        self.pending_tool_call = pending_tool_call
        self.tool_name = tool_name


# 执行单个 tool call：解析参数 -> 权限 -> 执行 -> trace + 回写 messages
# 返回 "pause" 表示已挂起等待人审（调用方应立即落盘退出），其余情况循环继续
def execute_tool_call(tc, step, spec, args_parsed, plan_mode, messages, parse_err=None) -> str:
    name = tc.function.name
    if parse_err:
        trace(
            {
                "event": "tool_call",
                "step": step,
                "tool_name": name,
                "args": None,
                "status": "parse_error",
                "result_summary": None,
                "error": parse_err,
                "risk": None,
                "reason": None,
                "source": None,
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": f"参数解析失败: {parse_err}",
            }
        )
        return "continue"

    if spec is None:
        error = f"unknown tool {name}"
        trace(
            {
                "event": "tool_call",
                "step": step,
                "tool_name": name,
                "args": args_parsed,
                "status": "tool_error",
                "result_summary": None,
                "error": error,
                "risk": None,
                "reason": None,
                "source": None,
            }
        )
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": error})
        return "continue"

    # 权限检查在 try 之外：拒绝时不执行工具，把原因回喂给模型
    # 本地工具与 MCP 工具走同一套 check_permission
    decision = check_permission(spec, plan_mode)
    if decision.action == "deny":
        # v6：非 plan mode 的 write 工具不再直接 deny，改为 HITL 挂起
        if decision.reason == "needs_human_review":
            return "pause"
        trace(
            {
                "event": "tool_call",
                "step": step,
                "tool_name": name,
                "args": args_parsed,
                "status": "denied",
                "result_summary": None,
                "error": None,
                "risk": spec.risk,
                "reason": decision.reason,
                "source": spec.source,
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": tc.id, "content": f"操作被拒绝: {decision.reason}"}
        )
        return "continue"

    try:
        # 执行工具，参数不匹配或工具内部异常都归为 tool_error
        result = spec.func(**args_parsed)
    except Exception as exc:
        result = None
        error = f"{type(exc).__name__}: {exc}"
        trace(
            {
                "event": "tool_call",
                "step": step,
                "tool_name": name,
                "args": args_parsed,
                "status": "tool_error",
                "result_summary": None,
                "error": error,
                "risk": spec.risk,
                "reason": None,
                "source": spec.source,
            }
        )
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": error})
        return "continue"

    trace(
        {
            "event": "tool_call",
            "step": step,
            "tool_name": name,
            "args": args_parsed,
            "status": "success",
            "result_summary": clip(result),
            "error": None,
            "risk": spec.risk,
            "reason": None,
            "source": spec.source,
        }
    )
    # 以 role=tool 追加执行结果
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return "continue"


# Agent 主循环：正常跑完返回 None；命中需人审的 write 工具返回 PausedSession
def run_agent(client, model, messages, openai_tools, plan_mode, max_steps, start_step, skill_config, session_id):
    for step in range(start_step, max_steps + 1):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=openai_tools
        )
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        print(f"--- step {step} ---")

        # 每轮调用模型先记一条，has_tool_calls 区分「去调工具」和「直接回答」
        trace(
            {
                "event": "model_call",
                "step": step,
                "has_tool_calls": bool(tool_calls),
                "content_summary": clip(msg.content or ""),
            }
        )

        if not tool_calls:
            # 无工具调用：打印最终回答并结束
            trace(
                {
                    "event": "final_answer",
                    "step": step,
                    "content_summary": clip(msg.content or ""),
                }
            )
            print(msg.content)
            return None

        # assistant 消息统一转成 dict，便于 HITL 时直接 JSON 序列化
        messages.append(json.loads(msg.model_dump_json()))

        for tc in tool_calls:
            args_parsed, parse_err = parse_tool_args(tc.function.arguments)
            spec = get_tool_spec(tc.function.name)
            if execute_tool_call(tc, step, spec, args_parsed, plan_mode, messages, parse_err) == "pause":
                pending = {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": args_parsed,
                    "step": step,
                    # 剩余未执行的 tool_calls 也要在恢复后回喂，否则下一条请求不合法
                    "remaining": [
                        {
                            "id": o.id,
                            "name": o.function.name,
                            "arguments": o.function.arguments,
                        }
                        for o in tool_calls[tool_calls.index(tc) + 1 :]
                    ],
                }
                state = {
                    "messages": messages,
                    "step": step,
                    "pending_tool_call": pending,
                    "skill_config": {
                        "name": skill_config.name,
                        "system_prompt": skill_config.system_prompt,
                        "allowed_tools": skill_config.allowed_tools,
                        "plan_mode": skill_config.plan_mode,
                    },
                    "plan_mode": plan_mode,
                    "max_steps": max_steps,
                }
                save_session(session_id, state)
                trace(
                    {
                        "event": "hitl_pause",
                        "session_id": session_id,
                        "tool_name": tc.function.name,
                    }
                )
                print(
                    f"需要人工审批，会话已挂起。请运行 --resume {session_id} 进行审批。"
                )
                return PausedSession(session_id, step, messages, pending, tc.function.name)

    # 超过最大步数：step 为循环结束后的实际步号（即上限 max_steps）
    trace({"event": "max_steps_exceeded", "step": step})
    print("max steps exceeded")
    return None


# --resume：读回挂起会话，终端人审后执行/拒绝 pending tool call，再接回主循环
def resume_session(session_id: str) -> None:
    state = load_session(session_id)
    if state is None:
        print(f"错误：会话不存在: {session_id}")
        sys.exit(1)
    if state.get("status") != "pending":
        print(f"错误：会话状态不是 pending（当前 {state.get('status')}），无法恢复")
        sys.exit(1)

    skill_cfg = state["skill_config"]
    skill_config = load_skill(skill_cfg["name"])
    plan_mode = state["plan_mode"]
    max_steps = state["max_steps"]
    messages = state["messages"]
    pending = state["pending_tool_call"]
    step = pending["step"]

    mcp_client = MCPClient()
    try:
        register_all()
        register_mcp_tools(mcp_client)
        openai_tools = to_openai_tools(allowed_tools=skill_config.allowed_tools)

        trace_file = init_trace(session_id)
        print("trace file:", trace_file)
        trace(
            {
                "event": "run_start",
                "session_id": session_id,
                "resumed": True,
                "model": os.environ["OPENAI_MODEL"],
                "skill": skill_config.name,
                "max_steps": max_steps,
                "plan_mode": plan_mode,
                "trace_file": str(trace_file),
            }
        )

        client = OpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.getenv("OPENAI_BASE_URL"),
            timeout=30,
        )
        model = os.environ["OPENAI_MODEL"]

        print(f"待审批操作：[{pending['name']}] 参数：{pending['args']}")
        answer = input("是否批准执行？(y) 批准 / (n) 拒绝: ").strip().lower()

        if answer == "y":
            update_status(session_id, "approved")
            spec = get_tool_spec(pending["name"])
            if spec is None:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": pending["id"],
                        "content": f"unknown tool {pending['name']}",
                    }
                )
            else:
                try:
                    result = spec.func(**pending["args"])
                    trace(
                        {
                            "event": "tool_call",
                            "step": step,
                            "tool_name": pending["name"],
                            "args": pending["args"],
                            "status": "success",
                            "result_summary": clip(result),
                            "error": None,
                            "risk": spec.risk,
                            "reason": None,
                            "source": spec.source,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": pending["id"], "content": str(result)}
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    trace(
                        {
                            "event": "tool_call",
                            "step": step,
                            "tool_name": pending["name"],
                            "args": pending["args"],
                            "status": "tool_error",
                            "result_summary": None,
                            "error": error,
                            "risk": spec.risk,
                            "reason": None,
                            "source": spec.source,
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": pending["id"], "content": error}
                    )
        else:
            update_status(session_id, "rejected")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending["id"],
                    "content": "用户拒绝了该操作。",
                }
            )

        # 挂起时未执行的其余 tool_calls 也要补上结果，保持 assistant/tool 配对合法
        for rest in pending.get("remaining", []):
            content = "操作被拒绝: 会话恢复中不执行挂起时未处理的调用"
            if answer == "y":
                rest_args, rest_err = parse_tool_args(rest.get("arguments") or "{}")
                rest_spec = get_tool_spec(rest["name"])
                decision = None if rest_err else check_permission(rest_spec, plan_mode) if rest_spec else None
                if rest_spec is not None and not rest_err and decision.action == "allow":
                    try:
                        content = str(rest_spec.func(**rest_args))
                        trace(
                            {
                                "event": "tool_call",
                                "step": step,
                                "tool_name": rest["name"],
                                "args": rest_args,
                                "status": "success",
                                "result_summary": clip(content),
                                "error": None,
                                "risk": rest_spec.risk,
                                "reason": None,
                                "source": rest_spec.source,
                            }
                        )
                    except Exception as exc:
                        content = f"{type(exc).__name__}: {exc}"
                        trace(
                            {
                                "event": "tool_call",
                                "step": step,
                                "tool_name": rest["name"],
                                "args": rest_args,
                                "status": "tool_error",
                                "result_summary": None,
                                "error": content,
                                "risk": rest_spec.risk,
                                "reason": None,
                                "source": rest_spec.source,
                            }
                        )
                else:
                    trace(
                        {
                            "event": "tool_call",
                            "step": step,
                            "tool_name": rest["name"],
                            "args": None,
                            "status": "denied",
                            "result_summary": None,
                            "error": None,
                            "risk": rest_spec.risk if rest_spec else None,
                            "reason": "hitl_resume_skipped",
                            "source": rest_spec.source if rest_spec else None,
                        }
                    )
            messages.append(
                {"role": "tool", "tool_call_id": rest["id"], "content": content}
            )

        # 带着恢复的 step 继续循环（从挂起步的下一步起），续写同一 trace 文件
        run_agent(
            client,
            model,
            messages,
            openai_tools,
            plan_mode,
            max_steps,
            start_step=step + 1,
            skill_config=skill_config,
            session_id=session_id,
        )
    finally:
        mcp_client.close()


def main():
    parser = argparse.ArgumentParser(description="极简 Agent Runtime v7")
    parser.add_argument(
        "--plan-mode",
        action="store_true",
        default=False,
        help="计划模式：拒绝所有 write 风险工具",
    )
    parser.add_argument(
        "--max-steps", type=int, default=5, help="Agent 循环最大步数，超过则直接结束"
    )
    parser.add_argument(
        "--skill",
        type=str,
        default="incident_response",
        help="技能名称，对应 src/runtime/skills/{skill}.json",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        metavar="SESSION_ID",
        help="恢复处于 pending 状态的 HITL 会话并进行人工审批",
    )
    args = parser.parse_args()

    # 本次业务生命周期的唯一标识：正常启动新生成，恢复时沿用 --resume 传入的 id
    if args.resume:
        resume_session(args.resume)
        return

    session_id = str(uuid.uuid4())

    skill_config = load_skill(args.skill)
    # Skill 声明 plan_mode 时强制生效，命令行参数只能加严不能放宽
    if skill_config.plan_mode:
        args.plan_mode = True

    register_all()  # 注册所有本地工具

    # MCP 子进程必须最后关闭，否则工具调用会打到已退出的 server
    mcp_client = MCPClient()
    try:
        register_mcp_tools(mcp_client)

        # trace 按 session_id 聚合：暂停/恢复续写同一个 trace_{session_id}.jsonl
        trace_file = init_trace(session_id)
        print("trace file:", trace_file)

        # 从环境变量初始化客户端
        client = OpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.getenv("OPENAI_BASE_URL"),
            timeout=30,
        )
        model = os.environ["OPENAI_MODEL"]

        trace(
            {
                "event": "run_start",
                "session_id": session_id,
                "model": model,
                "skill": skill_config.name,
                "max_steps": args.max_steps,
                "plan_mode": args.plan_mode,
                "trace_file": str(trace_file),
            }
        )

        # 初始化对话消息：system（来自 Skill）+ user
        messages = [
            {"role": "system", "content": skill_config.system_prompt},
            {"role": "user", "content": input("> ")},
        ]

        # 按 Skill 白名单过滤模型可见工具
        openai_tools = to_openai_tools(allowed_tools=skill_config.allowed_tools)

        run_agent(
            client,
            model,
            messages,
            openai_tools,
            args.plan_mode,
            args.max_steps,
            start_step=1,
            skill_config=skill_config,
            session_id=session_id,
        )
    finally:
        mcp_client.close()


if __name__ == "__main__":
    main()
