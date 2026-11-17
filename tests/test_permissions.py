import pytest
from permissions import check_permission

from tools import ToolSpec


def make_spec(risk: str) -> ToolSpec:
    return ToolSpec(
        name=f"tool_{risk}",
        description="dummy",
        parameters={"type": "object", "properties": {}},
        func=lambda **kwargs: "unused",
        risk=risk,
    )


class TestReadonly:
    @pytest.mark.parametrize("plan_mode", [False, True])
    def test_readonly_always_allowed(self, plan_mode):
        decision = check_permission(make_spec("readonly"), plan_mode=plan_mode)
        assert decision.action == "allow"
        assert decision.reason is None


class TestWriteInPlanMode:
    def test_write_denied_with_plan_mode_reason(self):
        decision = check_permission(make_spec("write"), plan_mode=True)
        assert decision.action == "deny"
        assert decision.reason == "plan_mode"


class TestWriteOutsidePlanMode:
    def test_write_denied_with_human_review_reason(self):
        decision = check_permission(make_spec("write"), plan_mode=False)
        assert decision.action == "deny"
        assert decision.reason == "needs_human_review"
