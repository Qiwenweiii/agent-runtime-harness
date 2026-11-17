import main as agent_main
import pytest
from helpers import (
    final_message,
    make_mock_client,
    read_trace_events,
    response_of,
    tool_call_message,
)

import runtime_trace
from tools import ToolSpec, register


def run(client, messages, plan_mode=False, max_steps=5):
    return agent_main.run_agent(
        client,
        "test-model",
        messages,
        [],
        plan_mode,
        max_steps,
        start_step=1,
        skill_config=None,
        session_id="test-session",
    )


@pytest.fixture
def add_tool(fresh_registry):
    def _impl(a, b):
        return str(a + b)

    register(
        ToolSpec(
            name="add",
            description="add two ints",
            parameters={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
            func=_impl,
            risk="readonly",
        )
    )
    return _impl


class TestToolCallThenAnswer:
    def test_full_flow(self, add_tool, fresh_registry, isolated_trace, capsys):
        client = make_mock_client(
            [
                response_of(tool_call_message("call_1", "add", '{"a": 1, "b": 2}')),
                response_of(final_message("答案是 3")),
            ]
        )
        messages = [{"role": "user", "content": "1+2 等于几？"}]

        assert run(client, messages) is None

        # 模型被调用两次：先工具、后回答；没有多余请求
        assert len(client.chat.completions.create.calls) == 2

        # assistant 工具消息被序列化成 dict 回写，随后跟 role=tool 的执行结果
        assistant = messages[1]
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["id"] == "call_1"
        tool_msg = messages[2]
        assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "3"}

        out = capsys.readouterr().out
        assert "答案是 3" in out


class TestPlanModeDeniesWrite:
    def test_write_tool_not_executed_and_reason_fed_back(
        self, fresh_registry, isolated_trace, mocker, capsys
    ):
        spy = mocker.Mock(return_value="不该被执行")
        register(
            ToolSpec(
                name="delete_file",
                description="delete a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                func=spy,
                risk="write",
            )
        )
        runtime_trace.init_trace("plan-mode-test")
        client = make_mock_client(
            [
                response_of(
                    tool_call_message("call_1", "delete_file", '{"path": "/tmp/x"}')
                ),
                response_of(final_message("好的，我不执行删除。")),
            ]
        )
        messages = [{"role": "user", "content": "删掉 /tmp/x"}]

        assert run(client, messages, plan_mode=True) is None

        # 工具被拒绝：func 完全没跑
        assert spy.call_count == 0

        # 回喂消息包含拒绝原因 plan_mode
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert tool_msgs[0]["content"] == "操作被拒绝: plan_mode"

        # trace 中留下 denied 事件
        denied = [
            e for e in read_trace_events() if e.get("status") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0]["reason"] == "plan_mode"
        assert denied[0]["tool_name"] == "delete_file"


class TestMaxStepsExceeded:
    def test_loop_truncates_at_max_steps(self, fresh_registry, isolated_trace, mocker, capsys):
        spy = mocker.Mock(return_value="ok")
        register(
            ToolSpec(
                name="probe",
                description="readonly probe",
                parameters={"type": "object", "properties": {}},
                func=spy,
                risk="readonly",
            )
        )
        runtime_trace.init_trace("max-steps-test")
        # 每一步都返回工具调用，永远走不完
        client = make_mock_client(
            [
                response_of(tool_call_message(f"call_{i}", "probe", "{}"))
                for i in range(3)
            ]
        )
        messages = [{"role": "user", "content": "不断调工具"}]

        assert run(client, messages, max_steps=2) is None

        # 只跑了 2 步：模型调 2 次、工具执行 2 次，第 3 次响应从未被消费
        assert len(client.chat.completions.create.calls) == 2
        assert spy.call_count == 2

        assert "max steps exceeded" in capsys.readouterr().out

        events = read_trace_events()
        truncations = [e for e in events if e.get("event") == "max_steps_exceeded"]
        assert len(truncations) == 1
        assert truncations[0]["step"] == 2
