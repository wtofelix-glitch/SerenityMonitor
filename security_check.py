"""
Serenity 写接口安全体检。

目标是验证 dashboard 与 bridge 的执行边界是否实际生效，而不是只看
环境变量是否存在。本模块不修改数据库、不触发交易执行。
"""
from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SecurityCheck:
    id: str
    label: str
    status: str
    weight: int
    detail: str
    remediation: str = ""

    @property
    def passed_weight(self) -> int:
        return self.weight if self.status == "pass" else 0


def _token_present(*names: str) -> bool:
    return any(bool(os.getenv(name)) for name in names)


def _status(score: int) -> str:
    if score >= 80:
        return "good"
    if score >= 55:
        return "watch"
    return "risk"


def _status_text(status: str) -> str:
    return {
        "pass": "通过",
        "warn": "提醒",
        "fail": "失败",
        "good": "稳健",
        "watch": "待加强",
        "risk": "需修缮",
    }.get(status, status)


def _dashboard_public_write_guard() -> SecurityCheck:
    """主动验证公网 Host 且无 token 时，所有写接口都被拒绝。"""
    try:
        from monitoring_dashboard import app

        client = app.test_client()
        endpoints = [
            ("/api/trades", {"code": "000988", "action": "buy", "price": 1}),
            ("/api/config", {"code": "000988", "stop_loss": 1}),
            ("/api/execute", {}),
            ("/api/hermes/trade", {"code": "600141", "action": "buy", "price": 1, "quantity": 100}),
            ("/api/hermes/balance", {"cash": 1000, "positions": []}),
        ]
        failures = []
        for path, payload in endpoints:
            resp = client.post(
                path,
                json=payload,
                headers={"Host": "serenity.example.com"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            if resp.status_code != 401:
                failures.append(f"{path} -> {resp.status_code}")
        if failures:
            return SecurityCheck(
                id="dashboard_public_write_guard",
                label="Dashboard 公网写接口闸门",
                status="fail",
                weight=25,
                detail="; ".join(failures),
                remediation="给所有 dashboard 写接口加 require_write_auth。",
            )
        return SecurityCheck(
            id="dashboard_public_write_guard",
            label="Dashboard 公网写接口闸门",
            status="pass",
            weight=25,
            detail="/api/trades、/api/config、/api/execute、/api/hermes/* 无 token 公网 Host 均返回 401",
        )
    except Exception as e:
        return SecurityCheck(
            id="dashboard_public_write_guard",
            label="Dashboard 公网写接口闸门",
            status="fail",
            weight=25,
            detail=f"无法主动验证: {e}",
            remediation="修复 dashboard 导入/测试客户端后重新运行 security-check。",
        )


def _dashboard_token_check() -> SecurityCheck:
    ok = _token_present("SERENITY_DASHBOARD_TOKEN", "SERENITY_API_TOKEN")
    return SecurityCheck(
        id="dashboard_token",
        label="Dashboard 写 token",
        status="pass" if ok else "warn",
        weight=10,
        detail="SERENITY_DASHBOARD_TOKEN/SERENITY_API_TOKEN 已配置" if ok else "未配置；当前依赖本地/LAN白名单与公网 Host 拦截",
        remediation="公网或隧道访问前设置 SERENITY_DASHBOARD_TOKEN。" if not ok else "",
    )


def _bridge_token_check() -> SecurityCheck:
    ok = _token_present("SERENITY_BRIDGE_TOKEN", "SERENITY_API_TOKEN")
    return SecurityCheck(
        id="bridge_token",
        label="Bridge 写 token",
        status="pass" if ok else "warn",
        weight=10,
        detail="SERENITY_BRIDGE_TOKEN/SERENITY_API_TOKEN 已配置" if ok else "未配置；当前依赖本地/LAN白名单",
        remediation="n8n/远程调用前设置 SERENITY_BRIDGE_TOKEN。" if not ok else "",
    )


def _bridge_auth_guard() -> SecurityCheck:
    try:
        import serenity_bridge_server as bridge

        do_post = inspect.getsource(bridge.Handler.do_POST)
        required = [
            'path == "/api/send"',
            "path.startswith(\"/api/serenity/\")",
            "_require_auth()",
        ]
        missing = [item for item in required if item not in do_post]
        if missing:
            return SecurityCheck(
                id="bridge_auth_guard",
                label="Bridge 写任务 auth 闸门",
                status="fail",
                weight=25,
                detail=f"do_POST 缺少: {', '.join(missing)}",
                remediation="/api/send 与 /api/serenity/* 必须先通过 _require_auth()。",
            )
        return SecurityCheck(
            id="bridge_auth_guard",
            label="Bridge 写任务 auth 闸门",
            status="pass",
            weight=25,
            detail="/api/send 与 /api/serenity/* 在 do_POST 中均先检查 _require_auth()",
        )
    except Exception as e:
        return SecurityCheck(
            id="bridge_auth_guard",
            label="Bridge 写任务 auth 闸门",
            status="fail",
            weight=25,
            detail=f"无法检查 bridge 源码: {e}",
            remediation="修复 bridge 模块导入后重新运行 security-check。",
        )


def _bridge_get_tasks_disabled() -> SecurityCheck:
    try:
        import serenity_bridge_server as bridge

        do_get = inspect.getsource(bridge.Handler.do_GET)
        if "path.startswith(\"/api/serenity/\")" in do_get and "405" in do_get:
            return SecurityCheck(
                id="bridge_get_tasks_disabled",
                label="Bridge GET 禁止执行任务",
                status="pass",
                weight=15,
                detail="GET /api/serenity/* 返回 405，只允许 POST 任务执行",
            )
        return SecurityCheck(
            id="bridge_get_tasks_disabled",
            label="Bridge GET 禁止执行任务",
            status="fail",
            weight=15,
            detail="未确认 GET /api/serenity/* 被 405 拦截",
            remediation="禁止 GET 触发任务执行，避免链接预取/爬虫误触发。",
        )
    except Exception as e:
        return SecurityCheck(
            id="bridge_get_tasks_disabled",
            label="Bridge GET 禁止执行任务",
            status="fail",
            weight=15,
            detail=f"无法检查 bridge GET 策略: {e}",
            remediation="修复 bridge 模块导入后重新运行 security-check。",
        )


def _bridge_cors_check() -> SecurityCheck:
    try:
        import serenity_bridge_server as bridge

        source = inspect.getsource(bridge.Handler)
        if "Access-Control-Allow-Origin" in source and "*" in source:
            return SecurityCheck(
                id="bridge_cors",
                label="Bridge CORS 策略",
                status="fail",
                weight=10,
                detail="Bridge 响应里存在 Access-Control-Allow-Origin: *",
                remediation="移除 wildcard CORS，或只允许可信 origin。",
            )
        return SecurityCheck(
            id="bridge_cors",
            label="Bridge CORS 策略",
            status="pass",
            weight=10,
            detail="未发现 wildcard CORS 响应头",
        )
    except Exception as e:
        return SecurityCheck(
            id="bridge_cors",
            label="Bridge CORS 策略",
            status="fail",
            weight=10,
            detail=f"无法检查 bridge CORS: {e}",
            remediation="修复 bridge 模块导入后重新运行 security-check。",
        )


def _env_template_check() -> SecurityCheck:
    path = PROJECT_ROOT / ".env.example"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        needed = ["SERENITY_DASHBOARD_TOKEN", "SERENITY_BRIDGE_TOKEN"]
        missing = [name for name in needed if name not in text]
        if not missing:
            return SecurityCheck(
                id="env_template",
                label="环境变量模板",
                status="pass",
                weight=5,
                detail=".env.example 已列出 dashboard/bridge token",
            )
        return SecurityCheck(
            id="env_template",
            label="环境变量模板",
            status="warn",
            weight=5,
            detail=f".env.example 缺少: {', '.join(missing)}",
            remediation="补齐 token 模板变量。",
        )
    return SecurityCheck(
        id="env_template",
        label="环境变量模板",
        status="warn",
        weight=5,
        detail="未发现 .env.example",
        remediation="新增 .env.example，只放占位值，不放真实密钥。",
    )


def run_security_checks() -> list[SecurityCheck]:
    return [
        _dashboard_public_write_guard(),
        _bridge_auth_guard(),
        _bridge_get_tasks_disabled(),
        _bridge_cors_check(),
        _dashboard_token_check(),
        _bridge_token_check(),
        _env_template_check(),
    ]


def build_security_report() -> dict[str, Any]:
    checks = run_security_checks()
    total_weight = sum(c.weight for c in checks)
    passed_weight = sum(c.passed_weight for c in checks)
    score = round(passed_weight / total_weight * 100) if total_weight else 0
    fail_count = sum(1 for c in checks if c.status == "fail")
    warn_count = sum(1 for c in checks if c.status == "warn")
    if fail_count:
        status = "risk"
    elif warn_count:
        status = "watch"
    else:
        status = _status(score)
    return {
        "title": "Serenity 写接口安全体检",
        "score": score,
        "status": status,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "checks": [
            {
                "id": c.id,
                "label": c.label,
                "status": c.status,
                "status_text": _status_text(c.status),
                "weight": c.weight,
                "detail": c.detail,
                "remediation": c.remediation,
            }
            for c in checks
        ],
    }


def format_security_report(report: dict[str, Any] | None = None) -> str:
    report = report or build_security_report()
    lines = [
        report["title"],
        "=" * 64,
        f"安全分: {report['score']}/100 ({_status_text(report['status'])})",
        f"失败: {report['fail_count']} | 提醒: {report['warn_count']}",
        "",
        "检查项",
    ]
    for check in report["checks"]:
        lines.append(
            f"- [{check['status_text']}] {check['label']} "
            f"({check['weight']}分): {check['detail']}"
        )
        if check["remediation"]:
            lines.append(f"  修缮: {check['remediation']}")

    lines.extend([
        "",
        "配置示例",
        "  export SERENITY_DASHBOARD_TOKEN='生成一个长随机值'",
        "  export SERENITY_BRIDGE_TOKEN='生成另一个长随机值'",
        "  curl -X POST -H \"X-Serenity-Token: $SERENITY_BRIDGE_TOKEN\" "
        "http://127.0.0.1:9388/api/serenity/status",
    ])
    return "\n".join(lines)


def main() -> None:
    print(format_security_report())


if __name__ == "__main__":
    main()
