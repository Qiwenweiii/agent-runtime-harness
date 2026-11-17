# 测试辅助：离线构造 OpenAI SDK 的消息对象与 mock 客户端，全程不发网络请求
from types import SimpleNamespace

from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

import runtime_trace


def tool_call_message(tool_id: str, name: str, arguments: str, content: str | None = None):
    tc = ChatCompletionMessageToolCall(
        id=tool_id,
        type="function",
        function=Function(name=name, arguments=arguments),
    )
    return ChatCompletionMessage(role="assistant", content=content, tool_calls=[tc])


def final_message(content: str):
    return ChatCompletionMessage(role="assistant", content=content)


def response_of(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# MagicMock 模拟 OpenAI 客户端：create 按顺序返回预置响应
def make_mock_client(responses):
    client = SimpleNamespace()
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if not responses:
            raise AssertionError("mock client received more model calls than scripted")
        return responses.pop(0)

    create.calls = calls
    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return client


def read_trace_events():
    import json

    assert runtime_trace.TRACE_FILE is not None, "test forgot to call init_trace"
    lines = runtime_trace.TRACE_FILE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]
