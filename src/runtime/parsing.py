import json


# 解析模型给出的参数串：失败只返回 (None, 错误信息)，绝不向上抛
def parse_tool_args(raw: str) -> tuple[dict | None, str | None]:
    # print(f"[DEBUG] parse_tool_args({raw})")

    try:
        parsed = json.loads(raw or "{}")
    except Exception as exc:
        return None, f"参数不是合法 JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, f"参数应为 JSON object，实际得到 {type(parsed).__name__}"
    return parsed, None
