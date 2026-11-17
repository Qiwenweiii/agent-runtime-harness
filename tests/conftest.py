import pytest
import session_store

import runtime_trace
import tools


# 每个测试用独立的空工具注册表，避免测试间互相污染
@pytest.fixture
def fresh_registry(monkeypatch):
    registry = {}
    monkeypatch.setattr(tools, "_REGISTRY", registry)
    return registry


# trace 落盘目录指向 tmp_path，测试产生的 jsonl 不落到项目 traces/
@pytest.fixture
def isolated_trace(tmp_path, monkeypatch):
    trace_dir = tmp_path / "traces"
    monkeypatch.setattr(runtime_trace, "TRACE_DIR", trace_dir)
    monkeypatch.setattr(runtime_trace, "TRACE_FILE", None)
    return trace_dir


# sessions.db 指向临时目录，绝不触碰项目根的真实 DB 文件
@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    return db_path
