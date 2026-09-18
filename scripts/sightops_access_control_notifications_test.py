from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-access-control-notifications.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-access-control-notifications-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import (
            ensure_access_control_schema,
            list_access_report_events,
            record_event,
            save_device,
            save_person,
        )
        from app.services.db_store import save_app_settings

        sent: list[dict[str, Any]] = []

        class FakeResponse:
            ok = True
            status_code = 200
            content = b'{"ok": true}'

            def json(self) -> dict[str, Any]:
                # serve aos dois destinos simulados: "ok" para o Telegram,
                # "messages" para a Cloud API
                return {"ok": True, "messages": [{"id": "wamid.TESTE", "message_status": "accepted"}]}

        def fake_post(url: str, **kwargs: Any) -> FakeResponse:
            sent.append({"url": url, **kwargs})
            return FakeResponse()

        import requests

        original_post = requests.post
        requests.post = fake_post
        token = set_current_tenant_slug("escola-alertas")
        try:
            ensure_access_control_schema()
            save_app_settings(
                {
                    "telegram_notifications": {
                        "enabled": True,
                        "bot_token": "telegram-token",
                        "chat_id": "-100123",
                    },
                    "access_control_whatsapp_notifications": {
                        "enabled": True,
                        "provider": "cloud_api",
                        "phone_number_id": "1299130376610413",
                        "access_token": "token-de-teste",
                        "template_name": "aviso_acesso_aluno",
                        "template_language": "pt_BR",
                    },
                }
            )
            person = save_person(
                {
                    "full_name": "Aluno Notificado",
                    "document_id": "11111111111",
                    "person_type": "student",
                    "class_name": "7A",
                    "site": "ESCOLA",
                    "guardian_phone": "(82) 99999-0000",
                    "whatsapp_enabled": True,
                }
            )
            device = save_device(
                {
                    "name": "Portaria Entrada",
                    "site": "ESCOLA",
                    "host": "10.0.0.10",
                    "username": "admin",
                    "password": "12345678",
                    "access_direction": "entrada",
                }
            )

            first_id = record_event(
                {
                    "site": "ESCOLA",
                    "device_id": device["id"],
                    "device_name": device["name"],
                    "device_role": device["access_direction"],
                    "person_id": person["id"],
                    "person_name_raw": person["full_name"],
                    "event_type": "entrada",
                    "raw_event_id": "entrada-1",
                    "occurred_at": "2026-08-20 07:10:00",
                }
            )
            duplicate_id = record_event(
                {
                    "site": "ESCOLA",
                    "device_id": device["id"],
                    "device_name": device["name"],
                    "device_role": device["access_direction"],
                    "person_id": person["id"],
                    "person_name_raw": person["full_name"],
                    "event_type": "entrada",
                    "raw_event_id": "entrada-1",
                    "occurred_at": "2026-08-20 07:10:00",
                }
            )

            assert duplicate_id == first_id
            assert len(sent) == 2, sent
            assert sent[0]["url"] == "https://api.telegram.org/bottelegram-token/sendMessage"
            assert sent[0]["json"]["chat_id"] == "-100123"
            assert "ENTRADA" in sent[0]["json"]["text"], sent[0]
            assert "Aluno Notificado" in sent[0]["json"]["text"], sent[0]
            assert sent[1]["url"].endswith("/1299130376610413/messages"), sent[1]
            assert sent[1]["url"].startswith("https://graph.facebook.com/"), sent[1]
            corpo = sent[1]["json"]
            assert corpo["to"] == "5582999990000", corpo
            assert corpo["type"] == "template", corpo
            assert corpo["template"]["name"] == "aviso_acesso_aluno", corpo
            # a mensagem vai em variaveis, nao em texto livre: e o que a Meta exige
            # para mensagem iniciada pela empresa
            variaveis = [p["text"] for p in corpo["template"]["components"][0]["parameters"]]
            assert variaveis[0] == "ENTRADA", variaveis
            assert variaveis[2] == "Aluno Notificado", variaveis

            events = list_access_report_events({"period": "all", "site": "ESCOLA"})
            assert events[0]["id"] == first_id, events
            assert "telegram_sent" in events[0]["notification_status"], events
            assert "whatsapp_sent" in events[0]["notification_status"], events
        finally:
            requests.post = original_post
            reset_current_tenant_slug(token)

    print("access-control notifications regression ok")


if __name__ == "__main__":
    main()
