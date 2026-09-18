from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
from app.services import connector_service, db_store
from app.services.access_control_store import (
    access_presence_summary,
    access_report_summary,
    list_access_report_events,
    list_devices,
    list_events,
    save_device,
    save_door_group,
    save_group,
    save_person,
    save_rule,
    set_door_group_members,
    set_group_members,
    list_provision_status_for_person,
)
from app.services.access_control_store import list_pending_provisions
from app.services.access_control_sync import (
    poll_device_events,
    provision_person_everywhere,
    resolve_target_devices_for_person,
    retry_pending_provisions,
)


def test_resolve_and_provision() -> None:
    token = set_current_tenant_slug("cliente-sync")
    try:
        person = save_person({"full_name": "Ana Teste", "document_id": "11111111111", "site": "Sede"})
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "SenhaTeste2011",
        })
        group = save_group({"name": "Alunos", "site": "Sede"})
        set_group_members(group["id"], [person["id"]])
        door_group = save_door_group({"name": "Portao", "site": "Sede"})
        set_door_group_members(door_group["id"], [device["id"]])
        save_rule({"people_group_id": group["id"], "door_group_id": door_group["id"]})

        targets = resolve_target_devices_for_person(person["id"])
        assert len(targets) == 1 and targets[0]["id"] == device["id"]

        with patch("app.services.access_control_sync.provision_person", return_value={"ok": True, "raw": "OK"}):
            result = provision_person_everywhere(person)
        assert result["ok"] is True
        assert result["results"][0]["device_id"] == device["id"]
        assert result["results"][0]["status"] == "ok"
        status = list_provision_status_for_person(person["id"])
        assert status[0]["status"] == "ok"
    finally:
        reset_current_tenant_slug(token)


def test_provision_failure_is_recorded_not_raised() -> None:
    from fastapi import HTTPException

    token = set_current_tenant_slug("cliente-sync-2")
    try:
        person = save_person({"full_name": "Bruno Teste", "document_id": "22222222222", "site": "Sede"})
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "SenhaTeste2011",
        })
        group = save_group({"name": "Alunos", "site": "Sede"})
        set_group_members(group["id"], [person["id"]])
        door_group = save_door_group({"name": "Portao", "site": "Sede"})
        set_door_group_members(door_group["id"], [device["id"]])
        save_rule({"people_group_id": group["id"], "door_group_id": door_group["id"]})

        with patch(
            "app.services.access_control_sync.provision_person",
            side_effect=HTTPException(status_code=502, detail="device is full"),
        ):
            result = provision_person_everywhere(person)
        assert result["ok"] is False
        assert result["results"][0]["status"] == "failed"
        assert "device is full" in result["results"][0]["error"]
        status = list_provision_status_for_person(person["id"])
        assert status[0]["status"] == "failed"
        assert "device is full" in status[0]["last_error"]
    finally:
        reset_current_tenant_slug(token)


def test_resolve_ignores_inactive_rule_and_inactive_device() -> None:
    token = set_current_tenant_slug("cliente-sync-3")
    try:
        person = save_person({"full_name": "Carla Teste", "document_id": "33333333333", "site": "Sede"})
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "SenhaTeste2011",
        })
        inactive_device = save_device({
            "name": "Catraca Desligada", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.34", "username": "admin", "password": "SenhaTeste2011",
            "active": False,
        })
        group = save_group({"name": "Alunos", "site": "Sede"})
        set_group_members(group["id"], [person["id"]])
        door_group = save_door_group({"name": "Portao", "site": "Sede"})
        set_door_group_members(door_group["id"], [device["id"], inactive_device["id"]])
        save_rule({"people_group_id": group["id"], "door_group_id": door_group["id"]})
        inactive_group = save_group({"name": "Ex-Alunos", "site": "Sede"})
        set_group_members(inactive_group["id"], [person["id"]])
        other_door_group = save_door_group({"name": "Outra Porta", "site": "Sede"})
        another_device = save_device({
            "name": "Catraca Outra", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.35", "username": "admin", "password": "SenhaTeste2011",
        })
        set_door_group_members(other_door_group["id"], [another_device["id"]])
        save_rule({
            "people_group_id": inactive_group["id"],
            "door_group_id": other_door_group["id"],
            "active": False,
        })

        targets = resolve_target_devices_for_person(person["id"])
        target_ids = {t["id"] for t in targets}
        assert target_ids == {device["id"]}
    finally:
        reset_current_tenant_slug(token)


def test_resolve_returns_empty_for_person_with_no_groups() -> None:
    token = set_current_tenant_slug("cliente-sync-4")
    try:
        person = save_person({"full_name": "Diego Teste", "document_id": "44444444444", "site": "Sede"})
        targets = resolve_target_devices_for_person(person["id"])
        assert targets == []
    finally:
        reset_current_tenant_slug(token)


