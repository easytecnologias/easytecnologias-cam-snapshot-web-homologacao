"""Historico de mensagens do WhatsApp (tela Messenger).

Cobre o modulo isolado (access_control_whatsapp_log.py) e a integracao com
o envio de verdade (_send_whatsapp_cloud/_send_whatsapp_evolution), que
precisa registrar toda tentativa -- sucesso e falha -- sem derrubar o envio
se o log falhar.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_DB_PATH"] = os.path.join(tmp, "sightops-wa-log.db")
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-wa-log-test-key"

        from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
        from app.services.access_control_store import ensure_access_control_schema, save_person
        from app.services.access_control_whatsapp_log import (
            list_conversations,
            list_messages,
            log_inbound_message,
            log_outbound_message,
            update_message_status,
        )

        token = set_current_tenant_slug("escola-wa-log")
        try:
            ensure_access_control_schema()

            # --- registra envio (sucesso) e chegada -- qualquer numero
            #     local (10/11 digitos, sem 55) e normalizado com o 55 na
            #     frente ao gravar (mesma regra do envio de verdade,
            #     _whatsapp_target) antes mesmo de casar por numero.
            log_outbound_message(
                "82 99999-0000", "ENTRADA - Controle de Acesso\nAluno: JOAO",
                status="whatsapp_sent", wa_message_id="wamid.111", template_name="aviso_acesso_aluno",
            )
            log_inbound_message("82999990000", "Obrigado!", from_name="Mae do Joao")

            # --- confirmacao de entrega chega depois, pelo webhook, casando por wamid
            ok = update_message_status("wamid.111", "delivered")
            assert ok, "deveria ter achado a mensagem pelo wamid"
            ok2 = update_message_status("wamid.999-nao-existe", "delivered")
            assert not ok2, "wamid desconhecido nao deveria achar nada"

            mensagens = list_messages("5582999990000")
            assert len(mensagens) == 2, mensagens
            assert mensagens[0]["direction"] == "out", mensagens[0]
            assert mensagens[0]["status"] == "delivered", mensagens[0]  # atualizado pelo webhook
            assert mensagens[1]["direction"] == "in", mensagens[1]
            assert mensagens[1]["from_name"] == "Mae do Joao", mensagens[1]

            # --- envio que falha tambem fica registrado (pra tela mostrar o problema)
            log_outbound_message("82 98888-7777", "texto qualquer", status="whatsapp_failed",
                                  error="numero invalido na Meta")
            falhas = list_messages("5582988887777")
            assert len(falhas) == 1, falhas
            assert falhas[0]["status"] == "whatsapp_failed", falhas[0]
            assert "invalido" in falhas[0]["error"], falhas[0]

            # --- lista de conversas: uma linha por numero, rotulada com o
            #     nome de quem ja tem esse guardian_phone cadastrado (mesmo
            #     que o guardian_phone tenha sido salvo sem o 55 -- casa
            #     pelas duas formas)
            save_person({
                "full_name": "JOAO ALUNO", "document_id": "11111111111",
                "enrollment_code": "2001", "guardian_phone": "82999990000",
                "guardian_name": "Mae do Joao",
            })
            conversas = {c["contact_number"]: c for c in list_conversations()}
            assert set(conversas.keys()) == {"5582999990000", "5582988887777"}, conversas
            assert conversas["5582999990000"]["person_name"] == "JOAO ALUNO", conversas["5582999990000"]
            assert conversas["5582999990000"]["last_direction"] == "in", conversas["5582999990000"]  # a mais recente
            assert conversas["5582988887777"]["person_name"] == "", conversas["5582988887777"]  # numero avulso

            # --- log_outbound_message chamado com o numero CRU (sem 55) --
            #     exatamente o que o backfill fazia antes desta correcao --
            #     tem que normalizar e cair na MESMA conversa de quem ja
            #     mandou com 55, nunca criar uma "conversa duplicada" pra
            #     mesma pessoa (foi exatamente esse bug em producao).
            save_person({
                "full_name": "GUSTAVO SEM DDI NO CADASTRO", "document_id": "22222222222",
                "enrollment_code": "3002", "guardian_phone": "79988613751",
            })
            log_outbound_message("5579988613751", "Aviso do controle de acesso.", status="whatsapp_sent")
            log_outbound_message("79988613751", "Mensagem reconstruida pelo backfill.", status="whatsapp_sent")
            mensagens_gustavo = list_messages("5579988613751")
            assert len(mensagens_gustavo) == 2, mensagens_gustavo
            conversas2 = {c["contact_number"]: c for c in list_conversations()}
            assert conversas2["5579988613751"]["person_name"] == "GUSTAVO SEM DDI NO CADASTRO", conversas2
            assert len(conversas2) == 3, conversas2  # nao virou uma 4a conversa

            # --- numero vazio nao levanta excecao nem cria linha nenhuma
            log_outbound_message("", "vazio", status="whatsapp_skipped")
            assert list_messages("") == []
        finally:
            reset_current_tenant_slug(token)

        # ====================================================================
        # --- integracao: envio de verdade (Cloud API) precisa logar sozinho
        # ====================================================================
        token2 = set_current_tenant_slug("escola-wa-log-integracao")
        try:
            from app.core.crypto import encrypt
            from app.services import access_control_notifications as notif

            settings = {
                "access_control_whatsapp_notifications": {
                    "enabled": True,
                    "provider": "cloud_api",
                    "phone_number_id": "123456",
                    "access_token": encrypt("token-de-teste"),
                    "template_name": "aviso_acesso_aluno",
                    "template_language": "pt_BR",
                },
            }

            class RespostaFalsa:
                status_code = 200
                text = '{"messages":[{"id":"wamid.abc123","message_status":"accepted"}]}'
                content = text.encode("utf-8")  # _resposta_json checa response.content antes de .json()

                def json(self):
                    import json as _json
                    return _json.loads(self.text)

            with patch.object(notif, "requests") as req_mock:
                req_mock.post.return_value = RespostaFalsa()
                # numero brasileiro sem DDI -- _numero_whatsapp acrescenta o
                # 55, e o log guarda exatamente o numero normalizado que foi
                # de fato usado no envio (mesmo formato que o webhook da Meta
                # manda de volta no from_number de uma resposta).
                event = {"guardian_phone": "82977776666", "whatsapp_enabled": True,
                         "site": "", "event_type": "entrada", "person_name": "MARIA"}
                status = notif._send_whatsapp(settings, event, "ENTRADA - MARIA")
            assert status == "whatsapp_sent", status

            registradas = list_messages("5582977776666")
            assert len(registradas) == 1, registradas
            assert registradas[0]["status"] == "whatsapp_sent", registradas[0]

            # a confirmacao de entrega, quando chegar pelo webhook, tem que
            # casar com essa mensagem pelo wamid capturado da resposta da Meta
            ok3 = update_message_status("wamid.abc123", "read")
            assert ok3, "o wamid da resposta da Meta precisa ter sido guardado"
            assert list_messages("5582977776666")[0]["status"] == "read"
        finally:
            reset_current_tenant_slug(token2)

    print("access-control whatsapp log (Messenger) ok")


if __name__ == "__main__":
    main()
