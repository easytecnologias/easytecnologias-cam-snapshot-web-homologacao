from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-access-control.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-access-control-reports-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import (
            access_report_summary,
            ensure_access_control_schema,
            list_access_report_events,
            list_devices,
            record_event,
            record_manual_exit,
            save_device,
            save_door_group,
            save_person,
            set_door_group_members,
        )

        token = set_current_tenant_slug("rads")
        try:
            ensure_access_control_schema()
            person = save_person(
                {
                    "full_name": "Aluno Teste",
                    "person_type": "student",
                    "document_id": "12345678901",
                    "site": "ESCOLA",
                    "active": True,
                }
            )
            entry_device = save_device(
                {
                    "name": "Portaria Entrada",
                    "site": "ESCOLA",
                    "host": "10.0.0.10",
                    "username": "admin",
                    "password": "12345678",
                    "access_direction": "entrada",
                    "active": True,
                }
            )
            exit_device = save_device(
                {
                    "name": "Portaria Saida",
                    "site": "ESCOLA",
                    "host": "10.0.0.11",
                    "username": "admin",
                    "password": "12345678",
                    "access_direction": "saida",
                    "active": True,
                }
            )
            assert {row["access_direction"] for row in list_devices()} == {"entrada", "saida"}
            entry_door_group = save_door_group({"name": "Portaria Entrada", "site": "ESCOLA"})
            set_door_group_members(entry_door_group["id"], [entry_device["id"]])
            empty_door_group = save_door_group({"name": "Grupo vazio", "site": "ESCOLA"})

            record_event(
                {
                    "device_id": entry_device["id"],
                    "device_role": entry_device["access_direction"],
                    "person_id": person["id"],
                    "person_name_raw": person["full_name"],
                    "event_type": "entrada",
                    "raw_event_id": "entry-1",
                    "occurred_at": "2026-08-20 07:10:00",
                }
            )
            record_event(
                {
                    "site": "ESCOLA",
                    "device_id": exit_device["id"],
                    "device_name": exit_device["name"],
                    "device_role": exit_device["access_direction"],
                    "person_id": person["id"],
                    "person_name_raw": person["full_name"],
                    "event_type": "saida",
                    "raw_event_id": "exit-1",
                    "occurred_at": "2026-08-20 11:40:00",
                }
            )
            manual = record_manual_exit(person["id"], site="ESCOLA", reason="responsavel buscou", operator_user="admin")

            summary = access_report_summary({"period": "all", "site": "ESCOLA"})
            assert summary["entries"] == 1, summary
            assert summary["exits"] == 1, summary
            assert summary["manual_exits"] == 1, summary
            assert summary["inside_now"] == 0, summary

            entry_events = list_access_report_events({"period": "all", "type": "entrada", "site": "ESCOLA"})
            assert len(entry_events) == 1, entry_events
            assert entry_events[0]["site"] == "ESCOLA", entry_events
            assert entry_events[0]["device_name"] == "Portaria Entrada", entry_events
            assert entry_events[0]["person_document"] == "12345678901", entry_events

            manual_events = list_access_report_events({"period": "all", "type": "saida_manual", "site": "ESCOLA"})
            assert len(manual_events) == 1, manual_events
            assert manual_events[0]["id"] == manual["id"], manual_events
            assert manual_events[0]["source"] == "manual", manual_events

            timed_events = list_access_report_events(
                {
                    "period": "custom",
                    "start": "2026-08-20 07:00:00",
                    "end": "2026-08-20 08:00:00",
                    "site": "ESCOLA",
                }
            )
            assert len(timed_events) == 1, timed_events
            assert timed_events[0]["event_type"] == "entrada", timed_events

            timed_summary = access_report_summary(
                {
                    "period": "custom",
                    "start": "2026-08-20T11:00",
                    "end": "2026-08-20T12:00",
                    "site": "ESCOLA",
                }
            )
            assert timed_summary["entries"] == 0, timed_summary
            assert timed_summary["exits"] == 1, timed_summary
            assert timed_summary["manual_exits"] == 0, timed_summary

            door_group_events = list_access_report_events(
                {"period": "all", "site": "ESCOLA", "door_group_id": entry_door_group["id"]}
            )
            assert len(door_group_events) == 1, door_group_events
            assert door_group_events[0]["device_id"] == entry_device["id"], door_group_events
            door_group_summary = access_report_summary(
                {"period": "all", "site": "ESCOLA", "door_group_id": entry_door_group["id"]}
            )
            assert door_group_summary["entries"] == 1, door_group_summary
            assert door_group_summary["exits"] == 0, door_group_summary

            assert list_access_report_events(
                {"period": "all", "site": "ESCOLA", "door_group_id": empty_door_group["id"]}
            ) == []
        finally:
            reset_current_tenant_slug(token)

        token = set_current_tenant_slug("outro-cliente")
        try:
            ensure_access_control_schema()
            assert list_access_report_events({"period": "all"}) == []
            assert access_report_summary({"period": "all"})["total"] == 0
        finally:
            reset_current_tenant_slug(token)

    print("access-control reports regression ok")


if __name__ == "__main__":
    main()
