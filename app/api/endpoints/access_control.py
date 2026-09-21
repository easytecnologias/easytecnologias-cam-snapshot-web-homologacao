from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse, StreamingResponse

import logging

from app.api.endpoints.auth import current_user
from app.services.access_control_store import (
    access_control_summary,
    access_present_people,
    access_report_summary,
    delete_device,
    delete_door_group,
    delete_group,
    delete_person,
    delete_rule,
    list_devices,
    list_door_groups,
    list_events,
    list_access_report_events,
    list_group_members,
    list_groups,
    list_people,
    list_people_sites,
    list_provision_status_for_people,
    list_provision_status_for_person,
    list_rules,
    save_device,
    save_door_group,
    save_group,
    save_person,
    save_rule,
    record_manual_exit,
    set_door_group_members,
    set_group_members,
    update_device_health,
    upsert_provision_status,
)
from app.services.access_control_device import get_system_info as device_get_system_info
from app.services.access_control_device import get_controller_person_photo as device_get_controller_person_photo
from app.services.access_control_device import list_controller_people as device_list_controller_people
from app.services.access_control_device import open_door as device_open_door
from app.services.access_control_photos import load_person_face_photo, save_person_face_photo
from app.services.access_control_sync import provision_person_everywhere, resolve_target_devices_for_person
from app.services.access_control_sync import enqueue_person_provisioning
from app.services.access_control_notifications import (
    assinatura_webhook_valida,
    disconnect_access_whatsapp,
    get_access_whatsapp_connection,
    resolver_cliente_por_numero,
    get_access_whatsapp_config,
    save_access_whatsapp_config,
    send_access_whatsapp_text,
    test_access_whatsapp,
)
from app.services.access_control_whatsapp_inbound import (
    approve_access_whatsapp_triage_item,
    approve_ready_access_whatsapp_triage_items,
    create_access_whatsapp_triage_item,
    ensure_access_whatsapp_inbound_token,
    extract_meta_inbound,
    extract_meta_statuses,
    list_access_whatsapp_triage,
    process_access_whatsapp_inbound,
    process_access_whatsapp_text,
    reject_access_whatsapp_triage_item,
    update_access_whatsapp_triage_item,
    verify_access_whatsapp_inbound_token,
)
from app.services.access_control_whatsapp_log import (
    list_conversations as list_whatsapp_conversations,
    list_messages as list_whatsapp_messages,
    log_inbound_message,
    update_message_status,
)
from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug

logger = logging.getLogger("cam-snapshot")

router = APIRouter(prefix="/api/access-control", tags=["access-control"])


def _enqueue_provisioning(person_ids: Iterable[str], *, force: bool = False) -> int:
    """Marca 'pending' cada par (pessoa, dispositivo) que as regras resolvem.

    Nunca chama o dispositivo: so grava o status. Quem realmente fala com a
    catraca e o loop de fundo (`retry_pending_provisions`). Isso e um Global
    Constraint deste plano -- salvar pessoa/grupo/regra nao pode ficar preso no
    timeout de uma catraca offline.

    `force=False` (grupo/grupo de porta/regra): so enfileira o par que ainda nao
    esta 'ok'. Renomear um grupo de 300 alunos nao precisa reprovisionar todo
    mundo -- o dado da pessoa nao mudou, so o caminho de acesso.
    `force=True` (salvar pessoa): reenfileira mesmo se ja estava 'ok', porque os
    dados que vao pra credencial (nome, ativo) acabaram de mudar.
    """
    return enqueue_person_provisioning(list(person_ids), force=force)


def _people_of_door_group(door_group_id: str) -> List[str]:
    """Pessoas que alcancam este grupo de portas por alguma regra ativa."""
    person_ids: List[str] = []
    for rule in list_rules():
        if not rule.get("active"):
            continue
        if str(rule.get("door_group_id") or "") != str(door_group_id):
            continue
        person_ids.extend(list_group_members(str(rule.get("people_group_id") or "")))
    return person_ids


def _provision_summary_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    ok_count = sum(1 for row in rows if str(row.get("status") or "") == "ok")
    failed_count = sum(1 for row in rows if str(row.get("status") or "") == "failed")
    pending_count = sum(1 for row in rows if str(row.get("status") or "") == "pending")
    last_error = next((str(row.get("last_error") or "") for row in rows if row.get("last_error")), "")
    if failed_count:
        status = "failed"
    elif pending_count:
        status = "pending"
    elif total and ok_count == total:
        status = "ok"
    else:
        status = "not_configured"
    return {
        "status": status,
        "total": total,
        "ok": ok_count,
        "pending": pending_count,
        "failed": failed_count,
        "last_error": last_error,
    }


