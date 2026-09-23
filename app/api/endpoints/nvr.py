from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, List

import requests
from fastapi import APIRouter, HTTPException, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from app.core.paths import BASE_DIR, NVR_INVENTORY_JSON_PATH, NVR_SNAPSHOT_DIR, SAIDA_DIR
from app.core.paths import DATA_DIR
from app.core.tenant_context import get_current_tenant_slug, tenant_recorder_inventory_path, tenant_report_logo_path, tenant_scoped_path, tenant_snapshot_dir
from app.services.camsnapshot.uploader_imgbb import upload_to_imgbb
from app.services.db_store import decorate_legacy_rows
from app.services.db_store import replace_recorder_inventory_rows
from app.services.db_store import legacy_rows_from_db
from app.services.db_store import load_app_settings, save_app_settings
from app.services.pdf_inventory_report import build_inventory_pdf_report, build_inventory_preview_image
from app.services import connector_routing_vnat as _vnat
import contextvars

# Conector do request atual (setado pelos endpoints que recebem connector_id) --
# pra virtualizar o alvo mesmo antes do gravador estar no inventario (ex.: scan).
_rec_req_connector: "contextvars.ContextVar[str]" = contextvars.ContextVar("rec_req_connector", default="")
from app.services.recorder_pdf_report import build_recorder_pdf_report
from app.services.ws_scan_service import (
    _connector_from_payload,
    _connector_has_tunnel,
    _decide_remote_only,
    _pick_probe_targets,
)
from app.services.connector_service import register_connector_known_targets

router = APIRouter(tags=["nvr"], prefix="/api/nvr")
_imgbb_progress_lock = threading.Lock()
_imgbb_progress: Dict[str, Any] = {
    "running": False,
    "finished": False,
    "total": 0,
    "done": 0,
    "success": 0,
    "failed": 0,
    "started_at": 0.0,
    "ended_at": 0.0,
    "last_file": "",
    "last_error": "",
    "message": "",
}


def _imgbb_progress_set(**kwargs: Any) -> None:
    with _imgbb_progress_lock:
        _imgbb_progress.update(kwargs)


def _imgbb_progress_get() -> Dict[str, Any]:
    with _imgbb_progress_lock:
        return dict(_imgbb_progress)


def _imgbb_progress_start(total: int) -> None:
    _imgbb_progress_set(
        running=True,
        finished=False,
        total=max(0, int(total or 0)),
        done=0,
        success=0,
        failed=0,
        started_at=time.time(),
        ended_at=0.0,
        last_file="",
        last_error="",
        message="Iniciando upload ImgBB...",
    )


def _imgbb_progress_finish(message: str = "") -> None:
    _imgbb_progress_set(
        running=False,
        finished=True,
        ended_at=time.time(),
        message=message or "Upload ImgBB finalizado.",
    )


class DVRScanRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    start_channel: int = Field(default=1, ge=1, le=256)
    end_channel: int = Field(default=32, ge=1, le=256)
    timeout_sec: float = Field(default=4.0, ge=1.0, le=20.0)
    imgbb: bool = False
    set_local: bool = False
    local: str = ""
    inventory_mode: str = "basico"
    scan_origin: str = ""
    connector_id: str = ""
    remote_connector_id: str = ""
    remote_only: bool = False


class DVRSnapshotUpdateRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    channel: int = Field(default=1, ge=1, le=256)
    timeout_sec: float = Field(default=6.0, ge=1.0, le=20.0)
    imgbb: bool = False


class DVRRenameChannelRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    channel: int = Field(default=1, ge=1, le=256)
    title: str = ""
    timeout_sec: float = Field(default=6.0, ge=1.0, le=20.0)


class DVRSetLocalRequest(BaseModel):
    ip: str
    http_port: int = Field(default=80, ge=1, le=65535)
    channel: int = Field(default=1, ge=1, le=256)
    local: str = ""


class DVRApplyLocalRequest(BaseModel):
    ip: str = ""
    http_port: int = Field(default=80, ge=1, le=65535)
    local: str = ""


class RecorderSaveRequest(BaseModel):
    recorders: List[Dict[str, Any]] = Field(default_factory=list)


