from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from app.core.crypto import decrypt, encrypt
from app.core.paths import DATA_DIR
from app.core.tenant_context import get_current_tenant_slug, reset_current_tenant_slug, set_current_tenant_slug
from app.cli.tools.ruijie_reyee import RuijieAuthError, lan_inventory as ruijie_lan_inventory_call

CONNECTORS_PATH = DATA_DIR / "connectors.json"
CONNECTOR_JOBS_PATH = DATA_DIR / "connector-jobs.json"
DEFAULT_WG_ENDPOINT = "201.182.184.84:51820"
DEFAULT_WG_NETWORK_PREFIX = "10.250.0"
DEFAULT_WG_SERVER_PUBLIC_KEY = "yR9WCTtf6Yp9ZqWLffqdQmWuBeqEB4WSLrzcztP1xQQ="
CGNAT_LAN_NETWORK = ipaddress.ip_network("100.64.0.0/10")

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_cidr(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except Exception:
        return ""
    if net.version != 4 or net.prefixlen >= 32:
        return ""
    if not (net.is_private or net.subnet_of(CGNAT_LAN_NETWORK)):
        return ""
    cidr = str(net)
    if cidr == "10.250.0.0/24":
        return ""
    return cidr


def _unique_cidrs(values: List[Any], collapse: bool = True) -> List[str]:
    networks: List[ipaddress.IPv4Network] = []
    for value in values:
        cidr = _normalize_cidr(value)
        if cidr:
            try:
                networks.append(ipaddress.ip_network(cidr, strict=False))
            except Exception:
                continue
    if collapse:
        networks = list(ipaddress.collapse_addresses(networks))
    seen: set[str] = set()
    out: List[str] = []
    for net in sorted(networks, key=lambda item: (int(item.network_address), item.prefixlen)):
        cidr = str(net)
        if cidr not in seen:
            seen.add(cidr)
            out.append(cidr)
    return out


def _split_cidr_values(value: Any) -> List[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str):
        return [_text(item) for item in re.split(r"[\s,;|]+", value) if _text(item)]
    return []


def _looks_like_wan_interface(value: Any) -> bool:
    name = _text(value).lower()
    if not name:
        return False
    return (
        name == "ether1"
        or "wan" in name
        or "internet" in name
        or "pppoe" in name
        or name.startswith("lte")
    )


def _cidrs_from_address_sample(value: Any) -> List[str]:
    cidrs: List[str] = []
    for item in re.split(r"[;\r\n]+", _text(value)):
        parts = [part.strip() for part in item.split("|")]
        first = parts[0] if parts else ""
        iface = parts[1] if len(parts) > 1 else ""
        if _looks_like_wan_interface(iface):
            continue
        if "/" in first:
            cidrs.append(first)
    return _unique_cidrs(cidrs)


def _trusted_lans_from_connector(row: Dict[str, Any], include_saved_tunnel: bool = True) -> List[str]:
    inventory = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
    host = row.get("host") if isinstance(row.get("host"), dict) else {}
    tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
    candidates: List[Any] = []
    address_sample = _text(inventory.get("address_sample") or inventory.get("ip_address_sample"))
    sample_cidrs = _cidrs_from_address_sample(address_sample)
    if sample_cidrs:
        candidates.extend(sample_cidrs)
    else:
        for key in ("lan_networks", "networks", "routes"):
            candidates.extend(_split_cidr_values(inventory.get(key)))
    candidates.extend(_split_cidr_values(host.get("lan_networks")))
    if include_saved_tunnel:
        candidates.extend(_split_cidr_values(tunnel.get("client_lans")))

    return _unique_cidrs(candidates)


def _private_24_from_ip(value: Any) -> str:
    raw = _text(value)
    try:
        ip = ipaddress.ip_address(raw)
    except Exception:
        return ""
    if ip.version != 4:
        return ""
    if not (ip.is_private or ip in CGNAT_LAN_NETWORK):
        return ""
    parts = raw.split(".")
    if len(parts) != 4:
        return ""
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"


def _sample_lans_from_text(value: Any) -> List[str]:
    text = _text(value)
    if not text:
        return []
    cidrs: List[str] = []
    for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        cidr = _private_24_from_ip(ip)
        if cidr:
            cidrs.append(cidr)
    return cidrs


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if data is not None else default
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _load_connectors() -> List[Dict[str, Any]]:
    data = _read_json(CONNECTORS_PATH, [])
    return data if isinstance(data, list) else []


def _save_connectors(rows: List[Dict[str, Any]]) -> None:
    _write_json(CONNECTORS_PATH, rows)


def _wg_keypair() -> Dict[str, str]:
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "private_key": base64.b64encode(private_raw).decode("ascii"),
        "public_key": base64.b64encode(public_raw).decode("ascii"),
    }


def _wireguard_server_public_key() -> str:
    return _text(os.getenv("WIREGUARD_SERVER_PUBLIC_KEY") or DEFAULT_WG_SERVER_PUBLIC_KEY)


