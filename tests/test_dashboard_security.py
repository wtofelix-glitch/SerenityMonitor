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


def test_broker_snapshot_endpoint_is_write_protected(monkeypatch):
    monkeypatch.delenv("SERENITY_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SERENITY_API_TOKEN", raising=False)

    client = app.test_client()
    response = client.post(
        "/api/broker-snapshot",
        json={"snapshot_at": "2026-07-04 15:30:00"},
        headers={"Host": "serenity.example.com"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 401


def test_broker_snapshot_endpoint_returns_validation_error(monkeypatch):
    import operations_center

    monkeypatch.setattr(
        operations_center,
        "import_broker_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("asset equation mismatch")),
    )
    client = app.test_client()
    response = client.post(
        "/api/broker-snapshot",
        json={"snapshot_at": "2026-07-04 15:30:00"},
        headers={"Host": "127.0.0.1:8401"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert "asset equation" in response.get_json()["error"]


def test_broker_snapshot_endpoint_passes_dry_run_without_mutating_payload(monkeypatch):
    import operations_center

    captured = []
    monkeypatch.setattr(
        operations_center,
        "import_broker_snapshot",
        lambda payload, dry_run=False: captured.append((payload, dry_run)) or {"saved": False},
    )
    client = app.test_client()
    response = client.post(
        "/api/broker-snapshot",
        json={"snapshot_at": "2026-07-04 15:30:00", "dry_run": True},
        headers={"Host": "127.0.0.1:8401"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert captured == [({"snapshot_at": "2026-07-04 15:30:00"}, True)]
    assert response.get_json()["data"]["saved"] is False
