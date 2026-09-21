"""Modulo Alerta: fluxo ponta a ponta sem rede (banco SQLite temporario).

Roda: python scripts/sightops_alert_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import ApiAuthMiddleware
from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
from app.services import alert_store as al
from app.services import db_store


def _in_tenant(slug, fn, *args, **kwargs):
    token = set_current_tenant_slug(slug)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_current_tenant_slug(token)


def _expect_error(exc_type, fn, *args, contains: str = "", **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        assert contains.lower() in str(exc).lower(), f"mensagem inesperada: {exc}"
        return
    raise AssertionError(f"esperava {exc_type.__name__} em {fn.__name__}")


def _activate(tenant: str, member_id: str) -> dict:
    code = _in_tenant(tenant, al.create_activation_code, member_id)["code"]
    out = al.activate_app(code.lower()[:4] + "-" + code.lower()[4:], "Moto G", "android")  # tolera caixa/hifen
    _expect_error(al.AppAuthError, al.activate_app, code, contains="invalido")  # uso unico
    return al.resolve_app_session(out["token"]) | {"token": out["token"]}


def test_member_validation() -> None:
    _expect_error(ValueError, _in_tenant, "pr-colegio", al.save_member, {"full_name": "Sem CPF"}, contains="CPF")
    m = _in_tenant("pr-colegio", al.save_member, {
        "full_name": "Maria Professora", "document_id": "123.456.789-01", "role_title": "Professora",
        "unit_name": "Escola Municipal A", "unit_lat": "-10,1860", "unit_lon": "-36.8250", "phone": "(82) 99999-0000",
    })
    assert m["document_id"] == "12345678901" and m["unit_lat"] == -10.186 and m["phone"] == "82999990000"
    _expect_error(ValueError, _in_tenant, "pr-colegio", al.save_member,
                  {"full_name": "Outra", "document_id": "12345678901"}, contains="ja cadastrado")
    # mesmo CPF em OUTRO cliente e permitido
    _in_tenant("outro-cliente", al.save_member, {"full_name": "Homonimo", "document_id": "12345678901"})


def test_full_flow() -> None:
    m = _in_tenant("pr-colegio", al.save_member, {"full_name": "Joao Servidor", "document_id": "22233344455", "unit_name": "Secretaria"})
    ctx = _activate("pr-colegio", m["id"])
    assert ctx["tenant_slug"] == "pr-colegio"
    assert al.app_status(ctx)["member"]["pins_configured"] is False

    _expect_error(ValueError, al.set_app_pins, ctx, "1234", "1234", contains="diferente")
    _expect_error(ValueError, al.set_app_pins, ctx, "12", "5678", contains="4 a 8")
    al.set_app_pins(ctx, "1234", "9876")
    ctx = al.resolve_app_session(ctx["token"]) | {"token": ctx["token"]}
    # trocar senha exige a atual
    _expect_error(ValueError, al.set_app_pins, ctx, "1111", "2222", "0000", contains="atual")

    # --- panico + idempotencia
    r1 = al.trigger_incident(ctx, {"lat": -10.19, "lon": -36.82, "accuracy": 12, "battery": 80})
    r2 = al.trigger_incident(ctx, {"lat": -10.191, "lon": -36.821})
    assert r1["created"] and not r2["created"] and r1["incident"]["id"] == r2["incident"]["id"]
    inc_id = r1["incident"]["id"]
    assert "kind" not in r1["incident"], "app nunca pode receber o tipo (vazaria a coacao)"
    al.record_positions(ctx, inc_id, [{"lat": -10.192, "lon": -36.822}, {"lat": 999, "lon": 0}])

    live = _in_tenant("pr-colegio", al.list_incidents)
    assert len(live) == 1 and live[0]["status"] == "new" and live[0]["member"]["full_name"] == "Joao Servidor"
    assert abs(live[0]["last_lat"] - -10.192) < 1e-9
    detail = _in_tenant("pr-colegio", al.get_incident, inc_id)
    assert len(detail["positions"]) == 3, detail["positions"]  # a invalida (lat 999) foi descartada
    assert _in_tenant("pr-colegio", al.live_summary)["new"] == 1

    # isolamento: outro cliente nao enxerga
    assert _in_tenant("outro-cliente", al.list_incidents) == []
    _expect_error(LookupError, _in_tenant, "outro-cliente", al.get_incident, inc_id)

    # --- coacao: senha errada, depois senha de coacao
    _expect_error(ValueError, al.cancel_incident, ctx, inc_id, "0000", contains="incorreta")
    with patch.object(al, "notify_async") as notify:
        out = al.cancel_incident(ctx, inc_id, "9876")
    assert out == {"ok": True, "tracking": True}
    notify.assert_called_once_with("pr-colegio", inc_id, "duress")
    inc = _in_tenant("pr-colegio", al.get_incident, inc_id)
    assert inc["kind"] == "duress" and inc["status"] == "new", inc
    assert al.app_status(ctx)["incident"]["id"] == inc_id  # continua aberto
    assert al.record_positions(ctx, inc_id, [{"lat": -10.2, "lon": -36.83}])["tracking"] is True

    # --- atendimento da central
    _expect_error(ValueError, _in_tenant, "pr-colegio", al.transition_incident, inc_id, "close", "guarda1", "", contains="Descreva")
    _in_tenant("pr-colegio", al.transition_incident, inc_id, "acknowledge", "guarda1")
    _expect_error(ValueError, _in_tenant, "pr-colegio", al.transition_incident, inc_id, "acknowledge", "guarda2", contains="ja esta")
    _in_tenant("pr-colegio", al.transition_incident, inc_id, "dispatch", "guarda1")
    _in_tenant("pr-colegio", al.add_incident_note, inc_id, "guarda1", "Viatura 03 a caminho")
    done = _in_tenant("pr-colegio", al.transition_incident, inc_id, "close", "guarda1", "Pessoa localizada e segura")
    assert done["status"] == "closed" and done["close_note"] == "Pessoa localizada e segura"
    actions = [x["action"] for x in done["log"]]
    assert actions == ["opened", "retriggered", "duress", "acknowledge", "dispatch", "note", "close"], actions
    # encerrado: app para de rastrear
    assert al.record_positions(ctx, inc_id, [{"lat": -10.2, "lon": -36.83}])["tracking"] is False
    assert al.app_status(ctx)["incident"] is None

    # --- cancelamento normal
    inc2 = al.trigger_incident(ctx, {"lat": -10.19, "lon": -36.82})["incident"]["id"]
    assert al.cancel_incident(ctx, inc2, "1234") == {"ok": True, "tracking": False}
    assert _in_tenant("pr-colegio", al.get_incident, inc2)["status"] == "cancelled"

    # --- desativar pessoa derruba o aparelho
    _in_tenant("pr-colegio", al.save_member, {**m, "active": False})
    _expect_error(al.AppAuthError, al.resolve_app_session, ctx["token"])


def test_escalation() -> None:
    m = _in_tenant("pr-colegio", al.save_member, {"full_name": "Ana Escalada", "document_id": "99988877766"})
    ctx = _activate("pr-colegio", m["id"])
    inc_id = al.trigger_incident(ctx, {"lat": -10.19, "lon": -36.82})["incident"]["id"]
    with patch.object(al, "_send_telegram_for", return_value={"ok": True}) as send:
        assert al.escalate_unattended(after_s=60) == []  # ainda nao passou 1 min
        sent = al.escalate_unattended(after_s=-5)
        assert [s["id"] for s in sent] == [inc_id]
        send.assert_called_once_with("pr-colegio", inc_id, "escalation")
        assert al.escalate_unattended(after_s=-5) == []  # nao reenvia
    msg = al._format_message(_in_tenant("pr-colegio", al.get_incident, inc_id), "escalation")
    assert "Ana Escalada" in msg and "maps.google.com/?q=-10.190000,-36.820000" in msg


def test_nearby_cameras() -> None:
    fake = {
        "olt": [
            {"ip": "10.0.0.1", "titulo": "Portao escola", "lat": "-10.1900", "lon": "-36.8200"},
            {"ip": "10.0.0.2", "titulo": "Longe", "lat": "-9.0", "lon": "-35.0"},
            {"ip": "10.0.0.3", "titulo": "Sem coord"},
        ],
        "switch": [{"ip": "10.0.0.4", "titulo": "Praca", "lat": "-10.1950", "lon": "-36.8200"}],
        "basic": [],
    }
    with patch("app.services.inventory_json.load_inventory_json", side_effect=lambda mode="olt", **_: fake[mode]):
        cams = _in_tenant("pr-colegio", al.nearby_cameras, -10.1901, -36.8201)
    assert [c["ip"] for c in cams] == ["10.0.0.1", "10.0.0.4"], cams
    assert cams[0]["distance_m"] < 20 and cams[1]["mode"] == "switch"


def test_unit_connector_cameras() -> None:
    m = _in_tenant("pr-colegio", al.save_member, {
        "full_name": "Professor Dutra", "document_id": "44455566677", "unit_connector_id": "8b1f1848aaaa",
    })
    assert m["unit_connector_id"] == "8b1f1848aaaa"
    assert _in_tenant("pr-colegio", al.list_members, "Dutra")[0]["unit_connector_id"] == "8b1f1848aaaa"
    fake = {
        "basic": [
            {"ip": "192.168.1.10", "titulo": "PATIO", "remote_connector_id": "8b1f1848aaaa"},  # sem coordenada
            {"ip": "192.168.1.11", "titulo": "CORREDOR", "remote_connector_id": "8b1f1848aaaa", "lat": "-10.1", "lon": "-36.8"},
            {"ip": "192.168.1.10", "titulo": "MESMO IP OUTRO CLIENTE", "remote_connector_id": "outro"},
        ],
        "olt": [], "switch": [],
    }
    with patch("app.services.inventory_json.load_inventory_json", side_effect=lambda mode="olt", **_: fake[mode]):
        cams = _in_tenant("pr-colegio", al.connector_cameras, "8b1f1848aaaa", -10.1, -36.8)
        assert _in_tenant("pr-colegio", al.connector_cameras, "") == []
    assert [c["titulo"] for c in cams] == ["CORREDOR", "PATIO"], cams
    assert cams[0]["distance_m"] == 0 and cams[1]["distance_m"] is None
    assert all(c["remote_connector_id"] == "8b1f1848aaaa" for c in cams)
    inc = _in_tenant("pr-colegio", al.list_members, "Dutra")
    assert inc


def test_app_route_is_public_only_for_app() -> None:
    mw = ApiAuthMiddleware.__new__(ApiAuthMiddleware)
    ApiAuthMiddleware.__init__(mw, app=None, settings=type("S", (), {})())
    assert mw._is_public_path("/api/alert/app/trigger")
    assert not mw._is_public_path("/api/alert/incidents")
    assert not mw._is_public_path("/api/alert/members")
    assert mw._match_role_rule("/api/alert/incidents/x/close", "POST") == "operator"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sightops-alert-") as tmp:
        db_store.SIGHTOPS_DB_PATH = Path(tmp) / "alert.db"
        status = db_store.init_db()
        assert status["schema_version"] >= 14, status
        test_member_validation()
        test_full_flow()
        test_escalation()
        test_nearby_cameras()
        test_unit_connector_cameras()
        test_app_route_is_public_only_for_app()
    print("OK alerta: cadastro, ativacao, panico, coacao, atendimento, escalonamento, cameras proximas, isolamento")


if __name__ == "__main__":
    main()
