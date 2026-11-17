# 极简会话状态存储：HITL 挂起时把 Agent 循环上下文落盘，--resume 时读回
# 只依赖标准库 sqlite3 + json，不引入 SQLAlchemy 等 ORM
import json
import sqlite3
from pathlib import Path


# 与 runtime_trace 同策略：向上找 pyproject.toml 定位项目根，DB 文件放在根目录
def _project_root() -> Path:
    for p in Path(__file__).resolve().parents:
        if (p / "pyproject.toml").exists():
            return p
    return Path.cwd()


DB_PATH = _project_root() / "sessions.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        "session_id TEXT PRIMARY KEY, state_json TEXT, status TEXT)"
    )
    return conn


# 状态整体序列化为 JSON 存库，status 固定从 "pending" 起
def save_session(session_id: str, state: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, state_json, status) VALUES (?, ?, ?)",
            (session_id, json.dumps(state, ensure_ascii=False), "pending"),
        )


# 读回并反序列化状态；session 不存在返回 None
def load_session(session_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT state_json, status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    state = json.loads(row[0])
    state["status"] = row[1]
    return state


# 审批后更新状态（如 "approved" / "rejected"）
def update_status(session_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET status = ? WHERE session_id = ?",
            (status, session_id),
        )
