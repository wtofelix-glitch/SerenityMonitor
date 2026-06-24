"""Dashboard write endpoint security checks."""

import sys

sys.path.insert(0, "/Users/mac/workspace/SerenityMonitor")

from monitoring_dashboard import app


def test_write_endpoint_rejects_public_host_without_token(monkeypatch):
    monkeypatch.delenv("SERENITY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    client = app.test_client()
    resp = client.post(
        "/api/config",
        json={"code": "000988"},
        headers={"Host": "serenity.example.com"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_quantdinger_consensus_is_read_only_on_public_host(monkeypatch):
    monkeypatch.delenv("SERENITY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    client = app.test_client()
    resp = client.get(
        "/api/quantdinger-consensus",
        headers={"Host": "serenity.example.com"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert "universe_decision" in payload["data"]


def test_write_endpoint_accepts_matching_token_on_public_host(monkeypatch):
    monkeypatch.setenv("SERENITY_DASHBOARD_TOKEN", "secret-test-token")

    client = app.test_client()
    resp = client.post(
        "/api/config",
        json={"code": "NO_SUCH_CODE"},
        headers={
            "Host": "serenity.example.com",
            "X-Serenity-Token": "secret-test-token",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 404
    assert "找不到" in resp.get_json()["msg"]


def test_hermes_trade_rejects_public_host_without_token(monkeypatch):
    monkeypatch.delenv("SERENITY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    client = app.test_client()
    resp = client.post(
        "/api/hermes/trade",
        json={"code": "600141", "action": "buy", "price": 1, "quantity": 100},
        headers={"Host": "serenity.example.com"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_hermes_balance_accepts_matching_token_on_public_host(monkeypatch):
    monkeypatch.setenv("SERENITY_DASHBOARD_TOKEN", "secret-test-token")

    client = app.test_client()
    resp = client.post(
        "/api/hermes/balance",
        json={},
        headers={
            "Host": "serenity.example.com",
            "X-Serenity-Token": "secret-test-token",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
