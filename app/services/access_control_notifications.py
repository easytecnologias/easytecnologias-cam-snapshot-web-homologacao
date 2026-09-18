from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import re
from typing import Any, Dict, List

import requests

from app.core.crypto import decrypt, encrypt
from app.core.tenant_context import get_current_tenant_slug
from app.services import db_store
from app.services.access_control_whatsapp_log import log_outbound_message

logger = logging.getLogger("cam-snapshot")


def _log_whatsapp_send(
    numero: Any, body: str, status: str, *,
    wa_message_id: str = "", template_name: str = "", error: str = "",
) -> None:
    """Registra a tentativa de envio pra tela Messenger -- nunca deixa uma
    falha de log derrubar o envio de verdade em si."""
    try:
        log_outbound_message(numero, body, status=status, wa_message_id=wa_message_id,
                              template_name=template_name, error=error)
    except Exception:
        logger.exception("Falha ao registrar mensagem de WhatsApp no historico")


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _event_label(event_type: Any) -> str:
    clean = _text(event_type, 32).lower()
    if clean == "saida_manual":
        return "SAIDA MANUAL"
    if clean == "saida":
        return "SAIDA"
    return "ENTRADA"


def _event_context(event: Dict[str, Any]) -> Dict[str, Any]:
    tenant = db_store._current_tenant_slug()
    event_id = _text(event.get("id"), 80)
    person_id = _text(event.get("person_id"), 80)
    device_id = _text(event.get("device_id"), 80)
    context: Dict[str, Any] = dict(event)
    with db_store._conn() as c:
        if event_id:
            row = c.execute(
                """
                SELECT * FROM access_events
                WHERE tenant_slug=? AND id=?
                """,
                (tenant, event_id),
            ).fetchone()
            if row:
                context.update(dict(row))
        if person_id:
            person = c.execute(
                """
                SELECT full_name, person_type, class_name, site, guardian_name,
                       guardian_phone, whatsapp_enabled
                FROM access_people
                WHERE tenant_slug=? AND id=?
                """,
                (tenant, person_id),
            ).fetchone()
            if person:
                data = dict(person)
                context["person_name"] = data.get("full_name") or context.get("person_name_raw", "")
                context["person_type"] = data.get("person_type", "")
                context["class_name"] = data.get("class_name", "")
                context["guardian_name"] = data.get("guardian_name", "")
                context["guardian_phone"] = data.get("guardian_phone", "")
                context["whatsapp_enabled"] = bool(int(data.get("whatsapp_enabled") or 0))
                if not _text(context.get("site"), 120):
                    context["site"] = data.get("site", "")
        if device_id:
            device = c.execute(
                """
                SELECT name, site, access_direction
                FROM access_devices
                WHERE tenant_slug=? AND id=?
                """,
                (tenant, device_id),
            ).fetchone()
            if device:
                data = dict(device)
                context["device_name"] = context.get("device_name") or data.get("name", "")
                context["device_role"] = context.get("device_role") or data.get("access_direction", "")
                if not _text(context.get("site"), 120):
                    context["site"] = data.get("site", "")
    return context


def _build_message(event: Dict[str, Any]) -> str:
    label = _event_label(event.get("event_type"))
    person = _text(event.get("person_name") or event.get("person_name_raw"), 160) or "Pessoa nao identificada"
    site = _text(event.get("site"), 120) or "--"
    device = _text(event.get("device_name"), 160) or "--"
    occurred_at = _text(event.get("occurred_at"), 40) or "--"
    class_name = _text(event.get("class_name"), 80)
    lines = [
        f"{label} - Controle de Acesso",
        f"Pessoa: {person}",
    ]
    if class_name:
        lines.append(f"Turma: {class_name}")
    lines.extend(
        [
            f"Site: {site}",
            f"Dispositivo: {device}",
            f"Horario: {occurred_at}",
        ]
    )
    return "\n".join(lines)


def _build_whatsapp_message(event: Dict[str, Any]) -> str:
    """Texto enviado ao responsavel pelo WhatsApp -- mesma redacao do template
    aprovado na Meta (aviso_acesso_aluno), usado aqui so pelo Evolution (a
    Cloud API manda pelo template dela, ja aprovado, sem passar por texto
    livre). Mantem os dois provedores com a mesma cara pro responsavel, seja
    qual for o canal ativo no site.
    """
    label = _event_label(event.get("event_type"))
    person = _text(event.get("person_name") or event.get("person_name_raw"), 160) or "Pessoa nao identificada"
    site = _text(event.get("site"), 120) or "--"
    occurred_at = _text(event.get("occurred_at"), 40) or "--"
    return (
        "Aviso do controle de acesso.\n\n"
        f"Evento: {label}\n"
        f"Escola: {site}\n"
        f"Aluno: {person}\n"
        f"Horário: {occurred_at}\n\n"
        "Mensagem automática Colegio + Seguro\n"
        "Não precisa responder"
    )


def _whatsapp_target(phone: Any) -> str:
    raw = _text(phone, 32)
    if raw.startswith("+"):
        return "+" + _digits(raw)
    digits = _digits(raw)
    if not digits:
        return ""
    if digits.startswith("55"):
        return f"+{digits}"
    if len(digits) in {10, 11}:
        return f"+55{digits}"
    return f"+{digits}"


def _numero_whatsapp(phone: Any) -> str:
    return _digits(_whatsapp_target(phone))


