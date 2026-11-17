import datetime
import json
import os
from pathlib import Path


def _project_root() -> Path:
    # 从当前文件向上找，第一个包含 pyproject.toml 的目录就是项目根
    for p in Path(__file__).resolve().parents:
        if (p / "pyproject.toml").exists():
            return p
    # 兜底：找不到就用 cwd，至少不崩
    return Path.cwd()


TRACE_DIR = _project_root() / "traces"
TRACE_FILE = None  # 本次运行的 trace 文件路径，init_trace() 之后才有值


# 结果/内容摘要统一截断到 200 字符
def clip(value, limit=200):
    return str(value)[:limit]


# 启动时创建 traces 目录并生成本次运行的 jsonl 文件，返回路径供终端打印
# 传入 session_id 时按会话聚合为 trace_{session_id}.jsonl，HITL 暂停/恢复续写同一文件；
# 未传入则沿用时间戳命名。文件一律 "a" 模式打开，恢复时不覆盖已有日志
def init_trace(session_id: str | None = None):
    global TRACE_FILE
    os.makedirs(TRACE_DIR, exist_ok=True)
    if session_id:
        TRACE_FILE = TRACE_DIR / f"trace_{session_id}.jsonl"
    else:
        # 精确到毫秒：同一秒内启动多次也要各自一个文件
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        TRACE_FILE = TRACE_DIR / f"{stamp}.jsonl"
    open(TRACE_FILE, "a", encoding="utf-8").close()
    return TRACE_FILE


# 事件落盘：一行一个 JSON，统一补齐 ISO timestamp
def trace(event: dict) -> None:
    if TRACE_FILE is None:
        return
    record = {"timestamp": datetime.datetime.now().isoformat(), **event}
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