def test_retry_pending_provisions_marks_success() -> None:
    token = set_current_tenant_slug("cliente-sync-5")
    try:
        person = save_person({"full_name": "Elis Teste", "document_id": "55555555555", "site": "Sede"})
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "SenhaTeste2011",
        })
        from app.services.access_control_store import upsert_provision_status

        upsert_provision_status(person["id"], device["id"], "pending")

        with patch(
            "app.services.access_control_sync.provision_person", return_value={"ok": True, "raw": "OK"}
        ) as mock_provision:
            result = retry_pending_provisions()
        assert result["ok"] is True
        assert result["retried"] == 1
        assert mock_provision.call_count == 1
        status = list_provision_status_for_person(person["id"])
        assert status[0]["status"] == "ok"
        assert list_pending_provisions() == []
    finally:
        reset_current_tenant_slug(token)


def test_retry_pending_provisions_loads_saved_face_photo() -> None:
    token = set_current_tenant_slug("cliente-sync-face")
    try:
        person = save_person({
            "full_name": "Elishafan Teste",
            "document_id": "66666666666",
            "site": "Sede",
            "controller_user_id": "1001",
        })
        device = save_device({
            "name": "Intelbras Portaria",
            "site": "Sede",
            "vendor": "intelbras",
            "host": "10.10.10.175",
            "username": "admin",
            "password": "secret",
        })
        from app.core.tenant_context import tenant_data_dir
        from app.services.access_control_store import update_person_face_photo, upsert_provision_status

        rel_path = f"access-control/faces/{person['id']}.jpg"
        target = tenant_data_dir() / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"face-jpeg")
        update_person_face_photo(person["id"], rel_path)
        upsert_provision_status(person["id"], device["id"], "pending")

        with patch("app.services.access_control_sync.provision_person", return_value={"ok": True, "raw": "OK"}) as mock_provision:
            result = retry_pending_provisions()

        assert result["retried"] == 1
        assert mock_provision.call_args.args[2] == b"face-jpeg"
    finally:
        reset_current_tenant_slug(token)


def test_retry_pending_provisions_skips_and_fails_orphaned_rows() -> None:
    """Se a pessoa ou o dispositivo referenciados por uma linha pendente ja
    foram removidos, retry_pending_provisions nao pode levantar excecao nem
    tentar chamar o dispositivo -- e a linha deve virar 'failed' com um erro
    explicito (nao ficar 'pending' parada pra sempre sem nenhum sinal)."""
    token = set_current_tenant_slug("cliente-sync-6")
    try:
        from app.services.access_control_store import upsert_provision_status

        upsert_provision_status("pessoa-inexistente", "dispositivo-inexistente", "pending")

        with patch("app.services.access_control_sync.provision_person") as mock_provision:
            result = retry_pending_provisions()
        assert result["ok"] is True
        assert result["retried"] == 0
        mock_provision.assert_not_called()
        status = list_provision_status_for_person("pessoa-inexistente")
        assert status[0]["status"] == "failed"
        assert status[0]["last_error"]
    finally:
        reset_current_tenant_slug(token)


def test_poll_device_events_records_events_and_returns_count() -> None:
    token = set_current_tenant_slug("cliente-sync-7")
    try:
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "SenhaTeste2011",
        })
        fake_events = [
            {"raw_id": "1", "occurred_at": "2026-08-16 10:00:00", "person_name_raw": "Fulano", "event_type": "entrada"},
            {"raw_id": "2", "occurred_at": "2026-08-16 10:05:00", "person_name_raw": "Ciclano", "event_type": "entrada"},
        ]
        with patch("app.services.access_control_sync.poll_events", return_value=fake_events) as mock_poll:
            count = poll_device_events(device["id"])
        assert count == 2
        assert mock_poll.call_count == 1
        from app.services.access_control_store import list_events

        recorded = list_events()
        assert len(recorded) == 2
        names = {e["person_name_raw"] for e in recorded}
        assert names == {"Fulano", "Ciclano"}
        assert all(e["device_id"] == device["id"] for e in recorded)
        stored_device = next(item for item in list_devices() if item["id"] == device["id"])
        assert stored_device["last_event_id"] == "2"
        assert stored_device["status"] == "online"
        assert stored_device["last_seen_at"]
    finally:
        reset_current_tenant_slug(token)


def test_poll_device_events_links_registered_people_and_uses_door_direction() -> None:
    token = set_current_tenant_slug("cliente-sync-reports")
    try:
        person = save_person({
            "full_name": "Aluno Relatorio",
            "document_id": "77777777777",
            "site": "ESCOLA",
            "controller_user_id": "42",
            "enrollment_code": "2026",
        })
        entry_device = save_device({
            "name": "Portaria Entrada",
            "site": "ESCOLA",
            "vendor": "intelbras",
            "host": "10.10.13.33",
            "username": "admin",
            "password": "SenhaTeste2011",
            "access_direction": "entrada",
        })
        exit_device = save_device({
            "name": "Portaria Saida",
            "site": "ESCOLA",
            "vendor": "intelbras",
            "host": "10.10.13.34",
            "username": "admin",
            "password": "SenhaTeste2011",
            "access_direction": "saida",
        })

        with patch("app.services.access_control_sync.poll_events", return_value=[{
            "raw_id": "entry-42",
            "occurred_at": "2026-08-20 07:00:00",
            "person_name_raw": "Aluno Relatorio",
            "controller_user_id": "42",
        }]):
            assert poll_device_events(entry_device["id"]) == 1
        with patch("app.services.access_control_sync.poll_events", return_value=[{
            "raw_id": "exit-42",
            "occurred_at": "2026-08-20 11:30:00",
            "person_name_raw": "Aluno Relatorio",
            "controller_user_id": "42",
            "event_type": "entrada",
        }]):
            assert poll_device_events(exit_device["id"]) == 1

        recorded = list_events()
        assert {event["person_id"] for event in recorded} == {person["id"]}
        summary = access_report_summary({"period": "all", "site": "ESCOLA"})
        assert summary["entries"] == 1, summary
        assert summary["exits"] == 1, summary
        assert summary["without_person"] == 0, summary
        assert access_presence_summary("ESCOLA")["inside_now"] == 0

        report_events = list_access_report_events({"period": "all", "site": "ESCOLA"})
        assert [event["event_type"] for event in report_events] == ["saida", "entrada"]
        assert all(event["person_name"] == "Aluno Relatorio" for event in report_events)
        assert report_events[0]["person_enrollment"] == "2026"
    finally:
        reset_current_tenant_slug(token)