def _whatsapp_provider(cfg: Dict[str, Any]) -> str:
    """Provider ativo do site: 'evolution' so quando escolhido explicitamente.

    Qualquer outra coisa (vazio, desconhecido, configuracao antiga sem o
    campo) cai em 'cloud_api' -- o canal oficial nunca muda de comportamento
    sem o usuario decidir isso na tela.
    """
    provider = _text(cfg.get("provider"), 40).lower()
    return "evolution" if provider == "evolution" else "cloud_api"


def _slug(text: Any) -> str:
    s = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(text or "").strip())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _evolution_platform_cfg() -> Dict[str, str]:
    """Endereco e chave do Evolution: infraestrutura da SightOps, um container
    so para todos os clientes -- por isso vem de variavel de ambiente, no
    mesmo padrao ja usado para o Zabbix (_default_zabbix_cfg em
    zabbix_monitoring_service.py), nunca de configuracao por site.

    So a URL tem valor padrao no codigo (endereco ja confirmado alcancavel a
    partir deste container); a chave nunca tem default aqui -- sem ela, o
    provider fica not_configured em vez de tentar falar com um Evolution
    sem autenticacao.
    """
    return {
        "base_url": _text(os.getenv("SIGHTOPS_EVOLUTION_URL") or "http://10.10.12.7:8090", 500).rstrip("/"),
        "api_key": _text(os.getenv("SIGHTOPS_EVOLUTION_API_KEY"), 300),
    }


def _evolution_default_instance(site_key: Any = "") -> str:
    """Nome de instancia unico por tenant+site.

    O container Evolution e compartilhado entre todos os clientes da
    SightOps: um default fixo tipo "sightops" colidiria entre escolas de
    tenants diferentes (ou entre sites do mesmo tenant). O nome derivado do
    tenant+site evita isso sem exigir que o usuario digite nada.
    """
    tenant = _slug(get_current_tenant_slug()) or "sightops"
    parte = _slug(site_key) or "padrao"
    return f"{tenant}-{parte}"[:120]


def _site_key(site: Any) -> str:
    return _text(site, 120)


