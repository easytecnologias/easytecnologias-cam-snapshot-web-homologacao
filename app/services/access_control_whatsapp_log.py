"""Historico de mensagens do WhatsApp do controle de acesso (tela Messenger).

So um registro do que ja passa pelo canal existente (Cloud API oficial ou
Evolution, conforme configurado por site em `access_control_notifications.py`)
-- nao e um canal de envio novo, nao fala direto com a Meta/Evolution. Toda
consulta e sempre por tenant, igual ao resto do controle de acesso.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List

from app.services import db_store


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _digits_br(value: Any) -> str:
    """Numero local (10/11 digitos, sem DDI) casa com o mesmo numero que a
    Cloud API guarda com o 55 na frente -- mesma regra de
    access_control_notifications._whatsapp_target, senao um guardian_phone
    cadastrado sem DDI nunca bate com o contact_number das mensagens."""
    digits = _digits(value)
    if digits and not digits.startswith("55") and len(digits) in (10, 11):
        return "55" + digits
    return digits


def _text(value: Any, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def ensure_whatsapp_log_schema() -> None:
    backend = db_store._db_backend()
    with db_store._conn() as c:
        db_store._exec_many_statements(
            c,
            backend,
            """
            CREATE TABLE IF NOT EXISTS access_whatsapp_messages (
              id TEXT PRIMARY KEY,
              tenant_slug TEXT NOT NULL,
              contact_number TEXT NOT NULL,
              direction TEXT NOT NULL,
              body TEXT NOT NULL DEFAULT '',
              template_name TEXT NOT NULL DEFAULT '',
              wa_message_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              from_name TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_wa_msg_tenant_contact_time
              ON access_whatsapp_messages(tenant_slug, contact_number, created_at);
            CREATE INDEX IF NOT EXISTS idx_wa_msg_tenant_wamid
              ON access_whatsapp_messages(tenant_slug, wa_message_id);
            CREATE INDEX IF NOT EXISTS idx_wa_msg_tenant_time
              ON access_whatsapp_messages(tenant_slug, created_at);
            """,
        )


def log_outbound_message(
    contact_number: Any,
    body: str,
    *,
    status: str,
    wa_message_id: str = "",
    template_name: str = "",
    error: str = "",
    created_at: str = "",
) -> None:
    """Chamado de dentro de _send_whatsapp_cloud/_send_whatsapp_evolution,
    logo apos a tentativa de envio -- registra sucesso E falha, pra tela
    mostrar quando uma notificacao nao saiu.

    `created_at` so existe pro backfill (sightops_whatsapp_backfill_from_events.py)
    reconstruir mensagem antiga com a data do evento original, em vez da
    data em que o backfill rodou -- vazio (uso normal) grava a hora atual."""
    # Sempre normalizado com o 55 na frente (mesma regra do envio de
    # verdade, _whatsapp_target) -- gravar so _digits() aqui foi o que
    # criou "conversa duplicada" no backfill: guardian_phone cru (sem 55)
    # virava um contact_number diferente do que o envio ao vivo ja tinha
    # gravado pra mesma pessoa.
    numero = _digits_br(contact_number)
    if not numero:
        return
    ensure_whatsapp_log_schema()
    tenant = db_store._current_tenant_slug()
    quando = _text(created_at, 40) or None
    with db_store._conn() as c:
        c.execute(
            f"""
            INSERT INTO access_whatsapp_messages(
              id, tenant_slug, contact_number, direction, body, template_name,
              wa_message_id, status, error, created_at, updated_at
            )
            VALUES(?, ?, ?, 'out', ?, ?, ?, ?, ?, {'?' if quando else "datetime('now')"}, datetime('now'))
            """,
            (
                uuid.uuid4().hex, tenant, numero, _text(body), _text(template_name, 120),
                _text(wa_message_id, 120), _text(status, 40), _text(error, 300),
            ) + ((quando,) if quando else ()),
        )


def log_inbound_message(contact_number: Any, body: str, *, from_name: str = "") -> None:
    numero = _digits_br(contact_number)
    if not numero:
        return
    ensure_whatsapp_log_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        c.execute(
            """
            INSERT INTO access_whatsapp_messages(
              id, tenant_slug, contact_number, direction, body, from_name,
              status, created_at, updated_at
            )
            VALUES(?, ?, ?, 'in', ?, ?, 'received', datetime('now'), datetime('now'))
            """,
            (uuid.uuid4().hex, tenant, numero, _text(body), _text(from_name, 160)),
        )


def update_message_status(wa_message_id: Any, status: Any, *, error: str = "") -> bool:
    """Casa a confirmacao de entrega/leitura que a Meta manda pelo webhook
    com a linha que log_outbound_message ja tinha criado (mesmo wamid)."""
    wamid = _text(wa_message_id, 120)
    if not wamid:
        return False
    ensure_whatsapp_log_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        cur = c.execute(
            """
            UPDATE access_whatsapp_messages
            SET status=?, error=?, updated_at=datetime('now')
            WHERE tenant_slug=? AND wa_message_id=?
            """,
            (_text(status, 40), _text(error, 300), tenant, wamid),
        )
        return (cur.rowcount or 0) > 0


def list_conversations(limit: int = 200) -> List[Dict[str, Any]]:
    """Uma linha por numero de contato, com a ultima mensagem -- rotulado com
    o nome do aluno/responsavel quando o numero bate com algum guardian_phone
    ja cadastrado (mesmo numero pode nao ter pessoa nenhuma: contato avulso)."""
    ensure_whatsapp_log_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        rows = c.execute(
            """
            SELECT contact_number, body, direction, status, created_at
            FROM access_whatsapp_messages
            WHERE tenant_slug=?
            ORDER BY created_at DESC
            """,
            (tenant,),
        ).fetchall()
        pessoas = c.execute(
            """
            SELECT full_name, guardian_phone, guardian_name, class_name, site
            FROM access_people
            WHERE tenant_slug=? AND guardian_phone <> ''
            """,
            (tenant,),
        ).fetchall()
    # Guarda pelas duas formas (com e sem o 55 na frente): contact_number das
    # mensagens de verdade sempre tem o 55 (_numero_whatsapp prefixa sempre),
    # mas guardian_phone costuma ser cadastrado sem -- casar so por uma forma
    # deixava a pessoa sem nome na tela toda vez que os dois formatos nao
    # batessem exatamente.
    por_numero: Dict[str, Dict[str, Any]] = {}
    for p in pessoas:
        d = dict(p)
        for num in {_digits(d.get("guardian_phone")), _digits_br(d.get("guardian_phone"))}:
            if num and num not in por_numero:
                por_numero[num] = d

    vistos: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        num = d["contact_number"]
        if num in vistos:
            continue
        pessoa = por_numero.get(num) or {}
        vistos[num] = {
            "contact_number": num,
            "last_body": d["body"],
            "last_direction": d["direction"],
            "last_status": d["status"],
            "last_at": d["created_at"],
            "person_name": pessoa.get("full_name") or "",
            "guardian_name": pessoa.get("guardian_name") or "",
            "class_name": pessoa.get("class_name") or "",
            "site": pessoa.get("site") or "",
        }
        if len(vistos) >= limit:
            break
    return list(vistos.values())


def list_messages(contact_number: Any, limit: int = 300) -> List[Dict[str, Any]]:
    numero = _digits_br(contact_number)
    if not numero:
        return []
    ensure_whatsapp_log_schema()
    tenant = db_store._current_tenant_slug()
    with db_store._conn() as c:
        rows = c.execute(
            """
            SELECT id, direction, body, status, error, from_name, created_at
            FROM access_whatsapp_messages
            WHERE tenant_slug=? AND contact_number=?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (tenant, numero, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]
