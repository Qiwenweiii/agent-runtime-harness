import json
from types import SimpleNamespace

import main as agent_main
import pytest
import session_store
from helpers import final_message, make_mock_client, response_of, tool_call_message
from skill import SkillConfig

import tools_impl

PENDING_ID = "call_pending_1"
SESSION_ID = "sess-hitl-1"


def build_state() -> dict:
    assistant = json.loads(
        tool_call_message(
            PENDING_ID, "restart_service", '{"service_name": "api"}'
        ).model_dump_json()
    )
    return {
        "messages": [
            {"role": "system", "content": "你是运维助手"},
            {"role": "user", "content": "重启 api 服务"},
            assistant,
        ],
        "step": 1,
        "pending_tool_call": {
            "id": PENDING_ID,
            "name": "restart_service",
            "args": {"service_name": "api"},
            "step": 1,
            "remaining": [],
        },
        "skill_config": {
            "name": "test_skill",
            "system_prompt": "你是运维助手",
            "allowed_tools": None,
            "plan_mode": False,
        },
        "plan_mode": False,
        "max_steps": 5,
    }


# resume_session 会拉起 MCP 子进程并构造真实 OpenAI 客户端，测试里全部替换掉
@pytest.fixture
def resume_env(fresh_registry, isolated_trace, isolated_db, monkeypatch, mocker):
    class FakeMCPClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_tools(self):
            return []

        def call_tool(self, name, arguments):
            raise AssertionError("测试不应该走到 MCP 工具调用")

        def close(self):
            pass

    monkeypatch.setattr(agent_main, "MCPClient", FakeMCPClient)
    monkeypatch.setattr(
        agent_main,
        "load_skill",
        lambda name: SkillConfig(
            name=name, system_prompt="你是运维助手", allowed_tools=None, plan_mode=False
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    client = make_mock_client([response_of(final_message("恢复完成"))])
    mocker.patch.object(agent_main, "OpenAI", return_value=client)
    return SimpleNamespace(client=client)


class TestSaveLoadSession:
    def test_roundtrip(self, isolated_db):
        state = {"messages": [{"role": "user", "content": "重启 api"}], "step": 3}
        session_store.save_session(SESSION_ID, state)

        loaded = session_store.load_session(SESSION_ID)
        assert loaded is not None
        assert loaded["messages"] == state["messages"]
        assert loaded["step"] == 3
        assert loaded["status"] == "pending"

    def test_unicode_preserved(self, isolated_db):
        session_store.save_session(SESSION_ID, {"note": "中文内容"})
        assert session_store.load_session(SESSION_ID)["note"] == "中文内容"

    def test_save_overwrites_same_id(self, isolated_db):
        session_store.save_session(SESSION_ID, {"v": 1})
        session_store.save_session(SESSION_ID, {"v": 2})
        assert session_store.load_session(SESSION_ID)["v"] == 2

    def test_missing_session_returns_none(self, isolated_db):
        assert session_store.load_session("no-such-session") is None

    def test_update_status(self, isolated_db):
        session_store.save_session(SESSION_ID, {"v": 1})
        session_store.update_status(SESSION_ID, "approved")
        assert session_store.load_session(SESSION_ID)["status"] == "approved"

    def test_db_file_is_isolated_to_tmp(self, isolated_db, tmp_path):
        session_store.save_session(SESSION_ID, {"v": 1})
        assert isolated_db.exists()
        assert isolated_db.parent == tmp_path


class TestResumeApprove:
    def test_y_executes_tool_and_continues(self, resume_env, isolated_db, mocker):
        spy = mocker.patch.object(tools_impl, "restart_service", return_value="api restarted (simulated)")
        session_store.save_session(SESSION_ID, build_state())
        mocker.patch("builtins.input", lambda *args: "y")

        agent_main.resume_session(SESSION_ID)

        # 批准后 pending 工具真实执行了一次
        spy.assert_called_once_with(service_name="api")

        # 审批后回喂工具结果，并带着它继续问模型
        assert resume_env.client.chat.completions.create.calls, "恢复后应继续主循环"
        sent = resume_env.client.chat.completions.create.calls[0]["messages"]
        tool_msg = next(m for m in sent if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == PENDING_ID
        assert "restarted" in tool_msg["content"]

        # 会话状态被更新为 approved
        assert session_store.load_session(SESSION_ID)["status"] == "approved"


class TestResumeReject:
    def test_n_skips_tool_and_feeds_rejection(self, resume_env, isolated_db, mocker):
        spy = mocker.patch.object(tools_impl, "restart_service", return_value="不该被执行")
        session_store.save_session(SESSION_ID, build_state())
        mocker.patch("builtins.input", lambda *args: "n")

        agent_main.resume_session(SESSION_ID)

        # 拒绝后工具没有被执行
        assert spy.call_count == 0

        # 拒绝原因回喂给模型，主循环继续
        assert resume_env.client.chat.completions.create.calls
        sent = resume_env.client.chat.completions.create.calls[0]["messages"]
        tool_msg = next(m for m in sent if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == PENDING_ID
        assert tool_msg["content"] == "用户拒绝了该操作。"

        # 会话状态被更新为 rejected
        assert session_store.load_session(SESSION_ID)["status"] == "rejected"
