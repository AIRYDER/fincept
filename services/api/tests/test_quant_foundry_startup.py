from __future__ import annotations

from fastapi import FastAPI


def test_configure_quant_foundry_gateway_attaches_gateway(
    monkeypatch, tmp_path
) -> None:
    from api.main import configure_quant_foundry_gateway

    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "local_mock")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "startup-secret")

    app = FastAPI()
    gateway = configure_quant_foundry_gateway(app, base_dir=tmp_path / "qf")

    assert app.state.quant_foundry_gateway is gateway
    assert gateway.health()["enabled"] is True
    assert gateway.health()["mode"] == "local_mock"