def _next_wireguard_client_address(rows: List[Dict[str, Any]], connector_id: str) -> str:
    used: set[int] = set()
    for item in rows:
        if _text(item.get("id")) == connector_id:
            continue
        tunnel = item.get("tunnel") if isinstance(item.get("tunnel"), dict) else {}
        raw = _text(tunnel.get("client_address")).split("/", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if address.version == 4 and str(address).startswith(f"{DEFAULT_WG_NETWORK_PREFIX}."):
            used.add(int(str(address).rsplit(".", 1)[1]))
    for host in range(2, 255):
        if host not in used:
            return f"{DEFAULT_WG_NETWORK_PREFIX}.{host}/32"
    raise ValueError("nao ha enderecos WireGuard livres para um novo conector")


def _load_jobs() -> List[Dict[str, Any]]:
    data = _read_json(CONNECTOR_JOBS_PATH, [])
    return data if isinstance(data, list) else []


def _save_jobs(rows: List[Dict[str, Any]]) -> None:
    _write_json(CONNECTOR_JOBS_PATH, rows)


def _mark_stale_running_jobs(jobs: List[Dict[str, Any]], timeout_seconds: int = 180) -> bool:
    changed = False
    now_ts = time.time()
    for job in jobs:
        if job.get("status") != "running":
            continue
        picked = _text(job.get("picked_at"))
        try:
            picked_ts = datetime.fromisoformat(picked.replace("Z", "+00:00")).timestamp()
        except Exception:
            picked_ts = 0
        if picked_ts and (now_ts - picked_ts) > timeout_seconds:
            job["status"] = "failed"
            job["finished_at"] = _now()
            job["error"] = "Tempo esgotado aguardando resultado do conector"
            changed = True
    return changed


def _visible_to_current_tenant(row: Dict[str, Any]) -> bool:
    """Isola conectores por tenant nas rotas usadas por usuario logado.

    O conector so e visivel pra quem criou (mesmo tenant_slug). Linha antiga
    sem tenant_slug (anterior a este isolamento) so aparece pra requisicoes
    sem contexto de tenant (deployment single-tenant), nunca "vaza" pra um
    tenant especifico por engano.

    Nao usar isto nas rotas /agent/* -- o RouterOS/agente nao tem sessao de
    usuario, autentica so por connector_id+token, e isso continua valendo
    como fronteira de acesso pra essas rotas (ver _auth_connector).
    """
    return _text(row.get("tenant_slug")) == get_current_tenant_slug()


def _public_connector(row: Dict[str, Any], include_token: bool = False) -> Dict[str, Any]:
    last_seen = _text(row.get("last_seen"))
    online = False
    if last_seen:
        # O MikroTik tem agente que bate heartbeat sozinho a cada 60s -- 90s de
        # janela reflete isso de verdade. O Ruijie e "pull": so atualiza
        # last_seen quando alguem clica em "Coletar LAN" (sem heartbeat), entao
        # a mesma janela curta o deixaria "offline" minutos depois de qualquer
        # coleta bem-sucedida, por engano. Usa uma janela bem mais longa nesse
        # caso -- e um sinal de "ultima coleta ok", nao de conexao continua.
        window_seconds = 90 if _text(row.get("type")).lower() != "ruijie" else 86400
        try:
            last_ts = datetime.fromisoformat(last_seen.replace("Z", "+00:00")).timestamp()
            online = (time.time() - last_ts) <= window_seconds
        except Exception:
            online = False
    out = dict(row)
    out["type"] = _text(out.get("type")) or "windows"
    out["status"] = "online" if online else "offline"
    if not include_token:
        out.pop("token", None)
    out.pop("password_enc", None)
    out.pop("vpn_password_enc", None)
    # A chave privada do WireGuard NAO pode sair na listagem: ela so existe pra
    # gerar o script do RouterOS (rota propria, download explicito). `out` e
    # copia rasa, entao o bloco tunnel tem que ser copiado antes do pop -- senao
    # o pop apagaria a chave do registro em memoria e quebraria o script.
    tunnel = out.get("tunnel")
    if isinstance(tunnel, dict):
        tunnel = dict(tunnel)
        tunnel.pop("client_private_key", None)
        out["tunnel"] = tunnel
    return out


def list_connectors(include_token: bool = False) -> Dict[str, Any]:
    with _lock:
        rows = [row for row in _load_connectors() if _visible_to_current_tenant(row)]
        connectors = [_public_connector(row, include_token=include_token) for row in rows]
        jobs = [job for job in _load_jobs() if any(_text(r.get("id")) == _text(job.get("connector_id")) for r in rows)]
    queued = sum(1 for job in jobs if job.get("status") in {"queued", "running"})
    return {"ok": True, "count": len(connectors), "queued_jobs": queued, "connectors": connectors}


def create_connector(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _text(payload.get("name")) or "Novo conector"
    client = _text(payload.get("client")) or "Cliente"
    site = _text(payload.get("site")) or "Matriz"
    public_base_url = _text(payload.get("public_base_url") or payload.get("public_url")).rstrip("/")
    connector_type = _text(payload.get("type")).lower() or "routeros"
    if connector_type not in {"routeros", "ruijie"}:
        raise ValueError("tipo de conector invalido")
    access_mode = _normalize_access_mode(payload.get("access_mode") or payload.get("network_mode"), connector_type)
    connector_id = _text(payload.get("id")) or secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    row = {
        "id": connector_id,
        "token": token,
        "tenant_slug": get_current_tenant_slug(),
        "type": connector_type,
        "access_mode": access_mode,
        "name": name,
        "client": client,
        "site": site,
        "created_at": _now(),
        "last_seen": "",
        "status": "offline",
        "version": "",
        "host": {},
        "inventory": {},
        "remote_ip": "",
        "public_base_url": public_base_url,
    }
    if connector_type == "ruijie":
        # O Reyee tem IP publico e nao roda agente -- o SightOps fala direto
        # com ele (login+API) sempre que precisar, guardando so o necessario
        # pra reconectar. Sem public_base_url/token de agente (nao se aplica).
        gw_host = _text(payload.get("gateway_host") or payload.get("host"))
        gw_user = _text(payload.get("gateway_user") or payload.get("username")) or "admin"
        gw_pass = _text(payload.get("gateway_password") or payload.get("password"))
        if not gw_host or not gw_pass:
            raise ValueError("conector Ruijie exige host e senha do gateway")
        row["gateway_host"] = gw_host
        row["gateway_user"] = gw_user
        row["password_enc"] = encrypt(gw_pass)
        # VPN (OpenVPN) e opcional na hora do cadastro -- o gateway precisa
        # ter o servidor OpenVPN habilitado e a config exportada primeiro (um
        # passo manual no eWeb). Se ja tiver isso em maos, guarda tudo junto.
        vpn_username = _text(payload.get("vpn_username"))
        vpn_password = _text(payload.get("vpn_password"))
        vpn_config = str(payload.get("vpn_config") or "").strip()
        if vpn_username or vpn_password or vpn_config:
            if not (vpn_username and vpn_password and vpn_config):
                raise ValueError("VPN exige usuario, senha e a configuracao (.ovpn) juntos")
            if not _looks_like_valid_ovpn_config(vpn_config):
                raise ValueError(
                    "essa configuracao nao parece ser o client.ovpn (pode ser o .tar inteiro) -- "
                    "extraia o .tar exportado do eWeb e use o arquivo client.ovpn de dentro dele"
                )
            row["vpn_username"] = vpn_username
            row["vpn_password_enc"] = encrypt(vpn_password)
            row["vpn_config"] = vpn_config
    with _lock:
        rows = _load_connectors()
        if any(_text(item.get("id")) == connector_id for item in rows):
            raise ValueError("id do conector ja existe")
        rows.append(row)
        _save_connectors(rows)
    return {"ok": True, "connector": _public_connector(row, include_token=True)}


def _looks_like_valid_ovpn_config(text: str) -> bool:
    """O eWeb exporta um .tar (client.ovpn + ca.crt + ca.key). Se alguem
    colar/enviar o .tar inteiro em vez do client.ovpn de dentro dele, o
    conteudo vira um cabecalho de tar ("ustar", bytes de controle) colado
    na frente da config real -- ja aconteceu de verdade em producao e
    deixa o container OpenVPN em crash-loop. Validacao espelha a do
    frontend (loadVpnConfigFile em connectors.js), mas aqui protege contra
    qualquer entrada, nao so o seletor de arquivo."""
    if "ustar" in text:
        return False
    if re.search(r"[\x00-\x08\x0e-\x1f]", text[:512]):
        return False
    return bool(re.search(r"^\s*(client|dev\s+tun|dev\s+tap|#|;)", text, re.MULTILINE))


def ruijie_update_vpn(connector_id: str, vpn_username: str, vpn_password: str, vpn_config: str) -> Dict[str, Any]:
    """Grava/atualiza a credencial e a config OpenVPN (.ovpn exportado do
    eWeb) de um conector Ruijie ja cadastrado -- separado do cadastro
    inicial porque a config so existe depois de habilitar o servidor
    OpenVPN no gateway e exportar manualmente (nao da pra automatizar
    esse passo pela API, so descobrimos o login/inventario por API)."""
    vpn_username = _text(vpn_username)
    vpn_password = _text(vpn_password)
    vpn_config = str(vpn_config or "").strip()
    if not (vpn_username and vpn_password and vpn_config):
        raise ValueError("informe usuario, senha e a configuracao (.ovpn) da VPN")
    if not _looks_like_valid_ovpn_config(vpn_config):
        raise ValueError(
            "essa configuracao nao parece ser o client.ovpn (pode ser o .tar inteiro) -- "
            "extraia o .tar exportado do eWeb e use o arquivo client.ovpn de dentro dele"
        )
    with _lock:
        rows = _load_connectors()
        row = next((r for r in rows if _text(r.get("id")) == _text(connector_id)), None)
        if not row or not _visible_to_current_tenant(row):
            raise ValueError("conector nao encontrado")
        if _text(row.get("type")) != "ruijie":
            raise ValueError("conector nao e do tipo Ruijie")
        row["vpn_username"] = vpn_username
        row["vpn_password_enc"] = encrypt(vpn_password)
        row["vpn_config"] = vpn_config
        _save_connectors(rows)
        return {"ok": True, "connector": _public_connector(row, include_token=False)}


def ruijie_collect_lan_inventory(connector_id: str) -> Dict[str, Any]:
    """Loga no gateway Ruijie e devolve o inventario de dispositivos da LAN.

    Chamado direto pelo SightOps (sem fila/job, sem agente) -- o gateway
    tem IP publico e responde na hora, diferente do fluxo assincrono do
    MikroTik (que precisa esperar o proximo ciclo do script no equipamento).
    """
    with _lock:
        rows = _load_connectors()
        row = next((r for r in rows if _text(r.get("id")) == _text(connector_id)), None)
        if not row or not _visible_to_current_tenant(row):
            raise ValueError("conector nao encontrado")
        if _text(row.get("type")) != "ruijie":
            raise ValueError("conector nao e do tipo Ruijie")
        gw_host = _text(row.get("gateway_host"))
        gw_user = _text(row.get("gateway_user")) or "admin"
        gw_pass_enc = _text(row.get("password_enc"))
    if not gw_host or not gw_pass_enc:
        raise ValueError("conector Ruijie sem host/senha cadastrados")
    gw_pass = decrypt(gw_pass_enc)
    try:
        result = ruijie_lan_inventory_call(gw_host, gw_user, gw_pass)
    except RuijieAuthError as exc:
        raise ValueError(f"falha ao autenticar no gateway Ruijie: {exc}") from exc
    with _lock:
        rows = _load_connectors()
        for item in rows:
            if _text(item.get("id")) == _text(connector_id):
                item["last_seen"] = _now()
                item["inventory"] = {
                    "ruijie_devices": result.get("devices", []),
                    "count": result.get("count", 0),
                }
                item["remote_ip"] = gw_host
                item["host"] = {
                    "hostname": _text(item.get("name")),
                    "model": f"SN {result.get('sn')}" if result.get("sn") else "",
                    "ips": [gw_host],
                }
                break
        _save_connectors(rows)
    return result


def get_connector(connector_id: str, include_token: bool = False, enforce_tenant: bool = False) -> Dict[str, Any] | None:
    """enforce_tenant=True deve ser usado por toda rota chamada por usuario logado
    (nao pelas rotas /agent/*, que autenticam por connector_id+token e nao tem
    sessao/tenant -- ver _visible_to_current_tenant)."""
    cid = _text(connector_id)
    with _lock:
        for row in _load_connectors():
            if _text(row.get("id")) == cid:
                if enforce_tenant and not _visible_to_current_tenant(row):
                    return None
                return _public_connector(row, include_token=include_token)
    return None


def _auth_connector(connector_id: str, token: str) -> Dict[str, Any]:
    cid = _text(connector_id)
    tok = _text(token)
    if not cid or not tok:
        raise PermissionError("credencial do conector ausente")
    rows = _load_connectors()
    for row in rows:
        if _text(row.get("id")) == cid and secrets.compare_digest(_text(row.get("token")), tok):
            return row
    raise PermissionError("token do conector invalido")


def accept_register(connector_id: str, token: str, payload: Dict[str, Any], remote_ip: str = "") -> Dict[str, Any]:
    with _lock:
        row = _auth_connector(connector_id, token)
        rows = _load_connectors()
        now = _now()
        for item in rows:
            if _text(item.get("id")) == _text(row.get("id")):
                item["last_seen"] = now
                item["registered_at"] = item.get("registered_at") or now
                item["version"] = _text(payload.get("version")) or item.get("version") or ""
                item["host"] = payload.get("host") if isinstance(payload.get("host"), dict) else item.get("host", {})
                if isinstance(payload.get("inventory"), dict):
                    current_inventory = item.get("inventory") if isinstance(item.get("inventory"), dict) else {}
                    next_inventory = dict(payload.get("inventory") or {})
                    for key in ("known_targets", "known_targets_updated_at"):
                        if key in current_inventory and key not in next_inventory:
                            next_inventory[key] = current_inventory[key]
                    item["inventory"] = next_inventory
                item["remote_ip"] = _text(remote_ip)
                item["status"] = "online"
                _refresh_auto_tunnel_lans(item)
                row = item
                break
        _save_connectors(rows)
    return {"ok": True, "connector": _public_connector(row)}


def accept_heartbeat(connector_id: str, token: str, payload: Dict[str, Any], remote_ip: str = "") -> Dict[str, Any]:
    return accept_register(connector_id, token, payload, remote_ip=remote_ip)


def _mark_connector_seen(connector_id: str) -> None:
    with _lock:
        rows = _load_connectors()
        changed = False
        for item in rows:
            if _text(item.get("id")) == _text(connector_id):
                item["last_seen"] = _now()
                item["status"] = "online"
                changed = True
                break
        if changed:
            _save_connectors(rows)


def _private_host_ip(value: Any) -> str:
    raw = _text(value)
    try:
        ip = ipaddress.ip_address(raw)
    except Exception:
        return ""
    if ip.version != 4:
        return ""
    if not (ip.is_private or ip in CGNAT_LAN_NETWORK):
        return ""
    return str(ip)


def _normalize_access_mode(value: Any, connector_type: Any = "routeros") -> str:
    if _text(connector_type).lower() != "routeros":
        return "public"
    mode = _text(value).lower()
    if mode in {"public", "ip_publico", "ip-publico", "direct", "direto"}:
        return "public"
    return "cgnat"


def register_connector_known_targets(connector_id: str, targets: List[Any]) -> Dict[str, Any]:
    """Guarda IPs privados consultados por um conector para rotas CGNAT seguras.

    Em SaaS, duas escolas podem usar a mesma LAN privada. O dado continua
    isolado por tenant no SightOps, mas a rota do Linux e global. Por isso o
    sincronizador so usa esses IPs como /32 e ainda bloqueia se o mesmo IP
    aparecer em mais de um conector.
    """
    cid = _text(connector_id)
    clean = []
    seen: set[str] = set()
    for target in targets or []:
        ip = _private_host_ip(target)
        if ip and ip not in seen:
            clean.append(ip)
            seen.add(ip)
    if not cid or not clean:
        return {"ok": True, "added": [], "known_targets": []}

    with _lock:
        rows = _load_connectors()
        row = next((item for item in rows if _text(item.get("id")) == cid), None)
        if row is None or not _visible_to_current_tenant(row):
            raise ValueError("conector nao encontrado")
        inventory = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
        current = []
        current_seen: set[str] = set()
        for item in inventory.get("known_targets") or []:
            ip = _private_host_ip(item)
            if ip and ip not in current_seen:
                current.append(ip)
                current_seen.add(ip)
        added = [ip for ip in clean if ip not in current_seen]
        if added:
            inventory["known_targets"] = (current + added)[-1000:]
            inventory["known_targets_updated_at"] = _now()
            row["inventory"] = inventory
            _save_connectors(rows)
        return {"ok": True, "added": added, "known_targets": inventory.get("known_targets") or current}


def _resolve_access_device_for_connector(connector_id: str, payload: Dict[str, Any]) -> str:
    from app.services.access_control_store import list_devices

    wanted_id = _text(payload.get("device_id") or payload.get("access_device_id"))
    wanted_host = _text(payload.get("host") or payload.get("device_host"))
    devices = list_devices()
    selected = None
    if wanted_id:
        selected = next((item for item in devices if _text(item.get("id")) == wanted_id), None)
    elif wanted_host:
        selected = next(
            (
                item for item in devices
                if _text(item.get("connector_id")) == _text(connector_id)
                and _text(item.get("host")).lower() == wanted_host.lower()
            ),
            None,
        )
    if not selected:
        raise ValueError("controladora nao encontrada para este cliente")
    if _text(selected.get("connector_id")) != _text(connector_id):
        raise ValueError("controladora nao pertence a este conector")
    return _text(selected.get("id"))


def accept_access_control_event(connector_id: str, token: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = _auth_connector(connector_id, token)
    tenant = _text(row.get("tenant_slug"))
    if not tenant:
        raise PermissionError("conector sem cliente vinculado")
    ctx_token = set_current_tenant_slug(tenant)
    try:
        from app.services.access_control_store import list_access_report_events
        from app.services.access_control_sync import record_device_event

        data = payload if isinstance(payload, dict) else {}
        device_id = _resolve_access_device_for_connector(connector_id, data)
        event_id = record_device_event(device_id, data, source="connector_push")
        events = list_access_report_events({"period": "all", "device_id": device_id, "limit": 50})
        event = next((item for item in events if _text(item.get("id")) == event_id), None)
    finally:
        reset_current_tenant_slug(ctx_token)
    _mark_connector_seen(connector_id)
    return {"ok": True, "event_id": event_id, "event": event or {"id": event_id, "device_id": device_id}}


def create_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    connector_id = _text(payload.get("connector_id"))
    job_type = _text(payload.get("type")) or "ping_many"
    job_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    if not connector_id:
        raise ValueError("connector_id obrigatorio")
    if job_type not in {
        "ping_many",
        "lan_inventory",
        "wireguard_install",
        "wireguard_probe",
        "wireguard_diagnose",
        "access_http_get",
        "access_http_post",
        "agent_upgrade",
    }:
        raise ValueError("tipo de job nao suportado neste MVP")
    with _lock:
        if not get_connector(connector_id, include_token=False, enforce_tenant=True):
            raise ValueError("conector nao encontrado")
        jobs = _load_jobs()
        job = {
            "id": secrets.token_hex(10),
            "connector_id": connector_id,
            "type": job_type,
            "payload": job_payload,
            "status": "queued",
            "created_at": _now(),
            "picked_at": "",
            "finished_at": "",
            "result": None,
            "error": "",
        }
        jobs.append(job)
        _save_jobs(jobs)
    return {"ok": True, "job": job}


def list_jobs(connector_id: str = "", limit: int = 50) -> Dict[str, Any]:
    """Uso por usuario logado (rota /api/connectors/{id}/jobs). Se connector_id
    for de outro tenant, devolve lista vazia -- nao revela nem que o conector
    existe."""
    cid = _text(connector_id)
    if cid and not get_connector(cid, include_token=False, enforce_tenant=True):
        return {"ok": True, "jobs": []}
    with _lock:
        raw_jobs = _load_jobs()
        if _mark_stale_running_jobs(raw_jobs):
            _save_jobs(raw_jobs)
        jobs = list(reversed(raw_jobs))
    if cid:
        jobs = [job for job in jobs if _text(job.get("connector_id")) == cid]
    return {"ok": True, "jobs": jobs[: max(1, min(int(limit or 50), 200))]}


def poll_job(connector_id: str, token: str) -> Dict[str, Any]:
    with _lock:
        _auth_connector(connector_id, token)
        jobs = _load_jobs()
        _mark_stale_running_jobs(jobs)
        selected = None
        now = _now()
        for job in jobs:
            if _text(job.get("connector_id")) == _text(connector_id) and job.get("status") == "queued":
                job["status"] = "running"
                job["picked_at"] = now
                selected = job
                break
        _save_jobs(jobs)
    return {"ok": True, "job": selected}


def accept_job_result(connector_id: str, token: str, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        _auth_connector(connector_id, token)
        jobs = _load_jobs()
        updated = None
        for job in jobs:
            if _text(job.get("id")) == _text(job_id) and _text(job.get("connector_id")) == _text(connector_id):
                ok = bool(payload.get("ok", True))
                job["status"] = "done" if ok else "failed"
                job["finished_at"] = _now()
                job["result"] = payload.get("result")
                job["error"] = _text(payload.get("error"))
                updated = job
                break
        if not updated:
            raise ValueError("job nao encontrado")
        _save_jobs(jobs)
    return {"ok": True, "job": updated}


def _parse_routeros_lan_inventory(result: str) -> Dict[str, Any]:
    leases: List[Dict[str, str]] = []
    arps: List[Dict[str, str]] = []
    neighbors: List[Dict[str, str]] = []
    for raw in re.split(r"[;\r\n]+", result or ""):
        parts = [part.strip() for part in raw.split("|")]
        if not parts or not parts[0]:
            continue
        kind = parts[0].lower()
        if kind == "dhcp" and len(parts) >= 5:
            leases.append({"ip": parts[1], "mac": parts[2], "host": parts[3], "status": parts[4]})
        elif kind == "arp" and len(parts) >= 3:
            arps.append({"ip": parts[1], "mac": parts[2]})
        elif kind == "neighbor" and len(parts) >= 5:
            neighbors.append({"ip": parts[1], "mac": parts[2], "identity": parts[3], "platform": parts[4]})
    return {
        "dhcp_leases": len(leases),
        "arp_entries": len(arps),
        "neighbors": len(neighbors),
        "dhcp_rows": leases,
        "arp_rows": arps,
        "neighbor_rows": neighbors,
        "collected_at": _now(),
    }


def _extract_connector_lans(row: Dict[str, Any], include_saved_tunnel: bool = True) -> List[str]:
    inventory = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
    trusted = _trusted_lans_from_connector(row, include_saved_tunnel=include_saved_tunnel)
    if trusted:
        return trusted[:64]

    candidates: List[Any] = []
    for key in ("dhcp_sample", "arp_sample", "neighbor_sample"):
        candidates.extend(_sample_lans_from_text(inventory.get(key)))
    return _unique_cidrs(candidates)[:64]


def _refresh_auto_tunnel_lans(row: Dict[str, Any]) -> bool:
    tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
    if not tunnel.get("enabled") or _text(tunnel.get("type")).lower() != "wireguard":
        return False
    mode = _text(tunnel.get("client_lans_mode") or "manual").lower()
    if mode not in {"auto", "all", "detected", "bootstrap"}:
        return False
    detected = _extract_connector_lans(row, include_saved_tunnel=False)
    if not detected:
        return False
    current = _unique_cidrs(tunnel.get("client_lans") or [])
    if current == detected:
        return False
    tunnel["client_lans"] = detected
    tunnel["client_lans_mode"] = "auto"
    tunnel["client_lans_detected_at"] = _now()
    tunnel["updated_at"] = _now()
    row["tunnel"] = tunnel
    return True


def _update_connector_inventory_from_job(connector_id: str, job: Dict[str, Any], result: str) -> None:
    if _text(job.get("type")) != "lan_inventory":
        return
    parsed = _parse_routeros_lan_inventory(result)
    with _lock:
        rows = _load_connectors()
        changed = False
        for row in rows:
            if _text(row.get("id")) != _text(connector_id):
                continue
            inventory = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
            inventory.update(parsed)
            row["inventory"] = inventory
            changed = True
            if _refresh_auto_tunnel_lans(row):
                changed = True
            break
        if changed:
            _save_connectors(rows)


def accept_routeros_job_result(connector_id: str, token: str, job_id: str, result: str, ok: bool = True, error: str = "") -> Dict[str, Any]:
    job_before = None
    with _lock:
        for item in _load_jobs():
            if _text(item.get("id")) == _text(job_id) and _text(item.get("connector_id")) == _text(connector_id):
                job_before = dict(item)
                break
    payload: Dict[str, Any] = {
        "ok": bool(ok),
        "result": {"routeros_ping": _text(result)},
        "error": _text(error),
    }
    if _text((job_before or {}).get("type")) == "lan_inventory":
        payload["result"] = {"routeros_inventory": _text(result), "inventory": _parse_routeros_lan_inventory(result)}
    elif _text((job_before or {}).get("type")) in {"access_http_get", "access_http_post"}:
        result_text = _text(result)
        failed = result_text.lower().startswith("fetch_error")
        payload["ok"] = bool(ok) and not failed
        payload["result"] = {"access_http": result_text}
        payload["error"] = _text(error) or (result_text if failed else "")
    response = accept_job_result(connector_id, token, job_id, payload)
    if ok and job_before:
        _update_connector_inventory_from_job(connector_id, job_before, result)
    return response


def _routeros_safe_target(value: Any) -> str:
    text = _text(value)
    if not text or len(text) > 160:
        return ""
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]+", text) else ""


def _routeros_string(value: Any, limit: int = 1200) -> str:
    text = _text(value)[:limit]
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _routeros_safe_access_url(value: Any) -> str:
    text = _text(value)
    if not text or len(text) > 1200:
        return ""
    if not text.startswith("http://"):
        return ""
    return text if re.fullmatch(r"[A-Za-z0-9_./:;?&=%+~#-]+", text) else ""


def _wireguard_routeros_address(client_address: str) -> str:
    ip = _text(client_address).split("/", 1)[0]
    return f"{ip}/24" if ip else f"{DEFAULT_WG_NETWORK_PREFIX}.2/24"


def _routeros_job_script_template(base_url: str, connector_id: str, token: str, job: Dict[str, Any] | None) -> str:
    base_url = base_url.rstrip("/")
    if not job:
        return """:do {/system scheduler set [find name="sightops-connector"] interval=10s} on-error={};
:put "SightOps: nenhum job pendente";
"""
    job_id = _text(job.get("id"))
    job_type = _text(job.get("type"))
    if job_type == "wireguard_install":
        row = get_connector(connector_id, include_token=True) or {}
        tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
        if not tunnel.get("client_private_key"):
            ensure_wireguard_tunnel(
                connector_id,
                {"lan_mode": "auto", "client_lans": "__auto__", "allow_empty_lans": True},
            )
            row = get_connector(connector_id, include_token=True) or {}
            tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
        endpoint = _text(tunnel.get("endpoint") or DEFAULT_WG_ENDPOINT)
        endpoint_host, _, endpoint_port = endpoint.partition(":")
        endpoint_port = endpoint_port or str(tunnel.get("listen_port") or 51820)
        client_address = _text(tunnel.get("client_address") or f"{DEFAULT_WG_NETWORK_PREFIX}.2/32")
        routeros_address = _wireguard_routeros_address(client_address)
        # inclui o bloco de transito isolado (10.201.0.0/16): p/ conectores com
        # tabela dedicada o servidor fala de 10.201.0.x, entao o cliente precisa
        # aceitar/rotear essa origem de volta (senao a resposta some).
        server_allowed = f"{DEFAULT_WG_NETWORK_PREFIX}.0/24,10.201.0.0/16"
        return f""":local result "wireguard_install:started";
:do {{/interface wireguard print count-only}} on-error={{:set result "wireguard_install:failed,RouterOS 7 required"; /tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json; :put "ERRO SightOps: WireGuard nao disponivel. Atualize o MikroTik para RouterOS 7."; :error "routeros-7-required";}};
:do {{/interface wireguard peers remove [/interface wireguard peers find interface="sightops-wg"];}} on-error={{}};
:do {{/ip address remove [find comment="SightOps WG"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG input"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG output"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG entrada"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG saida"];}} on-error={{}};
:do {{/ip route remove [find comment="SightOps WG"];}} on-error={{}};
:do {{/interface wireguard remove [/interface wireguard find name="sightops-wg"];}} on-error={{}};
/interface wireguard add name="sightops-wg" private-key="{_text(tunnel.get("client_private_key"))}" listen-port=13231 mtu=1420;
/ip address add address="{routeros_address}" interface="sightops-wg" comment="SightOps WG";
/interface wireguard peers add interface="sightops-wg" public-key="{_text(tunnel.get("server_public_key"))}" endpoint-address="{endpoint_host}" endpoint-port={endpoint_port} allowed-address="{server_allowed}" persistent-keepalive=25s comment="SightOps WG server";
/ip route add dst-address=10.201.0.0/16 gateway="sightops-wg" comment="SightOps WG";
/ip firewall filter add chain=input in-interface="sightops-wg" action=accept comment="SightOps WG input";
/ip firewall filter add chain=output out-interface="sightops-wg" action=accept comment="SightOps WG output";
/ip firewall filter add chain=forward in-interface="sightops-wg" action=accept comment="SightOps WG entrada";
/ip firewall filter add chain=forward out-interface="sightops-wg" action=accept comment="SightOps WG saida";
:do {{/ip firewall filter move [find comment="SightOps WG input"] destination=0}} on-error={{}};
:do {{/ip firewall filter move [find comment="SightOps WG output"] destination=0}} on-error={{}};
:do {{/ip firewall filter move [find comment="SightOps WG entrada"] destination=0}} on-error={{}};
:do {{/ip firewall filter move [find comment="SightOps WG saida"] destination=0}} on-error={{}};
:set result ("wireguard_install:done,{routeros_address},{endpoint_host}:{endpoint_port}");
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps WireGuard instalado: " . $result);
"""
    if job_type == "agent_upgrade":
        # Atualiza o proprio script do agente no roteador. Usa `set source=`
        # (substitui de uma vez) em vez de remove+add: se algo falhar, o script
        # antigo continua valendo e o conector nao fica mudo. O scheduler nao e
        # tocado -- ele so chama o script pelo nome.
        source = _routeros_agent_source(base_url, connector_id, token)
        return f""":local result "agent_upgrade:ok";
:do {{/system script set [find name="sightops-connector"] source={{{source}}};}} on-error={{:set result "agent_upgrade:failed";}};
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put $result;
"""

    if job_type == "wireguard_probe":
        return f""":local result "";
:local p1 [/ping address=10.250.0.1 src-address=10.250.0.2 count=3];
:local p2 [/ping address=10.250.0.1 count=3];
:local wgCount [:len [/interface wireguard find name="sightops-wg"]];
:local addrCount [:len [/ip address find interface="sightops-wg"]];
:local peerCount [:len [/interface wireguard peers find interface="sightops-wg"]];
:local inputPackets "missing"; :local outputPackets "missing"; :local forwardInPackets "missing"; :local forwardOutPackets "missing";
:local inputRule [/ip firewall filter find comment="SightOps WG input"]; :if ([:len $inputRule] > 0) do={{:set inputPackets [/ip firewall filter get [:pick $inputRule 0] packets]}};
:local outputRule [/ip firewall filter find comment="SightOps WG output"]; :if ([:len $outputRule] > 0) do={{:set outputPackets [/ip firewall filter get [:pick $outputRule 0] packets]}};
:local forwardInRule [/ip firewall filter find comment="SightOps WG entrada"]; :if ([:len $forwardInRule] > 0) do={{:set forwardInPackets [/ip firewall filter get [:pick $forwardInRule 0] packets]}};
:local forwardOutRule [/ip firewall filter find comment="SightOps WG saida"]; :if ([:len $forwardOutRule] > 0) do={{:set forwardOutPackets [/ip firewall filter get [:pick $forwardOutRule 0] packets]}};
:set result ("wireguard_probe:src=" . $p1 . ",auto=" . $p2 . ",wg=" . $wgCount . ",addr=" . $addrCount . ",peer=" . $peerCount . ",input_pkts=" . $inputPackets . ",output_pkts=" . $outputPackets . ",fwd_in_pkts=" . $forwardInPackets . ",fwd_out_pkts=" . $forwardOutPackets);
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps WireGuard probe: " . $result);
"""
    if job_type == "wireguard_diagnose":
        return f""":local result "wireguard_diagnose:";
:local wgCount [:len [/interface wireguard find name="sightops-wg"]];
:local addrCount [:len [/ip address find interface="sightops-wg"]];
:local peerCount [:len [/interface wireguard peers find interface="sightops-wg"]];
:set result ($result . " wg=" . $wgCount . " addr=" . $addrCount . " peer=" . $peerCount . ";");
:foreach i in=[/ip address find interface="sightops-wg"] do={{:set result ($result . " address|" . [/ip address get $i address] . "|" . [/ip address get $i network] . ";")}};
:foreach i in=[/interface wireguard peers find interface="sightops-wg"] do={{:set result ($result . " peer|" . [/interface wireguard peers get $i allowed-address] . "|" . [/interface wireguard peers get $i endpoint-address] . "|" . [/interface wireguard peers get $i endpoint-port] . "|" . [/interface wireguard peers get $i current-endpoint-address] . "|" . [/interface wireguard peers get $i last-handshake] . ";")}};
:foreach i in=[/ip route find where dst-address~"10.250"] do={{:set result ($result . " route|" . [/ip route get $i dst-address] . "|" . [/ip route get $i gateway] . "|" . [/ip route get $i active] . ";")}};
:local ruleN 0;
:foreach i in=[/ip firewall filter find] do={{:if ($ruleN < 60) do={{:local comment [/ip firewall filter get $i comment]; :local chain [/ip firewall filter get $i chain]; :local action [/ip firewall filter get $i action]; :local inIf [/ip firewall filter get $i in-interface]; :local outIf [/ip firewall filter get $i out-interface]; :local packets [/ip firewall filter get $i packets]; :set result ($result . " filter|" . $ruleN . "|" . $chain . "|" . $action . "|" . $inIf . "|" . $outIf . "|" . $packets . "|" . $comment . ";"); :set ruleN ($ruleN + 1)}}}};
:foreach i in=[/ip firewall nat find] do={{:local comment [/ip firewall nat get $i comment]; :if ($comment~"SightOps") do={{:set result ($result . " nat|" . [/ip firewall nat get $i chain] . "|" . [/ip firewall nat get $i action] . "|" . [/ip firewall nat get $i packets] . "|" . $comment . ";")}}}};
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps WireGuard diagnose: " . $result);
"""
    if job_type == "lan_inventory":
        return f""":local result "";
:foreach i in=[/ip dhcp-server lease find] do={{:local ip [/ip dhcp-server lease get $i address]; :local mac [/ip dhcp-server lease get $i mac-address]; :local host [/ip dhcp-server lease get $i host-name]; :local status [/ip dhcp-server lease get $i status]; :set result ($result . "dhcp|" . $ip . "|" . $mac . "|" . $host . "|" . $status . ";")}};
:foreach i in=[/ip arp find] do={{:local ip [/ip arp get $i address]; :local mac [/ip arp get $i mac-address]; :set result ($result . "arp|" . $ip . "|" . $mac . ";")}};
:foreach i in=[/ip neighbor find] do={{:local ip [/ip neighbor get $i address]; :local mac [/ip neighbor get $i mac-address]; :local ident [/ip neighbor get $i identity]; :local platform [/ip neighbor get $i platform]; :set result ($result . "neighbor|" . $ip . "|" . $mac . "|" . $ident . "|" . $platform . ";")}};
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps inventario LAN {job_id} executado");
"""
    if job_type == "access_http_get":
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        url = _routeros_safe_access_url(payload.get("url"))
        username = _routeros_string(payload.get("username"), 120)
        password = _routeros_string(payload.get("password"), 240)
        if not url:
            return f""":local result "fetch_error:url_invalida";
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
"""
        return f""":local result "";
:local ok "1";
:do {{
  :local response [/tool fetch url="{url}" user="{username}" password="{password}" http-auth-scheme=digest output=user as-value];
  :local status ($response->"status");
  :local data ($response->"data");
  :set result ("status=" . $status . ";data=" . $data);
}} on-error={{:set ok "0"; :set result "fetch_error"}};
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps access_http_get {job_id}: " . $result);
"""
    if job_type == "access_http_post":
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        url = _routeros_safe_access_url(payload.get("url"))
        username = _routeros_string(payload.get("username"), 120)
        password = _routeros_string(payload.get("password"), 240)
        content_type = _routeros_string(payload.get("content_type") or "application/json", 80)
        body = _routeros_string(payload.get("body"), 200000)
        if not url:
            return f""":local result "fetch_error:url_invalida";
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
"""
        return f""":local result "";
:local ok "1";
:do {{
  :local response [/tool fetch url="{url}" user="{username}" password="{password}" http-auth-scheme=digest http-method=post http-header-field="Content-Type:{content_type}" http-data="{body}" output=user as-value];
  :local status ($response->"status");
  :local data ($response->"data");
  :set result ("status=" . $status . ";data=" . $data);
}} on-error={{:set ok "0"; :set result "fetch_error"}};
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps access_http_post {job_id}: " . $result);
"""
    if job_type != "ping_many":
        return f""":local result "unsupported:0,";
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
"""
    raw_targets = (job.get("payload") or {}).get("targets") if isinstance(job.get("payload"), dict) else []
    targets = [_routeros_safe_target(item) for item in (raw_targets if isinstance(raw_targets, list) else [])]
    targets = [item for item in targets if item][:50]
    if not targets:
        targets = ["8.8.8.8"]
    target_list = ";".join(f'"{item}"' for item in targets)
    return f""":local result "";
:foreach target in={{{target_list}}} do={{:local received [/ping address=$target count=1]; :local online "0"; :if ($received > 0) do={{:set online "1"}}; :set result ($result . $target . ":" . $online . ",")}};
/tool fetch url="{base_url}/api/connectors/agent/routeros/jobs/{job_id}/result-text" http-method=post http-header-field="x-sightops-connector-id:{connector_id},x-sightops-connector-token:{token},Content-Type:text/plain" http-data=$result dst-path=sightops-job-result.json;
:put ("SightOps job {job_id} executado: " . $result);
"""


def build_routeros_job_script(base_url: str, connector_id: str, token: str) -> str:
    job = poll_job(connector_id, token).get("job")
    row = get_connector(connector_id, include_token=True) or {}
    saved_base_url = _text(row.get("public_base_url")).rstrip("/")
    if saved_base_url:
        base_url = saved_base_url
    return _routeros_job_script_template(base_url=base_url, connector_id=_text(connector_id), token=_text(token), job=job)


def delete_connector(connector_id: str) -> Dict[str, Any]:
    cid = _text(connector_id)
    with _lock:
        rows = _load_connectors()
        target = next((row for row in rows if _text(row.get("id")) == cid), None)
        if target is None or not _visible_to_current_tenant(target):
            return {"ok": True, "removed": 0}
        kept = [row for row in rows if _text(row.get("id")) != cid]
        jobs = [job for job in _load_jobs() if _text(job.get("connector_id")) != cid]
        _save_connectors(kept)
        _save_jobs(jobs)
    return {"ok": True, "removed": len(rows) - len(kept)}


def ensure_wireguard_tunnel(connector_id: str, payload: Dict[str, Any] | None = None, enforce_tenant: bool = False) -> Dict[str, Any]:
    """enforce_tenant=True na rota de usuario logado. Chamado tambem internamente
    pelo fluxo do proprio agente (wireguard_install job) sem contexto de tenant --
    la deve continuar False."""
    cid = _text(connector_id)
    data = payload if isinstance(payload, dict) else {}
    endpoint = _text(data.get("endpoint") or data.get("server_endpoint") or DEFAULT_WG_ENDPOINT)
    lan_mode = _text(data.get("lan_mode") or data.get("client_lans_mode") or "manual").lower()
    client_lans_raw = data.get("client_lans")
    allow_empty_lans = bool(data.get("allow_empty_lans") or data.get("bootstrap"))
    with _lock:
        rows = _load_connectors()
        idx = next((i for i, row in enumerate(rows) if _text(row.get("id")) == cid), -1)
        if idx < 0 or (enforce_tenant and not _visible_to_current_tenant(rows[idx])):
            raise ValueError("conector nao encontrado")
        row = rows[idx]
        if lan_mode in {"auto", "all", "detected"} or _text(client_lans_raw) == "__auto__":
            client_lans = _extract_connector_lans(row)
            if not client_lans and not allow_empty_lans:
                raise ValueError("nenhuma rede LAN detectada neste conector; atualize o conector ou informe uma rede especifica")
        elif isinstance(client_lans_raw, str):
            client_lans = _unique_cidrs([item.strip() for item in re.split(r"[\s,;]+", client_lans_raw) if item.strip()])
        elif isinstance(client_lans_raw, list):
            client_lans = _unique_cidrs(client_lans_raw)
        else:
            client_lans = []
        if not client_lans and not allow_empty_lans:
            raise ValueError("informe pelo menos uma rede LAN valida em CIDR, exemplo 192.168.20.0/24")
        tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
        # Todos os conectores falam com a mesma interface WireGuard do servidor.
        # A chave publica precisa ser a da plataforma, nunca um par aleatorio por cliente.
        tunnel["server_public_key"] = _wireguard_server_public_key()
        if not tunnel.get("client_private_key") or not tunnel.get("client_public_key"):
            client = _wg_keypair()
            tunnel["client_private_key"] = client["private_key"]
            tunnel["client_public_key"] = client["public_key"]
        requested_client_address = _text(data.get("client_address"))
        saved_client_address = _text(tunnel.get("client_address"))
        tunnel.update({
            "enabled": True,
            "type": "wireguard",
            "endpoint": endpoint,
            "listen_port": int(_text(data.get("listen_port")) or "51820"),
            "server_address": _text(data.get("server_address")) or f"{DEFAULT_WG_NETWORK_PREFIX}.1/24",
            "client_address": requested_client_address
            or saved_client_address
            or _next_wireguard_client_address(rows, cid),
            "client_lans": client_lans,
            "client_lans_mode": "auto" if lan_mode in {"auto", "all", "detected"} or _text(client_lans_raw) == "__auto__" or allow_empty_lans else "manual",
            "updated_at": _now(),
        })
        row["tunnel"] = tunnel
        rows[idx] = row
        _save_connectors(rows)
    public = _public_connector(row, include_token=False)
    if isinstance(public.get("tunnel"), dict):
        public["tunnel"].pop("server_private_key", None)
        public["tunnel"].pop("client_private_key", None)
    return {"ok": True, "connector": public, "tunnel": public.get("tunnel")}


def build_routeros_wireguard_script(connector_id: str) -> str:
    row = get_connector(connector_id, include_token=True, enforce_tenant=True)
    if not row:
        raise ValueError("conector nao encontrado")
    tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
    if not tunnel.get("client_private_key"):
        ensure_wireguard_tunnel(
            connector_id,
            {"lan_mode": "auto", "client_lans": "__auto__", "allow_empty_lans": True},
            enforce_tenant=True,
        )
        row = get_connector(connector_id, include_token=True) or row
        tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else tunnel
    endpoint = _text(tunnel.get("endpoint") or DEFAULT_WG_ENDPOINT)
    endpoint_host, _, endpoint_port = endpoint.partition(":")
    endpoint_port = endpoint_port or str(tunnel.get("listen_port") or 51820)
    client_address = _text(tunnel.get("client_address") or f"{DEFAULT_WG_NETWORK_PREFIX}.2/32")
    routeros_address = _wireguard_routeros_address(client_address)
    # inclui o bloco de transito isolado (10.201.0.0/16) -- ver nota no template do job.
    server_allowed = f"{DEFAULT_WG_NETWORK_PREFIX}.0/24,10.201.0.0/16"
    return f"""# SightOps WireGuard - RouterOS
# Cole no terminal do MikroTik do cliente. Requer RouterOS 7.

:do {{/interface wireguard print count-only}} on-error={{:put "ERRO SightOps: WireGuard nao disponivel. Atualize o MikroTik para RouterOS 7."; :error "routeros-7-required";}};

:do {{/interface wireguard peers remove [/interface wireguard peers find interface="sightops-wg"];}} on-error={{}};
:do {{/ip address remove [find comment="SightOps WG"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG input"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG output"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG entrada"];}} on-error={{}};
:do {{/ip firewall filter remove [find comment="SightOps WG saida"];}} on-error={{}};
:do {{/ip route remove [find comment="SightOps WG"];}} on-error={{}};
:do {{/interface wireguard remove [/interface wireguard find name="sightops-wg"];}} on-error={{}};

/interface wireguard add name="sightops-wg" private-key="{_text(tunnel.get("client_private_key"))}" listen-port=13231 mtu=1420;
/ip address add address="{routeros_address}" interface="sightops-wg" comment="SightOps WG";
/interface wireguard peers add interface="sightops-wg" public-key="{_text(tunnel.get("server_public_key"))}" endpoint-address="{endpoint_host}" endpoint-port={endpoint_port} allowed-address="{server_allowed}" persistent-keepalive=25s comment="SightOps WG server";
/ip route add dst-address=10.201.0.0/16 gateway="sightops-wg" comment="SightOps WG";
/ip firewall filter add chain=input in-interface="sightops-wg" action=accept comment="SightOps WG input";
/ip firewall filter add chain=output out-interface="sightops-wg" action=accept comment="SightOps WG output";
/ip firewall filter add chain=forward in-interface="sightops-wg" action=accept comment="SightOps WG entrada";
/ip firewall filter add chain=forward out-interface="sightops-wg" action=accept comment="SightOps WG saida";

:put "SightOps WireGuard configurado. O servidor sincroniza peer e rotas automaticamente."
:put "Cliente public-key: {_text(tunnel.get("client_public_key"))}"
:put "AllowedIPs servidor: {client_address}, {', '.join(_text(item) for item in (tunnel.get("client_lans") or []))}"
"""


def _agent_script_template(base_url: str, connector_id: str, token: str) -> str:
    base_url = base_url.rstrip("/")
    return f"""# SightOps Agent MVP
# Execute em PowerShell como usuario normal para testar.
# Depois este mesmo fluxo vira servico Windows.

$ErrorActionPreference = "SilentlyContinue"
$BaseUrl = "{base_url}"
$ConnectorId = "{connector_id}"
$Token = "{token}"
$Version = "0.1.0"

function Get-SightOpsHost {{
  $ips = @()
  try {{
    $ips = Get-NetIPAddress -AddressFamily IPv4 |
      Where-Object {{ $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" }} |
      Select-Object -ExpandProperty IPAddress
  }} catch {{}}
  $macs = @()
  try {{
    $macs = Get-NetAdapter | Where-Object {{ $_.Status -eq "Up" }} | Select-Object -ExpandProperty MacAddress
  }} catch {{}}
  return @{{
    hostname = $env:COMPUTERNAME
    user = [Environment]::UserName
    domain = [Environment]::UserDomainName
    os = (Get-CimInstance Win32_OperatingSystem).Caption
    ips = @($ips)
    macs = @($macs)
  }}
}}

function Invoke-SightOpsJson($Method, $Path, $Body) {{
  $headers = @{{
    "x-sightops-connector-id" = $ConnectorId
    "x-sightops-connector-token" = $Token
  }}
  $json = $null
  if ($null -ne $Body) {{ $json = ($Body | ConvertTo-Json -Depth 8) }}
  return Invoke-RestMethod -Method $Method -Uri "$BaseUrl$Path" -Headers $headers -ContentType "application/json" -Body $json
}}

function Test-SightOpsPing($Target) {{
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $ok = Test-Connection -ComputerName $Target -Count 1 -Quiet -ErrorAction SilentlyContinue
  $sw.Stop()
  return @{{
    target = $Target
    online = [bool]$ok
    rtt_ms = if ($ok) {{ [math]::Round($sw.Elapsed.TotalMilliseconds, 2) }} else {{ $null }}
  }}
}}

function Invoke-SightOpsJob($Job) {{
  if ($null -eq $Job) {{ return }}
  $result = @{{ ok = $true; result = @{{}}; error = "" }}
  try {{
    if ($Job.type -eq "ping_many") {{
      $targets = @($Job.payload.targets)
      $items = @()
      foreach ($target in $targets) {{
        if ([string]::IsNullOrWhiteSpace($target)) {{ continue }}
        $items += Test-SightOpsPing $target
      }}
      $result.result = @{{ targets = $targets; items = $items }}
    }} else {{
      $result.ok = $false
      $result.error = "Tipo de job nao suportado no agente"
    }}
  }} catch {{
    $result.ok = $false
    $result.error = $_.Exception.Message
  }}
  Invoke-SightOpsJson "POST" "/api/connectors/agent/jobs/$($Job.id)/result" $result | Out-Null
}}

Write-Host "SightOps Agent iniciado: $ConnectorId -> $BaseUrl"
Invoke-SightOpsJson "POST" "/api/connectors/agent/register" @{{ version = $Version; host = (Get-SightOpsHost) }} | Out-Null

while ($true) {{
  try {{
    Invoke-SightOpsJson "POST" "/api/connectors/agent/heartbeat" @{{ version = $Version; host = (Get-SightOpsHost) }} | Out-Null
    $poll = Invoke-SightOpsJson "GET" "/api/connectors/agent/jobs/poll" $null
    if ($poll.job) {{ Invoke-SightOpsJob $poll.job }}
  }} catch {{
    Write-Host ("Falha: " + $_.Exception.Message)
  }}
  Start-Sleep -Seconds 10
}}
"""


def _routeros_agent_source(base_url: str, connector_id: str, token: str) -> str:
    """Corpo do script do agente (o que vai dentro de source={...}).

    Isolado aqui pra ser reaproveitado pelo job agent_upgrade -- assim o
    script colado a mao e o atualizado remotamente nunca divergem."""
    base_url = base_url.rstrip("/")
    return f"""\
:local baseUrl "{base_url}";\
:local connectorId "{connector_id}";\
:local token "{token}";\
:local identity [/system identity get name];\
:local version [/system resource get version];\
:local board [/system routerboard get model];\
:local serial [/system routerboard get serial-number];\
:local uptime [/system resource get uptime];\
:local cpu [/system resource get cpu-load];\
:local totalMem [/system resource get total-memory];\
:local freeMem [/system resource get free-memory];\
:local dhcpCount [:len [/ip dhcp-server lease find]];\
:local arpCount [:len [/ip arp find]];\
:local neighborCount [:len [/ip neighbor find]];\
:local lanNetworks "";:local lanN 0;:foreach i in=[/ip address find disabled=no] do={{:if ($lanN < 100) do={{:local addr [/ip address get $i address];:local net [/ip address get $i network];:local slash [:find $addr "/"];:local prefix "";:if ([:typeof $slash] != "nil") do={{:set prefix [:pick $addr ($slash + 1) [:len $addr]]}};:if (($net != "") && ($prefix != "")) do={{:set lanNetworks ($lanNetworks . $net . "/" . $prefix . ";");:set lanN ($lanN + 1)}}}}}};\
:local addressSample "";:local addrN 0;:foreach i in=[/ip address find disabled=no] do={{:if ($addrN < 100) do={{:set addressSample ($addressSample . [/ip address get $i address] . "|" . [/ip address get $i interface] . ";");:set addrN ($addrN + 1)}}}};\
:local dhcpSample "";:local dhcpN 0;:foreach i in=[/ip dhcp-server lease find] do={{:if ($dhcpN < 500) do={{:set dhcpSample ($dhcpSample . [/ip dhcp-server lease get $i address] . "|" . [/ip dhcp-server lease get $i mac-address] . "|" . [/ip dhcp-server lease get $i status] . ";");:set dhcpN ($dhcpN + 1)}}}};\
:local arpSample "";:local arpN 0;:foreach i in=[/ip arp find] do={{:if ($arpN < 500) do={{:set arpSample ($arpSample . [/ip arp get $i address] . "|" . [/ip arp get $i mac-address] . ";");:set arpN ($arpN + 1)}}}};\
:local winboxPort "";:local winboxOff "";:local wwwPort "";:local wwwOff "";:do {{:set winboxPort [/ip service get [find name="winbox"] port];:set winboxOff [/ip service get [find name="winbox"] disabled];}} on-error={{}};:do {{:set wwwPort [/ip service get [find name="www"] port];:set wwwOff [/ip service get [find name="www"] disabled];}} on-error={{}};:local neighborSample "";:local neighN 0;:foreach i in=[/ip neighbor find] do={{:if ($neighN < 500) do={{:set neighborSample ($neighborSample . [/ip neighbor get $i address] . "|" . [/ip neighbor get $i mac-address] . ";");:set neighN ($neighN + 1)}}}};\
:local payload ("{{\\"version\\":\\"routeros-0.7\\",\\"host\\":{{\\"hostname\\":\\"" . $identity . "\\",\\"os\\":\\"RouterOS\\",\\"model\\":\\"" . $board . "\\",\\"serial\\":\\"" . $serial . "\\",\\"routeros\\":\\"" . $version . "\\",\\"uptime\\":\\"" . $uptime . "\\",\\"cpu_load\\":\\"" . $cpu . "\\",\\"memory_free\\":\\"" . $freeMem . "\\",\\"memory_total\\":\\"" . $totalMem . "\\"}},\\"inventory\\":{{\\"dhcp_leases\\":\\"" . $dhcpCount . "\\",\\"arp_entries\\":\\"" . $arpCount . "\\",\\"neighbors\\":\\"" . $neighborCount . "\\",\\"lan_networks\\":\\"" . $lanNetworks . "\\",\\"address_sample\\":\\"" . $addressSample . "\\",\\"dhcp_sample\\":\\"" . $dhcpSample . "\\",\\"arp_sample\\":\\"" . $arpSample . "\\",\\"neighbor_sample\\":\\"" . $neighborSample . "\\",\\"winbox_port\\":\\"" . $winboxPort . "\\",\\"winbox_disabled\\":\\"" . $winboxOff . "\\",\\"www_port\\":\\"" . $wwwPort . "\\",\\"www_disabled\\":\\"" . $wwwOff . "\\"}}}}");\
/tool fetch url=($baseUrl . "/api/connectors/agent/heartbeat") http-method=post http-header-field=("x-sightops-connector-id:" . $connectorId . ",x-sightops-connector-token:" . $token . ",Content-Type:application/json") http-data=$payload dst-path=sightops-connector-last.json;\
/tool fetch url=($baseUrl . "/api/connectors/agent/routeros/job.rsc") http-method=get http-header-field=("x-sightops-connector-id:" . $connectorId . ",x-sightops-connector-token:" . $token) dst-path=sightops-routeros-job.rsc;\
/import file-name=sightops-routeros-job.rsc;\
"""


def _routeros_script_template(base_url: str, connector_id: str, token: str) -> str:
    base_url = base_url.rstrip("/")
    source = _routeros_agent_source(base_url, connector_id, token)
    return f"""# SightOps RouterOS Connector MVP
# Cole no terminal do MikroTik. Ele cria um script e um scheduler de heartbeat.

:local baseUrl "{base_url}"
:local connectorId "{connector_id}"
:local token "{token}"

/system script remove [find name="sightops-connector"] 
/system scheduler remove [find name="sightops-connector"] 

/system script add name="sightops-connector" policy=read,write,test,policy source={{{source}}}

/system scheduler add name="sightops-connector" interval=1m start-time=startup on-event="/system script run sightops-connector"
/system script run sightops-connector

:put "SightOps RouterOS Connector instalado. O sinal deve aparecer online em ate 1 minuto."
"""


def build_agent_script(base_url: str, connector_id: str) -> str:
    row = get_connector(connector_id, include_token=True, enforce_tenant=True)
    if not row:
        raise ValueError("conector nao encontrado")
    return _agent_script_template(base_url=base_url, connector_id=_text(row.get("id")), token=_text(row.get("token")))


def build_routeros_script(base_url: str, connector_id: str) -> str:
    row = get_connector(connector_id, include_token=True, enforce_tenant=True)
    if not row:
        raise ValueError("conector nao encontrado")
    heartbeat_script = _routeros_script_template(
        base_url=base_url,
        connector_id=_text(row.get("id")),
        token=_text(row.get("token")),
    )
    if _normalize_access_mode(row.get("access_mode"), row.get("type")) == "public":
        return heartbeat_script

    ensure_wireguard_tunnel(
        connector_id,
        {"lan_mode": "auto", "client_lans": "__auto__", "allow_empty_lans": True},
        enforce_tenant=True,
    )
    wireguard_script = build_routeros_wireguard_script(connector_id)
    return f"""{wireguard_script}

# SightOps RouterOS Connector - heartbeat e jobs
{heartbeat_script}
"""