class RecorderDeleteRequest(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    mode: str = ""
    site: str = ""
    host: str = ""
    connector_id: str = ""
    remote_connector_id: str = ""


class DVRChangeIpRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    new_ip: str
    mask: str = ""
    gateway: str = ""
    dns1: str = ""
    dns2: str = ""
    timeout_sec: float = Field(default=8.0, ge=1.0, le=30.0)


class DVRSetNtpRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    address: str
    port: int = Field(default=123, ge=1, le=65535)
    timezone: int = 22
    update_period: int = Field(default=60, ge=1, le=86400)
    timeout_sec: float = Field(default=8.0, ge=1.0, le=30.0)


class DVRRebootRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    timeout_sec: float = Field(default=8.0, ge=1.0, le=30.0)


class DVRCameraChangeIpRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    channel: int = Field(default=1, ge=1, le=256)
    new_ip: str
    timeout_sec: float = Field(default=8.0, ge=1.0, le=30.0)


def _recorder_connector_for_host(ip: str) -> str:
    """Conector de um gravador pelo IP, lido do inventario de gravadores."""
    ip = str(ip or "").strip()
    if not ip:
        return ""
    try:
        for r in _read_rows():
            if str(r.get("host") or r.get("ip") or "").strip() == ip:
                return _rec_row_connector(r)
    except Exception:
        pass
    return ""


def _reach_recorder(ip: str) -> str:
    """IP a conectar de fato: virtual (modelo A) se o gravador for de um conector
    isolado, senao o real. Conector vem do request (scan) ou do inventario."""
    ip = str(ip or "").strip()
    if not ip:
        return ip
    cid = _rec_req_connector.get() or _recorder_connector_for_host(ip)
    return _vnat.virtual_ip_for(cid, ip) or ip


def _reach_camera(ip: str, recorder_host: str = "") -> str:
    """IP a conectar de fato numa camera atras do gravador. O conector vem do
    request (scan) ou do proprio gravador: cameras da faixa isolada so respondem
    no IP virtual (vnat), nunca no real."""
    ip = str(ip or "").strip()
    if not ip:
        return ip
    cid = _rec_req_connector.get() or _recorder_connector_for_host(recorder_host)
    return _vnat.virtual_ip_for(cid, ip) or ip


def _base(ip: str, port: int) -> str:
    ip = _reach_recorder(ip)  # conector isolado -> IP virtual (host NAT -> real)
    return f"http://{ip}:{int(port)}" if int(port) != 80 else f"http://{ip}"


def _normalize_rec_inventory_mode(value: str = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in {"olt", "via_olt", "via-olt"}:
        return "olt"
    if raw in {"switch", "sw", "via_switch", "via-switch"}:
        return "switch"
    return "basico"


def _recorder_scan_connector(req: DVRScanRequest) -> Dict[str, Any] | None:
    scan_origin = str(req.scan_origin or "").strip().lower()
    connector_id = str(req.connector_id or req.remote_connector_id or "").strip()
    if scan_origin == "connector" and not connector_id:
        raise HTTPException(status_code=400, detail="Selecione um conector para varrer este gravador.")
    if not connector_id and scan_origin != "connector":
        return None

    payload = {
        "connector_id": connector_id,
        "remote_connector_id": connector_id,
        "local": req.local,
        "scan_origin": scan_origin or "connector",
    }
    connector = _connector_from_payload(payload)
    if connector_id and str(req.ip or "").strip():
        try:
            register_connector_known_targets(connector_id, [req.ip])
        except Exception:
            pass
    has_tunnel = _connector_has_tunnel(connector)
    remote_only = _decide_remote_only(
        scan_origin="connector",
        connector_id=connector_id,
        connector_has_tunnel=has_tunnel,
        remote_only_requested=bool(req.remote_only),
        probe_targets=_pick_probe_targets(req.ip),
    )
    if remote_only:
        raise HTTPException(
            status_code=424,
            detail="O conector existe, mas o servidor ainda nao alcanca este gravador pela VPN. Verifique o WireGuard/rotas do cliente e tente novamente.",
        )
    return connector or {"id": connector_id}


def _tag_recorder_rows(rows: List[Dict[str, Any]], req: DVRScanRequest, connector: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    mode = _normalize_rec_inventory_mode(req.inventory_mode)
    site = str(req.local or "").strip()
    connector_id = str(req.connector_id or req.remote_connector_id or (connector or {}).get("id") or "").strip()
    connector_name = str((connector or {}).get("name") or (connector or {}).get("site") or site or "").strip()
    for row in rows:
        row["inventory_mode"] = mode
        if site:
            row["local"] = row.get("local") or site
            row["site"] = row.get("site") or site
            row["site_name"] = row.get("site_name") or site
        if connector_id:
            row["remote"] = True
            row["connector_id"] = connector_id
            row["remote_connector_id"] = connector_id
            row["remote_connector_name"] = connector_name
    return rows


def _same_recorder_scope(row: Dict[str, Any], req: DVRScanRequest) -> bool:
    if str(row.get("host") or "") != str(req.ip or ""):
        return False
    if int(row.get("http_port") or 80) != int(req.http_port):
        return False
    if _normalize_rec_inventory_mode(str(row.get("inventory_mode") or "")) != _normalize_rec_inventory_mode(req.inventory_mode):
        return False
    wanted_connector = str(req.connector_id or req.remote_connector_id or "").strip()
    row_connector = str(row.get("remote_connector_id") or row.get("connector_id") or "").strip()
    if wanted_connector or row_connector:
        return row_connector == wanted_connector
    return True


def _rec_row_mode(row: Dict[str, Any]) -> str:
    return _normalize_rec_inventory_mode(str(row.get("inventory_mode") or "basico"))


def _rec_row_site(row: Dict[str, Any]) -> str:
    return str(row.get("site") or row.get("site_name") or row.get("local") or "").strip()


def _rec_row_connector(row: Dict[str, Any]) -> str:
    return str(row.get("remote_connector_id") or row.get("connector_id") or "").strip()


def _rec_matches_delete_scope(
    row: Dict[str, Any],
    *,
    mode: str = "",
    site: str = "",
    host: str = "",
    connector_id: str = "",
) -> bool:
    wanted_mode = _normalize_rec_inventory_mode(mode) if str(mode or "").strip() else ""
    if wanted_mode and _rec_row_mode(row) != wanted_mode:
        return False
    wanted_site = str(site or "").strip().lower()
    if wanted_site and _rec_row_site(row).lower() != wanted_site:
        return False
    wanted_host = str(host or "").strip()
    row_host = str(row.get("host") or row.get("ip") or "").strip()
    if wanted_host and row_host != wanted_host:
        return False
    wanted_connector = str(connector_id or "").strip()
    if wanted_connector and _rec_row_connector(row) != wanted_connector:
        return False
    return True


def _read_rows() -> List[Dict[str, Any]]:
    if get_current_tenant_slug():
        p = tenant_recorder_inventory_path("nvr")
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    try:
        db_rows = legacy_rows_from_db("nvr")
        if db_rows:
            return db_rows
    except Exception:
        pass
    p = Path(NVR_INVENTORY_JSON_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_rows(rows: List[Dict[str, Any]]) -> None:
    if get_current_tenant_slug():
        p = tenant_recorder_inventory_path("nvr")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    try:
        replace_recorder_inventory_rows("nvr", rows)
        return
    except Exception:
        pass
    # fallback legado
    p = Path(NVR_INVENTORY_JSON_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_text(url: str, auth: HTTPDigestAuth, timeout: float) -> str:
    try:
        r = requests.get(url, auth=auth, timeout=timeout)
    except Exception:
        return ""
    if r.status_code != 200:
        return ""
    return r.text or ""


def _request_ok(url: str, auth: HTTPDigestAuth, timeout: float) -> tuple[bool, str, int]:
    try:
        r = requests.get(url, auth=auth, timeout=timeout)
    except requests.RequestException as e:
        return False, str(e), 0
    body = str(r.text or "")
    ok = r.status_code == 200 and ("Error" not in body or "OK" in body.upper())
    return ok, body, int(r.status_code)


def _request_any(
    method: str,
    url: str,
    user: str,
    password: str,
    timeout: float,
    data: str | bytes | None = None,
    headers: Dict[str, str] | None = None,
) -> tuple[bool, str, int]:
    method_u = str(method or "GET").upper()
    headers = headers or {}
    last_body = ""
    last_status = 0
    for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
        try:
            r = requests.request(method_u, url, auth=auth, timeout=timeout, data=data, headers=headers)
        except requests.RequestException as e:
            last_body = str(e)
            continue
        body = str(r.text or "")
        last_body = body
        last_status = int(r.status_code)
        if int(r.status_code) in (200, 201, 202, 204):
            return True, body, int(r.status_code)
    return False, last_body, last_status


def _xml_set_first_local(root: ET.Element, local_name: str, new_value: str) -> bool:
    want = str(local_name or "").strip().lower()
    if not want:
        return False
    for n in root.iter():
        if _xml_local(getattr(n, "tag", "")).lower() == want:
            n.text = str(new_value or "")
            return True
    return False


def _hik_rename_channel(base: str, user: str, password: str, channel: int, title: str, timeout: float) -> tuple[bool, str, int]:
    ch = max(1, int(channel))
    paths = (
        f"/ISAPI/ContentMgmt/InputProxy/channels/{ch}",
        f"/ISAPI/System/Video/inputs/channels/{ch}",
        f"/ISAPI/ContentMgmt/InputProxy/channels/{ch}/name",
    )
    last_body = ""
    last_status = 0
    for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
        with requests.Session() as s:
            for path in paths:
                get_url = f"{base}{path}"
                try:
                    rg = s.get(get_url, auth=auth, timeout=timeout)
                except requests.RequestException as e:
                    last_body = str(e)
                    continue
                body_get = str(rg.text or "")
                last_body = body_get
                last_status = int(rg.status_code)
                if int(rg.status_code) not in (200, 201, 202, 204):
                    continue

                try:
                    # Hikvision desta linha de firmware rejeita XML reserializado
                    # (prefixos/ordem), então preservamos o payload original e
                    # trocamos apenas o primeiro <name>...</name>.
                    safe_title = (
                        str(title or "")
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    xml_to_put, nrep = re.subn(
                        r"(<name>)(.*?)(</name>)",
                        r"\g<1>" + safe_title + r"\g<3>",
                        str(body_get or ""),
                        count=1,
                        flags=re.IGNORECASE | re.DOTALL,
                    )
                    if int(nrep or 0) <= 0:
                        # fallback: alguns endpoints retornam <Name>...</Name>
                        xml_to_put, nrep = re.subn(
                            r"(<Name>)(.*?)(</Name>)",
                            r"\g<1>" + safe_title + r"\g<3>",
                            str(body_get or ""),
                            count=1,
                            flags=re.IGNORECASE | re.DOTALL,
                        )
                    if int(nrep or 0) <= 0:
                        continue
                except Exception:
                    continue

                try:
                    rp = s.put(
                        get_url,
                        auth=auth,
                        timeout=timeout,
                        data=xml_to_put.encode("utf-8"),
                        headers={"Content-Type": "application/xml; charset=UTF-8"},
                    )
                except requests.RequestException as e:
                    last_body = str(e)
                    continue

                last_body = str(rp.text or "")
                last_status = int(rp.status_code)
                if int(rp.status_code) in (200, 201, 202, 204):
                    return True, last_body, last_status

                # Alguns Hikvision aceitam update por query CGI legado.
                if int(rp.status_code) == 401:
                    try:
                        q_title = quote(str(title or "").strip(), safe="")
                        legacy_url = f"{base}/ISAPI/System/Video/inputs/channels/{ch}?name={q_title}"
                        rp2 = s.put(legacy_url, auth=auth, timeout=timeout)
                        last_body = str(rp2.text or "")
                        last_status = int(rp2.status_code)
                        if int(rp2.status_code) in (200, 201, 202, 204):
                            return True, last_body, last_status
                    except Exception:
                        pass

    return False, last_body, last_status


def _hik_change_ip(
    base: str,
    user: str,
    password: str,
    new_ip: str,
    mask: str,
    gateway: str,
    dns1: str,
    dns2: str,
    timeout: float,
) -> tuple[bool, str, int]:
    path = "/ISAPI/System/Network/interfaces/1"
    get_url = f"{base}{path}"
    ok_get, body_get, status_get = _request_any("GET", get_url, user, password, timeout)
    if not ok_get:
        return False, body_get, status_get
    try:
        root = ET.fromstring(body_get or "")
    except Exception:
        return False, "xml invalido em interfaces/1", status_get

    changed = False
    changed = _xml_set_first_local(root, "ipAddress", new_ip) or changed
    if str(mask or "").strip():
        changed = _xml_set_first_local(root, "subnetMask", str(mask).strip()) or changed
    if str(gateway or "").strip():
        changed = _xml_set_first_local(root, "defaultGateway", str(gateway).strip()) or changed
    if str(dns1 or "").strip():
        changed = _xml_set_first_local(root, "PrimaryDNS", str(dns1).strip()) or changed
    if str(dns2 or "").strip():
        changed = _xml_set_first_local(root, "SecondaryDNS", str(dns2).strip()) or changed

    if not changed:
        return False, "sem campos para atualizar no XML de rede", 0

    xml_put = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8", errors="ignore")
    return _request_any(
        "PUT",
        get_url,
        user,
        password,
        timeout,
        data=xml_put,
        headers={"Content-Type": "application/xml"},
    )


def _hik_set_ntp(base: str, user: str, password: str, address: str, port: int, timezone: int, update_period: int, timeout: float) -> tuple[bool, str, int]:
    # 1) atualiza NTP server list
    ntp_url = f"{base}/ISAPI/System/time/ntpServers"
    ok_get, body_get, status_get = _request_any("GET", ntp_url, user, password, timeout)
    if not ok_get:
        return False, body_get, status_get
    try:
        root = ET.fromstring(body_get or "")
    except Exception:
        return False, "xml invalido em ntpServers", status_get

    changed = False
    for tag in ("hostName", "ipAddress", "address", "serverAddress", "ntpAddress"):
        changed = _xml_set_first_local(root, tag, str(address).strip()) or changed
    for tag in ("portNo", "port"):
        changed = _xml_set_first_local(root, tag, str(int(port))) or changed
    for tag in ("synchronizeInterval", "syncInterval", "updatePeriod"):
        changed = _xml_set_first_local(root, tag, str(int(update_period))) or changed
    if not changed:
        # fallback mínimo
        changed = _xml_set_first_local(root, "hostName", str(address).strip()) or changed
        changed = _xml_set_first_local(root, "portNo", str(int(port))) or changed

    xml_put = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8", errors="ignore")
    ok_put, body_put, status_put = _request_any(
        "PUT", ntp_url, user, password, timeout, data=xml_put, headers={"Content-Type": "application/xml"}
    )
    if not ok_put:
        return False, body_put, status_put

    # 2) força modo NTP em /ISAPI/System/time (se existir)
    time_url = f"{base}/ISAPI/System/time"
    ok_tget, body_tget, status_tget = _request_any("GET", time_url, user, password, timeout)
    if ok_tget:
        try:
            rt = ET.fromstring(body_tget or "")
            _xml_set_first_local(rt, "timeMode", "NTP")
            _xml_set_first_local(rt, "timeZone", str(int(timezone)))
            xml_time = ET.tostring(rt, encoding="utf-8", xml_declaration=True).decode("utf-8", errors="ignore")
            _request_any("PUT", time_url, user, password, timeout, data=xml_time, headers={"Content-Type": "application/xml"})
        except Exception:
            pass
    return True, body_put, status_put


def _hik_change_camera_ip(base: str, user: str, password: str, channel: int, new_ip: str, timeout: float) -> tuple[bool, str, int]:
    ch = max(1, int(channel))
    url = f"{base}/ISAPI/ContentMgmt/InputProxy/channels/{ch}"
    ok_get, body_get, status_get = _request_any("GET", url, user, password, timeout)
    if not ok_get:
        return False, body_get, status_get
    try:
        xml_put, nrep = re.subn(
            r"(<ipAddress>)(.*?)(</ipAddress>)",
            r"\g<1>" + str(new_ip or "").strip() + r"\g<3>",
            str(body_get or ""),
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if int(nrep or 0) <= 0:
            return False, "campo ipAddress nao encontrado no canal", 0
    except Exception:
        return False, "xml invalido no canal ISAPI", status_get
    return _request_any(
        "PUT",
        url,
        user,
        password,
        timeout,
        data=xml_put.encode("utf-8"),
        headers={"Content-Type": "application/xml; charset=UTF-8"},
    )


def _load_imgbb_key() -> str:
    try:
        obj = load_app_settings()
        if isinstance(obj, dict):
            key = str(obj.get("imgbb_key") or obj.get("imgbb_api_key") or "").strip().strip('"')
            if key:
                return key
    except Exception:
        pass
    return ""


def _load_zabbix_dvr_sync_settings() -> Dict[str, Any]:
    try:
        obj = load_app_settings()
        if not isinstance(obj, dict):
            return {}
        z = obj.get("zabbix_dvr_sync")
        return z if isinstance(z, dict) else {}
    except Exception:
        return {}


def _build_zabbix_rows_for_dvr(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        host_ip = str(r.get("host") or r.get("ip") or "").strip()
        if not host_ip:
            continue
        ch = int(r.get("channel") or 0)
        http_port = int(r.get("http_port") or 80)
        ch_txt = f"{ch:02d}" if ch > 0 else "00"
        title = str(r.get("title") or r.get("titulo") or "").strip() or f"CH {ch_txt}"
        lat = str(r.get("lat") or "").strip()
        lon = str(r.get("lon") or "").strip()
        map_url = f"https://www.google.com/maps?q={lat},{lon}" if lat and lon else ""
        out.append(
            {
                "source": "nvr",
                "ip": host_ip,
                "channel": ch,
                "http_port": http_port,
                "title": f"CH {ch_txt} - {title}",
                "titulo": f"CH {ch_txt} - {title}",
                "local": str(r.get("local") or "").strip(),
                "mac": str(r.get("mac") or "").strip(),
                "modelo": str(r.get("modelo") or "").strip(),
                "status": str(r.get("status") or "").strip(),
                "snapshot_url": str(r.get("imgbb_url") or r.get("snapshot_url") or "").strip(),
                "host_key": f"DVR-{host_ip}-CH{ch_txt}",
                "map_url": map_url,
                "lat": lat,
                "lon": lon,
            }
        )
    return out


def _zabbix_tenant_slug() -> str:
    return str(get_current_tenant_slug() or "default").strip().lower() or "default"


def _zabbix_host_safe(value: Any) -> str:
    safe = str(value or "").strip().upper()
    safe = re.sub(r"[^A-Z0-9_.-]+", "-", safe)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe or "DEFAULT"


def _zabbix_tenant_group(group: str, tenant: str = "") -> str:
    base = str(group or "").strip() or "Cameras"
    slug = _zabbix_host_safe(tenant or _zabbix_tenant_slug())
    marker = f" - {slug}"
    return base if base.upper().endswith(marker.upper()) else f"{base}{marker}"


def _auto_sync_dvr_status_to_zabbix(rows: List[Dict[str, Any]]) -> tuple[bool, str]:
    cfg = _load_zabbix_dvr_sync_settings()
    if not bool(cfg.get("enabled", True)):
        return False, "zabbix_dvr_sync desabilitado"

    url = str(cfg.get("url") or "").strip()
    user = str(cfg.get("user") or "").strip()
    password = str(cfg.get("pass") or "").strip()
    tenant_slug = _zabbix_tenant_slug()
    group = _zabbix_tenant_group(str(cfg.get("group") or "Cameras").strip() or "Cameras", tenant_slug)
    template = str(cfg.get("template") or "Template Module ICMP Ping").strip() or "Template Module ICMP Ping"
    template_dvr = str(cfg.get("template_dvr") or "Template Cam-Snapshot DVR Channel").strip() or "Template Cam-Snapshot DVR Channel"
    dvr_user = str(cfg.get("dvr_user") or "admin").strip() or "admin"
    dvr_pass = str(cfg.get("dvr_pass") or "").strip()
    if not url or not user or not password:
        return False, "credenciais Zabbix DVR nÃ£o configuradas"

    z_rows = _build_zabbix_rows_for_dvr(rows)
    if not z_rows:
        return False, "sem linhas DVR para sincronizar"

    tmp_inv = tenant_scoped_path("tmp/zabbix-source-inventory.auto-dvr.json", tenant_slug)
    try:
        tmp_inv.parent.mkdir(parents=True, exist_ok=True)
        tmp_inv.write_text(json.dumps(z_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return False, f"falha ao preparar inventÃ¡rio temporÃ¡rio: {e}"

    script = BASE_DIR / "tools" / "mk_zabbix_from_inventory.py"
    env = os.environ.copy()
    env.update(
        {
            "INV_PATH": str(tmp_inv),
            "ZBX_URL": url,
            "ZBX_USER": user,
            "ZBX_PASS": password,
            "ZBX_GROUP": group,
            # Isola por tenant o nome do host no Zabbix -- ver build_host_name
            # em tools/mk_zabbix_from_inventory.py.
            "ZBX_TENANT": tenant_slug,
            "ZBX_TEMPLATE": template,
            "ZBX_TEMPLATE_DVR": template_dvr,
            "ZBX_DVR_USER": dvr_user,
            "ZBX_DVR_PASS": dvr_pass,
            "ZBX_TG_AUTO": "0",
        }
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except Exception as e:
        return False, str(e)

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        return False, (msg[:600] if msg else f"exit {proc.returncode}")
    return True, ""


def _upload_imgbb_for_dvr_rows(rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int, str]:
    if not rows:
        return rows, 0, ""
    api_key = _load_imgbb_key()
    if not api_key:
        return rows, 0, "ImgBB API key nao configurada."

    files: list[str] = []
    name_map: dict[str, str] = {}
    for r in rows:
        snap_file = str(r.get("snapshot_file") or "").strip()
        host = str(r.get("host") or "").strip()
        ch = int(r.get("channel") or 0)
        if not snap_file:
            continue
        p = DATA_DIR / snap_file
        if not p.exists():
            continue
        s = str(p)
        files.append(s)
        if host and ch > 0:
            name_map[s] = f"{host}-ch{ch:02d}"

    if not files:
        return rows, 0, "Nenhum snapshot local encontrado para upload."

    uniq: list[str] = []
    seen: set[str] = set()
    for f in files:
        if f in seen:
            continue
        seen.add(f)
        uniq.append(f)

    total_candidates = len(uniq)
    _imgbb_progress_start(total_candidates)

    def _on_progress(ev: Dict[str, Any]) -> None:
        st = _imgbb_progress_get()
        done = int(st.get("done") or 0) + 1
        success = int(st.get("success") or 0) + (1 if bool(ev.get("ok")) else 0)
        failed = int(st.get("failed") or 0) + (0 if bool(ev.get("ok")) else 1)
        msg = f"ImgBB {done}/{total_candidates} (ok={success}, erro={failed})"
        err = str(ev.get("error") or "")
        if err:
            msg += f" - {err}"
        _imgbb_progress_set(
            done=done,
            success=success,
            failed=failed,
            last_file=str(ev.get("file") or ""),
            last_error=err,
            message=msg,
        )

    try:
        uploads = upload_to_imgbb(
            uniq,
            api_key=api_key,
            name_prefix="dvr",
            name_map=name_map,
            progress_cb=_on_progress,
        )
    except Exception as e:
        _imgbb_progress_finish(f"Erro ImgBB: {e}")
        return rows, 0, str(e)

    by_file: dict[str, dict[str, Any]] = {}
    for u in uploads or []:
        f = str(u.get("file") or "").strip()
        if f:
            by_file[f] = u

    changed = 0
    for r in rows:
        snap_file = str(r.get("snapshot_file") or "").strip()
        if not snap_file:
            continue
        p = DATA_DIR / snap_file
        u = by_file.get(str(p))
        if not u:
            continue
        url = str(u.get("url") or "").strip()
        thumb = str(u.get("thumbnail_url") or url).strip()
        if not url:
            continue
        before_url = str(r.get("imgbb_url") or "").strip()
        before_thumb = str(r.get("imgbb_thumb_url") or "").strip()
        row_changed = False
        if before_url != url:
            r["imgbb_url"] = url
            row_changed = True
        if thumb and before_thumb != thumb:
            r["imgbb_thumb_url"] = thumb
            row_changed = True
        if row_changed:
            changed += 1
    err_msg = ""
    if total_candidates > 0 and len(by_file) < total_candidates:
        err_msg = (
            f"ImgBB parcial: {len(by_file)}/{total_candidates} uploads concluÃ­dos. "
            "Verifique limite/rate da API key."
        )
    _imgbb_progress_finish(f"ImgBB finalizado: {len(by_file)}/{total_candidates} uploads concluÃ­dos.")
    return rows, changed, err_msg


@router.get("/imgbb/progress")
def api_dvr_imgbb_progress() -> Dict[str, Any]:
    st = _imgbb_progress_get()
    total = int(st.get("total") or 0)
    done = int(st.get("done") or 0)
    pct = int((done * 100 / total)) if total > 0 else 0
    st["percent"] = max(0, min(100, pct))
    return {"ok": True, **st}


def _parse_sysinfo(sysinfo_txt: str, devtype_txt: str) -> Dict[str, str]:
    model = ""
    serial = ""
    for line in (sysinfo_txt or "").splitlines():
        if line.startswith("serialNumber="):
            serial = line.split("=", 1)[1].strip()
    for line in (devtype_txt or "").splitlines():
        if line.startswith("type="):
            model = line.split("=", 1)[1].strip()
    if not model:
        m = re.search(r"type=([^\r\n]+)", devtype_txt or "")
        model = m.group(1).strip() if m else ""
    return {
        "modelo": model,
        "equip_serial": serial,
        "fabricante": "Intelbras/Dahua DVR",
    }


def _parse_mac(network_txt: str) -> str:
    m = re.search(r"PhysicalAddress=([0-9A-Fa-f:-]{12,17})", network_txt or "")
    if not m:
        return ""
    return m.group(1).strip().lower().replace("-", ":")


def _parse_titles(channel_txt: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for idx, name in re.findall(r"ChannelTitle\[(\d+)\]\.Name=([^\r\n]*)", channel_txt or ""):
        ch = int(idx) + 1
        title = str(name or "").strip()
        out[ch] = title or f"Canal {ch}"
    return out


def _norm_mac_text(v: str) -> str:
    s = str(v or "").strip().lower()
    if not s:
        return ""
    # aceita "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "aabbccddeeff", "aabb.ccdd.eeff"
    hex_only = re.sub(r"[^0-9a-f]", "", s)
    if len(hex_only) < 12:
        return ""
    hex_only = hex_only[:12]
    if not re.fullmatch(r"[0-9a-f]{12}", hex_only):
        return ""
    return ":".join(hex_only[i:i + 2] for i in range(0, 12, 2))


def _norm_ip_text(v: str) -> str:
    s = str(v or "").strip()
    m = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", s)
    return m.group(0) if m else ""


def _arp_lookup_mac(ip: str) -> str:
    ipn = _norm_ip_text(ip)
    if not ipn:
        return ""
    try:
        # força tentativa de popular ARP
        subprocess.run(["ping", "-n", "1", "-w", "300", ipn], capture_output=True, text=True, check=False)
    except Exception:
        pass
    try:
        p = subprocess.run(["arp", "-a", ipn], capture_output=True, text=True, check=False)
        txt = str((p.stdout or "") + "\n" + (p.stderr or ""))
        m = re.search(rf"{re.escape(ipn)}\s+([0-9a-fA-F:\-]{{12,17}})", txt)
        if m:
            return _norm_mac_text(m.group(1))
    except Exception:
        pass
    return ""


def _parse_dahua_channel_macs(remote_txt: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    txt = str(remote_txt or "")
    if not txt:
        return out
    dev_mac: Dict[int, str] = {}
    ch_dev: Dict[int, int] = {}
    direct_ch_mac: Dict[int, str] = {}
    # Ex.: RemoteDevice[0].MacAddress=AA-BB-CC-DD-EE-FF -> canal 1 (ou via mapeamento)
    for line in txt.splitlines():
        ln = str(line or "").strip()
        if not ln or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        key_l = key.lower()
        mval = _norm_mac_text(val)
        if ".mac" in key_l and mval:
            m_dev = re.search(r"remotedevice\[(\d+)\]", key_l)
            if m_dev:
                dev_mac[int(m_dev.group(1))] = mval
                continue
            m_ch = re.search(r"(?:networkcam|videoinput|channel)\[(\d+)\]", key_l)
            if m_ch:
                idx = int(m_ch.group(1))
                for ch in (idx + 1, idx):
                    if ch > 0 and ch not in direct_ch_mac:
                        direct_ch_mac[ch] = mval
        # mapeamento canal -> remote device index
        m_map = re.search(r"(?:videoinput|channel|networkcam)\[(\d+)\]\.(?:remotedevice|source|bind)\b", key_l)
        if m_map:
            try:
                ch_idx = int(m_map.group(1))
                dev_idx = int(str(val or "").strip())
                ch_dev[ch_idx + 1] = dev_idx
                if ch_idx > 0:
                    ch_dev[ch_idx] = dev_idx
            except Exception:
                pass
    out.update(direct_ch_mac)
    # Formato comum Intelbras/Dahua:
    # table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Mac=aa:bb:cc:dd:ee:ff
    for idx, mac in re.findall(
        r"RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.Mac(?:Address)?=([^\r\n]+)",
        txt,
        flags=re.IGNORECASE,
    ):
        mval = _norm_mac_text(mac)
        if not mval:
            continue
        ch = int(idx) + 1
        if ch > 0 and ch not in out:
            out[ch] = mval
    for ch, didx in ch_dev.items():
        if ch > 0 and ch not in out and didx in dev_mac:
            out[ch] = dev_mac[didx]
    return out


def _parse_dahua_channel_ips(remote_txt: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    txt = str(remote_txt or "")
    if not txt:
        return out
    dev_ip: Dict[int, str] = {}
    ch_dev: Dict[int, int] = {}
    # base-0 -> canal +1
    for idx, ip in re.findall(r"RemoteDevice\[(\d+)\]\.(?:IP|Address|HostIP)=([^\r\n]+)", txt):
        ipn = _norm_ip_text(ip)
        if ipn:
            dev_ip[int(idx)] = ipn
    for idx, ip in re.findall(r"NetWorkCam\[(\d+)\]\.(?:IP|Address|HostIP)=([^\r\n]+)", txt):
        ch = int(idx) + 1
        ipn = _norm_ip_text(ip)
        if ch > 0 and ipn and ch not in out:
            out[ch] = ipn
    # varredura genérica linha a linha para mais modelos
    for line in txt.splitlines():
        ln = str(line or "").strip()
        if not ln or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        key_l = key.lower()
        ipn = _norm_ip_text(val)
        if ipn:
            m_dev = re.search(r"remotedevice\[(\d+)\]", key_l)
            if m_dev and any(k in key_l for k in (".ip", ".address", ".host")):
                dev_ip[int(m_dev.group(1))] = ipn
            m_ch = re.search(r"(?:networkcam|videoinput|channel)\[(\d+)\]", key_l)
            if m_ch and any(k in key_l for k in (".ip", ".address", ".host")):
                idx = int(m_ch.group(1))
                for ch in (idx + 1, idx):
                    if ch > 0 and ch not in out:
                        out[ch] = ipn
        m_map = re.search(r"(?:videoinput|channel|networkcam)\[(\d+)\]\.(?:remotedevice|source|bind)\b", key_l)
        if m_map:
            try:
                ch_idx = int(m_map.group(1))
                dev_idx = int(str(val or "").strip())
                ch_dev[ch_idx + 1] = dev_idx
                if ch_idx > 0:
                    ch_dev[ch_idx] = dev_idx
            except Exception:
                pass
    for ch, didx in ch_dev.items():
        if ch > 0 and ch not in out and didx in dev_ip:
            out[ch] = dev_ip[didx]
    # Formato comum Intelbras/Dahua:
    # table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.Address=10.10.11.52
    for idx, ip in re.findall(
        r"RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.Address=([^\r\n]+)",
        txt,
        flags=re.IGNORECASE,
    ):
        ipn = _norm_ip_text(ip)
        if not ipn:
            continue
        ch = int(idx) + 1
        if ch > 0 and ch not in out:
            out[ch] = ipn
    return out


def _parse_dahua_channel_models(remote_txt: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    txt = str(remote_txt or "")
    if not txt:
        return out
    # Formato comum:
    # table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_0.DeviceType=VIP-1130-B-G4
    for idx, model in re.findall(
        r"RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.(?:DeviceType|Model|Product|Type)=([^\r\n]+)",
        txt,
        flags=re.IGNORECASE,
    ):
        m = str(model or "").strip()
        if not m:
            continue
        ch = int(idx) + 1
        if ch > 0 and ch not in out:
            out[ch] = m
    # Fallback linha-a-linha para variações
    for line in txt.splitlines():
        ln = str(line or "").strip()
        if not ln or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        key_l = key.lower()
        m_dev = re.search(r"remotedevice\.uuid:system_config_netcamera_info_(\d+)\.", key_l)
        if not m_dev:
            continue
        if not any(k in key_l for k in (".devicetype", ".model", ".product", ".type")):
            continue
        model = str(val or "").strip()
        if not model:
            continue
        ch = int(m_dev.group(1)) + 1
        if ch > 0 and ch not in out:
            out[ch] = model
    return out


def _xml_local(tag: str) -> str:
    t = str(tag or "")
    return t.split("}", 1)[-1] if "}" in t else t


def _hik_get_text(url: str, auth: Any, timeout: float) -> str:
    try:
        r = requests.get(url, auth=auth, timeout=timeout)
    except Exception:
        return ""
    if int(r.status_code) != 200:
        return ""
    return str(r.text or "")


def _parse_hik_device_info(device_xml: str) -> Dict[str, str]:
    model = ""
    serial = ""
    try:
        root = ET.fromstring(device_xml or "")
        for n in root.iter():
            k = _xml_local(getattr(n, "tag", "")).lower()
            v = str(getattr(n, "text", "") or "").strip()
            if not v:
                continue
            if k in ("model", "devicetype"):
                model = model or v
            elif k in ("serialnumber", "serialno"):
                serial = serial or v
    except Exception:
        pass
    return {
        "modelo": model,
        "equip_serial": serial,
        "fabricante": "Hikvision/ISAPI NVR",
    }


def _parse_hik_mac(network_xml: str) -> str:
    try:
        root = ET.fromstring(network_xml or "")
        for n in root.iter():
            k = _xml_local(getattr(n, "tag", "")).lower()
            if k in ("macaddress", "physicaladdress"):
                v = str(getattr(n, "text", "") or "").strip().lower().replace("-", ":")
                if v:
                    return v
    except Exception:
        pass
    m = re.search(r"<(?:macAddress|physicalAddress)>([^<]+)</", network_xml or "", flags=re.IGNORECASE)
    if m:
        return str(m.group(1) or "").strip().lower().replace("-", ":")
    return ""


def _parse_hik_channels(ch_xml: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not ch_xml:
        return out
    try:
        root = ET.fromstring(ch_xml)
        cur_id = 0
        cur_name = ""
        for n in root.iter():
            k = _xml_local(getattr(n, "tag", "")).lower()
            v = str(getattr(n, "text", "") or "").strip()
            if not v:
                continue
            if k in ("id", "channelid", "proxychannelid"):
                try:
                    cur_id = int(v)
                except Exception:
                    cur_id = 0
            elif k in ("name", "channelname"):
                cur_name = v
            if cur_id > 0 and cur_name:
                out[cur_id] = cur_name
                cur_id = 0
                cur_name = ""
    except Exception:
        pass
    return out


def _parse_hik_channel_macs(ch_xml: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not ch_xml:
        return out
    try:
        root = ET.fromstring(ch_xml)
        # tenta por bloco de canal (mais confiavel)
        for node in root.iter():
            tag = _xml_local(getattr(node, "tag", "")).lower()
            if tag not in ("inputproxychannel", "videoinputchannel", "streamingchannel"):
                continue
            chid = 0
            cmac = ""
            for c in node.iter():
                k = _xml_local(getattr(c, "tag", "")).lower()
                v = str(getattr(c, "text", "") or "").strip()
                if not v:
                    continue
                if k in ("id", "channelid", "proxychannelid"):
                    try:
                        chid = int(v)
                    except Exception:
                        chid = 0
                elif k in ("macaddress", "physicaladdress"):
                    cmac = _norm_mac_text(v)
            if chid > 0 and cmac:
                out[chid] = cmac
        if out:
            return out
        # fallback linear
        cur_id = 0
        cur_mac = ""
        for n in root.iter():
            k = _xml_local(getattr(n, "tag", "")).lower()
            v = str(getattr(n, "text", "") or "").strip()
            if not v:
                continue
            if k in ("id", "channelid", "proxychannelid"):
                try:
                    cur_id = int(v)
                except Exception:
                    cur_id = 0
            elif k in ("macaddress", "physicaladdress"):
                cur_mac = _norm_mac_text(v)
            if cur_id > 0 and cur_mac:
                out[cur_id] = cur_mac
                cur_id = 0
                cur_mac = ""
    except Exception:
        pass
    return out


def _parse_hik_channel_ips(ch_xml: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not ch_xml:
        return out
    try:
        root = ET.fromstring(ch_xml)
        for node in root.iter():
            tag = _xml_local(getattr(node, "tag", "")).lower()
            if tag not in ("inputproxychannel", "videoinputchannel", "streamingchannel"):
                continue
            chid = 0
            cip = ""
            for c in node.iter():
                k = _xml_local(getattr(c, "tag", "")).lower()
                v = str(getattr(c, "text", "") or "").strip()
                if not v:
                    continue
                if k in ("id", "channelid", "proxychannelid"):
                    try:
                        chid = int(v)
                    except Exception:
                        chid = 0
                elif k in ("ipaddress", "address", "hostaddress"):
                    cip = _norm_ip_text(v)
            if chid > 0 and cip:
                out[chid] = cip
        if out:
            return out
        cur_id = 0
        cur_ip = ""
        for n in root.iter():
            k = _xml_local(getattr(n, "tag", "")).lower()
            v = str(getattr(n, "text", "") or "").strip()
            if not v:
                continue
            if k in ("id", "channelid", "proxychannelid"):
                try:
                    cur_id = int(v)
                except Exception:
                    cur_id = 0
            elif k in ("ipaddress", "address", "hostaddress"):
                cur_ip = _norm_ip_text(v)
            if cur_id > 0 and cur_ip:
                out[cur_id] = cur_ip
                cur_id = 0
                cur_ip = ""
    except Exception:
        pass
    return out


def _parse_hik_channel_models(ch_xml: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not ch_xml:
        return out
    try:
        root = ET.fromstring(ch_xml)
        for node in root.iter():
            tag = _xml_local(getattr(node, "tag", "")).lower()
            if tag not in ("inputproxychannel", "videoinputchannel", "streamingchannel"):
                continue
            chid = 0
            cmodel = ""
            for c in node.iter():
                k = _xml_local(getattr(c, "tag", "")).lower()
                v = str(getattr(c, "text", "") or "").strip()
                if not v:
                    continue
                if k in ("id", "channelid", "proxychannelid"):
                    try:
                        chid = int(v)
                    except Exception:
                        chid = 0
                elif k in ("model", "devicetype", "product", "type"):
                    if not cmodel:
                        cmodel = v
            if chid > 0 and cmodel:
                out[chid] = cmodel
    except Exception:
        pass
    return out


def _parse_bool_text(value: Any) -> bool | None:
    txt = str(value or "").strip().lower()
    if not txt:
        return None
    if txt in {"true", "1", "yes", "on", "online", "connected", "up", "normal", "ok", "enable", "enabled", "active", "recording"}:
        return True
    if txt in {"false", "0", "no", "off", "offline", "disconnected", "down", "error", "disable", "disabled", "inactive", "idle", "stop", "stopped"}:
        return False
    return None


def _format_capacity_mb(value: float) -> str:
    try:
        mb = float(value or 0)
    except Exception:
        mb = 0.0
    if mb <= 0:
        return ""
    if mb >= 1024 * 1024:
        return f"{mb / 1024 / 1024:.1f} TB"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


def _num_from_text(value: Any) -> float:
    txt = str(value or "").strip()
    if not txt:
        return 0.0
    txt = txt.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", txt)
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except Exception:
        return 0.0


def _xml_tags_by_local_name(xml_text: str) -> Dict[str, List[str]]:
    tags: Dict[str, List[str]] = {}
    if not xml_text:
        return tags
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return tags
    for node in root.iter():
        key = _xml_local(getattr(node, "tag", "")).lower()
        val = str(getattr(node, "text", "") or "").strip()
        if key and val:
            tags.setdefault(key, []).append(val)
    return tags


def _first_tag(tags: Dict[str, List[str]], *keys: str) -> str:
    for key in keys:
        vals = tags.get(str(key or "").lower()) or []
        for val in vals:
            txt = str(val or "").strip()
            if txt:
                return txt
    return ""


def _parse_hik_storage_health(*xml_parts: str) -> Dict[str, Any]:
    disks: List[Dict[str, Any]] = []
    for xml_text in xml_parts:
        if not xml_text:
            continue
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            continue
        for node in root.iter():
            tag = _xml_local(getattr(node, "tag", "")).lower()
            if tag not in {"hdd", "harddisk", "storagevolume", "volume"}:
                continue
            item: Dict[str, str] = {}
            for child in node.iter():
                key = _xml_local(getattr(child, "tag", "")).lower()
                val = str(getattr(child, "text", "") or "").strip()
                if key and val:
                    item[key] = val
            total = _num_from_text(
                item.get("capacity")
                or item.get("hddcapacity")
                or item.get("totalsize")
                or item.get("totalspace")
                or item.get("size")
            )
            free = _num_from_text(
                item.get("freespace")
                or item.get("free")
                or item.get("remainspace")
                or item.get("remaincapacity")
                or item.get("unusedspace")
            )
            status = (
                item.get("status")
                or item.get("diskstatus")
                or item.get("hddstatus")
                or item.get("healthstatus")
                or item.get("state")
                or ""
            )
            if total or free or status:
                disks.append({"total_mb": total, "free_mb": free, "status": status})
    if not disks:
        return {}
    total_mb = sum(float(d.get("total_mb") or 0) for d in disks)
    free_mb = sum(float(d.get("free_mb") or 0) for d in disks)
    bad = [
        str(d.get("status") or "").strip()
        for d in disks
        if str(d.get("status") or "").strip().lower() not in {"", "ok", "normal", "rw", "readwrite", "formatting"}
    ]
    status_text = "normal" if not bad else "; ".join(bad[:3])
    total_txt = _format_capacity_mb(total_mb)
    free_txt = _format_capacity_mb(free_mb)
    used_pct = ""
    if total_mb > 0 and free_mb >= 0:
        used_pct = f"{max(0.0, min(100.0, (1.0 - (free_mb / total_mb)) * 100.0)):.0f}% usado"
    summary_bits = [status_text]
    if total_txt:
        summary_bits.append(total_txt)
    if free_txt:
        summary_bits.append(f"{free_txt} livre")
    if used_pct:
        summary_bits.append(used_pct)
    return {
        "hdd_status": " - ".join(summary_bits),
        "hdd_total": total_txt,
        "hdd_free": free_txt,
        "hdd_used_percent": used_pct,
        "hdd_count": len(disks),
    }


def _parse_dahua_storage_health(*text_parts: str) -> Dict[str, Any]:
    text = "\n".join(str(t or "") for t in text_parts if t)
    if not text.strip():
        return {}
    totals = [_num_from_text(v) for v in re.findall(r"(?:TotalBytes|TotalSpace|Total|Capacity|Size)\s*=\s*([^\r\n]+)", text, flags=re.IGNORECASE)]
    frees = [_num_from_text(v) for v in re.findall(r"(?:FreeBytes|FreeSpace|Remain|RemainSpace|Unused)\s*=\s*([^\r\n]+)", text, flags=re.IGNORECASE)]
    statuses = [
        str(v or "").strip()
        for v in re.findall(r"(?:Status|State|Health)\s*=\s*([^\r\n]+)", text, flags=re.IGNORECASE)
        if str(v or "").strip()
    ]
    # Muitos Dahua/Intelbras retornam bytes. Se o numero for grande demais,
    # convertemos para MB para usar o mesmo formato do Hikvision.
    total = sum(t / (1024 * 1024) if t > 10_000_000 else t for t in totals)
    free = sum(f / (1024 * 1024) if f > 10_000_000 else f for f in frees)
    if not total and not free and not statuses:
        return {}
    bad = [s for s in statuses if s.lower() not in {"ok", "normal", "readwrite", "rw", "ready"}]
    status_text = "normal" if not bad else "; ".join(bad[:3])
    total_txt = _format_capacity_mb(total)
    free_txt = _format_capacity_mb(free)
    bits = [status_text]
    if total_txt:
        bits.append(total_txt)
    if free_txt:
        bits.append(f"{free_txt} livre")
    return {
        "hdd_status": " - ".join(bits),
        "hdd_total": total_txt,
        "hdd_free": free_txt,
        "hdd_count": max(len(totals), len(statuses), 1),
    }


def _parse_hik_network_health(network_xml: str) -> Dict[str, Any]:
    tags = _xml_tags_by_local_name(network_xml)
    ipaddr = _first_tag(tags, "ipaddress", "ipv4address")
    mask = _first_tag(tags, "subnetmask", "ipv4subnetmask")
    gateway = _first_tag(tags, "defaultgateway", "gateway", "ipv4defaultgateway")
    dns = _first_tag(tags, "primarydns", "dnsserver", "primarydnserver")
    mac = _first_tag(tags, "macaddress", "physicaladdress")
    try:
        root = ET.fromstring(network_xml or "")
        for node in root.iter():
            tag = _xml_local(getattr(node, "tag", "")).lower()
            if tag in {"defaultgateway", "gateway", "ipv4defaultgateway"} and not gateway:
                for child in node.iter():
                    key = _xml_local(getattr(child, "tag", "")).lower()
                    val = str(getattr(child, "text", "") or "").strip()
                    if key in {"ipaddress", "ipv4address"} and val:
                        gateway = val
                        break
            if tag in {"primarydns", "dnsserver", "primarydnserver"} and not dns:
                val = str(getattr(node, "text", "") or "").strip()
                if val:
                    dns = val
    except Exception:
        pass
    bits = []
    if ipaddr:
        bits.append(ipaddr)
    if gateway:
        bits.append(f"gw {gateway}")
    if dns:
        bits.append(f"dns {dns}")
    out: Dict[str, Any] = {}
    if bits:
        out["network_status"] = " - ".join(bits)
    if ipaddr:
        out["nvr_ip"] = ipaddr
    if mask:
        out["nvr_mask"] = mask
    if gateway:
        out["nvr_gateway"] = gateway
    if dns:
        out["nvr_dns"] = dns
    if mac:
        out["nvr_mac"] = _norm_mac_text(mac)
    return out


def _parse_dahua_network_health(network_txt: str) -> Dict[str, Any]:
    text = str(network_txt or "")
    if not text.strip():
        return {}
    def pick(*patterns: str) -> str:
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                val = str(m.group(1) or "").strip()
                if val:
                    return val
        return ""
    ipaddr = pick(r"\.IPAddress\s*=\s*([^\r\n]+)", r"\.IP\s*=\s*([^\r\n]+)")
    mask = pick(r"\.SubnetMask\s*=\s*([^\r\n]+)")
    gateway = pick(r"\.DefaultGateway\s*=\s*([^\r\n]+)", r"\.Gateway\s*=\s*([^\r\n]+)")
    dns = pick(r"\.DnsServers?\[0\]\s*=\s*([^\r\n]+)", r"\.PreferredDns\s*=\s*([^\r\n]+)")
    mac = pick(r"\.PhysicalAddress\s*=\s*([^\r\n]+)", r"\.MACAddress\s*=\s*([^\r\n]+)")
    bits = []
    if ipaddr:
        bits.append(ipaddr)
    if gateway:
        bits.append(f"gw {gateway}")
    if dns:
        bits.append(f"dns {dns}")
    out: Dict[str, Any] = {}
    if bits:
        out["network_status"] = " - ".join(bits)
    if ipaddr:
        out["nvr_ip"] = ipaddr
    if mask:
        out["nvr_mask"] = mask
    if gateway:
        out["nvr_gateway"] = gateway
    if dns:
        out["nvr_dns"] = dns
    if mac:
        out["nvr_mac"] = _norm_mac_text(mac)
    return out


def _parse_platform_status(*payloads: str) -> Dict[str, Any]:
    text = "\n".join(str(p or "") for p in payloads if p)
    if not text.strip():
        return {}
    tags: Dict[str, List[str]] = {}
    for payload in payloads:
        part = str(payload or "").strip()
        if not part.startswith("<"):
            continue
        for key, vals in _xml_tags_by_local_name(part).items():
            tags.setdefault(key, []).extend(vals)
    enabled = None
    for key in ("enabled", "enable", "ezvizenable", "hikconnectenable"):
        enabled = _parse_bool_text(_first_tag(tags, key))
        if enabled is not None:
            break
    status = _first_tag(tags, "registerstatus", "onlinestatus", "status", "connectstatus", "connectionstatus")
    if not status:
        m = re.search(r"(?:RegisterStatus|OnlineStatus|Status|ConnectStatus|ConnectionStatus)\s*=\s*([^\r\n<]+)", text, flags=re.IGNORECASE)
        status = str(m.group(1) or "").strip() if m else ""
    if enabled is None:
        m = re.search(r"(?:Enable|Enabled)\s*=\s*([^\r\n<]+)", text, flags=re.IGNORECASE)
        enabled = _parse_bool_text(m.group(1)) if m else None
    status_l = status.lower()
    online = None
    if any(v in status_l for v in ("online", "connected", "registered", "normal")):
        online = True
    elif any(v in status_l for v in ("offline", "disconnected", "unregistered", "error", "failed")):
        online = False
    if online is None and enabled is not None:
        online = bool(enabled)
    if online is None and not status:
        return {}
    label = "online" if online else "offline"
    if enabled is False:
        label = "desabilitada"
    elif status:
        label = f"{label} ({status})"
    return {
        "platform_status": label,
        "cloud_status": label,
        "hik_connect_status": label,
        "p2p_status": label,
        "platform_online": bool(online),
    }


def _parse_hik_recording_status(xml_text: str, channels: range) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out
    for node in root.iter():
        tag = _xml_local(getattr(node, "tag", "")).lower()
        if tag not in {"track", "trackstatus", "recordstatus", "recordingstatus", "channelstatus"}:
            continue
        data: Dict[str, str] = {}
        for child in node.iter():
            key = _xml_local(getattr(child, "tag", "")).lower()
            val = str(getattr(child, "text", "") or "").strip()
            if key and val:
                data[key] = val
        ch = 0
        for key in ("channelid", "inputchannelid", "videochannel", "channel"):
            if data.get(key):
                ch = int(_num_from_text(data.get(key)))
                break
        if ch <= 0:
            track = data.get("id") or data.get("trackid") or ""
            num = int(_num_from_text(track))
            if num >= 100:
                ch = num // 100
            elif num > 0:
                ch = num
        if ch <= 0:
            continue
        status = " ".join(
            data.get(k, "")
            for k in ("recording", "record", "recordenable", "enabled", "enable", "status", "state")
            if data.get(k)
        ).strip()
        rec = _parse_bool_text(status)
        if rec is None and status:
            status_l = status.lower()
            rec = any(v in status_l for v in ("record", "enable", "normal", "active"))
            if any(v in status_l for v in ("stop", "disable", "error", "idle")):
                rec = False
        out[ch] = {"recording": rec, "recording_status": status or ("gravando" if rec else "")}
    if out:
        return out
    for ch in channels:
        text_l = xml_text.lower()
        if f">{int(ch)}<" in text_l or f">{int(ch)}01<" in text_l:
            out[int(ch)] = {"recording_status": "configurado"}
    return out


def _parse_dahua_recording_status(text: str) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    if not text:
        return out
    for match in re.finditer(r"(?:table\.RecordMode|RecordMode|Mode)\[(\d+)\]\s*=\s*([^\r\n]+)", text, flags=re.IGNORECASE):
        ch = int(match.group(1)) + 1
        val = str(match.group(2) or "").strip()
        rec = _parse_bool_text(val)
        if rec is None:
            rec = val.lower() not in {"0", "off", "stop", "closed", "none"}
        out[ch] = {"recording": rec, "recording_status": val}
    for match in re.finditer(r"(?:channels|channel)\[(\d+)\]\s*=\s*([^\r\n]+)", text, flags=re.IGNORECASE):
        ch = int(match.group(1)) + 1
        val = str(match.group(2) or "").strip()
        rec = _parse_bool_text(val)
        if rec is not None:
            out[ch] = {"recording": rec, "recording_status": val}
    return out


def _hik_track_id(channel: int) -> int:
    return int(channel) * 100 + 1


def _hik_recording_search_xml(channel: int, start: datetime, end: datetime, max_results: int = 1) -> str:
    search_id = f"sightops-{int(time.time() * 1000)}-{int(channel)}"
    start_txt = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_txt = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
  <searchID>{search_id}</searchID>
  <trackList>
    <trackID>{_hik_track_id(channel)}</trackID>
  </trackList>
  <timeSpanList>
    <timeSpan>
      <startTime>{start_txt}</startTime>
      <endTime>{end_txt}</endTime>
    </timeSpan>
  </timeSpanList>
  <maxResults>{int(max_results)}</maxResults>
  <searchResultPosition>0</searchResultPosition>
  <metadataList>
    <metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
  </metadataList>
</CMSearchDescription>"""


def _hik_search_has_recording(base: str, auth: Any, timeout: float, channel: int, start: datetime, end: datetime) -> bool | None:
    body = _hik_recording_search_xml(channel, start, end, max_results=1)
    try:
        r = requests.post(
            f"{base}/ISAPI/ContentMgmt/search",
            auth=auth,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=(2.0, float(timeout)),
        )
    except Exception:
        return None
    if int(r.status_code) not in {200, 201}:
        return None
    txt = str(r.text or "")
    m = re.search(r"<(?:numOfMatches|totalMatches|responseStatusStrg)>([^<]+)</", txt, flags=re.IGNORECASE)
    if m:
        val = str(m.group(1) or "").strip()
        if val.lower() in {"ok", "success"}:
            return "searchmatchitem" in txt.lower() or "<trackid>" in txt.lower()
        try:
            return int(float(val)) > 0
        except Exception:
            pass
    low = txt.lower()
    if "searchmatchitem" in low or "<trackid>" in low or "<playbackuri>" in low:
        return True
    if "nomatch" in low or "no match" in low:
        return False
    return None


def _hik_collect_recording_search_health(
    base: str,
    auth: Any,
    timeout: float,
    channels: range,
    status_by_channel: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    now = datetime.utcnow()
    probe_channels = [int(ch) for ch in channels][:64]
    # Confirma gravacao por evidência de playback. A janela de 24h cobre
    # canais em gravacao continua e canais por movimento sem depender de
    # flag ambigua de "canal online".
    for ch in probe_channels:
        has_recent = _hik_search_has_recording(
            base,
            auth,
            timeout,
            ch,
            now - timedelta(hours=24),
            now,
        )
        if has_recent is None:
            continue
        status_by_channel[ch] = {
            "recording": bool(has_recent),
            "recording_status": "playback encontrado 24h" if has_recent else "sem playback 24h",
        }

    sample_channel = 0
    for ch in probe_channels:
        if status_by_channel.get(ch, {}).get("recording") is True:
            sample_channel = ch
            break
    if not sample_channel and probe_channels:
        sample_channel = probe_channels[0]
    if not sample_channel:
        return out

    retention_days = 0
    for days in (1, 3, 7, 15, 30, 60, 90):
        target = now - timedelta(days=days)
        has_old = _hik_search_has_recording(
            base,
            auth,
            timeout,
            sample_channel,
            target,
            target + timedelta(hours=6),
        )
        if has_old is True:
            retention_days = days
        elif has_old is False and retention_days:
            break
    if retention_days:
        out["recording_days"] = str(retention_days)
        out["retention_days"] = str(retention_days)
        out["retention"] = f"{retention_days} dias ou mais"
    return out


def _collect_nvr_health(
    base: str,
    auth: Any,
    mode: str,
    timeout: float,
    channels: range,
    network_payload: str = "",
) -> Dict[str, Any]:
    health: Dict[str, Any] = {"recorder_health_collected": False, "recording_by_channel": {}}
    def safe_get(path: str) -> str:
        try:
            return _get_text(f"{base}{path}", auth, timeout)
        except Exception:
            return ""

    if mode == "hik":
        storage_xml = _hik_get_text(f"{base}/ISAPI/ContentMgmt/Storage/hdd", auth, timeout)
        if not storage_xml:
            storage_xml = _hik_get_text(f"{base}/ISAPI/ContentMgmt/storage", auth, timeout)
        health.update(_parse_hik_storage_health(storage_xml))
        network = _parse_hik_network_health(network_payload)
        health.update({k: v for k, v in network.items() if v})
        platform_payloads = []
        for path in (
            "/ISAPI/System/Network/EZVIZ",
            "/ISAPI/System/Network/HikConnect",
            "/ISAPI/System/Network/PlatformAccess",
            "/ISAPI/System/Network/PPPoE/1",
        ):
            txt = _hik_get_text(f"{base}{path}", auth, timeout)
            if txt:
                platform_payloads.append(txt)
        health.update(_parse_platform_status(*platform_payloads))
        rec_payloads = []
        for path in (
            "/ISAPI/ContentMgmt/record/tracks",
            "/ISAPI/ContentMgmt/record/status",
            "/ISAPI/ContentMgmt/record/tracks/status",
        ):
            txt = _hik_get_text(f"{base}{path}", auth, timeout)
            if txt:
                rec_payloads.append(txt)
        rec_by_ch: Dict[int, Dict[str, Any]] = {}
        for payload in rec_payloads:
            rec_by_ch.update(_parse_hik_recording_status(payload, channels))
        health.update(_hik_collect_recording_search_health(base, auth, timeout, channels, rec_by_ch))
        health["recording_by_channel"] = rec_by_ch
    else:
        storage_parts = []
        for path in (
            "/cgi-bin/storage.cgi?action=getDeviceAllInfo",
            "/cgi-bin/storage.cgi?action=getDiskInfo",
            "/cgi-bin/storage.cgi?action=getAllDiskInfo",
            "/cgi-bin/configManager.cgi?action=getConfig&name=Storage",
        ):
            txt = safe_get(path)
            if txt:
                storage_parts.append(txt)
        health.update(_parse_dahua_storage_health(*storage_parts))
        health.update({k: v for k, v in _parse_dahua_network_health(network_payload).items() if v})
        platform_parts = []
        for path in (
            "/cgi-bin/configManager.cgi?action=getConfig&name=VSP_PaaS",
            "/cgi-bin/configManager.cgi?action=getConfig&name=T2UServer",
            "/cgi-bin/configManager.cgi?action=getConfig&name=P2P",
        ):
            txt = safe_get(path)
            if txt:
                platform_parts.append(txt)
        health.update(_parse_platform_status(*platform_parts))
        rec_parts = []
        for path in (
            "/cgi-bin/configManager.cgi?action=getConfig&name=RecordMode",
            "/cgi-bin/recordManager.cgi?action=getStatus",
        ):
            txt = safe_get(path)
            if txt:
                rec_parts.append(txt)
        rec_by_ch: Dict[int, Dict[str, Any]] = {}
        for payload in rec_parts:
            rec_by_ch.update(_parse_dahua_recording_status(payload))
        health["recording_by_channel"] = rec_by_ch
    health["recorder_health_collected"] = any(
        health.get(k)
        for k in ("hdd_status", "network_status", "platform_status", "recording_by_channel")
    )
    return health


def _parse_hik_channel_runtime_status(xml_text: str, fallback_channel: int = 0) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    tags: Dict[str, str] = {}
    channel_id = int(fallback_channel or 0)
    for node in root.iter():
        key = _xml_local(getattr(node, "tag", "")).lower()
        val = str(getattr(node, "text", "") or "").strip()
        if not val:
            continue
        tags[key] = val
        if key in ("id", "channelid", "proxychannelid", "videoinputid", "inputchannelid"):
            try:
                channel_id = int(val)
            except Exception:
                pass

    online = None
    for key in ("online", "isonline", "connected", "isconnected", "enabled", "enable"):
        parsed = _parse_bool_text(tags.get(key))
        if parsed is not None:
            online = parsed
            break

    if online is None:
        status_text = " ".join(
            str(tags.get(k) or "").strip().lower()
            for k in ("connectionstatus", "status", "statedescription", "chandetectresult", "workstatus", "inputportstatus")
            if tags.get(k)
        ).strip()
        if status_text:
            positive = ("online", "connected", "connect", "normal", "working", "active", "using", "inuse", "up", "ok")
            negative = ("offline", "disconnected", "disconnect", "videoloss", "video loss", "error", "failed", "down")
            if any(token in status_text for token in positive):
                online = True
            elif any(token in status_text for token in negative):
                online = False

    if channel_id > 0:
        out["channel"] = channel_id
    if online is not None:
        out["online"] = bool(online)
    if tags.get("connectionstatus"):
        out["connection_status"] = str(tags.get("connectionstatus") or "").strip()
    if tags.get("chandetectresult"):
        out["detect_result"] = str(tags.get("chandetectresult") or "").strip()
    if tags.get("protocolname"):
        out["protocol"] = str(tags.get("protocolname") or "").strip()
    elif tags.get("streamtransprotocol"):
        out["protocol"] = str(tags.get("streamtransprotocol") or "").strip()
    return out


def _hik_collect_channel_runtime_statuses(
    base: str,
    auth: Any,
    timeout: float,
    channels: range,
) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for ch in channels:
        urls = (
            f"{base}/ISAPI/ContentMgmt/InputProxy/channels/{int(ch)}/status",
            f"{base}/ISAPI/System/Video/inputs/channels/{int(ch)}/status",
        )
        for url in urls:
            xml_text = _hik_get_text(url, auth, timeout)
            if not xml_text:
                continue
            parsed = _parse_hik_channel_runtime_status(xml_text, fallback_channel=int(ch))
            if parsed:
                out[int(parsed.get('channel') or ch)] = parsed
                break
    return out


def _load_online_ip_inventory_map() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for row in legacy_rows_from_db("ip"):
            if not isinstance(row, dict):
                continue
            ip = _norm_ip_text(row.get("ip") or row.get("host") or "")
            status = str(row.get("status") or "").strip().lower()
            if not ip or status != "online":
                continue
            out[ip] = row
    except Exception:
        return {}
    return out


def _hik_fetch_camera_mac(ip: str, user: str, password: str, timeout: float) -> str:
    cip = _norm_ip_text(ip)
    if not cip or not user:
        return ""
    auth = HTTPDigestAuth(user, password)
    urls = (
        f"http://{cip}/ISAPI/System/deviceInfo",
        f"http://{cip}/ISAPI/System/Network/interfaces",
        f"http://{cip}/ISAPI/System/Network/interfaces/1",
        f"http://{cip}/ISAPI/System/Network",
    )
    for url in urls:
        try:
            r = requests.get(url, auth=auth, timeout=(2.0, float(timeout)))
            if int(r.status_code) != 200:
                continue
            txt = str(r.text or "")
            m = re.search(r"<(?:macAddress|physicalAddress)>([^<]+)</", txt, flags=re.IGNORECASE)
            if m:
                mm = _norm_mac_text(m.group(1))
                if mm:
                    return mm
        except Exception:
            continue
    return ""


def _probe_nvr_stack(base: str, user: str, password: str, timeout: float) -> Dict[str, Any]:
    # 1) Dahua/Intelbras style (cgi-bin)
    auth_d = HTTPDigestAuth(user, password)
    sysinfo_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getSystemInfo", auth_d, timeout)
    devtype_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getDeviceType", auth_d, timeout)
    network_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0", auth_d, timeout)
    channel_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle", auth_d, timeout)
    remote_parts = []
    for cfg in ("RemoteDevice", "NetWorkCam", "VideoInput", "Camera", "IPC"):
        t = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name={cfg}", auth_d, timeout)
        if t:
            remote_parts.append(t)
    remote_txt = "\n".join(remote_parts)
    if channel_txt or sysinfo_txt:
        return {
            "ok": True,
            "mode": "dahua",
            "auth": auth_d,
            "sysinfo_txt": sysinfo_txt,
            "devtype_txt": devtype_txt,
            "network_txt": network_txt,
            "channel_txt": channel_txt,
            "remote_txt": remote_txt,
        }

    auth_b = HTTPBasicAuth(user, password)
    sysinfo_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getSystemInfo", auth_b, timeout)
    devtype_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getDeviceType", auth_b, timeout)
    network_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0", auth_b, timeout)
    channel_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle", auth_b, timeout)
    remote_parts = []
    for cfg in ("RemoteDevice", "NetWorkCam", "VideoInput", "Camera", "IPC"):
        t = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name={cfg}", auth_b, timeout)
        if t:
            remote_parts.append(t)
    remote_txt = "\n".join(remote_parts)
    if channel_txt or sysinfo_txt:
        return {
            "ok": True,
            "mode": "dahua",
            "auth": auth_b,
            "sysinfo_txt": sysinfo_txt,
            "devtype_txt": devtype_txt,
            "network_txt": network_txt,
            "channel_txt": channel_txt,
            "remote_txt": remote_txt,
        }

    # 2) Hikvision/ISAPI style
    for auth in (auth_d, auth_b):
        device_xml = _hik_get_text(f"{base}/ISAPI/System/deviceInfo", auth, timeout)
        if not device_xml:
            continue
        channels_xml = _hik_get_text(f"{base}/ISAPI/ContentMgmt/InputProxy/channels", auth, timeout)
        if not channels_xml:
            channels_xml = _hik_get_text(f"{base}/ISAPI/System/Video/inputs/channels", auth, timeout)
        network_xml = _hik_get_text(f"{base}/ISAPI/System/Network/interfaces", auth, timeout)
        return {
            "ok": True,
            "mode": "hik",
            "auth": auth,
            "device_xml": device_xml,
            "channels_xml": channels_xml,
            "network_xml": network_xml,
        }

    return {"ok": False}


def _video_loss_channels(base: str, auth: HTTPDigestAuth, timeout: float) -> set[int]:
    url = f"{base}/cgi-bin/eventManager.cgi?action=getEventIndexes&code=VideoLoss"
    try:
        r = requests.get(url, auth=auth, timeout=timeout)
        if r.status_code != 200:
            return set()
        txt = r.text or ""
    except Exception:
        return set()

    out: set[int] = set()
    for v in re.findall(r"channels\[\d+\]=(\d+)", txt):
        try:
            # Em muitos DVRs Dahua/Intelbras esse Ã­ndice vem base-0.
            # Ex.: channels[]=15 corresponde ao canal 16.
            out.add(int(v) + 1)
        except Exception:
            continue
    return out


def _snapshot_is_dark(img_path: Path) -> bool:
    try:
        from PIL import Image
        import numpy as np

        im = Image.open(img_path).convert("L").resize((320, 180))
        arr = np.asarray(im, dtype=np.uint8)
        mean = float(arr.mean())
        std = float(arr.std())
        dark_ratio = float((arr < 28).mean())
        # Sem sinal costuma vir quase preto, com baixÃ­ssima variaÃ§Ã£o.
        return bool(mean < 24 and std < 10 and dark_ratio > 0.94)
    except Exception:
        return False


def _snapshot_for_channel(base: str, auth: Any, timeout: float, channel: int, out_name: str) -> tuple[str, bool]:
    snap_dir = tenant_snapshot_dir("nvr") if get_current_tenant_slug() else NVR_SNAPSHOT_DIR
    snap_dir.mkdir(parents=True, exist_ok=True)
    url = f"{base}/cgi-bin/snapshot.cgi?channel={int(channel)}"
    try:
        # Timeout separado (connect/read) evita travar handshake + stream.
        r = requests.get(url, auth=auth, timeout=(2.0, float(timeout)), stream=True)
        ctype = str(r.headers.get("Content-Type") or "").lower()
        if r.status_code == 200 and "image" in ctype:
            out_file = snap_dir / out_name
            with out_file.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return f"/data/nvr_snapshot/{out_file.name}", _snapshot_is_dark(out_file)
    except requests.RequestException:
        pass
    except Exception:
        pass

    # Fallback ISAPI (Hikvision-like): /ISAPI/Streaming/channels/101/picture
    ch_code = f"{int(channel)}01"
    for hurl in (
        f"{base}/ISAPI/Streaming/channels/{ch_code}/picture",
        f"{base}/ISAPI/ContentMgmt/StreamingProxy/channels/{ch_code}/picture",
    ):
        try:
            r = requests.get(hurl, auth=auth, timeout=(2.0, float(timeout)), stream=True)
            ctype = str(r.headers.get("Content-Type") or "").lower()
            if r.status_code != 200 or "image" not in ctype:
                continue
            out_file = snap_dir / out_name
            with out_file.open("wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return f"/data/nvr_snapshot/{out_file.name}", _snapshot_is_dark(out_file)
        except Exception:
            continue
    return "", False


@router.get("/inventory")
def api_dvr_inventory(site: str = "") -> Dict[str, Any]:
    # Tenant com arquivo proprio: essa e a UNICA fonte que /save, /delete e
    # /clear tocam (ver _read_rows/_write_rows). Consultar o banco primeiro
    # aqui faria a tela voltar a mostrar dados congelados sempre que a tabela
    # `recorders` tiver qualquer linha para o tenant, ignorando exclusoes e
    # edicoes feitas pela UI -- o mesmo bug ja corrigido no inventario de
    # cameras (ver load_inventory_json).
    if get_current_tenant_slug():
        rows = _read_rows()
        site_norm = str(site or "").strip().lower()
        if site_norm:
            def _matches(row: Dict[str, Any]) -> bool:
                vals = [
                    str(row.get("site") or "").strip(),
                    str(row.get("site_name") or "").strip(),
                    str(row.get("local") or "").strip(),
                ]
                return any(v.lower() == site_norm for v in vals if v)
            rows = [r for r in rows if isinstance(r, dict) and _matches(r)]
        return {"ok": True, "inventory": rows}

    rows = legacy_rows_from_db("nvr", site=site)
    if not rows:
        rows = _read_rows()
        rows = decorate_legacy_rows("nvr", rows, site=site)
    return {"ok": True, "inventory": rows}


@router.post("/save")
def api_nvr_save(req: RecorderSaveRequest) -> Dict[str, Any]:
    rows = _read_rows()
    # Casa a edicao por (host, canal) -- NAO por connector_id. O frontend
    # (saveEditRec) nao envia connector_id no payload, entao incluir ele na
    # chave fazia a edicao de uma linha COM connector nunca casar, cair no
    # ramo de insercao e DUPLICAR a linha (com connector/site vazios). Bug real
    # que apagou site e duplicou canais do NVR da Mega Alarmes (UFV-RODOANEL).
    updates: Dict[tuple[str, int], Dict[str, Any]] = {}
    for item in req.recorders or []:
        host = str(item.get("host") or item.get("ip") or "").strip()
        try:
            channel = int(item.get("channel") or 0)
        except Exception:
            channel = 0
        if host and channel > 0:
            updates[(host, channel)] = item

    if not updates:
        raise HTTPException(status_code=400, detail="Nenhum canal informado para salvar.")

    editable_fields = {
        "title", "local", "status", "nvr_model", "camera_ip", "camera_model",
        "camera_mac", "mac", "equip_serial", "pon", "onu_id", "onu_name",
        "onu_serial", "switch_ip", "switch_port", "switch_vlan", "video_loss",
        "snapshot_url", "imgbb_url", "imgbb_thumb_url", "site", "remote",
        "remote_connector_id", "recorder_user", "http_port", "name", "recorder_name", "inventory_mode",
        "hdd_status", "hdd_total", "hdd_free", "hdd_used_percent", "hdd_count",
        "network_status", "nvr_ip", "nvr_mask", "nvr_gateway", "nvr_dns",
        "platform_status", "cloud_status", "hik_connect_status", "p2p_status", "platform_online",
        "recording", "is_recording", "recording_status", "recording_days", "retention_days", "retention",
        "recorder_health_collected",
    }
    updated = 0
    found: set[tuple[str, str, int]] = set()
    for row in rows:
        host = str(row.get("host") or row.get("ip") or "").strip()
        try:
            channel = int(row.get("channel") or 0)
        except Exception:
            channel = 0
        key = (host, channel)
        item = updates.get(key)
        if not item:
            continue
        found.add(key)
        before = dict(row)
        for field in editable_fields:
            if field in item:
                row[field] = item.get(field)
        if "modelo" in item and "camera_model" not in item:
            row["camera_model"] = item.get("modelo")
        if row != before:
            updated += 1

    inserted = 0
    for key, item in updates.items():
        if key in found:
            continue
        new_row = {field: item.get(field, "") for field in editable_fields if field in item}
        new_row["host"] = key[0]
        new_row["channel"] = key[1]
        new_row.setdefault("title", str(item.get("title") or f"Canal {key[1]:02d}").strip())
        new_row.setdefault("status", str(item.get("status") or "online").strip())
        rows.append(new_row)
        inserted += 1

    _write_rows(rows)
    return {"ok": True, "updated": updated, "inserted": inserted, "total": len(rows)}


@router.post("/delete")
def api_nvr_delete(req: RecorderDeleteRequest) -> Dict[str, Any]:
    keys: set[tuple[str, int]] = set()
    for item in req.items or []:
        host = str(item.get("host") or item.get("ip") or "").strip()
        try:
            channel = int(item.get("channel") or 0)
        except Exception:
            channel = 0
        if host and channel > 0:
            keys.add((host, channel))
    if not keys:
        raise HTTPException(status_code=400, detail="Nenhum canal informado para apagar.")

    rows = _read_rows()
    mode = str(req.mode or "").strip()
    site = str(req.site or "").strip()
    host_filter = str(req.host or "").strip()
    connector_id = str(req.connector_id or req.remote_connector_id or "").strip()
    kept = []
    removed = 0
    for row in rows:
        host = str(row.get("host") or row.get("ip") or "").strip()
        try:
            channel = int(row.get("channel") or 0)
        except Exception:
            channel = 0
        if (host, channel) in keys and _rec_matches_delete_scope(
            row,
            mode=mode,
            site=site,
            host=host_filter,
            connector_id=connector_id,
        ):
            removed += 1
            continue
        kept.append(row)
    _write_rows(kept)
    return {"ok": True, "removed": removed, "total": len(kept)}


@router.post("/clear")
def api_dvr_clear(site: str = "", host: str = "", mode: str = "", connector_id: str = "") -> Dict[str, Any]:
    site_norm = str(site or "").strip().lower()
    host_norm = str(host or "").strip()
    mode_norm = str(mode or "").strip()
    connector_norm = str(connector_id or "").strip()
    if site_norm or host_norm or mode_norm or connector_norm:
        rows = _read_rows()
        kept = [
            r for r in rows
            if not (
                isinstance(r, dict)
                and _rec_matches_delete_scope(
                    r,
                    mode=mode_norm,
                    site=site_norm,
                    host=host_norm,
                    connector_id=connector_norm,
                )
            )
        ]
        removed_rows = max(0, len(rows) - len(kept))
        _write_rows(kept)
        return {
            "ok": True,
            "cleared": True,
            "scope": "filtered",
            "site": site.strip(),
            "host": host_norm,
            "mode": _normalize_rec_inventory_mode(mode_norm) if mode_norm else "",
            "removed_rows": removed_rows,
            "remaining": len(kept),
        }

    _write_rows([])
    try:
        p = tenant_recorder_inventory_path("nvr") if get_current_tenant_slug() else Path(NVR_INVENTORY_JSON_PATH)
        if p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        snap_dir = tenant_snapshot_dir("nvr") if get_current_tenant_slug() else NVR_SNAPSHOT_DIR
        for p in snap_dir.glob("*.jpg"):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass
    return {"ok": True, "cleared": True, "scope": "all"}

@router.post("/imgbb/upload")
def api_dvr_imgbb_upload(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    rows = _read_rows()
    if not rows:
        return {"ok": True, "uploaded": 0, "error": "Inventario NVR vazio.", "inventory": []}
    data = payload if isinstance(payload, dict) else {}
    selected = data.get("selected")
    target_rows = rows
    if isinstance(selected, list) and selected:
        keys = set()
        for it in selected:
            if not isinstance(it, dict):
                continue
            host = str(it.get("host") or "").strip()
            ch = int(it.get("channel") or 0)
            if host and ch > 0:
                keys.add((host, ch))
        if keys:
            target_rows = [r for r in rows if (str(r.get("host") or "").strip(), int(r.get("channel") or 0)) in keys]
    rows2, uploaded, err = _upload_imgbb_for_dvr_rows(target_rows)
    upd_map = {
        (str(r.get("host") or "").strip(), int(r.get("channel") or 0)): r
        for r in rows2
        if isinstance(r, dict)
    }
    merged: List[Dict[str, Any]] = []
    for r in rows:
        key = (str(r.get("host") or "").strip(), int(r.get("channel") or 0))
        merged.append(upd_map.get(key, r))
    _write_rows(merged)
    return {"ok": True, "uploaded": int(uploaded), "error": err, "inventory": merged}


def _rows_for_pdf(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        ch = int(r.get("channel") or 0)
        host = str(r.get("host") or "").strip()
        if ":" in host:
            host = host.split(":", 1)[0].strip()
        http_port = int(r.get("http_port") or 80)
        snap_file = str(r.get("snapshot_file") or "").strip()
        if not snap_file and host and ch > 0:
            snap_file = f"nvr_snapshot/{host.replace('.', '_')}_{http_port}_ch{ch:02d}.jpg"
        out.append(
            {
                "ip": str(r.get("camera_ip") or host or "").strip(),
                "host": host,
                "http_port": http_port,
                "channel": ch,
                "titulo": (f"CH {ch:02d} - " if ch > 0 else "") + str(r.get("title") or "").strip(),
                "status": str(r.get("status") or "").strip(),
                "local": str(r.get("local") or "").strip(),
                "modelo": str(r.get("camera_model") or r.get("modelo") or "").strip(),
                "mac": str(r.get("camera_mac") or r.get("mac") or "").strip(),
                "snapshot_url": str(r.get("snapshot_url") or "").strip(),
                "snapshot_file": snap_file,
            }
        )
    return out


def _load_rows_for_report(site: str = "", mode: str = "", items: str = "") -> List[Dict[str, Any]]:
    rows = legacy_rows_from_db("nvr", site=site)
    if not rows:
        rows = _read_rows()
        rows = decorate_legacy_rows("nvr", rows, site=site)
    mode_norm = _normalize_rec_inventory_mode(mode) if str(mode or "").strip() else ""
    if mode_norm:
        rows = [r for r in rows if isinstance(r, dict) and _rec_row_mode(r) == mode_norm]
    wanted: set[tuple[str, int]] = set()
    for raw in str(items or "").split(","):
        part = raw.strip()
        if not part or "|" not in part:
            continue
        host, channel_txt = part.rsplit("|", 1)
        try:
            ch = int(channel_txt)
        except Exception:
            ch = 0
        host = host.strip()
        if host and ch > 0:
            wanted.add((host, ch))
    if wanted:
        rows = [
            r for r in rows
            if (str(r.get("host") or r.get("ip") or "").strip(), int(r.get("channel") or 0)) in wanted
        ]
    return rows


@router.get("/report/settings")
def api_nvr_report_settings() -> Dict[str, Any]:
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("nvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    logo_path = tenant_report_logo_path("nvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "nvr-report-logo.png")
    return {"ok": True, "company_name": str(rep.get("company_name") or "").strip(), "has_logo": bool(logo_path.exists())}


@router.post("/report/settings")
def api_nvr_report_settings_save(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    company = str(data.get("company_name") or "").strip()
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("nvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    rep["company_name"] = company
    obj["nvr_pdf_report"] = rep
    save_app_settings(obj)
    return {"ok": True, "company_name": company}


@router.post("/report/logo")
def api_nvr_report_logo(file: bytes = File(...)) -> Dict[str, Any]:
    if not file:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    logo_path = tenant_report_logo_path("nvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "nvr-report-logo.png")
    logo_path.parent.mkdir(parents=True, exist_ok=True)
    logo_path.write_bytes(file)
    return {"ok": True, "has_logo": True}


@router.get("/report/preview.jpg")
def api_nvr_report_preview_jpg(site: str = "", company_name: str = "") -> FileResponse:
    rows = legacy_rows_from_db("nvr", site=site)
    if not rows:
        rows = _read_rows()
        rows = decorate_legacy_rows("nvr", rows, site=site)
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("nvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    company = str(company_name or rep.get("company_name") or "").strip()
    logo_path = tenant_report_logo_path("nvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "nvr-report-logo.png")
    logo = logo_path if logo_path.exists() else None
    img_path = build_inventory_preview_image(
        _rows_for_pdf(rows),
        site=site,
        company_name=company,
        logo_path=logo,
        include_olt=False,
        module_label="NVR",
    )
    return FileResponse(path=img_path, media_type="image/jpeg", filename=img_path.name, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.get("/report.pdf")
def api_nvr_report_pdf(site: str = "", company_name: str = "", mode: str = "", items: str = "") -> FileResponse:
    rows = _load_rows_for_report(site=site, mode=mode, items=items)
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("nvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    company = str(company_name or rep.get("company_name") or "").strip()
    logo_path = tenant_report_logo_path("nvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "nvr-report-logo.png")
    logo = logo_path if logo_path.exists() else None
    pdf_path = build_recorder_pdf_report(
        rows,
        site=site,
        company_name=company,
        logo_path=logo,
        recorder_type="nvr",
        module_label="Gravadores NVR",
    )
    return FileResponse(path=pdf_path, media_type="application/pdf", filename=pdf_path.name, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.post("/channel/rename")
def api_dvr_channel_rename(req: DVRRenameChannelRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    title = str(req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title obrigatorio")

    base = _base(ip, req.http_port)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao no NVR")
    mode = str(probe.get("mode") or "")

    if mode == "hik":
        ok, body, status = _hik_rename_channel(base, req.user, req.password, int(req.channel), title, req.timeout_sec)
        if not ok:
            raise HTTPException(status_code=502, detail=f"rename ISAPI falhou: status={status} body={str(body)[:240]}")
    else:
        auth = HTTPDigestAuth(req.user, req.password)
        idx0 = max(0, int(req.channel) - 1)
        q_title = quote(title, safe="")
        url = f"{base}/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[{idx0}].Name={q_title}"

        try:
            r = requests.get(url, auth=auth, timeout=req.timeout_sec)
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"falha ao renomear canal no NVR: {e}")

        body = str(r.text or "")
        if r.status_code != 200 or ("Error" in body and "OK" not in body.upper()):
            raise HTTPException(status_code=502, detail=f"rename falhou: status={r.status_code} body={body[:240]}")

    rows = _read_rows()
    updated = 0
    for row in rows:
        if (
            str(row.get("host") or "") == ip
            and int(row.get("http_port") or 80) == int(req.http_port)
            and int(row.get("channel") or 0) == int(req.channel)
        ):
            row["title"] = title
            updated += 1
    if updated:
        _write_rows(rows)

    return {
        "ok": True,
        "ip": ip,
        "http_port": int(req.http_port),
        "channel": int(req.channel),
        "title": title,
        "updated_rows": updated,
    }


@router.post("/channel/local")
def api_dvr_channel_local(req: DVRSetLocalRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    ch = int(req.channel)
    local = str(req.local or "").strip()

    rows = _read_rows()
    updated = 0
    updated_row: Dict[str, Any] | None = None
    for row in rows:
        if (
            str(row.get("host") or "") == ip
            and int(row.get("http_port") or 80) == int(req.http_port)
            and int(row.get("channel") or 0) == ch
        ):
            row["local"] = local
            updated += 1
            updated_row = row
            break
    if updated:
        _write_rows(rows)
        try:
            _auto_sync_dvr_status_to_zabbix([updated_row or {}])
        except Exception:
            pass

    return {
        "ok": True,
        "ip": ip,
        "http_port": int(req.http_port),
        "channel": ch,
        "local": local,
        "updated_rows": updated,
    }


@router.post("/local/apply")
def api_dvr_local_apply(req: DVRApplyLocalRequest) -> Dict[str, Any]:
    local = str(req.local or "").strip()
    if not local:
        raise HTTPException(status_code=400, detail="local obrigatorio")

    ip = str(req.ip or "").strip()
    port = int(req.http_port)
    rows = _read_rows()
    changed_rows: List[Dict[str, Any]] = []
    updated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if ip:
            if str(row.get("host") or "") != ip:
                continue
            if int(row.get("http_port") or 80) != port:
                continue
        if str(row.get("local") or "").strip() == local:
            continue
        row["local"] = local
        updated += 1
        changed_rows.append(row)

    if updated > 0:
        _write_rows(rows)
        try:
            _auto_sync_dvr_status_to_zabbix(changed_rows)
        except Exception:
            pass

    return {
        "ok": True,
        "updated_rows": updated,
        "local": local,
        "ip": ip,
        "http_port": port,
    }


class DVRNetworkApplyRequest(BaseModel):
    ip: str
    user: str = "admin"
    password: str
    http_port: int = Field(default=80, ge=1, le=65535)
    timeout_sec: float = Field(default=8.0, ge=1.0, le=30.0)
    new_ip: str = ""
    mask: str = ""
    gateway: str = ""
    dns1: str = ""
    dns2: str = ""
    new_tcp_port: int | None = Field(default=None, ge=1, le=65535)
    new_http_port: int | None = Field(default=None, ge=1, le=65535)
    new_rtsp_port: int | None = Field(default=None, ge=1, le=65535)


@router.get("/network")
def api_dvr_network_get(
    ip: str,
    user: str = "admin",
    password: str = "",
    http_port: int = 80,
    timeout_sec: float = 8.0,
) -> Dict[str, Any]:
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")

    base = _base(ip, http_port)
    auth = HTTPDigestAuth(user, password)
    eth0_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0", auth, timeout_sec)
    net_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network", auth, timeout_sec)
    if not eth0_txt and not net_txt:
        raise HTTPException(status_code=502, detail="NVR nao respondeu a consulta de rede (verifique usuario/senha)")

    def pick(text: str, *patterns: str) -> str:
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                val = str(m.group(1) or "").strip()
                if val:
                    return val
        return ""

    return {
        "ok": True,
        "ip": pick(eth0_txt, r"\.IPAddress\s*=\s*([^\r\n]+)") or ip,
        "mask": pick(eth0_txt, r"\.SubnetMask\s*=\s*([^\r\n]+)"),
        "gateway": pick(eth0_txt, r"\.DefaultGateway\s*=\s*([^\r\n]+)"),
        "dns1": pick(eth0_txt, r"\.DnsServers?\[0\]\s*=\s*([^\r\n]+)"),
        "dns2": pick(eth0_txt, r"\.DnsServers?\[1\]\s*=\s*([^\r\n]+)"),
        "mac": pick(eth0_txt, r"\.PhysicalAddress\s*=\s*([^\r\n]+)", r"\.MACAddress\s*=\s*([^\r\n]+)"),
        "tcp_port": pick(net_txt, r"(?<!S)TCPPort\s*=\s*([^\r\n]+)"),
        "http_port": pick(net_txt, r"(?<!S)HTTPPort\s*=\s*([^\r\n]+)"),
        "https_port": pick(net_txt, r"HTTPSPort\s*=\s*([^\r\n]+)"),
        "rtsp_port": pick(net_txt, r"RTSPPort\s*=\s*([^\r\n]+)"),
    }


@router.post("/network")
def api_dvr_network_apply(req: DVRNetworkApplyRequest) -> Dict[str, Any]:
    """Aplica config de rede completa (IP/mascara/gateway/DNS/portas), nao
    so o IP como o /change_ip antigo. Mesma tabela CGI (configManager
    Network / Network.eth0), so que expondo os campos que ja existiam no
    dispositivo mas nunca tinham tela nenhuma (portas TCP/HTTP/RTSP)."""
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")

    base = _base(ip, req.http_port)
    auth = HTTPDigestAuth(req.user, req.password)

    params: List[str] = []
    new_ip = str(req.new_ip or "").strip()
    if new_ip:
        params.append(f"Network.eth0.IPAddress={quote(new_ip, safe='')}")
    if str(req.mask or "").strip():
        params.append(f"Network.eth0.SubnetMask={quote(str(req.mask).strip(), safe='')}")
    if str(req.gateway or "").strip():
        params.append(f"Network.eth0.DefaultGateway={quote(str(req.gateway).strip(), safe='')}")
    if str(req.dns1 or "").strip():
        params.append(f"Network.eth0.DnsServers[0]={quote(str(req.dns1).strip(), safe='')}")
    if str(req.dns2 or "").strip():
        params.append(f"Network.eth0.DnsServers[1]={quote(str(req.dns2).strip(), safe='')}")
    if req.new_tcp_port:
        params.append(f"Network.TCPPort={int(req.new_tcp_port)}")
    if req.new_http_port:
        params.append(f"Network.HTTPPort={int(req.new_http_port)}")
    if req.new_rtsp_port:
        params.append(f"Network.RTSPPort={int(req.new_rtsp_port)}")

    if not params:
        raise HTTPException(status_code=400, detail="nenhum campo de rede informado para aplicar")

    q = "&".join(params)
    url = f"{base}/cgi-bin/configManager.cgi?action=setConfig&{q}"
    ok, body, status = _request_ok(url, auth, req.timeout_sec)
    if not ok:
        raise HTTPException(status_code=502, detail=f"falha ao aplicar config de rede: status={status} body={body[:240]}")

    result: Dict[str, Any] = {"ok": True, "ip": new_ip or ip}
    if new_ip and new_ip != ip:
        rows = _read_rows()
        changed = 0
        for row in rows:
            if str(row.get("host") or "") == ip:
                row["host"] = new_ip
                changed += 1
        if changed:
            _write_rows(rows)
        result["old_ip"] = ip
        result["updated_rows"] = changed
    return result


@router.post("/change_ip")
def api_dvr_change_ip(req: DVRChangeIpRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    new_ip = str(req.new_ip or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    if not new_ip:
        raise HTTPException(status_code=400, detail="new_ip obrigatorio")

    base = _base(ip, req.http_port)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao no NVR")
    mode = str(probe.get("mode") or "")
    if mode == "hik":
        ok, body, status = _hik_change_ip(
            base,
            req.user,
            req.password,
            new_ip,
            str(req.mask or "").strip(),
            str(req.gateway or "").strip(),
            str(req.dns1 or "").strip(),
            str(req.dns2 or "").strip(),
            req.timeout_sec,
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"troca de IP (ISAPI) falhou: status={status} body={str(body)[:240]}")
    else:
        auth = HTTPDigestAuth(req.user, req.password)
        params = [f"Network.eth0.IPAddress={quote(new_ip, safe='')}"]
        if str(req.mask or "").strip():
            params.append(f"Network.eth0.SubnetMask={quote(str(req.mask).strip(), safe='')}")
        if str(req.gateway or "").strip():
            params.append(f"Network.eth0.DefaultGateway={quote(str(req.gateway).strip(), safe='')}")
        if str(req.dns1 or "").strip():
            params.append(f"Network.eth0.DnsServers[0]={quote(str(req.dns1).strip(), safe='')}")
        if str(req.dns2 or "").strip():
            params.append(f"Network.eth0.DnsServers[1]={quote(str(req.dns2).strip(), safe='')}")
        q = "&".join(params)
        url = f"{_base(ip, req.http_port)}/cgi-bin/configManager.cgi?action=setConfig&{q}"
        ok, body, status = _request_ok(url, auth, req.timeout_sec)
        if not ok:
            raise HTTPException(status_code=502, detail=f"troca de IP falhou: status={status} body={body[:240]}")

    rows = _read_rows()
    changed = 0
    for row in rows:
        if str(row.get("host") or "") == ip:
            row["host"] = new_ip
            changed += 1
    if changed:
        _write_rows(rows)
    return {"ok": True, "old_ip": ip, "new_ip": new_ip, "http_port": int(req.http_port), "updated_rows": changed}


@router.post("/channel/change_ip")
def api_dvr_channel_change_ip(req: DVRCameraChangeIpRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    new_ip = str(req.new_ip or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    if not new_ip:
        raise HTTPException(status_code=400, detail="new_ip obrigatorio")

    base = _base(ip, req.http_port)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao no NVR")

    mode = str(probe.get("mode") or "")
    if mode != "hik":
        raise HTTPException(status_code=400, detail="troca de IP de camera disponivel apenas para NVR Hikvision")

    ok, body, status = _hik_change_camera_ip(base, req.user, req.password, int(req.channel), new_ip, req.timeout_sec)
    if not ok:
        raise HTTPException(status_code=502, detail=f"troca de IP da camera falhou: status={status} body={str(body)[:240]}")

    rows = _read_rows()
    changed = 0
    for row in rows:
        if (
            str(row.get("host") or "") == ip
            and int(row.get("http_port") or 80) == int(req.http_port)
            and int(row.get("channel") or 0) == int(req.channel)
        ):
            row["camera_ip"] = new_ip
            changed += 1
    if changed:
        _write_rows(rows)

    return {
        "ok": True,
        "ip": ip,
        "http_port": int(req.http_port),
        "channel": int(req.channel),
        "new_ip": new_ip,
        "updated_rows": changed,
    }


@router.post("/ntp")
def api_dvr_set_ntp(req: DVRSetNtpRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    if not str(req.address or "").strip():
        raise HTTPException(status_code=400, detail="address obrigatorio")

    base = _base(ip, req.http_port)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao no NVR")
    mode = str(probe.get("mode") or "")
    if mode == "hik":
        ok, body, status = _hik_set_ntp(
            base,
            req.user,
            req.password,
            str(req.address).strip(),
            int(req.port),
            int(req.timezone),
            int(req.update_period),
            req.timeout_sec,
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"NTP (ISAPI) falhou: status={status} body={str(body)[:240]}")
    else:
        auth = HTTPDigestAuth(req.user, req.password)
        q = "&".join(
            [
                "NTP.Enable=true",
                f"NTP.Address={quote(str(req.address).strip(), safe='')}",
                f"NTP.Port={int(req.port)}",
                f"NTP.TimeZone={int(req.timezone)}",
                f"NTP.UpdatePeriod={int(req.update_period)}",
            ]
        )
        url = f"{_base(ip, req.http_port)}/cgi-bin/configManager.cgi?action=setConfig&{q}"
        ok, body, status = _request_ok(url, auth, req.timeout_sec)
        if not ok:
            raise HTTPException(status_code=502, detail=f"NTP falhou: status={status} body={body[:240]}")
    return {"ok": True, "ip": ip, "http_port": int(req.http_port), "address": str(req.address), "port": int(req.port)}


@router.post("/reboot")
def api_dvr_reboot(req: DVRRebootRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    base = _base(ip, req.http_port)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao no NVR")
    mode = str(probe.get("mode") or "")
    if mode == "hik":
        ok, body, status = _request_any(
            "PUT",
            f"{base}/ISAPI/System/reboot",
            req.user,
            req.password,
            req.timeout_sec,
            data="",
            headers={"Content-Type": "application/xml"},
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"reboot ISAPI falhou: status={status} body={str(body)[:240]}")
    else:
        auth = HTTPDigestAuth(req.user, req.password)
        url = f"{_base(ip, req.http_port)}/cgi-bin/magicBox.cgi?action=reboot"
        ok, body, status = _request_ok(url, auth, req.timeout_sec)
        if not ok:
            raise HTTPException(status_code=502, detail=f"reboot falhou: status={status} body={body[:240]}")
    return {"ok": True, "ip": ip}


@router.post("/scan")
def api_dvr_scan(req: DVRScanRequest) -> Dict[str, Any]:
    # Seta o conector do request pra o _base virtualizar o probe (o gravador
    # ainda nao esta no inventario durante a varredura). Reset garantido -- o
    # endpoint sync roda em threadpool e a contextvar sobreviveria a request.
    _tok = _rec_req_connector.set(str(getattr(req, "connector_id", "") or getattr(req, "remote_connector_id", "") or "").strip())
    try:
        return _api_dvr_scan_impl(req)
    finally:
        _rec_req_connector.reset(_tok)


def _api_dvr_scan_impl(req: DVRScanRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    if req.end_channel < req.start_channel:
        raise HTTPException(status_code=400, detail="end_channel deve ser >= start_channel")
    local_default = str(req.local or "").strip()
    connector = _recorder_scan_connector(req)

    base = _base(ip, req.http_port)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao ou NVR sem resposta")

    auth = probe.get("auth")
    mode = str(probe.get("mode") or "")
    if mode == "hik":
        meta = _parse_hik_device_info(str(probe.get("device_xml") or ""))
        mac = _parse_hik_mac(str(probe.get("network_xml") or ""))
        titles = _parse_hik_channels(str(probe.get("channels_xml") or ""))
        ch_macs = _parse_hik_channel_macs(str(probe.get("channels_xml") or ""))
        ch_ips = _parse_hik_channel_ips(str(probe.get("channels_xml") or ""))
        ch_models = _parse_hik_channel_models(str(probe.get("channels_xml") or ""))
        video_loss: set[int] = set()
    else:
        meta = _parse_sysinfo(str(probe.get("sysinfo_txt") or ""), str(probe.get("devtype_txt") or ""))
        mac = _parse_mac(str(probe.get("network_txt") or ""))
        titles = _parse_titles(str(probe.get("channel_txt") or ""))
        ch_macs = _parse_dahua_channel_macs(str(probe.get("remote_txt") or ""))
        ch_ips = _parse_dahua_channel_ips(str(probe.get("remote_txt") or ""))
        ch_models = _parse_dahua_channel_models(str(probe.get("remote_txt") or ""))
        video_loss = _video_loss_channels(base, auth, req.timeout_sec)

    # fallback por ARP usando IP da camera por canal
    mac_cache: Dict[str, str] = {}
    for ch, cip in list(ch_ips.items()):
        if ch_macs.get(ch):
            continue
        ipn = _norm_ip_text(cip)
        if not ipn:
            continue
        reach = _reach_camera(ipn, ip)
        # Hikvision: tenta ler MAC direto da camera via ISAPI com as mesmas credenciais.
        if mode == "hik":
            cmac = _hik_fetch_camera_mac(reach, req.user, req.password, req.timeout_sec)
            if cmac:
                ch_macs[ch] = cmac
                continue
        if ipn not in mac_cache:
            mac_cache[ipn] = _arp_lookup_mac(reach)
        if mac_cache[ipn]:
            ch_macs[ch] = mac_cache[ipn]

    start = max(1, int(req.start_channel))
    end = max(start, int(req.end_channel))
    hik_runtime: Dict[int, Dict[str, Any]] = {}
    if mode == "hik":
        hik_runtime = _hik_collect_channel_runtime_statuses(base, auth, req.timeout_sec, range(start, end + 1))
    recorder_health = _collect_nvr_health(
        base,
        auth,
        mode,
        req.timeout_sec,
        range(start, end + 1),
        network_payload=str(probe.get("network_xml") or probe.get("network_txt") or ""),
    )
    recording_by_channel: Dict[int, Dict[str, Any]] = recorder_health.get("recording_by_channel") or {}
    recorder_common_health = {
        k: v
        for k, v in recorder_health.items()
        if k != "recording_by_channel" and v not in ("", None, [], {})
    }
    ip_inventory_online = _load_online_ip_inventory_map()

    old = _read_rows()
    old_local_map: Dict[int, str] = {}
    for r in old:
        if str(r.get("host") or "") == ip and int(r.get("http_port") or 80) == int(req.http_port):
            ch_old = int(r.get("channel") or 0)
            if ch_old > 0:
                old_local_map[ch_old] = str(r.get("local") or "").strip()

    rows_new: List[Dict[str, Any]] = []
    online = 0
    for ch in range(start, end + 1):
        fname = f"{ip.replace('.', '_')}_{int(req.http_port)}_ch{ch:02d}.jpg"
        snap_url, snap_dark = _snapshot_for_channel(base, auth, req.timeout_sec, ch, fname)
        title = titles.get(ch) or f"Canal {ch}"
        title_norm = (title or "").strip().lower()
        is_default_title = bool(re.fullmatch(r"canal\s*\d*", title_norm))
        runtime_online = hik_runtime.get(ch, {}).get("online")
        camera_ip_norm = _norm_ip_text(ch_ips.get(ch) or "")
        ip_inventory_online_hit = bool(camera_ip_norm and ip_inventory_online.get(camera_ip_norm))
        has_camera_identity = bool(camera_ip_norm or ch_macs.get(ch) or ch_models.get(ch))

        if runtime_online is True:
            status = "online"
        elif ip_inventory_online_hit:
            status = "online"
        elif snap_url and not snap_dark:
            status = "online"
        elif ch in video_loss or snap_dark:
            status = "sem_camera" if is_default_title else "camera_offline"
        elif not has_camera_identity and is_default_title:
            status = "sem_camera"
        else:
            status = "offline"
        if status == "online":
            online += 1
        row_local = local_default if bool(req.set_local) else old_local_map.get(ch, "")
        recording_info = recording_by_channel.get(ch) or {}
        row = {
                "source": "nvr",
                "host": ip,
                "http_port": int(req.http_port),
                "channel": ch,
                "ip": f"CH {ch:02d}",
                "mac": str(ch_macs.get(ch) or "").strip(),
                "camera_mac": str(ch_macs.get(ch) or "").strip(),
                "mac_camera": str(ch_macs.get(ch) or "").strip(),
                "nvr_mac": mac,
                "nvr_model": str(meta.get("modelo") or "").strip(),
                "fabricante": meta.get("fabricante") or "DVR/NVR",
                "modelo": str(ch_models.get(ch) or "").strip(),
                "camera_model": str(ch_models.get(ch) or "").strip(),
                "equip_serial": meta.get("equip_serial") or "",
                "title": title,
                "local": row_local,
                "status": status,
                "video_loss": (ch in video_loss),
                "snapshot_dark": bool(snap_dark),
                "snapshot_url": snap_url,
                "snapshot_file": (f"nvr_snapshot/{Path(snap_url).name}" if snap_url else ""),
                "camera_ip": str(ch_ips.get(ch) or "").strip(),
            }
        row.update(recorder_common_health)
        if recording_info:
            row["recording"] = recording_info.get("recording")
            row["is_recording"] = recording_info.get("recording")
            row["recording_status"] = str(recording_info.get("recording_status") or "").strip()
        else:
            row["recording"] = False if status != "online" else None
            row["is_recording"] = False if status != "online" else None
            row["recording_status"] = "sem camera ou offline" if status != "online" else "nao confirmado pelo playback"
        rows_new.append(row)

    # Nunca substitui o inventario atual por vazio em um update/scan com falha parcial.
    if not rows_new:
        raise HTTPException(
            status_code=502,
            detail="leitura do NVR nao retornou canais; inventario atual preservado",
        )

    rows_new = _tag_recorder_rows(rows_new, req, connector)
    keep = [r for r in old if not _same_recorder_scope(r, req)]
    merged = keep + rows_new
    imgbb_uploaded = 0
    imgbb_error = ""
    if bool(req.imgbb):
        merged, imgbb_uploaded, imgbb_error = _upload_imgbb_for_dvr_rows(merged)
    _write_rows(merged)
    returned_rows = [
        r for r in merged
        if (str(r.get("host") or "") == ip and int(r.get("http_port") or 80) == int(req.http_port))
    ]
    returned_rows.sort(key=lambda r: int(r.get("channel") or 0))
    zbx_ok, zbx_err = _auto_sync_dvr_status_to_zabbix(returned_rows)

    return {
        "ok": True,
        "host": ip,
        "http_port": int(req.http_port),
        "channels_total": len(rows_new),
        "online_channels": online,
        "offline_channels": max(0, len(rows_new) - online),
        "inventory_path": str(NVR_INVENTORY_JSON_PATH),
        "inventory": returned_rows,
        "imgbb_uploaded": imgbb_uploaded,
        "imgbb_error": imgbb_error,
        "zabbix_sync_ok": bool(zbx_ok),
        "zabbix_sync_error": zbx_err,
    }


@router.post("/snapshot/update")
def api_dvr_snapshot_update(req: DVRSnapshotUpdateRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")

    base = _base(ip, req.http_port)
    ch = int(req.channel)
    probe = _probe_nvr_stack(base, req.user, req.password, req.timeout_sec)
    if not bool(probe.get("ok")):
        raise HTTPException(status_code=401, detail="falha de autenticacao ou NVR sem resposta")

    auth = probe.get("auth")
    mode = str(probe.get("mode") or "")
    if mode == "hik":
        meta = _parse_hik_device_info(str(probe.get("device_xml") or ""))
        mac = _parse_hik_mac(str(probe.get("network_xml") or ""))
        titles = _parse_hik_channels(str(probe.get("channels_xml") or ""))
        ch_macs = _parse_hik_channel_macs(str(probe.get("channels_xml") or ""))
        ch_ips = _parse_hik_channel_ips(str(probe.get("channels_xml") or ""))
        ch_models = _parse_hik_channel_models(str(probe.get("channels_xml") or ""))
        video_loss: set[int] = set()
    else:
        meta = _parse_sysinfo(str(probe.get("sysinfo_txt") or ""), str(probe.get("devtype_txt") or ""))
        mac = _parse_mac(str(probe.get("network_txt") or ""))
        titles = _parse_titles(str(probe.get("channel_txt") or ""))
        ch_macs = _parse_dahua_channel_macs(str(probe.get("remote_txt") or ""))
        ch_ips = _parse_dahua_channel_ips(str(probe.get("remote_txt") or ""))
        ch_models = _parse_dahua_channel_models(str(probe.get("remote_txt") or ""))
        video_loss = _video_loss_channels(base, auth, req.timeout_sec)
    hik_runtime: Dict[int, Dict[str, Any]] = {}
    if mode == "hik":
        hik_runtime = _hik_collect_channel_runtime_statuses(base, auth, req.timeout_sec, range(ch, ch + 1))
    ip_inventory_online = _load_online_ip_inventory_map()

    if not ch_macs.get(ch):
        cip = _norm_ip_text(ch_ips.get(ch) or "")
        if cip:
            reach = _reach_camera(cip, ip)
            ch_macs[ch] = _hik_fetch_camera_mac(reach, req.user, req.password, req.timeout_sec) or _arp_lookup_mac(reach)

    fname = f"{ip.replace('.', '_')}_{int(req.http_port)}_ch{ch:02d}.jpg"
    snap_url, snap_dark = _snapshot_for_channel(base, auth, req.timeout_sec, ch, fname)
    title = titles.get(ch) or f"Canal {ch}"
    title_norm = (title or "").strip().lower()
    is_default_title = bool(re.fullmatch(r"canal\s*\d*", title_norm))
    runtime_online = hik_runtime.get(ch, {}).get("online")
    camera_ip_norm = _norm_ip_text(ch_ips.get(ch) or "")
    ip_inventory_online_hit = bool(camera_ip_norm and ip_inventory_online.get(camera_ip_norm))

    if runtime_online is True:
        status = "online"
    elif ip_inventory_online_hit:
        status = "online"
    elif snap_url and not snap_dark:
        status = "online"
    elif ch in video_loss or snap_dark:
        status = "sem_camera" if is_default_title else "camera_offline"
    else:
        status = "offline"

    row = {
        "source": "nvr",
        "host": ip,
        "http_port": int(req.http_port),
        "channel": ch,
        "ip": f"CH {ch:02d}",
        "mac": str(ch_macs.get(ch) or "").strip(),
        "camera_mac": str(ch_macs.get(ch) or "").strip(),
        "mac_camera": str(ch_macs.get(ch) or "").strip(),
        "nvr_mac": mac,
        "nvr_model": str(meta.get("modelo") or "").strip(),
        "fabricante": meta.get("fabricante") or "DVR/NVR",
        "modelo": str(ch_models.get(ch) or "").strip(),
        "camera_model": str(ch_models.get(ch) or "").strip(),
        "equip_serial": meta.get("equip_serial") or "",
        "title": title,
        "status": status,
        "video_loss": (ch in video_loss),
        "snapshot_dark": bool(snap_dark),
        "snapshot_url": snap_url,
        "snapshot_file": (f"nvr_snapshot/{Path(snap_url).name}" if snap_url else ""),
        "camera_ip": str(ch_ips.get(ch) or "").strip(),
    }

    imgbb_uploaded = 0
    imgbb_error = ""
    if bool(req.imgbb):
        upd_rows, imgbb_uploaded, imgbb_error = _upload_imgbb_for_dvr_rows([row])
        row = (upd_rows or [row])[0]
        if imgbb_error:
            row["imgbb_error"] = imgbb_error

    old = _read_rows()
    keep = [
        r for r in old
        if not (
            str(r.get("host") or "") == ip
            and int(r.get("http_port") or 80) == int(req.http_port)
            and int(r.get("channel") or 0) == ch
        )
    ]
    keep.append(row)
    keep.sort(key=lambda r: (str(r.get("host") or ""), int(r.get("http_port") or 80), int(r.get("channel") or 0)))
    _write_rows(keep)
    zbx_ok, zbx_err = _auto_sync_dvr_status_to_zabbix([row])
    return {
        "ok": True,
        "updated": True,
        "row": row,
        "imgbb_requested": bool(req.imgbb),
        "imgbb_uploaded": int(imgbb_uploaded),
        "imgbb_error": imgbb_error,
        "zabbix_sync_ok": bool(zbx_ok),
        "zabbix_sync_error": zbx_err,
    }
