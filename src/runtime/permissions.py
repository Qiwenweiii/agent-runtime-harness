from dataclasses import dataclass
from typing import Literal

from tools import ToolSpec


@dataclass
class PermissionDecision:
    action: Literal["allow", "deny"]
    reason: str | None  # allow 时为 None；deny 时为 "plan_mode" 或 "needs_human_review"


# 按 risk 与 plan_mode 判定工具是否可执行；v3 里 write 一律拒绝，v4 再替换为人审暂停
def check_permission(spec: ToolSpec, plan_mode: bool) -> PermissionDecision:
    if spec.risk == "readonly":
        return PermissionDecision(action="allow", reason=None)
    if plan_mode:
        return PermissionDecision(action="deny", reason="plan_mode")
    return PermissionDecision(action="deny", reason="needs_human_review")
