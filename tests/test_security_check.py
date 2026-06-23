"""写接口安全体检测试。"""

import security_check


def test_security_report_verifies_write_guards_and_warns_without_tokens(monkeypatch):
    monkeypatch.delenv("SERENITY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    report = security_check.build_security_report()
    checks = {c["id"]: c for c in report["checks"]}

    assert report["status"] == "watch"
    assert report["fail_count"] == 0
    assert report["warn_count"] == 2
    assert report["score"] >= 80
    assert checks["dashboard_public_write_guard"]["status"] == "pass"
    assert checks["bridge_auth_guard"]["status"] == "pass"
    assert checks["bridge_get_tasks_disabled"]["status"] == "pass"
    assert checks["bridge_cors"]["status"] == "pass"
    assert checks["dashboard_token"]["status"] == "warn"
    assert checks["bridge_token"]["status"] == "warn"
    assert checks["env_template"]["status"] == "pass"


def test_security_report_passes_with_specific_tokens(monkeypatch):
    monkeypatch.setenv("SERENITY_DASHBOARD_TOKEN", "dashboard-test-token")
    monkeypatch.setenv("SERENITY_BRIDGE_TOKEN", "bridge-test-token")
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    report = security_check.build_security_report()
    checks = {c["id"]: c for c in report["checks"]}

    assert report["status"] == "good"
    assert report["fail_count"] == 0
    assert report["warn_count"] == 0
    assert report["score"] == 100
    assert checks["dashboard_token"]["status"] == "pass"
    assert checks["bridge_token"]["status"] == "pass"


def test_security_report_format_includes_next_steps(monkeypatch):
    monkeypatch.delenv("SERENITY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    rendered = security_check.format_security_report()

    assert "Serenity 写接口安全体检" in rendered
    assert "SERENITY_DASHBOARD_TOKEN" in rendered
    assert "SERENITY_BRIDGE_TOKEN" in rendered
