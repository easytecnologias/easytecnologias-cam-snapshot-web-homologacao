from __future__ import annotations

import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from app.core.tenant_context import tenant_recorder_inventory_path, tenant_scoped_path, tenant_snapshot_dir
from app.services.connector_service import get_connector, list_connectors, register_connector_known_targets
from app.services import connector_routing_vnat as _vnat
from app.services.inventory_json import inventory_row_key, load_inventory_json, save_inventory_json
from app.services.camsnapshot.device_info import get_network_config, set_network_ip, set_channel_title
from app.api.endpoints.nvr import _recorder_connector_for_host

router = APIRouter(prefix="/api/deployments", tags=["deployments"])

# marca ja detectada por gravador (base|usuario) -- evita um probe por acao
_RECORDER_FAMILY_CACHE: Dict[str, str] = {}


def _deployments_path() -> Path:
    return tenant_scoped_path("deployments.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm_mac(value: Any) -> str:
    text = _text(value).lower().replace("-", ":").replace(".", ":")
    text = re.sub(r"[^0-9a-f:]", "", text)
    text = re.sub(r":+", ":", text).strip(":")
    if ":" not in text and len(text) == 12:
        text = ":".join(text[i:i + 2] for i in range(0, 12, 2))
    return text


def _parse_lat_lon(value: Any) -> tuple[str, str]:
    text = _text(value).replace(";", ",")
    if not text:
        return "", ""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        return "", ""
    try:
        lat = float(parts[0])
        lon = float(parts[1])
    except Exception:
        return "", ""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "", ""
    return f"{lat:.8f}".rstrip("0").rstrip("."), f"{lon:.8f}".rstrip("0").rstrip(".")


def _reach_deploy_host(host: str, connector_id: str = "") -> str:
    """Gravador atras de conector isolado -> IP virtual (vnat) pra o
    container da API alcancar. Sem mapa vnat, devolve o host real intacto."""
    real = _text(host)
    if not real:
        return real
    cid = _text(connector_id)
    if not cid:
        try:
            cid = _recorder_connector_for_host(real)
        except Exception:
            cid = ""
    try:
        return _vnat.virtual_ip_for(cid, real) or real
    except Exception:
        return real


def _recorder_base_url(host: str, port: Any = None) -> str:
    text = _text(host)
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text.rstrip("/")
    p = _text(port)
    if p and p not in ("80", "0"):
        return f"http://{text}:{p}".rstrip("/")
    return f"http://{text}".rstrip("/")


def _parse_recorder_info(text: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    accepted = {
        "deviceclass", "devicetype", "serialnumber", "machine_name",
        "hardwareversion", "softwareversion", "type", "model",
        "producttype", "productname", "machinemodel",
    }
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        raw_key = key.strip().lower()
        normalized_key = next(
            (candidate for candidate in accepted if raw_key == candidate or raw_key.endswith(f".{candidate}")),
            "",
        )
        if normalized_key:
            info[normalized_key] = value.strip()
    return info


def _recorder_model(info: Dict[str, str]) -> str:
    candidates = (
        info.get("devicetype"), info.get("model"), info.get("producttype"),
        info.get("productname"), info.get("machinemodel"), info.get("deviceclass"),
    )
    for value in candidates:
        text = _text(value)
        numeric_only = bool(re.fullmatch(r"\d+(?:[.,]\d+)?(?:\s*(?:ch|channel|canais))?", text, re.IGNORECASE))
        generic = text.lower() in {"nvr", "dvr", "ipc", "device"}
        if text and not numeric_only and not generic:
            return text
    return ""


def _try_recorder_request(url: str, user: str, password: str, timeout: float) -> requests.Response:
    last_exc: Exception | None = None
    last_resp: requests.Response | None = None
    for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
        try:
            resp = requests.get(url, auth=auth, timeout=timeout, verify=False)
            last_resp = resp
            if resp.status_code not in (401, 403):
                return resp
        except Exception as exc:
            last_exc = exc
    if last_resp is not None:
        return last_resp
    if last_exc:
        raise last_exc
    raise RuntimeError("falha desconhecida")


def _set_config_url(base: str, params: Dict[str, Any]) -> str:
    return f"{base}/cgi-bin/configManager.cgi?action=setConfig&{urlencode(params)}"


def _hik_input_proxy_xml(channel: int, camera_ip: str, camera_user: str, camera_password: str,
                         title: str, manage_port: int, protocol: str) -> str:
    """XML_InputProxyChannel (ISAPI 16.2.169) pra vincular uma camera a um canal.

    adminProtocol: HIKVISION fala com camera Hikvision; ONVIF cobre as outras
    marcas (Intelbras, por exemplo). managePortNo e a porta de GERENCIA -- 8000
    no protocolo proprietario, 80 no ONVIF -- nao a porta web da camera."""
    import xml.sax.saxutils as _x

    def e(v: Any) -> str:
        return _x.escape(str(v or ""))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<InputProxyChannel version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        f"<id>{int(channel)}</id>"
        f"<name>{e(title)}</name>"
        "<sourceInputPortDescriptor>"
        f"<adminProtocol>{e(protocol)}</adminProtocol>"
        "<addressingFormatType>ipaddress</addressingFormatType>"
        f"<ipAddress>{e(camera_ip)}</ipAddress>"
        f"<managePortNo>{int(manage_port)}</managePortNo>"
        "<srcInputPort>1</srcInputPort>"
        f"<userName>{e(camera_user)}</userName>"
        f"<password>{e(camera_password)}</password>"
        "<streamType>auto</streamType>"
        "</sourceInputPortDescriptor>"
        "</InputProxyChannel>"
    )


def _hik_request(method: str, url: str, user: str, password: str, body: str = "", timeout: float = 10.0) -> requests.Response:
    """ISAPI aceita digest; algumas versoes antigas so basic -- tenta os dois."""
    last_exc: Exception | None = None
    last_resp: requests.Response | None = None
    for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
        try:
            resp = requests.request(
                method, url, auth=auth, timeout=timeout, verify=False,
                data=body.encode("utf-8") if body else None,
                headers={"Content-Type": "application/xml"} if body else None,
            )
            last_resp = resp
            if resp.status_code not in (401, 403):
                return resp
        except Exception as exc:
            last_exc = exc
    if last_resp is not None:
        return last_resp
    if last_exc:
        raise last_exc
    raise RuntimeError("falha desconhecida")


def _hik_response_ok(resp: requests.Response) -> bool:
    if not (200 <= int(resp.status_code) < 300):
        return False
    corpo = (resp.text or "").lower()
    # ISAPI devolve 200 mesmo recusando: quem decide e o statusCode do XML.
    if "<statuscode>" in corpo:
        return "<statuscode>1<" in corpo or "ok" in corpo
    return True


def _hik_add_camera(base: str, user: str, password: str, channel: int, camera_ip: str,
                    camera_user: str, camera_password: str, title: str) -> tuple[bool, str]:
    """Vincula a camera ao canal. Tenta o protocolo proprietario primeiro (camera
    Hikvision) e cai pra ONVIF (outras marcas). Se o canal ja existe, o POST
    recusa -- ai configura por PUT no canal."""
    url_lista = f"{base}/ISAPI/ContentMgmt/InputProxy/channels"
    url_canal = f"{url_lista}/{int(channel)}"
    ultimo = ""
    for protocolo, porta in (("HIKVISION", 8000), ("ONVIF", 80)):
        corpo = _hik_input_proxy_xml(channel, camera_ip, camera_user, camera_password, title, porta, protocolo)
        for metodo, url in (("PUT", url_canal), ("POST", url_lista)):
            try:
                resp = _hik_request(metodo, url, user, password, corpo)
            except Exception as exc:
                ultimo = f"{protocolo}/{metodo}: {exc}"
                continue
            if _hik_response_ok(resp):
                return True, f"{protocolo} via {metodo}"
            ultimo = f"{protocolo}/{metodo}: HTTP {resp.status_code} {(resp.text or '').strip()[:120]}"
    return False, ultimo or "sem resposta do gravador"


def _hik_remove_camera(base: str, user: str, password: str, channel: int) -> tuple[bool, str]:
    """Solta o canal (ISAPI 15.2.8, DELETE)."""
    try:
        resp = _hik_request("DELETE", f"{base}/ISAPI/ContentMgmt/InputProxy/channels/{int(channel)}", user, password)
    except Exception as exc:
        return False, str(exc)
    if _hik_response_ok(resp):
        return True, "canal liberado"
    return False, f"HTTP {resp.status_code} {(resp.text or '').strip()[:120]}"


def _recorder_set_config(base: str, user: str, password: str, params: Dict[str, Any], timeout: float = 8.0) -> requests.Response:
    return _try_recorder_request(_set_config_url(base, params), user, password, timeout=timeout)


def _recorder_config_ok(resp: requests.Response) -> bool:
    if not (200 <= int(resp.status_code) < 300):
        return False
    body = (resp.text or "").strip().lower()
    return not body or "error" not in body


def _parse_channel_titles(text: str) -> Dict[int, str]:
    titles: Dict[int, str] = {}
    for idx, name in re.findall(r"ChannelTitle\[(\d+)\]\.Name=([^\r\n]*)", text or ""):
        try:
            ch = int(idx) + 1
        except Exception:
            continue
        titles[ch] = _text(name)
    return titles


def _parse_remote_device_channels(text: str) -> Dict[int, Dict[str, str]]:
    grouped: Dict[int, Dict[str, str]] = {}
    for idx, key, value in re.findall(r"(?:RemoteDevice|RemoteDeviceInfo|NetWorkCam|Camera|IPC)\[(\d+)\]\.([^=\r\n]+)=([^\r\n]*)", text or ""):
        try:
            ch = int(idx) + 1
        except Exception:
            continue
        grouped.setdefault(ch, {})[key.strip().lower()] = value.strip()
    for idx, key, value in re.findall(r"RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.([^=\r\n]+)=([^\r\n]*)", text or ""):
        try:
            ch = int(idx) + 1
        except Exception:
            continue
        grouped.setdefault(ch, {})[key.strip().lower()] = value.strip()
    used: Dict[int, Dict[str, str]] = {}
    for ch, fields in grouped.items():
        def field(*names: str) -> str:
            for name in names:
                wanted = name.lower()
                for key, value in fields.items():
                    if key == wanted or key.endswith(f".{wanted}"):
                        found = _text(value)
                        if found:
                            return found
            return ""

        enabled = _text(fields.get("enable") or fields.get("enabled")).lower()
        disabled = enabled in ("false", "0", "no", "off")
        address = _text(
            fields.get("address")
            or fields.get("ipaddress")
            or fields.get("ip")
            or fields.get("host")
            or fields.get("url")
        )
        placeholder_address = address in ("0.0.0.0", "192.168.0.0")
        name = _text(
            fields.get("videoinputs[0].name")
            or fields.get("name")
            or fields.get("devicename")
            or fields.get("title")
        )
        model = field("devicetype", "deviceclass", "devicemodel", "machinemodel", "model", "productname")
        mac = field("mac", "macaddress", "physicaladdress")
        if not disabled and not placeholder_address and (address or name):
            used[ch] = {"camera_ip": address, "title": name, "camera_model": model, "camera_mac": mac}
    return used


def _recorder_family(base: str, user: str, password: str) -> str:
    """"intelbras" (CGI) ou "hikvision" (ISAPI), perguntando ao proprio
    gravador. O assistente so falava CGI; num Hikvision as etapas depois do
    login (canais, snapshot, adicionar camera) caiam em silencio."""
    chave = f"{base}|{user}"
    cache = _RECORDER_FAMILY_CACHE.get(chave)
    if cache:
        return cache
    familia = "intelbras"
    try:
        resp = _try_recorder_request(f"{base}/ISAPI/System/deviceInfo", user, password, timeout=4.0)
        codigo = int(resp.status_code)
        if 200 <= codigo < 300 and "deviceinfo" in (resp.text or "").lower():
            familia = "hikvision"
        elif codigo in (401, 403):
            # A rota ISAPI existe e so pediu credencial -- num Intelbras ela
            # nem existiria (404). Sem isso, senha errada fazia o gravador
            # Hikvision ser tratado como Intelbras e as acoes falhavam mudas.
            familia = "hikvision"
    except Exception:
        pass
    # So memoriza deteccao feita COM credencial aceita; senao um erro de senha
    # congelaria a marca pro resto da vida do processo.
    if familia == "hikvision" or password:
        _RECORDER_FAMILY_CACHE[chave] = familia
    return familia


def _fetch_hik_live_channels(base: str, user: str, password: str, total: int) -> Tuple[Dict[int, Dict[str, str]], bool]:
    """Canais ocupados de um NVR Hikvision, pelo InputProxy (ISAPI).

    Reaproveita os parsers ja usados no inventario de gravadores (nvr.py), que
    tratam as variacoes de XML entre firmwares -- nao vale reescrever isso."""
    from requests.auth import HTTPDigestAuth as _Digest

    try:
        from app.api.endpoints.nvr import (
            _hik_get_text,
            _parse_hik_channels,
            _parse_hik_channel_ips,
            _parse_hik_channel_models,
        )
    except Exception:
        return {}, False

    auth = _Digest(user, password)
    xml = _hik_get_text(f"{base}/ISAPI/ContentMgmt/InputProxy/channels", auth, 6.0)
    if not xml:
        xml = _hik_get_text(f"{base}/ISAPI/System/Video/inputs/channels", auth, 6.0)
    if not xml:
        return {}, False

    nomes = _parse_hik_channels(xml)
    ips = _parse_hik_channel_ips(xml)
    modelos = _parse_hik_channel_models(xml)

    try:
        teto = max(1, min(int(total or 32), 128))
    except Exception:
        teto = 32

    used: Dict[int, Dict[str, str]] = {}
    # Canal so conta como OCUPADO se tem camera atras (ip/modelo). Nome sozinho
    # nao serve: o firmware ja vem com "Camera 01".."Camera 32" preenchidos, e
    # ai o assistente mostraria o gravador inteiro como cheio.
    for ch in sorted(set(nomes) | set(ips) | set(modelos)):
        if not (1 <= ch <= teto):
            continue
        ip = str(ips.get(ch) or "").strip()
        modelo = str(modelos.get(ch) or "").strip()
        if not ip and not modelo:
            continue
        dados: Dict[str, str] = {}
        if ip:
            dados["camera_ip"] = ip
        if modelo:
            dados["model"] = modelo
        titulo = str(nomes.get(ch) or "").strip()
        if titulo:
            dados["title"] = titulo
        used[ch] = dados
    return used, True


def _fetch_recorder_live_channels(base: str, user: str, password: str, total: int) -> Tuple[Dict[int, Dict[str, str]], bool]:
    if _recorder_family(base, user, password) == "hikvision":
        return _fetch_hik_live_channels(base, user, password, total)
    return _fetch_intelbras_live_channels(base, user, password, total)


def _fetch_intelbras_live_channels(base: str, user: str, password: str, total: int) -> Tuple[Dict[int, Dict[str, str]], bool]:
    titles: Dict[int, str] = {}
    used: Dict[int, Dict[str, str]] = {}
    remote_success = False
    try:
        title_resp = _try_recorder_request(
            f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
            user,
            password,
            timeout=5.0,
        )
        if 200 <= title_resp.status_code < 300:
            titles = _parse_channel_titles(title_resp.text)
    except Exception:
        pass

    remote_paths = (
        "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice",
        "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDeviceInfo",
        "/cgi-bin/configManager.cgi?action=getConfig&name=InputProxy",
        "/cgi-bin/configManager.cgi?action=getConfig&name=NetWorkCam",
        "/cgi-bin/configManager.cgi?action=getConfig&name=Camera",
        "/cgi-bin/configManager.cgi?action=getConfig&name=IPC",
    )
    for path in remote_paths:
        try:
            resp = _try_recorder_request(f"{base}{path}", user, password, timeout=5.0)
            if 200 <= resp.status_code < 300:
                remote_success = True
                remote_used = _parse_remote_device_channels(resp.text)
                for ch, data in remote_used.items():
                    used.setdefault(ch, {}).update({k: v for k, v in data.items() if v})
        except Exception:
            continue

    for ch, title in titles.items():
        if ch in used and title:
            used[ch].setdefault("title", title)

    try:
        total = int(total or 32)
    except Exception:
        total = 32
    return {ch: data for ch, data in used.items() if 1 <= ch <= max(1, min(total, 128))}, remote_success


def _capture_recorder_snapshots(
    base: str,
    user: str,
    password: str,
    host: str,
    channels: Dict[int, Dict[str, str]],
) -> None:
    if not channels:
        return
    snap_dir = tenant_snapshot_dir("nvr")
    safe_host = re.sub(r"[^0-9A-Za-z_-]+", "_", host).strip("_") or "nvr"
    hik = _recorder_family(base, user, password) == "hikvision"

    def capture(channel: int) -> tuple[int, str]:
        # Hikvision nao tem snapshot.cgi: a foto do canal sai pelo ISAPI, e o
        # id do stream e canal*100+1 (canal 1 -> 101).
        url = (
            f"{base}/ISAPI/Streaming/channels/{int(channel) * 100 + 1}/picture"
            if hik
            else f"{base}/cgi-bin/snapshot.cgi?channel={int(channel)}"
        )
        for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
            try:
                resp = requests.get(url, auth=auth, timeout=(2.0, 6.0), stream=True, verify=False)
                ctype = str(resp.headers.get("Content-Type") or "").lower()
                if resp.status_code != 200 or "image" not in ctype:
                    continue
                filename = f"deploy_{safe_host}_ch{int(channel):03d}.jpg"
                target = snap_dir / filename
                with target.open("wb") as handle:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            handle.write(chunk)
                return channel, f"/data/nvr_snapshot/{filename}"
            except Exception:
                continue
        return channel, ""

    with ThreadPoolExecutor(max_workers=min(6, len(channels))) as pool:
        futures = [pool.submit(capture, channel) for channel in channels]
        for future in as_completed(futures):
            channel, snapshot_url = future.result()
            if snapshot_url and channel in channels:
                channels[channel]["snapshot_url"] = snapshot_url


def _read_rows() -> List[Dict[str, Any]]:
    path = _deployments_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _write_rows(rows: List[Dict[str, Any]]) -> None:
    path = _deployments_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_recorder_rows(source: str) -> List[Dict[str, Any]]:
    path = tenant_recorder_inventory_path(source)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_recorder_rows(source: str, rows: List[Dict[str, Any]]) -> None:
    path = tenant_recorder_inventory_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _upsert_recorder_channel(payload: Dict[str, Any], camera_row: Dict[str, Any]) -> Dict[str, Any]:
    source = _text(payload.get("recorder_type")).lower()
    if source not in ("nvr", "dvr"):
        return {"ok": False, "skipped": True, "reason": "tipo de gravador nao informado"}

    host = _text(payload.get("recorder_host"))
    try:
        channel = int(_text(payload.get("recorder_channel")) or "0")
    except Exception:
        channel = 0
    if not host or channel <= 0:
        return {"ok": False, "skipped": True, "reason": "host/canal do gravador nao informados"}

    rows = _read_recorder_rows(source)
    title = _text(payload.get("recorder_title")) or _text(payload.get("camera_title")) or f"Canal {channel:02d}"
    camera_ip = _text(payload.get("recorder_camera_ip")) or _text(camera_row.get("ip"))
    camera_mac = _text(camera_row.get("mac"))
    camera_model = _text(camera_row.get("modelo") or camera_row.get("model"))
    site = _text(camera_row.get("local") or camera_row.get("site") or payload.get("site"))
    channel_row = {
        "host": host,
        "channel": channel,
        "title": title,
        "local": site,
        "status": "online",
        "camera_ip": camera_ip,
        "camera_mac": camera_mac,
        "camera_model": camera_model,
        "modelo": camera_model if source == "dvr" else _text(payload.get("recorder_model")),
        "recorder_user": _text(payload.get("recorder_user")),
        "remote": bool(_text(payload.get("connector_id"))),
        "remote_connector_id": _text(payload.get("connector_id")),
        "inventory_mode": _normalize_inventory_mode(_text(payload.get("inventory_mode"))),
        "updated_at": _now(),
    }

    updated = False
    for idx, existing in enumerate(rows):
        existing_host = _text(existing.get("host") or existing.get("ip"))
        try:
            existing_channel = int(existing.get("channel") or 0)
        except Exception:
            existing_channel = 0
        if existing_host == host and existing_channel == channel:
            rows[idx] = {**existing, **channel_row}
            updated = True
            break
    if not updated:
        rows.append(channel_row)
    _write_recorder_rows(source, rows)
    return {"ok": True, "source": source, "host": host, "channel": channel, "camera_ip": camera_ip, "updated": updated}


def _recorder_channel_grid(
    source: str,
    host: str,
    total: int = 32,
    live_used: Dict[int, Dict[str, str]] | None = None,
    live_authoritative: bool = False,
) -> List[Dict[str, Any]]:
    rows = _read_recorder_rows(source)
    used: Dict[int, Dict[str, Any]] = {}
    host_norm = _text(host)
    if not live_authoritative:
        for row in rows:
            row_host = _text(row.get("host") or row.get("ip"))
            if row_host != host_norm:
                continue
            try:
                ch = int(row.get("channel") or 0)
            except Exception:
                ch = 0
            if ch <= 0:
                continue
            used[ch] = row
    for ch, data in (live_used or {}).items():
        used[ch] = {**used.get(ch, {}), **data, "live": True}
    try:
        total = int(total or 32)
    except Exception:
        total = 32
    total = max(1, min(total, 128))
    return [
        {
            "channel": ch,
            "used": ch in used,
            "source": "nvr" if used.get(ch, {}).get("live") else "inventario",
            "title": _text(used.get(ch, {}).get("title") or used.get(ch, {}).get("titulo")),
            "camera_ip": _text(used.get(ch, {}).get("camera_ip")),
            "camera_model": _text(used.get(ch, {}).get("camera_model") or used.get(ch, {}).get("modelo")),
            "camera_mac": _text(used.get(ch, {}).get("camera_mac") or used.get(ch, {}).get("mac")),
            "snapshot_url": _text(used.get(ch, {}).get("snapshot_url")),
        }
        for ch in range(1, total + 1)
    ]


def _connector_inventory(connector_id: str) -> Dict[str, Any]:
    row = get_connector(connector_id, include_token=False, enforce_tenant=True)
    if not row:
        raise HTTPException(status_code=404, detail="conector nao encontrado")
    inventory = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
    return {"connector": row, "inventory": inventory}


def _inventory_sources(inv: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for source, key in (("dhcp", "dhcp_rows"), ("arp", "arp_rows"), ("neighbor", "neighbor_rows")):
        for item in inv.get(key) or []:
            if not isinstance(item, dict):
                continue
            found = dict(item)
            found["source"] = source
            if not found.get("ip") and found.get("address"):
                found["ip"] = found.get("address")
            found["mac_norm"] = _norm_mac(found.get("mac") or found.get("mac_address"))
            rows.append(found)
    for source, key in (("dhcp", "dhcp_sample"), ("arp", "arp_sample"), ("neighbor", "neighbor_sample")):
        sample = _text(inv.get(key))
        if not sample:
            continue
        for chunk in sample.split(";"):
            parts = [part.strip() for part in chunk.split("|")]
            if len(parts) < 2 or not parts[0]:
                continue
            found = {
                "source": source,
                "ip": parts[0],
                "address": parts[0],
                "mac": parts[1],
                "status": parts[2] if len(parts) > 2 else "",
                "mac_norm": _norm_mac(parts[1]),
            }
            rows.append(found)
    return rows


def _lookup_in_connector(connector_id: str, query: str = "") -> Dict[str, Any]:
    data = _connector_inventory(connector_id)
    q = _text(query)
    q_mac = _norm_mac(q)
    q_low = q.lower()
    matches: List[Dict[str, Any]] = []
    for item in _inventory_sources(data["inventory"]):
        values = [
            _text(item.get("ip")),
            _text(item.get("address")),
            _text(item.get("host")),
            _text(item.get("identity")),
            _text(item.get("platform")),
            _text(item.get("mac")),
            _text(item.get("mac_address")),
            _text(item.get("mac_norm")),
        ]
        if not q or any(q_low and q_low in value.lower() for value in values) or (q_mac and q_mac == item.get("mac_norm")):
            matches.append(item)
    return {"ok": True, "connector": data["connector"], "matches": matches[:100], "count": len(matches)}


def _ip_in_use(ip: str, connector_id: str = "", site: str = "") -> Dict[str, Any]:
    wanted = _text(ip)
    matches: List[Dict[str, Any]] = []
    if connector_id:
        data = _connector_inventory(connector_id)
        for item in _inventory_sources(data["inventory"]):
            if _text(item.get("ip") or item.get("address")) == wanted:
                matches.append(item)
    for mode in ("basic", "olt", "switch"):
        for row in load_inventory_json(mode=mode, site=site) or []:
            if _text(row.get("ip") or row.get("IP")) == wanted:
                found = dict(row)
                found["source"] = f"inventory_{mode}"
                matches.append(found)
    return {"ip": wanted, "in_use": bool(matches), "matches": matches[:50]}


@router.get("")
def api_deployments_list() -> Dict[str, Any]:
    rows = list(reversed(_read_rows()))
    return {"ok": True, "deployments": rows[:100], "count": len(rows)}


@router.get("/lookup")
def api_deployments_lookup(connector_id: str, query: str = "") -> Dict[str, Any]:
    return _lookup_in_connector(connector_id, query)


@router.get("/ip-check")
def api_deployments_ip_check(ip: str, connector_id: str = "", site: str = "") -> Dict[str, Any]:
    if not _text(ip):
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    return {"ok": True, **_ip_in_use(ip, connector_id=connector_id, site=site)}


@router.post("/apply-camera-ip")
def api_deployments_apply_camera_ip(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Aplica um novo IP direto na camera (CGI Dahua/Intelbras), herdando
    mascara/gateway da config atual dela. Equipamento vivo -- ver aviso na UI."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    ip = _text(payload.get("ip"))
    new_ip = _text(payload.get("new_ip"))
    user = _text(payload.get("usuario")) or "admin"
    password = _text(payload.get("senha"))
    if not ip:
        raise HTTPException(status_code=400, detail="ip atual da camera obrigatorio")
    if not new_ip:
        raise HTTPException(status_code=400, detail="novo ip obrigatorio")
    if not password:
        raise HTTPException(status_code=400, detail="senha da camera obrigatoria")

    net = get_network_config(ip, user, password)
    if not net or not net.get("subnet_mask"):
        raise HTTPException(status_code=502, detail="Nao consegui ler a configuracao de rede atual da camera.")

    result = set_network_ip(ip, user, password, new_ip, net["subnet_mask"], net.get("gateway") or "")
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Falha ao aplicar novo IP na camera: {result.get('response') or result.get('error') or 'sem detalhe'}",
        )
    return {
        "ok": True,
        "ip": ip,
        "new_ip": new_ip,
        "subnet_mask": net["subnet_mask"],
        "gateway": net.get("gateway") or "",
        "response": result.get("response"),
    }


@router.post("/save-camera-title")
def api_deployments_save_camera_title(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Grava o titulo/nome (OSD) direto na camera fisica (CGI Dahua/Intelbras)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    ip = _text(payload.get("ip"))
    title = _text(payload.get("title"))
    user = _text(payload.get("usuario")) or "admin"
    password = _text(payload.get("senha"))
    if not ip:
        raise HTTPException(status_code=400, detail="ip da camera obrigatorio")
    if not title:
        raise HTTPException(status_code=400, detail="titulo obrigatorio")
    if not password:
        raise HTTPException(status_code=400, detail="senha da camera obrigatoria")

    result = set_channel_title(ip, user, password, title)
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"Falha ao gravar titulo na camera: {result.get('response') or result.get('error') or 'sem detalhe'}",
        )
    return {"ok": True, "ip": ip, "title": title, "response": result.get("response")}


@router.post("")
def api_deployments_save(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    row = dict(payload)
    row["id"] = _text(row.get("id")) or secrets.token_hex(8)
    row["created_at"] = row.get("created_at") or _now()
    row["updated_at"] = _now()
    rows = _read_rows()
    rows = [item for item in rows if _text(item.get("id")) != row["id"]]
    rows.append(row)
    _write_rows(rows)
    return {"ok": True, "deployment": row}


def _normalize_inventory_mode(value: str) -> str:
    v = (value or "").strip().lower()
    if v in ("basico", "basic"):
        return "basic"
    if v == "switch":
        return "switch"
    return "olt"


@router.post("/recorder-login")
def api_deployments_recorder_login(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    source = _text(payload.get("recorder_type")).lower()
    if source not in ("nvr", "dvr"):
        raise HTTPException(status_code=400, detail="tipo de gravador obrigatorio")
    host = _text(payload.get("recorder_host") or payload.get("host"))
    user = _text(payload.get("recorder_user") or payload.get("user") or "admin")
    password = _text(payload.get("recorder_password") or payload.get("password"))
    if not host:
        raise HTTPException(status_code=400, detail="host do gravador obrigatorio")
    if not user or not password:
        raise HTTPException(status_code=400, detail="usuario e senha do gravador obrigatorios")

    connector_id = _text(payload.get("connector_id") or payload.get("remote_connector_id"))
    if connector_id:
        try:
            register_connector_known_targets(connector_id, [host])
        except Exception:
            pass

    reach_host = _reach_deploy_host(host, connector_id)
    base = _recorder_base_url(reach_host, payload.get("recorder_http_port") or payload.get("http_port"))
    probes = [
        ("/cgi-bin/magicBox.cgi?action=getSystemInfo", "intelbras"),
        ("/cgi-bin/magicBox.cgi?action=getDeviceType", "intelbras"),
        ("/cgi-bin/global.cgi?action=getCurrentTime", "intelbras"),
        ("/ISAPI/System/deviceInfo", "hikvision"),
    ]
    last_error = ""
    for path, family in probes:
        url = f"{base}{path}"
        try:
            resp = _try_recorder_request(url, user, password, timeout=5.0)
        except requests.Timeout:
            last_error = "tempo esgotado ao conectar no gravador"
            continue
        except Exception as exc:
            last_error = str(exc)
            continue
        if resp.status_code in (401, 403):
            last_error = "usuario ou senha recusados pelo gravador"
            continue
        if 200 <= resp.status_code < 300:
            body = resp.text or ""
            info = _parse_recorder_info(body)
            if family == "intelbras":
                supplemental_paths = (
                    "/cgi-bin/magicBox.cgi?action=getDeviceType",
                    "/cgi-bin/configManager.cgi?action=getConfig&name=DeviceInfo",
                )
                for supplemental_path in supplemental_paths:
                    try:
                        supplemental_resp = _try_recorder_request(
                            f"{base}{supplemental_path}", user, password, timeout=5.0,
                        )
                        if 200 <= supplemental_resp.status_code < 300:
                            info.update(_parse_recorder_info(supplemental_resp.text))
                    except Exception:
                        continue
            model = _recorder_model(info)
            try:
                channel_total = int(payload.get("recorder_channel_total") or payload.get("channel_total") or 32)
            except Exception:
                channel_total = 32
            live_used, live_authoritative = _fetch_recorder_live_channels(base, user, password, channel_total)
            if source == "nvr":
                _capture_recorder_snapshots(base, user, password, host, live_used)
            return {
                "ok": True,
                "source": source,
                "host": host,
                "brand": "Hikvision" if family == "hikvision" else "Intelbras",
                "model": model,
                "device_type": info.get("devicetype") or "",
                "serial": info.get("serialnumber") or "",
                "name": info.get("machine_name") or "",
                "status_code": resp.status_code,
                "probe": path,
                "channel_total": channel_total,
                "channels": _recorder_channel_grid(source, host, channel_total, live_used=live_used, live_authoritative=live_authoritative),
                "message": "Login confirmado no gravador.",
            }
        last_error = f"gravador respondeu HTTP {resp.status_code}"
    raise HTTPException(status_code=400, detail=last_error or "nao foi possivel entrar no gravador")


@router.post("/recorder-channels")
def api_deployments_recorder_channels(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    source = _text(payload.get("recorder_type")).lower()
    if source not in ("nvr", "dvr"):
        raise HTTPException(status_code=400, detail="tipo de gravador obrigatorio")
    host = _text(payload.get("recorder_host") or payload.get("host"))
    if not host:
        raise HTTPException(status_code=400, detail="host do gravador obrigatorio")
    try:
        total = int(payload.get("recorder_channel_total") or payload.get("channel_total") or 32)
    except Exception:
        total = 32
    user = _text(payload.get("recorder_user") or payload.get("user") or "admin")
    password = _text(payload.get("recorder_password") or payload.get("password"))
    connector_id = _text(payload.get("connector_id") or payload.get("remote_connector_id"))
    live_used: Dict[int, Dict[str, str]] = {}
    live_authoritative = False
    if user and password:
        reach_host = _reach_deploy_host(host, connector_id)
        base = _recorder_base_url(reach_host, payload.get("recorder_http_port") or payload.get("http_port"))
        live_used, live_authoritative = _fetch_recorder_live_channels(base, user, password, total)
    channels = _recorder_channel_grid(source, host, total, live_used=live_used, live_authoritative=live_authoritative)
    used = sum(1 for item in channels if item.get("used"))
    return {"ok": True, "source": source, "host": host, "channel_total": len(channels), "used": used, "free": len(channels) - used, "channels": channels}


@router.post("/recorder-add-camera")
def api_deployments_recorder_add_camera(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    source = _text(payload.get("recorder_type")).lower()
    if source not in ("nvr", "dvr"):
        raise HTTPException(status_code=400, detail="tipo de gravador obrigatorio")
    host = _text(payload.get("recorder_host") or payload.get("host"))
    user = _text(payload.get("recorder_user") or payload.get("user") or "admin")
    password = _text(payload.get("recorder_password") or payload.get("password"))
    camera_ip = _text(payload.get("recorder_camera_ip") or payload.get("camera_ip"))
    camera_user = _text(payload.get("camera_user") or "admin")
    camera_password = _text(payload.get("camera_password"))
    title = _text(payload.get("recorder_title") or payload.get("camera_title"))
    try:
        channel = int(_text(payload.get("recorder_channel") or payload.get("channel")) or "0")
    except Exception:
        channel = 0
    try:
        total = int(payload.get("recorder_channel_total") or payload.get("channel_total") or 32)
    except Exception:
        total = 32
    if not host or not user or not password:
        raise HTTPException(status_code=400, detail="entre no gravador informando host, usuario e senha")
    if not channel:
        raise HTTPException(status_code=400, detail="selecione um canal livre")
    if not camera_ip:
        raise HTTPException(status_code=400, detail="ip da camera obrigatorio")
    if not camera_user or not camera_password:
        raise HTTPException(status_code=400, detail="usuario e senha da camera obrigatorios")
    if not title:
        raise HTTPException(status_code=400, detail="titulo da camera obrigatorio")

    connector_id = _text(payload.get("connector_id") or payload.get("remote_connector_id"))
    reach_host = _reach_deploy_host(host, connector_id)
    base = _recorder_base_url(reach_host, payload.get("recorder_http_port") or payload.get("http_port"))
    live_used, live_authoritative = _fetch_recorder_live_channels(base, user, password, total)
    if not live_authoritative:
        raise HTTPException(status_code=400, detail="nao consegui confirmar os canais ao vivo do gravador")
    if channel in live_used:
        current = live_used.get(channel) or {}
        current_label = _text(current.get("title") or current.get("camera_ip") or f"canal {channel:02d}")
        raise HTTPException(status_code=409, detail=f"canal {channel:02d} ja esta ocupado: {current_label}")

    # Hikvision nao fala configManager/RemoteDevice: o vinculo e pelo InputProxy.
    if _recorder_family(base, user, password) == "hikvision":
        ok, detalhe = _hik_add_camera(base, user, password, channel, camera_ip, camera_user, camera_password, title)
        if not ok:
            raise HTTPException(status_code=400, detail=f"nao consegui vincular a camera ao canal {channel:02d}: {detalhe}")
        live_used_after, _ = _fetch_recorder_live_channels(base, user, password, total)
        if channel not in live_used_after:
            raise HTTPException(
                status_code=400,
                detail=f"o gravador aceitou o canal {channel:02d} ({detalhe}), mas ele nao aparece ocupado -- confira usuario/senha da camera",
            )
        # mesmo fechamento do fluxo Intelbras: sem isso o vinculo nao entra no
        # inventario e a camera some do gravador na proxima tela.
        camera_row = {
            "ip": camera_ip,
            "titulo": title,
            "modelo": _text(payload.get("camera_model")),
            "fabricante": _text(payload.get("camera_manufacturer")),
            "mac": _norm_mac(payload.get("camera_mac")),
            "local": _text(payload.get("site") or payload.get("local")),
        }
        recorder_link = _upsert_recorder_channel(payload, camera_row)
        return {
            "ok": True,
            "source": source,
            "host": host,
            "channel": channel,
            "camera_ip": camera_ip,
            "title": title,
            "confirmed": True,
            "recorder_link": recorder_link,
            "status": detalhe,
            "channels": _recorder_channel_grid(source, host, total, live_used=live_used_after, live_authoritative=True),
        }

    idx = channel - 1
    remote_common = {
        "Enable": "true",
        "Address": camera_ip,
        "Port": _text(payload.get("camera_tcp_port") or "37777"),
        "HttpPort": _text(payload.get("camera_http_port") or "80"),
        "RtspPort": _text(payload.get("camera_rtsp_port") or "554"),
        "UserName": camera_user,
        "Password": camera_password,
        "ProtocolType": _text(payload.get("recorder_protocol") or "Private"),
        "VideoInputs[0].Name": title,
    }
    attempts: List[Dict[str, Any]] = []
    for prefix in (f"RemoteDevice[{idx}]", f"RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{idx}"):
        attempts.append({f"{prefix}.{k}": v for k, v in remote_common.items()})
    last_status = ""
    configured = False
    live_used_after: Dict[int, Dict[str, str]] = {}
    for params in attempts:
        try:
            resp = _recorder_set_config(base, user, password, params)
            last_status = f"HTTP {resp.status_code}: {(resp.text or '').strip()[:160]}"
            if _recorder_config_ok(resp):
                try:
                    _recorder_set_config(base, user, password, {f"ChannelTitle[{idx}].Name": title}, timeout=5.0)
                except Exception:
                    pass
                live_used_after, _ = _fetch_recorder_live_channels(base, user, password, total)
                if channel in live_used_after:
                    configured = True
                    break
        except Exception as exc:
            last_status = str(exc)
    if not configured:
        raise HTTPException(status_code=400, detail=f"falha ao configurar camera no gravador: {last_status}")

    camera_row = {
        "ip": camera_ip,
        "titulo": title,
        "modelo": _text(payload.get("camera_model")),
        "fabricante": _text(payload.get("camera_manufacturer")),
        "mac": _norm_mac(payload.get("camera_mac")),
        "local": _text(payload.get("site") or payload.get("local")),
    }
    recorder_link = _upsert_recorder_channel(payload, camera_row)
    return {
        "ok": True,
        "source": source,
        "host": host,
        "channel": channel,
        "camera_ip": camera_ip,
        "title": title,
        "confirmed": True,
        "recorder_link": recorder_link,
        "channels": _recorder_channel_grid(source, host, total, live_used=live_used_after, live_authoritative=True),
    }


@router.post("/recorder-remove-camera")
def api_deployments_recorder_remove_camera(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Solta um canal do gravador.

    O frontend ja chamava esta rota, mas ela nunca existiu no backend: o botao
    "Excluir canal" respondia 404 em qualquer marca."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    source = _text(payload.get("recorder_type")).lower()
    if source not in ("nvr", "dvr"):
        raise HTTPException(status_code=400, detail="tipo de gravador obrigatorio")
    host = _text(payload.get("recorder_host") or payload.get("host"))
    user = _text(payload.get("recorder_user") or payload.get("user") or "admin")
    password = _text(payload.get("recorder_password") or payload.get("password"))
    if not host or not user or not password:
        raise HTTPException(status_code=400, detail="entre no gravador informando host, usuario e senha")
    try:
        channel = int(_text(payload.get("recorder_channel") or payload.get("channel")) or "0")
    except Exception:
        channel = 0
    if not channel:
        raise HTTPException(status_code=400, detail="selecione o canal a excluir")
    try:
        total = int(payload.get("recorder_channel_total") or payload.get("channel_total") or 32)
    except Exception:
        total = 32

    connector_id = _text(payload.get("connector_id") or payload.get("remote_connector_id"))
    reach_host = _reach_deploy_host(host, connector_id)
    base = _recorder_base_url(reach_host, payload.get("recorder_http_port") or payload.get("http_port"))

    if _recorder_family(base, user, password) == "hikvision":
        ok, detalhe = _hik_remove_camera(base, user, password, channel)
    else:
        # Intelbras/Dahua nao apaga o slot: desliga o RemoteDevice do canal.
        idx = channel - 1
        ok, detalhe = False, ""
        for prefix in (f"RemoteDevice[{idx}]", f"RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{idx}"):
            try:
                resp = _recorder_set_config(base, user, password, {f"{prefix}.Enable": "false"})
            except Exception as exc:
                detalhe = str(exc)
                continue
            if _recorder_config_ok(resp):
                ok, detalhe = True, "canal desabilitado"
                break
            detalhe = f"HTTP {resp.status_code}: {(resp.text or '').strip()[:120]}"
    if not ok:
        raise HTTPException(status_code=400, detail=f"nao consegui excluir o canal {channel:02d}: {detalhe}")

    live_used, autoritativo = _fetch_recorder_live_channels(base, user, password, total)
    if autoritativo and channel in live_used:
        raise HTTPException(
            status_code=400,
            detail=f"o gravador aceitou o comando ({detalhe}), mas o canal {channel:02d} continua ocupado",
        )
    return {
        "ok": True,
        "source": source,
        "host": host,
        "channel": channel,
        "status": detalhe,
        "channels": _recorder_channel_grid(source, host, total, live_used=live_used, live_authoritative=autoritativo),
        "message": f"Canal {channel:02d} liberado.",
    }


@router.post("/commit-camera")
def api_deployments_commit_camera(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")
    ip = _text(payload.get("camera_ip"))
    title = _text(payload.get("camera_title"))
    if not ip:
        raise HTTPException(status_code=400, detail="ip da camera obrigatorio")
    if not title:
        raise HTTPException(status_code=400, detail="titulo da camera obrigatorio")
    inv_mode = _normalize_inventory_mode(_text(payload.get("inventory_mode")))

    connector_id = _text(payload.get("connector_id"))
    site = _text(payload.get("site") or payload.get("local"))
    mac = _norm_mac(payload.get("camera_mac"))
    location = _text(payload.get("location") or payload.get("camera_location"))
    lat, lon = _parse_lat_lon(location)
    row = {
        "ip": ip,
        "mac": mac,
        "fabricante": _text(payload.get("camera_manufacturer")),
        "modelo": _text(payload.get("camera_model")),
        "usuario": _text(payload.get("camera_user")),
        "senha": _text(payload.get("camera_password")),
        "titulo": title,
        "status": "online",
        "local": site,
        "site": site,
        "site_name": site,
        "physical_location": location,
        "lat": lat,
        "lon": lon,
        "remote": bool(connector_id),
        "remote_connector_id": connector_id,
        "onu_serial": _text(payload.get("onu_serial")),
        "vlan": _text(payload.get("vlan")),
        "recorder_host": _text(payload.get("recorder_host")),
        "recorder_type": _text(payload.get("recorder_type")),
        "recorder_channel": _text(payload.get("recorder_channel")),
        "deployment_id": _text(payload.get("id")),
        "installed_at": _now(),
    }
    rows = load_inventory_json(mode=inv_mode) or []
    key = inventory_row_key(row)
    updated = False
    for idx, existing in enumerate(rows):
        # Casa por IP, por MAC, ou pela chave com connector_id: a etapa de
        # "puxar dados da camera" (rescan-single-ip) ja pode ter criado a
        # linha sem remote_connector_id, e sem isso aqui viraria duplicata.
        existing_ip = _text(existing.get("ip"))
        existing_mac = _norm_mac(existing.get("mac"))
        existing_connector = _text(existing.get("remote_connector_id") or existing.get("connector_id"))
        existing_site = _text(existing.get("site") or existing.get("site_name") or existing.get("local"))
        same_plain_inventory = not connector_id and not existing_connector
        same_remote_inventory = bool(connector_id) and existing_connector == connector_id
        same_site_remote_fallback = bool(connector_id) and not existing_connector and existing_site.lower() == site.lower()
        same = (
            inventory_row_key(existing) == key
            or ((same_plain_inventory or same_remote_inventory or same_site_remote_fallback) and existing_ip == ip)
            or ((same_plain_inventory or same_remote_inventory or same_site_remote_fallback) and mac and existing_mac == mac)
        )
        if same:
            rows[idx] = {**existing, **row}
            updated = True
            break
    if not updated:
        rows.append(row)
    save_inventory_json(rows, mode=inv_mode)

    # "Puxar dados da camera" sempre grava em modo "olt"; se o tecnico
    # escolheu um inventario diferente aqui, tira a linha orfa de "olt"
    # pra nao duplicar o cadastro entre dois arquivos.
    if inv_mode != "olt":
        olt_rows = load_inventory_json(mode="olt") or []
        filtered = [
            r for r in olt_rows
            if _text(r.get("ip")) != ip and not (mac and _norm_mac(r.get("mac")) == mac)
        ]
        if len(filtered) != len(olt_rows):
            save_inventory_json(filtered, mode="olt")

    recorder_link = _upsert_recorder_channel(payload, row)
    saved = api_deployments_save({
        **payload,
        "status": "camera_registered",
        "camera_inventory_key": key,
        "recorder_link": recorder_link,
    })
    return {
        "ok": True,
        "created": not updated,
        "inventory_key": key,
        "camera": row,
        "recorder_link": recorder_link,
        "deployment": saved.get("deployment"),
        "inventory_mode": inv_mode,
    }


@router.get("/connectors")
def api_deployments_connectors() -> Dict[str, Any]:
    return list_connectors(include_token=False)