def test_poll_device_events_swallows_device_error_and_returns_zero() -> None:
    """poll_events documenta que levanta HTTPException para erro real do
    dispositivo (nao devolve lista vazia silenciosa). Task 7 vai chamar
    poll_device_events por dispositivo dentro de um loop continuo -- um
    dispositivo fora do ar nao pode derrubar o loop nem propagar excecao
    para quem chama esta funcao."""
    from fastapi import HTTPException

    token = set_current_tenant_slug("cliente-sync-8")
    try:
        device = save_device({
            "name": "Catraca Portao", "site": "Sede", "vendor": "dahua",
            "host": "10.10.13.33", "username": "admin", "password": "SenhaTeste2011",
        })
        with patch(
            "app.services.access_control_sync.poll_events",
            side_effect=HTTPException(status_code=502, detail="device offline"),
        ):
            count = poll_device_events(device["id"])
        assert count == 0
        from app.services.access_control_store import list_events

        assert list_events() == []
    finally:
        reset_current_tenant_slug(token)


def test_connector_push_event_records_immediately_in_connector_tenant() -> None:
    from app.core.tenant_context import get_current_tenant_slug
    from app.services.connector_service import accept_access_control_event, create_connector

    token = set_current_tenant_slug("cliente-sync-push")
    try:
        connector = create_connector({
            "id": "conn-push",
            "name": "Conector Push",
            "client": "Cliente Push",
            "site": "ESCOLA",
            "type": "routeros",
        })["connector"]
        person = save_person({
            "full_name": "Aluno Push",
            "document_id": "88888888888",
            "site": "ESCOLA",
            "controller_user_id": "77",
            "enrollment_code": "2077",
        })
        device = save_device({
            "name": "Entrada Push",
            "site": "ESCOLA",
            "vendor": "intelbras",
            "host": "10.10.13.77",
            "connector_id": "conn-push",
            "username": "admin",
            "password": "SenhaTeste2011",
            "access_direction": "entrada",
        })
    finally:
        reset_current_tenant_slug(token)

    result = accept_access_control_event("conn-push", connector["token"], {
        "device_id": device["id"],
        "raw_id": "push-77",
        "occurred_at": "2026-08-22 16:10:00",
        "controller_user_id": "77",
        "person_name_raw": "Aluno Push",
    })

    assert result["ok"] is True
    assert result["event"]["person_id"] == person["id"]
    assert result["event"]["event_type"] == "entrada"
    assert result["event"]["source"] == "connector_push"
    assert get_current_tenant_slug() == ""

    token = set_current_tenant_slug("cliente-sync-push")
    try:
        report_events = list_access_report_events({"period": "all", "site": "ESCOLA"})
        assert len(report_events) == 1
        assert report_events[0]["person_name"] == "Aluno Push"
        assert report_events[0]["device_name"] == "Entrada Push"
    finally:
        reset_current_tenant_slug(token)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sightops-access-sync-") as tmp:
        db_store.SIGHTOPS_DB_PATH = Path(tmp) / "access.db"
        connector_service.CONNECTORS_PATH = Path(tmp) / "connectors.json"
        connector_service.CONNECTOR_JOBS_PATH = Path(tmp) / "connector-jobs.json"
        db_store.init_db()
        test_resolve_and_provision()
        test_provision_failure_is_recorded_not_raised()
        test_resolve_ignores_inactive_rule_and_inactive_device()
        test_resolve_returns_empty_for_person_with_no_groups()
        test_retry_pending_provisions_marks_success()
        test_retry_pending_provisions_loads_saved_face_photo()
        test_retry_pending_provisions_skips_and_fails_orphaned_rows()
        test_poll_device_events_records_events_and_returns_count()
        test_poll_device_events_links_registered_people_and_uses_door_direction()
        test_poll_device_events_swallows_device_error_and_returns_zero()
        test_connector_push_event_records_immediately_in_connector_tenant()
    print("OK access control sync: resolucao de regra e provisionamento nao bloqueante")


if __name__ == "__main__":
    main()
