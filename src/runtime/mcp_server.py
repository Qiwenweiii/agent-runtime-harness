# 极简 MCP Server：通过 stdio 与父进程交换 JSON-RPC 2.0 消息，一行一个 JSON
import json
import sys

TOOLS = [
    {
        "name": "get_pod_events",
        "description": "获取 K8s Pod 的事件日志",
        "inputSchema": {
            "type": "object",
            "properties": {"pod_name": {"type": "string"}},
            "required": ["pod_name"],
        },
    }
]


def handle_result(method: str, params: dict) -> dict:
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "get_pod_events":
            raise ValueError(f"unknown tool: {name}")
        pod = arguments.get("pod_name", "")
        return {"content": f"pod={pod} events: BackOff restarting failed container"}
    raise ValueError(f"method not found: {method}")


def main():
    while True:
        line = sys.stdin.readline()
        if not line:  # 父进程关闭 stdin，退出
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": handle_result(req.get("method", ""), req.get("params") or {}),
            }
        except json.JSONDecodeError as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }
        except Exception as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
