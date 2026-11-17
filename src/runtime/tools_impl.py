from tools import ToolSpec, register


# 工具一：查询服务状态
def get_service_status(service_name: str) -> str:
    print(f"[TOOL] get_service_status({service_name})")
    return f"service={service_name} status=running uptime=3d"


# 工具二：查询最近日志
def get_recent_logs(service_name: str, lines: int = 10) -> str:
    return "ERROR 2026-10-20 10:01 connection timeout"


# 工具三：重启服务，纯模拟，用于验证 write 风险的权限拦截
def restart_service(service_name: str) -> str:
    print(f"[TOOL] restart_service({service_name})")
    return f"service={service_name} restarted (simulated)"


def register_all() -> None:
    register(
        ToolSpec(
            name="get_service_status",
            description="Get the running status of a service",
            parameters={
                "type": "object",
                "properties": {"service_name": {"type": "string"}},
                "required": ["service_name"],
            },
            func=get_service_status,
            risk="readonly",
        )
    )

    register(
        ToolSpec(
            name="get_recent_logs",
            description="Get recent logs of a service",
            parameters={
                "type": "object",
                "properties": {
                    "service_name": {"type": "string"},
                    "lines": {"type": "integer", "default": 10},
                },
                "required": ["service_name"],
            },
            func=get_recent_logs,
            risk="readonly",
        )
    )

    register(
        ToolSpec(
            name="restart_service",
            description="Restart a service",
            parameters={
                "type": "object",
                "properties": {"service_name": {"type": "string"}},
                "required": ["service_name"],
            },
            func=restart_service,
            risk="write",
        )
    )
