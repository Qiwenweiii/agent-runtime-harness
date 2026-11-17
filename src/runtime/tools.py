from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

# risk 只做声明，v2 不消费它做任何拦截
Risk = Literal["readonly", "write"]
ALLOWED_RISKS = ("readonly", "write")


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema，原样传给 SDK
    func: Callable
    risk: Risk
    source: Literal["local", "mcp"] = "local"


# 注册表：name -> ToolSpec
_REGISTRY: dict[str, ToolSpec] = {}


# 注册一个工具，重名直接抛错，避免静默覆盖
def register(spec: ToolSpec) -> None:
    if spec.risk not in ALLOWED_RISKS:
        raise ValueError(f"非法 risk: {spec.risk!r}，只允许 {ALLOWED_RISKS}")
    if spec.name in _REGISTRY:
        raise ValueError(f"工具重名，已注册: {spec.name}")
    _REGISTRY[spec.name] = spec


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    return list(_REGISTRY.values())


# 转成 OpenAI SDK 的 tools 参数格式
# allowed_tools 非空时只暴露白名单内的工具（Skill 装配用）
def to_openai_tools(allowed_tools: list[str] | None = None) -> list[dict]:
    specs = _REGISTRY.values()
    if allowed_tools:
        specs = [spec for spec in specs if spec.name in allowed_tools]
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]