def _provision_summary_for_person(person_id: str) -> Dict[str, Any]:
    return _provision_summary_from_rows(list_provision_status_for_person(person_id))


def _attach_people_provision_summary(people: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped = list_provision_status_for_people([str(person.get("id") or "") for person in people])
    for person in people:
        person["provision_summary"] = _provision_summary_from_rows(grouped.get(str(person.get("id") or ""), []))
    return people


class AccessPersonRequest(BaseModel):
    id: Optional[str] = ""
    full_name: str = Field(min_length=1, max_length=160)
    person_type: str = "student"
    document_id: str = ""
    enrollment_code: str = ""
    class_name: str = ""
    site: str = ""
    controller_user_id: str = ""
    guardian_name: str = ""
    guardian_phone: str = ""
    whatsapp_enabled: bool = True
    active: bool = True
    notes: str = ""


class AccessDeviceRequest(BaseModel):
    id: Optional[str] = ""
    name: str = Field(min_length=1, max_length=160)
    site: str = ""
    vendor: str = "dahua"
    model: str = ""
    host: str = Field(min_length=1)
    connector_id: str = ""
    username: str = "admin"
    password: Optional[str] = ""
    access_direction: str = "entrada"
    active: bool = True


class AccessGroupRequest(BaseModel):
    id: Optional[str] = ""
    name: str = Field(min_length=1, max_length=160)
    site: str = ""
    active: bool = True
    member_ids: List[str] = []


class AccessDoorGroupRequest(BaseModel):
    id: Optional[str] = ""
    name: str = Field(min_length=1, max_length=160)
    site: str = ""
    active: bool = True
    device_ids: List[str] = []


class AccessRuleRequest(BaseModel):
    id: Optional[str] = ""
    people_group_id: str
    door_group_id: str
    name: str = ""
    weekdays: str = "1234567"
    time_start: str = ""
    time_end: str = ""
    active: bool = True


class AccessManualExitRequest(BaseModel):
    person_id: str
    site: str = ""
    reason: str = ""


class AccessWhatsappConfigRequest(BaseModel):
    site: str = ""
    enabled: bool = False
    provider: str = "cloud_api"
    base_url: str = ""
    api_key: str = ""
    # Sem campo `instance`: o nome da instancia do Evolution e derivado do
    # tenant+site no servidor (_evolution_default_instance). Aceitar um nome
    # vindo do corpo deixaria qualquer usuario autenticado operar a instancia
    # de outro cliente -- o container Evolution e a chave de admin sao
    # compartilhados entre todos os tenants da SightOps.


class AccessWhatsappTestRequest(BaseModel):
    number: str
    site: str = ""


class AccessWhatsappDisconnectRequest(BaseModel):
    site: str = ""


class AccessWhatsappInboundSimulateRequest(BaseModel):
    from_number: str
    text: str = ""
    site: str = ""
    photo_base64: str = ""


class AccessWhatsappTriageRequest(BaseModel):
    text: str = ""
    raw_text: str = ""
    site: str = ""
    source_group: str = ""
    source_group_jid: str = ""
    from_number: str = ""
    from_name: str = ""
    photo_url: str = ""
    photo_base64: str = ""
    suggested: Dict[str, Any] = {}
    status: str = ""
    reason: str = ""


@router.get("/summary")
def api_access_control_summary() -> Dict[str, Any]:
    return {"ok": True, "summary": access_control_summary()}


@router.get("/whatsapp")
def api_access_control_whatsapp_get(site: str = Query("")) -> Dict[str, Any]:
    return {"ok": True, **get_access_whatsapp_config(site)}


@router.put("/whatsapp")
def api_access_control_whatsapp_put(req: AccessWhatsappConfigRequest) -> Dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    return {"ok": True, **save_access_whatsapp_config(payload)}


@router.post("/whatsapp/test")
def api_access_control_whatsapp_test(req: AccessWhatsappTestRequest) -> Dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    result = test_access_whatsapp(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Nao foi possivel enviar o teste.")
    return result


@router.get("/whatsapp/connection")
def api_access_control_whatsapp_connection(site: str = Query(""), summary: bool = Query(False), refresh_qr: bool = Query(False)) -> Dict[str, Any]:
    result = get_access_whatsapp_connection(refresh_qr=refresh_qr, site=site, resumo=summary)
    if not result.get("ok") and result.get("state") == "error":
        raise HTTPException(status_code=502, detail=result.get("error") or "Falha ao consultar WhatsApp.")
    return result


@router.post("/whatsapp/disconnect")
def api_access_control_whatsapp_disconnect(req: AccessWhatsappDisconnectRequest) -> Dict[str, Any]:
    result = disconnect_access_whatsapp(req.site)
    if not result.get("ok"):
        # "este site nao usa Evolution" e "Evolution nao configurado nesta
        # plataforma" sao erro de configuracao/pedido, nao falha do gateway:
        # devolver 502 para eles mandava o operador caçar um problema de rede
        # que nao existe. 502 fica so para falha real do Evolution.
        upstream = result.get("state") == "error"
        raise HTTPException(
            status_code=502 if upstream else 400,
            detail=result.get("error") or "Falha ao desconectar WhatsApp.",
        )
    return result


@router.get("/whatsapp/inbound-info")
def api_access_control_whatsapp_inbound_info(request: Request) -> Dict[str, Any]:
    token = ensure_access_whatsapp_inbound_token()
    base_url = str(request.base_url).rstrip("/")
    tenant = str(getattr(request.state, "current_tenant_slug", "") or "").strip().lower()
    webhook_path = f"/api/access-control/whatsapp/inbound/{tenant or 'default'}"
    return {
        "ok": True,
        "webhook_url": f"{base_url}{webhook_path}?token={token}",
        "token_preview": f"{token[:6]}...{token[-4:]}",
        "commands": [
            "cadastro",
            "nome: Maria Silva matricula: 1234 site: RESERVA grupo: GERAL",
            "resumo",
            "confirmar",
            "cancelar",
        ],
    }


@router.get("/whatsapp/conversations")
def api_access_control_whatsapp_conversations() -> Dict[str, Any]:
    return {"ok": True, "conversations": list_whatsapp_conversations()}


@router.get("/whatsapp/conversations/{numero}/messages")
def api_access_control_whatsapp_conversation_messages(numero: str) -> Dict[str, Any]:
    return {"ok": True, "messages": list_whatsapp_messages(numero)}


@router.post("/whatsapp/inbound/simulate")
def api_access_control_whatsapp_inbound_simulate(req: AccessWhatsappInboundSimulateRequest) -> Dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    result = process_access_whatsapp_text(
        payload.get("from_number"),
        payload.get("text"),
        site=payload.get("site"),
        photo_base64=payload.get("photo_base64"),
    )
    return {"ok": bool(result.get("ok")), **result}


@router.get("/whatsapp/triage")
def api_access_control_whatsapp_triage(status: str = Query("")) -> Dict[str, Any]:
    return {"ok": True, **list_access_whatsapp_triage(status)}


@router.post("/whatsapp/triage")
def api_access_control_whatsapp_triage_create(req: AccessWhatsappTriageRequest) -> Dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    item = create_access_whatsapp_triage_item(payload)
    return {"ok": True, "item": item, **list_access_whatsapp_triage()}


@router.put("/whatsapp/triage/{item_id}")
def api_access_control_whatsapp_triage_update(item_id: str, req: AccessWhatsappTriageRequest) -> Dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    try:
        item = update_access_whatsapp_triage_item(item_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "item": item, **list_access_whatsapp_triage()}


@router.post("/whatsapp/triage/{item_id}/approve")
def api_access_control_whatsapp_triage_approve(item_id: str) -> Dict[str, Any]:
    try:
        result = approve_access_whatsapp_triage_item(item_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result, **list_access_whatsapp_triage()}


@router.post("/whatsapp/triage/{item_id}/reject")
def api_access_control_whatsapp_triage_reject(item_id: str, req: AccessWhatsappTriageRequest) -> Dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    try:
        item = reject_access_whatsapp_triage_item(item_id, payload.get("reason"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "item": item, **list_access_whatsapp_triage()}


@router.post("/whatsapp/triage/approve-ready")
def api_access_control_whatsapp_triage_approve_ready() -> Dict[str, Any]:
    return {"ok": True, **approve_ready_access_whatsapp_triage_items(), **list_access_whatsapp_triage()}


@router.get("/whatsapp/meta/{tenant_slug}")
def api_access_control_whatsapp_meta_verify(
    tenant_slug: str,
    hub_mode: str = Query("", alias="hub.mode"),
    hub_challenge: str = Query("", alias="hub.challenge"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
) -> PlainTextResponse:
    """Handshake que a Meta faz ao cadastrar o webhook.

    Ela chama uma vez com hub.challenge e so aceita a URL se receber o mesmo
    valor de volta em texto puro -- JSON aqui faz o cadastro falhar.
    """
    ctx_token = set_current_tenant_slug(tenant_slug)
    try:
        if hub_mode != "subscribe" or not verify_access_whatsapp_inbound_token(hub_verify_token):
            raise HTTPException(status_code=403, detail="Token de verificacao invalido.")
        return PlainTextResponse(hub_challenge)
    finally:
        reset_current_tenant_slug(ctx_token)


@router.post("/whatsapp/meta/{tenant_slug}")
async def api_access_control_whatsapp_meta_webhook(tenant_slug: str, request: Request) -> Dict[str, Any]:
    """Recebe mensagens e confirmacoes de entrega da Cloud API.

    Responde 200 mesmo em erro de conteudo: a Meta reenvia e depois desativa o
    webhook de quem devolve falha, e perder o canal e pior do que perder um
    evento isolado.
    """
    # O corpo cru precisa vir antes do json(): a assinatura e calculada sobre os
    # bytes exatos que a Meta enviou, e reserializar mudaria o resultado.
    corpo = await request.body()

    # O App Secret e um so, do app inteiro, e mora no cliente dono do app --
    # que e o slug da URL. A checagem precisa rodar ANTES de resolver de quem e
    # a mensagem: e ela que decide se o payload merece ser processado. Guardar
    # o segredo por site fazia a verificacao nao achar nada e deixar passar.
    ctx_dono = set_current_tenant_slug(tenant_slug)
    try:
        assinatura_ok = assinatura_webhook_valida(
            corpo, request.headers.get("x-hub-signature-256", "")
        )
    finally:
        reset_current_tenant_slug(ctx_dono)

    if not assinatura_ok:
        logger.warning("Webhook do WhatsApp com assinatura invalida; descartado.")
        # 200 de proposito: erro faz a Meta reenviar e depois desativar o webhook
        return {"ok": True, "ignored": "assinatura invalida"}

    try:
        payload = json.loads(corpo or b"{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # O app da Meta tem um webhook so para todos os clientes. Quem separa um do
    # outro e o numero que recebeu -- o slug da URL vale apenas como reserva.
    numero_id = ""
    for entrada in payload.get("entry") if isinstance(payload.get("entry"), list) else []:
        for mudanca in (entrada or {}).get("changes") or [] if isinstance(entrada, dict) else []:
            valor = (mudanca or {}).get("value") if isinstance(mudanca, dict) else {}
            numero_id = str(((valor or {}).get("metadata") or {}).get("phone_number_id") or "")
            if numero_id:
                break
        if numero_id:
            break

    dono = resolver_cliente_por_numero(numero_id) if numero_id else {}
    if numero_id and not dono:
        logger.warning(
            "Webhook do WhatsApp para numero %s sem cliente configurado; ignorado.", numero_id
        )
        return {"ok": True, "ignored": "numero desconhecido"}

    ctx_token = set_current_tenant_slug(dono.get("tenant") or tenant_slug)
    try:
        for st in extract_meta_statuses(payload):
            if st.get("status") == "failed":
                logger.warning(
                    "WhatsApp nao entregou %s para %s: %s",
                    st.get("message_id"), st.get("recipient"), st.get("error") or "sem motivo informado",
                )
            else:
                logger.info(
                    "WhatsApp %s para %s (%s)",
                    st.get("status"), st.get("recipient"), st.get("message_id"),
                )
            try:
                update_message_status(st.get("message_id"), st.get("status"), error=st.get("error") or "")
            except Exception:
                logger.exception("Falha ao atualizar status de WhatsApp no historico")

        recebidas = extract_meta_inbound(payload)
        if not recebidas.get("from_number"):
            return {"ok": True, "handled": "status"}

        try:
            log_inbound_message(
                recebidas["from_number"], recebidas.get("text") or "",
                from_name=recebidas.get("from_name") or "",
            )
        except Exception:
            logger.exception("Falha ao registrar mensagem recebida do WhatsApp no historico")

        try:
            result = process_access_whatsapp_inbound(payload)
        except Exception as exc:
            logger.exception("Falha ao processar mensagem recebida do WhatsApp: %s", exc)
            return {"ok": False, "error": str(exc)}

        reply = str(result.get("reply") or "")
        if reply:
            send_access_whatsapp_text(
                recebidas["from_number"], reply,
                site=(result.get("draft") or {}).get("site") or dono.get("site", ""),
            )
        return {"ok": True, "handled": "message"}
    finally:
        reset_current_tenant_slug(ctx_token)


@router.post("/whatsapp/inbound/{tenant_slug}")
async def api_access_control_whatsapp_inbound_webhook(
    tenant_slug: str,
    request: Request,
    token: str = Query(""),
    x_sightops_whatsapp_token: str = Header(""),
) -> Dict[str, Any]:
    ctx_token = set_current_tenant_slug(tenant_slug)
    try:
        if not verify_access_whatsapp_inbound_token(token or x_sightops_whatsapp_token):
            raise HTTPException(status_code=403, detail="Token de webhook invalido.")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        result = process_access_whatsapp_inbound(payload if isinstance(payload, dict) else {})
        reply = str(result.get("reply") or "")
        reply_status: Dict[str, Any] = {"ok": False, "status": "not_sent"}
        from_number = str((result.get("draft") or {}).get("from_number") or "")
        if not from_number:
            extracted = payload if isinstance(payload, dict) else {}
            from_number = str(
                extracted.get("from")
                or extracted.get("sender")
                or extracted.get("number")
                or ((extracted.get("data") or {}).get("sender") if isinstance(extracted.get("data"), dict) else "")
                or ""
            )
        if reply and not result.get("ignored") and not result.get("triage") and from_number:
            reply_status = send_access_whatsapp_text(from_number, reply, site=(result.get("draft") or {}).get("site", ""))
        return {"ok": bool(result.get("ok")), "reply": reply, "reply_status": reply_status}
    finally:
        reset_current_tenant_slug(ctx_token)


@router.get("/people")
def api_access_control_people(
    search: str = Query(""),
    active: str = Query(""),
    person_type: str = Query(""),
    site: str = Query(""),
) -> Dict[str, Any]:
    people = list_people(search=search, active=active, person_type=person_type, site=site)
    return {"ok": True, "count": len(people), "people": _attach_people_provision_summary(people)}


@router.get("/people/sites")
def api_access_control_people_sites() -> Dict[str, Any]:
    return {"ok": True, "sites": list_people_sites()}


@router.post("/people")
def api_access_control_save_person(req: AccessPersonRequest) -> Dict[str, Any]:
    try:
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        # Aluno cadastrado pela tela precisa de matricula: e ela que identifica
        # o aluno, evita duplicata e vira o ID dele na controladora. Importacao
        # e rotinas internas nao passam por aqui.
        tipo = str(payload.get("person_type") or "student").lower()
        if tipo == "student" and not str(payload.get("enrollment_code") or "").strip():
            raise HTTPException(status_code=400, detail="Informe a matricula do aluno.")
        # Sem site, o evento nao casa com nenhuma configuracao de notificacao e
        # o aviso ao responsavel some sem erro nenhum.
        if not str(payload.get("site") or "").strip():
            raise HTTPException(status_code=400, detail="Escolha a escola/site da pessoa.")
        person = save_person(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Nao chama o dispositivo aqui -- so marca "pending" e devolve na hora.
    # O loop de fundo (Task 7, `retry_pending_provisions`) e quem realmente
    # fala com a catraca, a cada 60-900s. Chamar o dispositivo de forma
    # sincrona aqui prenderia a resposta HTTP de salvar pessoa ate o
    # timeout se a catraca estiver lenta/offline -- proibido pelo Global
    # Constraint deste plano ("Nenhuma chamada ao dispositivo pode
    # bloquear a resposta HTTP de salvar uma pessoa/regra").
    _enqueue_provisioning([person["id"]], force=True)
    return {"ok": True, "person": person}


@router.post("/people/import")
async def api_access_control_import_people(
    arquivo: UploadFile = File(...),
    site: str = Query(""),
    aplicar: bool = Query(False),
) -> Dict[str, Any]:
    """Importa alunos de planilha (XLSX ou CSV).

    Sem `aplicar=true` faz apenas a analise: devolve o que seria criado,
    atualizado e recusado, sem gravar nada. Gravar so depois que o usuario viu o
    que vai acontecer -- planilha de escola vem com surpresa, e meia importacao
    e pior do que nenhuma.
    """
    from app.services.access_control_import import analisar_planilha, aplicar_planilha

    conteudo = await arquivo.read()
    if not conteudo:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    if len(conteudo) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Arquivo maior que 10 MB.")

    nome = arquivo.filename or ""
    try:
        if aplicar:
            resultado = aplicar_planilha(conteudo, site=site, nome_arquivo=nome)
            criados = [p["id"] for p in list_people()
                       if str(p.get("enrollment_code") or "").strip()]
            _enqueue_provisioning(criados, force=False)
            return resultado
        return analisar_planilha(conteudo, site=site, nome_arquivo=nome)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/people/{person_id}")
def api_access_control_delete_person(person_id: str) -> Dict[str, Any]:
    removed = delete_person(person_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Pessoa nao encontrada neste cliente.")
    return {"ok": True, "removed": True}


@router.post("/people/{person_id}/face-photo")
async def api_access_control_person_face_photo(
    person_id: str,
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    content_type = str(file.content_type or "").lower()
    if content_type and content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Envie uma foto JPG, PNG ou WebP.")
    try:
        result = save_person_face_photo(person_id, await file.read())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _enqueue_provisioning([result["person"]["id"]], force=True)
    return {"ok": True, **result}


@router.get("/people/{person_id}/face-photo")
def api_access_control_person_face_photo_get(person_id: str) -> Response:
    people = list_people()
    person = next((p for p in people if p["id"] == person_id), None)
    if not person:
        raise HTTPException(status_code=404, detail="Pessoa nao encontrada neste cliente.")
    photo = load_person_face_photo(person)
    if photo is None:
        raise HTTPException(status_code=404, detail="Foto facial nao encontrada.")
    return Response(
        content=photo,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.post("/people/{person_id}/sync")
def api_access_control_sync_person(person_id: str) -> Dict[str, Any]:
    people = list_people()
    person = next((p for p in people if p["id"] == person_id), None)
    if not person:
        raise HTTPException(status_code=404, detail="Pessoa nao encontrada neste cliente.")
    result = provision_person_everywhere(person)
    return {"ok": True, **result, "provision_summary": _provision_summary_for_person(person_id)}


@router.get("/devices")
def api_access_control_devices() -> Dict[str, Any]:
    return {"ok": True, "devices": list_devices()}


@router.post("/devices")
def api_access_control_save_device(req: AccessDeviceRequest) -> Dict[str, Any]:
    try:
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        device = save_device(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "device": device}


@router.post("/devices/{device_id}/test")
def api_access_control_test_device(device_id: str) -> Dict[str, Any]:
    from app.services.access_control_store import get_device_with_password

    device = get_device_with_password(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Dispositivo nao encontrado neste cliente.")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        info = device_get_system_info(device)
    except HTTPException:
        try:
            update_device_health(device_id, status="offline", last_seen_at=now)
        except ValueError:
            pass
        raise
    model = str(info.get("updateSerial") or info.get("deviceType") or device.get("model") or "").strip()
    updated = update_device_health(device_id, status="online", model=model, last_seen_at=now)
    return {"ok": True, "device": updated, "info": info}


@router.delete("/devices/{device_id}")
def api_access_control_delete_device(device_id: str) -> Dict[str, Any]:
    removed = delete_device(device_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Dispositivo nao encontrado neste cliente.")
    return {"ok": True, "removed": True}


@router.post("/devices/{device_id}/open-door")
def api_access_control_open_door(
    device_id: str,
    channel: int = 1,
    user: Dict[str, Any] = Depends(current_user),
) -> Dict[str, Any]:
    """Abre a porta remotamente.

    Exige usuario identificado: abrir porta e ato de seguranca fisica e tem de
    ficar registrado com nome. Antes a rota nao recebia o usuario -- nem para
    autorizar, nem para o log."""
    from app.services.access_control_store import get_device_with_password

    device = get_device_with_password(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Dispositivo nao encontrado neste cliente.")
    result = device_open_door(device, channel=channel)
    logging.getLogger("cam-snapshot").info(
        "abertura remota de porta: device=%s canal=%s por=%s (%s)",
        device_id, channel, user.get("username"), user.get("role"),
    )
    return {"ok": True, **result}


@router.post("/devices/{device_id}/import-people")
def api_access_control_import_device_people(device_id: str) -> Dict[str, Any]:
    from app.services.access_control_store import get_device_with_password

    device = get_device_with_password(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Dispositivo nao encontrado neste cliente.")
    controller_people = device_list_controller_people(device)
    existing_by_controller_id = {
        str(person.get("controller_user_id") or ""): person
        for person in list_people()
        if str(person.get("controller_user_id") or "")
    }
    imported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    photos_imported = 0
    photos_missing = 0
    photo_errors: List[Dict[str, str]] = []
    site = str(device.get("site") or "").strip()
    device_prefix = "".join(ch for ch in str(device.get("id") or device_id) if ch.isalnum())[:16]
    for controller_person in controller_people:
        controller_user_id = str(controller_person.get("controller_user_id") or "").strip()
        full_name = str(controller_person.get("full_name") or "").strip()
        if not controller_user_id or not full_name:
            skipped.append(controller_person)
            continue
        existing = existing_by_controller_id.get(controller_user_id)
        person = save_person(
            {
                "id": existing["id"] if existing else f"ctl-{device_prefix}-{controller_user_id}",
                "full_name": full_name,
                "person_type": existing.get("person_type", "student") if existing else "student",
                "document_id": controller_person.get("document_id", ""),
                "enrollment_code": existing.get("enrollment_code", "") if existing else controller_user_id,
                "class_name": existing.get("class_name", "") if existing else "",
                "site": existing.get("site", "") or site if existing else site,
                "controller_user_id": controller_user_id,
                "guardian_name": existing.get("guardian_name", "") if existing else "",
                "guardian_phone": existing.get("guardian_phone", "") if existing else "",
                "whatsapp_enabled": existing.get("whatsapp_enabled", True) if existing else True,
                "active": True,
                "notes": existing.get("notes", "") if existing else f"Importado da controladora {device.get('name') or device_id}.",
            }
        )
        if not str(person.get("face_photo_path") or "").strip():
            try:
                photo = device_get_controller_person_photo(device, controller_user_id)
            except HTTPException as exc:
                photo_errors.append({"controller_user_id": controller_user_id, "error": str(exc.detail)})
                photo = None
            if photo:
                try:
                    photo_result = save_person_face_photo(person["id"], photo)
                    person = photo_result["person"]
                    photos_imported += 1
                except ValueError as exc:
                    photo_errors.append({"controller_user_id": controller_user_id, "error": str(exc)})
            else:
                photos_missing += 1
        existing_by_controller_id[controller_user_id] = person
        imported.append(person)
    return {
        "ok": True,
        "device_id": device_id,
        "device_name": device.get("name", ""),
        "read": len(controller_people),
        "imported": len(imported),
        "skipped": len(skipped),
        "photos_imported": photos_imported,
        "photos_missing": photos_missing,
        "photo_errors": photo_errors,
        "people": imported,
    }


@router.get("/groups")
def api_access_control_groups() -> Dict[str, Any]:
    groups = list_groups()
    for group in groups:
        group["member_ids"] = list_group_members(group["id"])
    return {"ok": True, "groups": groups}


@router.post("/groups")
def api_access_control_save_group(req: AccessGroupRequest) -> Dict[str, Any]:
    try:
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        member_ids = payload.pop("member_ids", [])
        group = save_group(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    set_group_members(group["id"], member_ids)
    group["member_ids"] = member_ids
    # A spec manda enfileirar provisionamento "ao salvar uma pessoa, ou mudar
    # membership de grupo, ou criar/editar uma regra". Sem este passo, a ordem
    # normal de implantacao (cria pessoas -> cria grupo -> cria grupo de porta
    # -> cria regra) nao gerava nenhuma linha pendente e o loop de fundo nunca
    # provisionava ninguem: retry_pending_provisions() so reprocessa linhas que
    # ja existem, nunca descobre pares (pessoa, dispositivo) novos sozinho.
    _enqueue_provisioning(member_ids)
    return {"ok": True, "group": group}


@router.delete("/groups/{group_id}")
def api_access_control_delete_group(group_id: str) -> Dict[str, Any]:
    removed = delete_group(group_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Grupo nao encontrado neste cliente.")
    return {"ok": True, "removed": True}


@router.get("/door-groups")
def api_access_control_door_groups() -> Dict[str, Any]:
    from app.services.access_control_store import list_door_group_members

    door_groups = list_door_groups()
    for door_group in door_groups:
        door_group["device_ids"] = list_door_group_members(door_group["id"])
    return {"ok": True, "door_groups": door_groups}


@router.post("/door-groups")
def api_access_control_save_door_group(req: AccessDoorGroupRequest) -> Dict[str, Any]:
    try:
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        device_ids = payload.pop("device_ids", [])
        door_group = save_door_group(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    set_door_group_members(door_group["id"], device_ids)
    door_group["device_ids"] = device_ids
    # Mudou o conjunto de portas: toda pessoa que chega neste grupo de portas
    # por alguma regra ativa pode ter ganhado um dispositivo novo.
    _enqueue_provisioning(_people_of_door_group(door_group["id"]))
    return {"ok": True, "door_group": door_group}


@router.delete("/door-groups/{door_group_id}")
def api_access_control_delete_door_group(door_group_id: str) -> Dict[str, Any]:
    removed = delete_door_group(door_group_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Grupo de portas nao encontrado neste cliente.")
    return {"ok": True, "removed": True}


@router.get("/rules")
def api_access_control_rules() -> Dict[str, Any]:
    return {"ok": True, "rules": list_rules()}


@router.post("/rules")
def api_access_control_save_rule(req: AccessRuleRequest) -> Dict[str, Any]:
    try:
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        rule = save_rule(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # A regra e o que liga grupo de pessoas a grupo de portas -- criar/reativar
    # uma regra costuma ser o passo que finalmente da acesso a um dispositivo.
    if rule.get("active"):
        _enqueue_provisioning(list_group_members(str(rule.get("people_group_id") or "")))
    return {"ok": True, "rule": rule}


@router.delete("/rules/{rule_id}")
def api_access_control_delete_rule(rule_id: str) -> Dict[str, Any]:
    removed = delete_rule(rule_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Regra nao encontrada neste cliente.")
    return {"ok": True, "removed": True}


@router.get("/events")
def api_access_control_events(
    person_id: str = Query(""),
    site: str = Query(""),
    limit: int = Query(200),
) -> Dict[str, Any]:
    events = list_events(person_id=person_id, site=site, limit=limit)
    return {"ok": True, "count": len(events), "events": events}


@router.get("/reports/summary")
def api_access_control_report_summary(
    period: str = Query("today"),
    start: str = Query(""),
    end: str = Query(""),
    type: str = Query(""),
    site: str = Query(""),
    search: str = Query(""),
    device_id: str = Query(""),
    door_group_id: str = Query(""),
) -> Dict[str, Any]:
    filters = {
        "period": period,
        "start": start,
        "end": end,
        "type": type,
        "site": site,
        "search": search,
        "device_id": device_id,
        "door_group_id": door_group_id,
    }
    return {"ok": True, "summary": access_report_summary(filters)}


@router.get("/reports/events")
def api_access_control_report_events(
    period: str = Query("today"),
    start: str = Query(""),
    end: str = Query(""),
    type: str = Query(""),
    site: str = Query(""),
    search: str = Query(""),
    device_id: str = Query(""),
    door_group_id: str = Query(""),
    limit: int = Query(300),
) -> Dict[str, Any]:
    filters = {
        "period": period,
        "start": start,
        "end": end,
        "type": type,
        "site": site,
        "search": search,
        "device_id": device_id,
        "door_group_id": door_group_id,
        "limit": limit,
    }
    events = list_access_report_events(filters)
    return {"ok": True, "count": len(events), "events": events}


@router.get("/reports/present")
def api_access_control_report_present(
    site: str = Query(""),
    device_id: str = Query(""),
    door_group_id: str = Query(""),
) -> Dict[str, Any]:
    people = access_present_people(site=site, device_id=device_id, door_group_id=door_group_id)
    return {"ok": True, "count": len(people), "people": people}


@router.get("/live/stream")
async def api_access_control_live_stream(
    request: Request,
    type: str = Query(""),
    site: str = Query(""),
    device_id: str = Query(""),
    door_group_id: str = Query(""),
) -> StreamingResponse:
    filters = {
        "period": "today",
        "type": type,
        "site": site,
        "device_id": device_id,
        "door_group_id": door_group_id,
        "limit": 1,
    }

    async def stream():
        last_event_id = ""
        last_ping = datetime.now(timezone.utc)
        initial = await asyncio.to_thread(list_access_report_events, filters)
        if initial:
            last_event_id = str(initial[0].get("id") or "")
        yield "event: ready\ndata: {}\n\n"
        while True:
            if await request.is_disconnected():
                break
            events = await asyncio.to_thread(list_access_report_events, filters)
            latest = events[0] if events else None
            latest_id = str((latest or {}).get("id") or "")
            if latest and latest_id and latest_id != last_event_id:
                last_event_id = latest_id
                payload = json.dumps({"event": latest}, ensure_ascii=False, default=str)
                yield f"event: access-event\ndata: {payload}\n\n"
            now = datetime.now(timezone.utc)
            if (now - last_ping).total_seconds() >= 20:
                last_ping = now
                yield f"event: ping\ndata: {json.dumps({'ts': now.isoformat(timespec='seconds')})}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/reports/manual-exit")
def api_access_control_manual_exit(req: AccessManualExitRequest, request: Request) -> Dict[str, Any]:
    try:
        user = getattr(request.state, "user", {}) or {}
        operator_user = str(user.get("username") or user.get("sub") or "")
        event = record_manual_exit(req.person_id, site=req.site, reason=req.reason, operator_user=operator_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "event": event}
