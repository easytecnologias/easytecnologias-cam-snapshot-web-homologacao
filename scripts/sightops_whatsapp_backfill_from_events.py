"""Reconstroi o historico do Messenger a partir de access_events antigos.

O historico de WhatsApp (access_whatsapp_messages) so comecou a ser gravado
em 2026-09-04 -- eventos de acesso anteriores a isso ja mandaram WhatsApp de
verdade (fica registrado em access_events.notification_status), mas o texto
enviado nunca ficou guardado em lugar nenhum ate agora. Este script
reconstroi cada envio (so os que realmente saíram: whatsapp_sent ou
whatsapp_failed -- nunca whatsapp_skipped, que significa que nada foi
mandado) usando a MESMA funcao que o envio real usa pra montar o texto
(_build_whatsapp_message), pra ficar identico ao que teria sido logado se o
Messenger ja existisse na hora.

Nao reconstroi mensagem RECEBIDA (resposta do responsavel): esse texto
nunca foi guardado em lugar nenhum antes de hoje, nao tem como recuperar.

Uso:
    python3 scripts/sightops_whatsapp_backfill_from_events.py <tenant_slug> --antes "2026-09-04 14:55:00" [--dry-run]

--antes: so eventos com occurred_at ANTES desse horario entram -- evita
duplicar o que o hook novo ja registrou ao vivo pros eventos de hoje.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    ap = argparse.ArgumentParser()
    ap.add_argument("tenant_slug")
    ap.add_argument("--antes", required=True, help="so eventos com occurred_at < este valor (evita duplicar o que ja foi logado ao vivo)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
    from app.services import db_store
    from app.services.access_control_notifications import _build_whatsapp_message, _event_context
    from app.services.access_control_whatsapp_log import ensure_whatsapp_log_schema, log_outbound_message

    token = set_current_tenant_slug(args.tenant_slug)
    try:
        ensure_whatsapp_log_schema()
        with db_store._conn() as c:
            linhas = c.execute(
                """
                SELECT id, person_id, device_id, event_type, occurred_at, notification_status
                FROM access_events
                WHERE tenant_slug=?
                  AND occurred_at < ?
                  AND (notification_status LIKE ? OR notification_status LIKE ?)
                ORDER BY occurred_at ASC
                """,
                (args.tenant_slug, args.antes, "%whatsapp_sent%", "%whatsapp_failed%"),
            ).fetchall()

        print(f"{len(linhas)} eventos com WhatsApp enviado/falhado antes de {args.antes}")
        criados = 0
        sem_telefone = 0
        for row in linhas:
            evento = dict(row)
            status = "whatsapp_failed" if "whatsapp_failed" in (evento.get("notification_status") or "") else "whatsapp_sent"
            contexto = _event_context(evento)
            telefone = contexto.get("guardian_phone") or ""
            if not telefone:
                sem_telefone += 1
                continue
            corpo = _build_whatsapp_message(contexto)
            if args.dry_run:
                print(f"  [dry-run] {evento['occurred_at']} -> {telefone} ({status}): {corpo[:60]!r}...")
            else:
                log_outbound_message(telefone, corpo, status=status, created_at=evento["occurred_at"])
            criados += 1
        print(f"{'seriam criadas' if args.dry_run else 'criadas'}: {criados} mensagens; sem telefone (puladas): {sem_telefone}")
    finally:
        reset_current_tenant_slug(token)


if __name__ == "__main__":
    main()