def _access_whatsapp_site_configs(settings: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = settings.get("access_control_whatsapp_notifications_by_site") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _access_whatsapp_cfg(settings: Dict[str, Any], site: Any = "") -> Dict[str, Any]:
    global_cfg = settings.get("access_control_whatsapp_notifications") or {}
    if not isinstance(global_cfg, dict):
        global_cfg = {}
    key = _site_key(site)
    site_cfg = _access_whatsapp_site_configs(settings).get(key) if key else None
    if not site_cfg:
        return dict(global_cfg)
    merged = dict(global_cfg)
    merged.update(site_cfg)
    # Escolher o canal e decisao manual POR SITE (constraint da spec: nunca
    # herdada, nunca automatica). Herdar `provider` do canal padrao do cliente
    # faria com que por o padrao em Evolution jogasse junto todo site que
    # nunca escolheu provedor nenhum -- inclusive os que estao enviando pela
    # Meta com credencial propria. Um site com config propria e sem
    # `provider` volta ao default seguro do _whatsapp_provider (cloud_api).
    # `instance` sai junto por ser campo do mesmo par (e hoje so historico).
    # Os demais campos continuam herdando: um site sem credencial Meta
    # propria usar o token do cliente e o comportamento legitimo de sempre.
    for campo in ("provider", "instance", "instance_name"):
        if campo not in site_cfg:
            merged.pop(campo, None)
    return merged


def _send_telegram(settings: Dict[str, Any], message: str) -> str:
    cfg = settings.get("telegram_notifications") or {}
    if not cfg.get("enabled"):
        return "telegram_skipped"
    token = _text(cfg.get("bot_token"), 200)
    chat_id = _text(cfg.get("chat_id"), 120)
    if not token or not chat_id:
        return "telegram_skipped"
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": html.escape(message),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    data = response.json() if response.content else {}
    if response.ok and data.get("ok"):
        return "telegram_sent"
    return "telegram_failed"


_GRAPH_VERSION = "v21.0"


def _cifrar_token(novo: Any, antigo: Any) -> str:
    """Guarda o token cifrado, preservando o atual quando o campo vem vazio."""
    texto = _text(novo, 900)
    if texto:
        return encrypt(texto)
    return _text(antigo, 900)      # ja esta cifrado no que veio do banco


def assinatura_webhook_valida(corpo: bytes, cabecalho: str, site: Any = "") -> bool:
    """Confere a assinatura que a Meta poe em cada POST do webhook.

    Sem isso, qualquer um com a URL forja mensagem recebida: injeta item de
    triagem no cliente e faz o sistema responder para um numero escolhido por
    ele -- gastando o saldo e queimando a reputacao do numero da escola.

    Sem App Secret configurado a checagem nao roda e o webhook segue aberto;
    por isso o aviso e de nivel WARNING e nomeia o que fazer.
    """
    segredo = decrypt(_text(_access_whatsapp_cfg(db_store.load_app_settings(), site).get("app_secret"), 900))
    if not segredo:
        logger.warning(
            "Webhook do WhatsApp sem App Secret configurado: a assinatura nao esta "
            "sendo conferida e qualquer um com a URL pode forjar mensagens."
        )
        return True
    esperado = hmac.new(segredo.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    recebido = _text(cabecalho, 200).replace("sha256=", "").strip()
    return hmac.compare_digest(esperado, recebido)


def _cloud_cfg(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Credenciais da Cloud API oficial da Meta."""
    return {
        # Sem heranca de instance/api_key: sao campos de outro provedor e faziam
        # uma configuracao velha da Evolution passar por configuracao da Meta.
        "phone_number_id": _text(cfg.get("phone_number_id"), 60),
        # Token permanente da Meta: nao expira e envia mensagem em nome da
        # escola. Fica cifrado como as senhas de OLT/camera. decrypt() devolve
        # como veio o que foi gravado antes desta camada, entao token antigo
        # continua funcionando.
        "access_token": decrypt(_text(cfg.get("access_token"), 900)),
        "template_name": _text(cfg.get("template_name"), 120),
        "template_language": _text(cfg.get("template_language") or "pt_BR", 12),
    }


def _cloud_template_params(event: Dict[str, Any], message: str) -> list[Dict[str, str]]:
    """Variaveis do template, na ordem em que foram cadastradas na Meta.

    A Cloud API nao aceita texto livre em mensagem iniciada pela empresa: o corpo
    e um template aprovado com lacunas. Por isso o evento e desmontado em campos
    em vez de mandar a frase pronta que o Telegram recebe.
    """
    return [
        {"type": "text", "text": _event_label(event.get("event_type")) or "Acesso"},
        {"type": "text", "text": _text(event.get("site"), 60) or "-"},
        {"type": "text", "text": _text(event.get("person_name") or event.get("person_name_raw"), 60) or "-"},
        {"type": "text", "text": _text(event.get("occurred_at"), 40) or "-"},
    ]


def _send_whatsapp_cloud(cfg: Dict[str, Any], event: Dict[str, Any], message: str) -> str:
    """Envia pela API oficial da Meta.

    Diferente dos provedores nao oficiais, aqui a resposta diz o que aconteceu:
    'accepted' com o wamid, ou um objeto de erro com motivo. Falha nao vira
    silencio -- vai para o log com a mensagem que a Meta devolveu.
    """
    dados = _cloud_cfg(cfg)
    numero = _numero_whatsapp(event.get("guardian_phone"))
    if not dados["phone_number_id"] or not dados["access_token"] or not numero:
        return "whatsapp_skipped"
    if event.get("whatsapp_enabled") is False:
        return "whatsapp_skipped"

    if dados["template_name"]:
        corpo = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "template",
            "template": {
                "name": dados["template_name"],
                "language": {"code": dados["template_language"]},
                "components": [{"type": "body", "parameters": _cloud_template_params(event, message)}],
            },
        }
    else:
        # Sem template proprio cadastrado ainda: usa o de exemplo da Meta, que
        # existe em toda conta nova. Serve para validar a ligacao ponta a ponta.
        corpo = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "template",
            "template": {"name": "hello_world", "language": {"code": "en_US"}},
        }

    response = requests.post(
        f"https://graph.facebook.com/{_GRAPH_VERSION}/{dados['phone_number_id']}/messages",
        headers={"Authorization": f"Bearer {dados['access_token']}", "Content-Type": "application/json"},
        json=corpo,
        timeout=25,
    )
    payload = _resposta_json(response)
    wamid = ((payload.get("messages") or [{}])[0] or {}).get("id") or ""
    if 200 <= int(response.status_code or 0) < 300:
        estado = ((payload.get("messages") or [{}])[0] or {}).get("message_status") or "accepted"
        if estado in {"accepted", "sent", "delivered", "read"}:
            _log_whatsapp_send(numero, message, "whatsapp_sent", wa_message_id=wamid,
                                template_name=dados["template_name"])
            return "whatsapp_sent"
        logger.warning("Cloud API devolveu estado inesperado: %s", estado)
        _log_whatsapp_send(numero, message, "whatsapp_failed", wa_message_id=wamid,
                            template_name=dados["template_name"], error=f"estado inesperado: {estado}")
        return "whatsapp_failed"
    erro = payload.get("error") or {}
    logger.warning(
        "Cloud API recusou a mensagem (HTTP %s): %s | codigo %s",
        response.status_code,
        erro.get("message") or response.text[:200],
        erro.get("code"),
    )
    _log_whatsapp_send(numero, message, "whatsapp_failed", template_name=dados["template_name"],
                        error=str(erro.get("message") or response.text[:200]))
    return "whatsapp_failed"


def _evolution_instance_cfg(cfg: Dict[str, Any], site_key: Any = "") -> Dict[str, str]:
    """Credenciais + instancia prontas para chamar o Evolution deste site.

    base_url/api_key sao sempre da plataforma (_evolution_platform_cfg),
    nunca do que foi salvo por site -- mesmo que a configuracao antiga do
    site tenha algo gravado ali.

    O nome da instancia tambem NAO vem mais do que foi salvo: e sempre
    derivado do tenant+site aqui no servidor. Dois motivos, ambos de
    isolamento entre clientes no container Evolution compartilhado:

    1. Toda configuracao salva antes desta feature tem instance="sightops"
       gravado (era o default do modelo antigo, inclusive em configuracoes
       puramente cloud_api). Como "sightops" e uma string truthy, um
       fallback do tipo `cfg.get("instance") or default` nunca chegava no
       default seguro: os dois primeiros clientes a migrar um site para
       Evolution cairiam na MESMA instancia e derrubariam a sessao um do
       outro.
    2. O valor salvo veio de um corpo de requisicao (PUT /whatsapp) sem
       validacao nenhuma. Com a chave de admin da plataforma nas maos do
       backend, um instance escolhido pelo cliente permitiria falar com a
       instancia de OUTRO tenant (o nome e deduzivel: {slug}-{site}):
       mandar mensagem pela sessao alheia, pegar o QR de pareamento dela
       ou desconecta-la.

    O que ficou gravado em `instance` segue no banco por historico, mas
    nunca mais e usado para montar URL do Evolution.
    """
    plataforma = _evolution_platform_cfg()
    return {
        "base_url": plataforma["base_url"],
        "api_key": plataforma["api_key"],
        "instance": _evolution_default_instance(site_key),
    }


def _evolution_headers(conn: Dict[str, str]) -> Dict[str, str]:
    return {"apikey": conn["api_key"], "Content-Type": "application/json"}


def _evolution_state_label(data: Dict[str, Any]) -> str:
    """Traduz o estado bruto do Evolution para um vocabulario fixo.

    So 'open'/'connected'/'online' vira 'connected'. Qualquer outra coisa
    -- inclusive vazio -- fica como offline/desconhecido: a instancia
    orfa "presidente-dutra" ficou dias em 'connecting' com a sessao morta,
    e uma leitura otimista teria mostrado ela como conectada.
    """
    raw = (
        data.get("state") or data.get("status")
        or (data.get("instance") or {}).get("state")
        or (data.get("instance") or {}).get("status") or ""
    )
    state = _text(raw, 40).lower()
    if state in {"open", "connected", "online"}:
        return "connected"
    if state in {"connecting", "qrcode", "qr", "pairing"}:
        return "waiting_qr"
    if state in {"close", "closed", "disconnected", "offline"}:
        return "disconnected"
    # Estado desconhecido nao passa como texto cru: a tela tem tratamento
    # so para o vocabulario fixo e mostraria o rotulo do Evolution sem
    # traducao, do lado de um "conectado?" que ninguem sabe responder.
    return "unknown"


def _evolution_qrcode_base64(data: Dict[str, Any]) -> str:
    raw = data.get("base64") or (data.get("qrcode") or {}).get("base64") or ""
    return _text(raw, 200000)


def _send_whatsapp_evolution(cfg: Dict[str, Any], event: Dict[str, Any], message: str, *, site_key: Any = "") -> str:
    """Envia pelo Evolution (WhatsApp Web nao oficial, biblioteca Baileys).

    Aceita 2xx como sucesso, igual a versao anterior a remocao -- confirmar
    entrega mensagem-a-mensagem fica fora de escopo (spec, secao "Fora de
    escopo"); o que corrige o bug historico e a checagem de saude ativa em
    get_access_whatsapp_connection(), nao o envio em si.
    """
    conn = _evolution_instance_cfg(cfg, site_key)
    if not conn["base_url"] or not conn["api_key"] or not conn["instance"]:
        return "whatsapp_skipped"
    numero = _numero_whatsapp(event.get("guardian_phone"))
    if not numero:
        return "whatsapp_skipped"
    if event.get("whatsapp_enabled") is False:
        return "whatsapp_skipped"
    response = requests.post(
        f"{conn['base_url']}/message/sendText/{conn['instance']}",
        headers=_evolution_headers(conn),
        json={"number": numero, "text": message, "linkPreview": False},
        timeout=20,
    )
    if 200 <= int(response.status_code or 0) < 300:
        _log_whatsapp_send(numero, message, "whatsapp_sent")
        return "whatsapp_sent"
    logger.warning(
        "Evolution recusou a mensagem (HTTP %s): %s",
        response.status_code,
        getattr(response, "text", "")[:200],
    )
    _log_whatsapp_send(numero, message, "whatsapp_failed",
                        error=f"HTTP {response.status_code}: {getattr(response, 'text', '')[:200]}")
    return "whatsapp_failed"


def _evolution_connection_status(
    cfg: Dict[str, Any], site_key: Any, *, refresh_qr: bool = False, probe_only: bool = False
) -> Dict[str, Any]:
    """Estado da sessao Evolution deste site.

    `probe_only=True` = so observa, nao mexe. Criar instancia e pedir QR sao
    efeitos colaterais legitimos quando um humano clicou em "Verificar
    conexao", mas essa mesma funcao e chamada a cada ciclo do monitoramento
    (list_access_whatsapp_channels -> monitoring_service.refresh_from_inventory,
    ~2 min, por tenant e por site, para sempre). Sem esse freio, um canal
    desconectado ganhava um QR Code novo a cada dois minutos, sem ninguem
    olhando -- e o monitoramento passava a criar no Evolution instancias que
    nao existiam. Checagem de saude nao altera o que observa.
    """
    conn = _evolution_instance_cfg(cfg, site_key)
    if not conn["base_url"] or not conn["api_key"]:
        return {
            "ok": False, "configured": False, "state": "not_configured", "connected": False,
            "qrcode": "", "external_connection": False, "site": _site_key(site_key), "provider": "evolution",
            "error": "Evolution API nao configurado nesta plataforma (SIGHTOPS_EVOLUTION_URL/SIGHTOPS_EVOLUTION_API_KEY).",
        }
    headers = _evolution_headers(conn)
    resultado: Dict[str, Any] = {
        "ok": True, "configured": True, "connected": False, "state": "unknown",
        "qrcode": "", "external_connection": False, "site": _site_key(site_key),
        "instance": conn["instance"], "provider": "evolution",
    }
    try:
        state_response = requests.get(f"{conn['base_url']}/instance/connectionState/{conn['instance']}", headers=headers, timeout=15)
    except Exception as exc:
        return {**resultado, "ok": False, "state": "error", "error": str(exc)}

    if state_response.status_code == 404:
        if probe_only:
            # Instancia ainda nao existe no Evolution: para quem so observa,
            # isso e um canal fora do ar -- criar aqui seria o monitoramento
            # provisionando sessao sozinho.
            return {**resultado, "state": "disconnected", "connected": False}
        try:
            create_response = requests.post(
                f"{conn['base_url']}/instance/create", headers=headers,
                json={"instanceName": conn["instance"], "integration": "WHATSAPP-BAILEYS", "qrcode": True},
                timeout=25,
            )
        except Exception as exc:
            return {**resultado, "ok": False, "state": "error", "error": str(exc)}
        create_data = _resposta_json(create_response)
        estado = _evolution_state_label(create_data)
        resultado.update({"state": estado, "connected": estado == "connected", "qrcode": _evolution_qrcode_base64(create_data)})
        return resultado

    if int(state_response.status_code or 0) >= 400:
        # 401 de chave errada, 500 do container: sem isso caia em {} -> estado
        # "unknown" com ok=True, e a tela dizia "Desconhecido" para o que na
        # verdade e Evolution mal configurado.
        detalhe = getattr(state_response, "text", "")[:200]
        return {
            **resultado, "ok": False, "state": "error",
            "error": f"Evolution respondeu HTTP {state_response.status_code}: {detalhe or 'sem detalhe'}",
        }

    state_data = _resposta_json(state_response)
    estado = _evolution_state_label(state_data)
    # so estado genuinamente "open" conta como conectado -- ver docstring de
    # _evolution_state_label. Isso e a verificacao de saude ativa do design.
    resultado.update({"state": estado, "connected": estado == "connected"})

    if probe_only:
        return resultado

    if refresh_qr or not resultado["connected"]:
        try:
            qr_response = requests.get(f"{conn['base_url']}/instance/connect/{conn['instance']}", headers=headers, timeout=25)
            qr_data = _resposta_json(qr_response)
            qrcode = _evolution_qrcode_base64(qr_data)
            if qrcode:
                resultado["qrcode"] = qrcode
                if resultado["state"] in {"unknown", "disconnected", "error"}:
                    resultado["state"] = "waiting_qr"
        except Exception as exc:
            resultado["qr_error"] = str(exc)
    return resultado


def disconnect_access_whatsapp(site: Any = "") -> Dict[str, Any]:
    settings = db_store.load_app_settings()
    cfg = _access_whatsapp_cfg(settings, site)
    site_key = _site_key(site)
    if _whatsapp_provider(cfg) != "evolution":
        return {"ok": False, "configured": False, "state": "not_configured", "connected": False, "qrcode": "", "error": "Este site nao usa o Evolution API."}
    conn = _evolution_instance_cfg(cfg, site_key)
    if not conn["base_url"] or not conn["api_key"]:
        return {"ok": False, "configured": False, "state": "not_configured", "connected": False, "qrcode": "", "error": "Evolution API nao configurado nesta plataforma."}
    headers = _evolution_headers(conn)
    try:
        response = requests.delete(f"{conn['base_url']}/instance/logout/{conn['instance']}", headers=headers, timeout=25)
    except Exception as exc:
        return {"ok": False, "configured": True, "state": "error", "connected": False, "qrcode": "", "error": str(exc)}
    if response.status_code == 404:
        return {"ok": True, "configured": True, "state": "disconnected", "connected": False, "qrcode": ""}
    if not (200 <= int(response.status_code or 0) < 300):
        detail = getattr(response, "text", "")[:300] or "Falha ao desconectar WhatsApp."
        return {"ok": False, "configured": True, "state": "error", "connected": False, "qrcode": "", "error": detail}
    return {"ok": True, "configured": True, "state": "disconnected", "connected": False, "qrcode": ""}


def send_access_whatsapp_text(number: Any, message: str, *, site: Any = "") -> Dict[str, Any]:
    """Envia uma resposta direta pelo mesmo canal usado nas notificacoes."""
    target = _numero_whatsapp(number)
    text = _text(message, 4000)
    cfg = _access_whatsapp_cfg(db_store.load_app_settings(), site)
    if not target:
        return {"ok": False, "status": "whatsapp_skipped", "error": "Numero invalido."}
    if not text:
        return {"ok": False, "status": "whatsapp_skipped", "error": "Mensagem vazia."}
    if not cfg.get("enabled"):
        return {"ok": False, "status": "whatsapp_skipped", "error": "WhatsApp desativado."}
    evento = {"guardian_phone": target, "whatsapp_enabled": True, "site": _site_key(site)}
    try:
        if _whatsapp_provider(cfg) == "evolution":
            status = _send_whatsapp_evolution(cfg, evento, text, site_key=_site_key(site))
        else:
            status = _send_whatsapp_cloud(cfg, evento, text)
    except Exception as exc:
        logger.warning("Falha ao enviar resposta WhatsApp de acesso: %s", exc)
        return {"ok": False, "status": "whatsapp_failed", "error": str(exc)}
    return {"ok": status == "whatsapp_sent", "status": status}


def _resposta_json(response: requests.Response) -> Dict[str, Any]:
    try:
        data = response.json() if response.content else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def resolver_cliente_por_numero(phone_number_id: Any) -> Dict[str, str]:
    """Descobre de qual cliente e escola e o numero que recebeu a mensagem.

    A Meta entrega um webhook unico para o app inteiro. Como cada cliente tem
    seu proprio numero, o phone_number_id do payload e o que separa um do outro
    -- sem isso, mensagem de um cliente seria gravada nos dados de outro.
    """
    alvo = _text(phone_number_id, 60)
    if not alvo:
        return {}
    from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
    from app.services.monitoring_service import list_monitoring_tenants

    try:
        clientes = list_monitoring_tenants()
    except Exception as exc:
        logger.warning("Nao foi possivel listar clientes para resolver o webhook: %s", exc)
        return {}

    for slug in clientes:
        ctx = set_current_tenant_slug(slug)
        try:
            settings = db_store.load_app_settings()
            candidatos = [("", settings.get("access_control_whatsapp_notifications") or {})]
            candidatos += list(_access_whatsapp_site_configs(settings).items())
            for site, cfg in candidatos:
                if not isinstance(cfg, dict):
                    continue
                if _text(cfg.get("phone_number_id"), 60) == alvo:
                    return {"tenant": slug, "site": site}
        except Exception as exc:
            logger.warning("Falha ao ler configuracao de %s ao resolver webhook: %s", slug, exc)
        finally:
            reset_current_tenant_slug(ctx)
    return {}


def _cloud_connection_status(settings: Dict[str, Any], cfg: Dict[str, Any], site_efetivo: str, *, resumo: bool = False) -> Dict[str, Any]:
    """Estado do canal oficial.

    Nao existe QR nem sessao para cair: a autenticacao e um token permanente.
    O que interessa saber e se as credenciais estao no lugar e se o template
    escolhido ja foi aprovado pela Meta.
    """
    dados = _cloud_cfg(cfg)

    # Sem site escolhido, o indicador do topo perguntaria pela configuracao
    # global e diria "nao configurado" mesmo com escolas enviando normalmente.
    # Entao, na falta dela, responde pela primeira escola configurada.
    # So o indicador do topo (resumo) responde por outra escola. No painel de
    # Conexoes, escolher "Padrao do cliente" tem que mostrar o padrao do cliente
    # -- e nao os dados de uma escola que o usuario nao selecionou.
    sites_ok: list[str] = []
    if not site_efetivo and resumo:
        for nome_site in _access_whatsapp_site_configs(settings):
            candidato = _cloud_cfg(_access_whatsapp_cfg(settings, nome_site))
            if candidato["phone_number_id"] and candidato["access_token"]:
                sites_ok.append(nome_site)
        if (not dados["phone_number_id"] or not dados["access_token"]) and sites_ok:
            site_efetivo = sites_ok[0]
            cfg = _access_whatsapp_cfg(settings, site_efetivo)
            dados = _cloud_cfg(cfg)

    if not dados["phone_number_id"] or not dados["access_token"]:
        return {
            "ok": False,
            "configured": False,
            "state": "not_configured",
            "connected": False,
            "qrcode": "",
            "external_connection": True,
            "provider": "cloud_api",
            "error": "Informe Phone Number ID e token da API oficial.",
        }
    resultado = {
        "ok": True,
        "configured": True,
        "connected": True,
        "state": "connected",
        "qrcode": "",
        "external_connection": True,
        "provider": "cloud_api",
        "phone_number_id": dados["phone_number_id"],
        "template_name": dados["template_name"] or "hello_world",
        "message": "Canal oficial da Meta: sem QR Code nem sessao para cair.",
        "site": site_efetivo,
        # quantas escolas tem canal proprio: com mais de uma, citar so a
        # primeira daria a impressao de que as outras estao fora
        "configured_sites": sites_ok,
    }
    # Dados do proprio numero: o Phone Number ID nao diz nada para quem olha a
    # tela, mas o numero formatado, o nome exibido e a qualidade dizem.
    try:
        resposta = requests.get(
            f"https://graph.facebook.com/{_GRAPH_VERSION}/{dados['phone_number_id']}",
            params={"fields": "display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {dados['access_token']}"},
            timeout=15,
        )
        numero = _resposta_json(resposta)
        if numero.get("display_phone_number"):
            resultado["display_phone_number"] = numero["display_phone_number"]
        if numero.get("verified_name"):
            resultado["verified_name"] = numero["verified_name"]
        if numero.get("quality_rating"):
            resultado["quality_rating"] = numero["quality_rating"]
    except Exception as exc:
        logger.warning("Nao foi possivel consultar o numero na Meta: %s", exc)

    nome = dados["template_name"]
    waba = _text(cfg.get("waba_id"), 60)
    if nome and waba:
        try:
            resposta = requests.get(
                f"https://graph.facebook.com/{_GRAPH_VERSION}/{waba}/message_templates",
                params={"name": nome, "fields": "name,status"},
                headers={"Authorization": f"Bearer {dados['access_token']}"},
                timeout=15,
            )
            for item in (_resposta_json(resposta).get("data") or []):
                if item.get("name") == nome:
                    resultado["template_status"] = item.get("status")
                    break
        except Exception as exc:
            logger.warning("Nao foi possivel consultar o template na Meta: %s", exc)
    return resultado


def get_access_whatsapp_connection(
    *, refresh_qr: bool = False, site: Any = "", resumo: bool = False, probe_only: bool = False
) -> Dict[str, Any]:
    """Estado real da conexao do canal ativo do site: Meta ou Evolution.

    `probe_only=True` para quem so observa (monitoramento): nao cria
    instancia nem gera QR Code -- ver _evolution_connection_status.
    """
    settings = db_store.load_app_settings()
    cfg = _access_whatsapp_cfg(settings, site)
    site_efetivo = _site_key(site)
    if _whatsapp_provider(cfg) == "evolution":
        return _evolution_connection_status(cfg, site_efetivo, refresh_qr=refresh_qr, probe_only=probe_only)
    return _cloud_connection_status(settings, cfg, site_efetivo, resumo=resumo)


def list_access_whatsapp_channels() -> List[Dict[str, Any]]:
    """Canais WhatsApp configurados, com status real de conexao de cada um.

    Provider-aware: "connected" para cloud_api continua exigindo que a
    consulta na Graph API tenha devolvido display_phone_number (so acontece
    com token valido); para evolution, exige estado "connected" na checagem
    ativa feita por get_access_whatsapp_connection() -- nunca so a
    configuracao estar presente.

    Um canal por site com config propria, mais o "padrao do cliente" quando
    ele tiver credenciais proprias (Meta ou Evolution) nao amarradas a
    nenhum site especifico. Usado para alimentar o card do Dashboard e o
    Zabbix -- ver refresh_from_inventory() em monitoring_service.py.
    """
    settings = db_store.load_app_settings()
    channels: List[Dict[str, Any]] = []

    def _channel(site: str, label: str) -> Dict[str, Any]:
        cfg = _access_whatsapp_cfg(settings, site)
        provider = _whatsapp_provider(cfg)
        # probe_only: este caminho roda no ciclo de monitoramento e tem que
        # ser somente-leitura (nada de /instance/create nem /instance/connect).
        result = get_access_whatsapp_connection(site=site, probe_only=True)
        connected = bool(result.get("connected")) if provider == "evolution" else bool(result.get("display_phone_number"))
        return {
            "site": site, "label": label, "provider": provider,
            "configured": bool(result.get("configured")),
            "connected": connected,
            "phone_number_id": result.get("phone_number_id", ""),
            "display_phone_number": result.get("display_phone_number", ""),
            "quality_rating": result.get("quality_rating", ""),
        }

    global_raw = settings.get("access_control_whatsapp_notifications") or {}
    global_cfg = global_raw if isinstance(global_raw, dict) else {}
    if _whatsapp_provider(global_cfg) == "evolution":
        global_conn = _evolution_instance_cfg(global_cfg, "")
        global_ready = bool(global_conn["base_url"] and global_conn["api_key"])
    else:
        global_dados = _cloud_cfg(global_cfg)
        global_ready = bool(global_dados["phone_number_id"] and global_dados["access_token"])
    if global_ready:
        # O rotulo precisa ser unico por cliente: e ele que vira o nome visivel
        # do host no Zabbix (zabbix_monitoring_service.py), que exige nome unico
        # globalmente. Com o mesmo texto fixo para todo tenant, o host.create()
        # do segundo cliente com canal padrao falhava e abortava o ciclo de
        # monitoramento para todos os tenants processados depois dele.
        tenant_slug = str(get_current_tenant_slug() or "").strip().lower()
        label = f"Padrao do cliente ({tenant_slug})" if tenant_slug else "Padrao do cliente"
        channels.append(_channel("", label))

    for site_name in _access_whatsapp_site_configs(settings):
        channels.append(_channel(site_name, site_name))

    return channels


def _send_whatsapp(settings: Dict[str, Any], event: Dict[str, Any], message: str) -> str:
    cfg = _access_whatsapp_cfg(settings, event.get("site"))
    if not cfg.get("enabled"):
        return "whatsapp_skipped"
    if _whatsapp_provider(cfg) == "evolution":
        return _send_whatsapp_evolution(cfg, event, message, site_key=_site_key(event.get("site")))
    return _send_whatsapp_cloud(cfg, event, message)


def get_access_whatsapp_config(site: Any = "") -> Dict[str, Any]:
    settings = db_store.load_app_settings()
    site_key = _site_key(site)
    site_configs = _access_whatsapp_site_configs(settings)
    cfg = _access_whatsapp_cfg(settings, site_key)
    provider = _whatsapp_provider(cfg)
    if provider == "cloud_api":
        instance = ""
        dados = _cloud_cfg(cfg)
        configured = bool(dados["phone_number_id"] and dados["access_token"])
    else:
        conn = _evolution_instance_cfg(cfg, site_key)
        instance = conn["instance"]
        configured = bool(conn["base_url"] and conn["api_key"])
    resultado = {
        "site": site_key,
        "enabled": bool(cfg.get("enabled")),
        "configured": configured,
        "site_configured": bool(site_key and site_key in site_configs),
        "provider": provider,
        # sem base_url: e o endereco interno do container Evolution da
        # plataforma (IP de LAN), a tela nunca mostrou esse campo, e devolver
        # infraestrutura interna para qualquer usuario autenticado do cliente
        # e exposicao sem contrapartida.
        "instance": instance,
    }
    # Campos da API oficial voltam sempre, nao so quando ela e o provider
    # ativo -- trocar para Evolution e depois voltar pra Meta nao pode
    # obrigar reditar Phone Number ID/token so porque o provider mudou.
    resultado.update({
        "phone_number_id": _text(cfg.get("phone_number_id"), 60),
        "waba_id": _text(cfg.get("waba_id"), 60),
        "template_name": _text(cfg.get("template_name"), 120),
        "template_language": _text(cfg.get("template_language") or "pt_BR", 12),
        # o token nunca volta para a tela; so se ele existe
        "token_saved": bool(_text(cfg.get("access_token"), 600)),
    })
    return resultado


def save_access_whatsapp_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = db_store.load_app_settings()
    site_key = _site_key(payload.get("site"))
    site_configs = _access_whatsapp_site_configs(settings)
    old_global = settings.get("access_control_whatsapp_notifications") or {}
    if not isinstance(old_global, dict):
        old_global = {}
    old = site_configs.get(site_key, {}) if site_key else old_global
    provider = _whatsapp_provider({"provider": payload.get("provider") or old.get("provider") or "cloud_api"})
    api_key = _text(payload.get("api_key"), 300) or _text(old.get("api_key"), 300) or _text(old_global.get("api_key"), 300)
    # O nome da instancia e sempre derivado do tenant+site aqui no servidor:
    # nem o que o cliente mandou no corpo do PUT, nem o que ja estava salvo
    # (as configuracoes antigas trazem o "sightops" fixo do modelo anterior,
    # que colide entre clientes no container compartilhado). Ver
    # _evolution_instance_cfg para o porque completo.
    instance = _evolution_default_instance(site_key)
    saved_cfg = {
        "enabled": bool(payload.get("enabled")),
        "provider": provider,
        # api_key por site nao e mais usado para falar com o Evolution (a
        # credencial real vem da plataforma, ver _evolution_platform_cfg) --
        # mantido so por compatibilidade com o que ja foi salvo antes.
        "api_key": api_key,
        "instance": instance,
    }
    # Campos da API oficial ficam gravados sempre, nao so quando ela e o
    # provider ativo agora. So aceitam valor novo do payload quando o
    # provider salvo e cloud_api (o frontend nunca manda esses campos com
    # Evolution selecionado); do contrario preserva o que ja estava --
    # trocar para Evolution e depois voltar pra Meta nao pode apagar
    # Phone Number ID/WABA ID/template/token so por causa da troca.
    payload_cloud = payload if provider == "cloud_api" else {}
    saved_cfg.update({
        "phone_number_id": _text(payload_cloud.get("phone_number_id") or old.get("phone_number_id"), 60),
        "waba_id": _text(payload_cloud.get("waba_id") or old.get("waba_id"), 60),
        "access_token": _cifrar_token(payload_cloud.get("access_token"), old.get("access_token")),
        "template_name": _text(payload_cloud.get("template_name") or old.get("template_name"), 120),
        "template_language": _text(payload_cloud.get("template_language") or old.get("template_language") or "pt_BR", 12),
        "app_secret": _cifrar_token(payload_cloud.get("app_secret"), old.get("app_secret")),
    })
    if site_key:
        site_configs[site_key] = saved_cfg
        settings["access_control_whatsapp_notifications_by_site"] = site_configs
    else:
        settings["access_control_whatsapp_notifications"] = saved_cfg
    db_store.save_app_settings(settings)
    return get_access_whatsapp_config(site_key)


def test_access_whatsapp(payload: Dict[str, Any]) -> Dict[str, Any]:
    number = _text(payload.get("number") or payload.get("to"), 40)
    if not _numero_whatsapp(number):
        return {"ok": False, "error": "Informe um numero de WhatsApp para teste."}
    site_key = _site_key(payload.get("site"))
    cfg = _access_whatsapp_cfg(db_store.load_app_settings(), site_key)
    if not cfg.get("enabled"):
        return {"ok": False, "error": "WhatsApp desativado."}
    # o site precisa ir no contexto: _evolution_instance_cfg deriva o nome da
    # instancia so a partir dele (nunca do que ficou salvo em cfg), entao sem
    # isso aqui o teste de um site especifico falava com a instancia errada
    # (a padrao do cliente, nao a desse site).
    context = {"guardian_phone": number, "whatsapp_enabled": True, "site": site_key}
    message = "Teste SightOps - notificacoes do Controle de Acesso configuradas."
    try:
        status = _send_whatsapp({"access_control_whatsapp_notifications": cfg}, context, message)
    except Exception as exc:
        logger.warning("Falha no teste de WhatsApp de acesso: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": status == "whatsapp_sent", "status": status}


def notify_access_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Envia notificacoes de acesso sem deixar falha externa quebrar o evento."""
    context = _event_context(event)
    message = _build_message(context)
    settings = db_store.load_app_settings()
    statuses: list[str] = []
    for sender in (_send_telegram,):
        try:
            statuses.append(sender(settings, message))
        except Exception as exc:
            logger.warning("Falha ao enviar Telegram de acesso: %s", exc)
            statuses.append("telegram_failed")
    try:
        statuses.append(_send_whatsapp(settings, context, _build_whatsapp_message(context)))
    except Exception as exc:
        logger.warning("Falha ao enviar WhatsApp de acesso: %s", exc)
        statuses.append("whatsapp_failed")
    return {"ok": all(not status.endswith("_failed") for status in statuses), "statuses": statuses}
