# 极简 MCP 客户端：以子进程方式启动 mcp_server.py，通过 stdio 收发 JSON-RPC 2.0
import json
import subprocess
import sys
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"


class MCPClient:
    def __init__(self):
        self._proc = subprocess.Popen(
            [sys.executable, str(SERVER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._next_id = 0

    def send_request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server 已退出，无法读取响应")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error']}")
        return response["result"]

    def list_tools(self) -> list[dict]:
        return self.send_request("tools/list", {})["tools"]

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self.send_request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return result["content"]

    def close(self):
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        self._proc.terminate()
        self._proc.wait()
