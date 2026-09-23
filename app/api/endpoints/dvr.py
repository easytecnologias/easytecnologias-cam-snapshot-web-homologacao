from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any, Dict, List

import requests
from fastapi import APIRouter, HTTPException, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from requests.auth import HTTPDigestAuth

from app.core.paths import BASE_DIR, DVR_INVENTORY_JSON_PATH, DVR_SNAPSHOT_DIR, SAIDA_DIR
from app.core.paths import DATA_DIR
from app.core.tenant_context import get_current_tenant_slug, tenant_recorder_inventory_path, tenant_report_logo_path, tenant_scoped_path, tenant_snapshot_dir
from app.services.camsnapshot.uploader_imgbb import upload_to_imgbb
from app.services.db_store import decorate_legacy_rows
from app.services.db_store import replace_recorder_inventory_rows
from app.services.db_store import legacy_rows_from_db
from app.services.db_store import load_app_settings, save_app_settings
from app.services.pdf_inventory_report import build_inventory_pdf_report, build_inventory_preview_image
from app.services.recorder_pdf_report import build_recorder_pdf_report
from app.services.ws_scan_service import (
    _connector_from_payload,
    _connector_has_tunnel,
    _decide_remote_only,
    _pick_probe_targets,
)

router = APIRouter(tags=["dvr"], prefix="/api/dvr")
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



import contextvars as _contextvars
from app.services import connector_routing_vnat as _vnat

# Conector do request atual (setado pela varredura) -- pra virtualizar o alvo
# antes do gravador estar no inventario. Ver nota igual no nvr.py.
_rec_req_connector: "_contextvars.ContextVar[str]" = _contextvars.ContextVar("dvr_req_connector", default="")


def _recorder_connector_for_host(ip: str) -> str:
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
    ip = str(ip or "").strip()
    if not ip:
        return ip
    cid = _rec_req_connector.get() or _recorder_connector_for_host(ip)
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
        p = tenant_recorder_inventory_path("dvr")
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    try:
        db_rows = legacy_rows_from_db("dvr")
        if db_rows:
            return db_rows
    except Exception:
        pass
    p = Path(DVR_INVENTORY_JSON_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_rows(rows: List[Dict[str, Any]]) -> None:
    if get_current_tenant_slug():
        p = tenant_recorder_inventory_path("dvr")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    try:
        replace_recorder_inventory_rows("dvr", rows)
        return
    except Exception:
        pass
    # fallback legado
    p = Path(DVR_INVENTORY_JSON_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_text(url: str, auth: HTTPDigestAuth, timeout: float) -> str:
    r = requests.get(url, auth=auth, timeout=timeout)
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
                "source": "dvr",
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
        return False, "credenciais Zabbix DVR não configuradas"

    z_rows = _build_zabbix_rows_for_dvr(rows)
    if not z_rows:
        return False, "sem linhas DVR para sincronizar"

    tmp_inv = tenant_scoped_path("tmp/zabbix-source-inventory.auto-dvr.json", tenant_slug)
    try:
        tmp_inv.parent.mkdir(parents=True, exist_ok=True)
        tmp_inv.write_text(json.dumps(z_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return False, f"falha ao preparar inventário temporário: {e}"

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
            f"ImgBB parcial: {len(by_file)}/{total_candidates} uploads concluídos. "
            "Verifique limite/rate da API key."
        )
    _imgbb_progress_finish(f"ImgBB finalizado: {len(by_file)}/{total_candidates} uploads concluídos.")
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
            # Em muitos DVRs Dahua/Intelbras esse índice vem base-0.
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
        # Sem sinal costuma vir quase preto, com baixíssima variação.
        return bool(mean < 24 and std < 10 and dark_ratio > 0.94)
    except Exception:
        return False


def _snapshot_for_channel(base: str, auth: HTTPDigestAuth, timeout: float, channel: int, out_name: str) -> tuple[str, bool]:
    snap_dir = tenant_snapshot_dir("dvr") if get_current_tenant_slug() else DVR_SNAPSHOT_DIR
    snap_dir.mkdir(parents=True, exist_ok=True)
    url = f"{base}/cgi-bin/snapshot.cgi?channel={int(channel)}"
    try:
        # Timeout separado (connect/read) evita travar handshake + stream.
        r = requests.get(url, auth=auth, timeout=(2.0, float(timeout)), stream=True)
        ctype = str(r.headers.get("Content-Type") or "").lower()
        if r.status_code != 200 or "image" not in ctype:
            return "", False
        out_file = snap_dir / out_name
        with out_file.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return f"/data/dvr_snapshot/{out_file.name}", _snapshot_is_dark(out_file)
    except requests.RequestException:
        return "", False
    except Exception:
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

    rows = legacy_rows_from_db("dvr", site=site)
    if not rows:
        rows = _read_rows()
        rows = decorate_legacy_rows("dvr", rows, site=site)
    return {"ok": True, "inventory": rows}


@router.post("/save")
def api_dvr_save(req: RecorderSaveRequest) -> Dict[str, Any]:
    rows = _read_rows()
    updates: Dict[tuple[str, str, int], Dict[str, Any]] = {}
    for item in req.recorders or []:
        host = str(item.get("host") or item.get("ip") or "").strip()
        try:
            channel = int(item.get("channel") or 0)
        except Exception:
            channel = 0
        if host and channel > 0:
            connector_id = str(item.get("remote_connector_id") or item.get("connector_id") or "").strip()
            updates[(connector_id, host, channel)] = item

    if not updates:
        raise HTTPException(status_code=400, detail="Nenhum canal informado para salvar.")

    editable_fields = {
        "title", "local", "status", "mac", "modelo", "model", "equip_serial",
        "pon", "onu_id", "onu_name", "onu_serial", "switch_ip", "switch_port",
        "switch_vlan", "video_loss", "snapshot_url", "imgbb_url", "imgbb_thumb_url",
        "site", "remote", "remote_connector_id", "recorder_user", "http_port", "name", "recorder_name", "inventory_mode",
    }
    updated = 0
    found: set[tuple[str, str, int]] = set()
    for row in rows:
        host = str(row.get("host") or row.get("ip") or "").strip()
        try:
            channel = int(row.get("channel") or 0)
        except Exception:
            channel = 0
        connector_id = str(row.get("remote_connector_id") or row.get("connector_id") or "").strip()
        key = (connector_id, host, channel)
        item = updates.get(key)
        if not item:
            continue
        found.add(key)
        before = dict(row)
        for field in editable_fields:
            if field in item:
                row[field] = item.get(field)
        if "camera_mac" in item and "mac" not in item:
            row["mac"] = item.get("camera_mac")
        if "camera_model" in item and "modelo" not in item:
            row["modelo"] = item.get("camera_model")
        if row != before:
            updated += 1

    inserted = 0
    for key, item in updates.items():
        if key in found:
            continue
        new_row = {field: item.get(field, "") for field in editable_fields if field in item}
        new_row["host"] = key[1]
        new_row["channel"] = key[2]
        new_row.setdefault("title", str(item.get("title") or f"Canal {key[2]:02d}").strip())
        new_row.setdefault("status", str(item.get("status") or "online").strip())
        rows.append(new_row)
        inserted += 1

    _write_rows(rows)
    return {"ok": True, "updated": updated, "inserted": inserted, "total": len(rows)}


@router.post("/delete")
def api_dvr_delete(req: RecorderDeleteRequest) -> Dict[str, Any]:
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
        p = tenant_recorder_inventory_path("dvr") if get_current_tenant_slug() else Path(DVR_INVENTORY_JSON_PATH)
        if p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        snap_dir = tenant_snapshot_dir("dvr") if get_current_tenant_slug() else DVR_SNAPSHOT_DIR
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
        return {"ok": True, "uploaded": 0, "error": "Inventario DVR vazio.", "inventory": []}
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
            snap_file = f"dvr_snapshot/{host.replace('.', '_')}_{http_port}_ch{ch:02d}.jpg"
        out.append(
            {
                "ip": host,
                "host": host,
                "http_port": http_port,
                "channel": ch,
                "titulo": (f"CH {ch:02d} - " if ch > 0 else "") + str(r.get("title") or "").strip(),
                "status": str(r.get("status") or "").strip(),
                "local": str(r.get("local") or "").strip(),
                "modelo": str(r.get("modelo") or "").strip(),
                "mac": str(r.get("mac") or "").strip(),
                "snapshot_url": str(r.get("snapshot_url") or "").strip(),
                "snapshot_file": snap_file,
            }
        )
    return out


def _load_rows_for_report(site: str = "", mode: str = "", items: str = "") -> List[Dict[str, Any]]:
    rows = legacy_rows_from_db("dvr", site=site)
    if not rows:
        rows = _read_rows()
        rows = decorate_legacy_rows("dvr", rows, site=site)
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
def api_dvr_report_settings() -> Dict[str, Any]:
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("dvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    logo_path = tenant_report_logo_path("dvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "dvr-report-logo.png")
    return {"ok": True, "company_name": str(rep.get("company_name") or "").strip(), "has_logo": bool(logo_path.exists())}


@router.post("/report/settings")
def api_dvr_report_settings_save(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    company = str(data.get("company_name") or "").strip()
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("dvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    rep["company_name"] = company
    obj["dvr_pdf_report"] = rep
    save_app_settings(obj)
    return {"ok": True, "company_name": company}


@router.post("/report/logo")
def api_dvr_report_logo(file: bytes = File(...)) -> Dict[str, Any]:
    if not file:
        raise HTTPException(status_code=400, detail="Arquivo vazio.")
    logo_path = tenant_report_logo_path("dvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "dvr-report-logo.png")
    logo_path.parent.mkdir(parents=True, exist_ok=True)
    logo_path.write_bytes(file)
    return {"ok": True, "has_logo": True}


@router.get("/report/preview.jpg")
def api_dvr_report_preview_jpg(site: str = "", company_name: str = "") -> FileResponse:
    rows = legacy_rows_from_db("dvr", site=site)
    if not rows:
        rows = _read_rows()
        rows = decorate_legacy_rows("dvr", rows, site=site)
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("dvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    company = str(company_name or rep.get("company_name") or "").strip()
    logo_path = tenant_report_logo_path("dvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "dvr-report-logo.png")
    logo = logo_path if logo_path.exists() else None
    img_path = build_inventory_preview_image(
        _rows_for_pdf(rows),
        site=site,
        company_name=company,
        logo_path=logo,
        include_olt=False,
        module_label="DVR",
    )
    return FileResponse(path=img_path, media_type="image/jpeg", filename=img_path.name, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.get("/report.pdf")
def api_dvr_report_pdf(site: str = "", company_name: str = "", mode: str = "", items: str = "") -> FileResponse:
    rows = _load_rows_for_report(site=site, mode=mode, items=items)
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("dvr_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    company = str(company_name or rep.get("company_name") or "").strip()
    logo_path = tenant_report_logo_path("dvr") if get_current_tenant_slug() else (DATA_DIR / "input" / "dvr-report-logo.png")
    logo = logo_path if logo_path.exists() else None
    pdf_path = build_recorder_pdf_report(
        rows,
        site=site,
        company_name=company,
        logo_path=logo,
        recorder_type="dvr",
        module_label="Gravadores DVR",
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
    auth = HTTPDigestAuth(req.user, req.password)
    idx0 = max(0, int(req.channel) - 1)
    q_title = quote(title, safe="")
    url = f"{base}/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[{idx0}].Name={q_title}"

    try:
        r = requests.get(url, auth=auth, timeout=req.timeout_sec)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"falha ao renomear canal no DVR: {e}")

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


@router.post("/ntp")
def api_dvr_set_ntp(req: DVRSetNtpRequest) -> Dict[str, Any]:
    ip = req.ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    if not str(req.address or "").strip():
        raise HTTPException(status_code=400, detail="address obrigatorio")

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
    auth = HTTPDigestAuth(req.user, req.password)
    url = f"{_base(ip, req.http_port)}/cgi-bin/magicBox.cgi?action=reboot"
    ok, body, status = _request_ok(url, auth, req.timeout_sec)
    if not ok:
        raise HTTPException(status_code=502, detail=f"reboot falhou: status={status} body={body[:240]}")
    return {"ok": True, "ip": ip}


@router.post("/scan")
def api_dvr_scan(req: DVRScanRequest) -> Dict[str, Any]:
    # Conector do request -> _base virtualiza o probe (gravador ainda nao esta
    # no inventario na varredura). Reset garantido (endpoint sync em threadpool).
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

    auth = HTTPDigestAuth(req.user, req.password)
    base = _base(ip, req.http_port)

    try:
        sysinfo_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getSystemInfo", auth, req.timeout_sec)
        devtype_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getDeviceType", auth, req.timeout_sec)
        network_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0", auth, req.timeout_sec)
        channel_txt = _get_text(
            f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle", auth, req.timeout_sec
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"falha ao conectar no DVR: {e}")

    if not channel_txt and not sysinfo_txt:
        raise HTTPException(status_code=401, detail="falha de autenticacao ou DVR sem resposta")

    meta = _parse_sysinfo(sysinfo_txt, devtype_txt)
    mac = _parse_mac(network_txt)
    titles = _parse_titles(channel_txt)
    video_loss = _video_loss_channels(base, auth, req.timeout_sec)

    if titles:
        max_ch = max(titles.keys())
    else:
        max_ch = req.end_channel
    start = max(1, int(req.start_channel))
    end = min(int(req.end_channel), int(max_ch))

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

        if snap_url and not snap_dark:
            status = "online"
        elif ch in video_loss or snap_dark:
            status = "sem_camera" if is_default_title else "camera_offline"
        else:
            status = "offline"
        if status == "online":
            online += 1
        row_local = local_default if bool(req.set_local) else old_local_map.get(ch, "")
        rows_new.append(
            {
                "source": "dvr",
                "host": ip,
                "http_port": int(req.http_port),
                "channel": ch,
                "ip": f"CH {ch:02d}",
                "mac": mac,
                "fabricante": meta.get("fabricante") or "DVR/NVR",
                "modelo": meta.get("modelo") or "",
                "equip_serial": meta.get("equip_serial") or "",
                "title": title,
                "local": row_local,
                "status": status,
                "video_loss": (ch in video_loss),
                "snapshot_dark": bool(snap_dark),
                "snapshot_url": snap_url,
                "snapshot_file": (f"dvr_snapshot/{Path(snap_url).name}" if snap_url else ""),
            }
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
        "inventory_path": str(DVR_INVENTORY_JSON_PATH),
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

    auth = HTTPDigestAuth(req.user, req.password)
    base = _base(ip, req.http_port)
    ch = int(req.channel)

    try:
        sysinfo_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getSystemInfo", auth, req.timeout_sec)
        devtype_txt = _get_text(f"{base}/cgi-bin/magicBox.cgi?action=getDeviceType", auth, req.timeout_sec)
        network_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0", auth, req.timeout_sec)
        channel_txt = _get_text(
            f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle", auth, req.timeout_sec
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"falha ao conectar no DVR: {e}")

    if not channel_txt and not sysinfo_txt:
        raise HTTPException(status_code=401, detail="falha de autenticacao ou DVR sem resposta")

    meta = _parse_sysinfo(sysinfo_txt, devtype_txt)
    mac = _parse_mac(network_txt)
    titles = _parse_titles(channel_txt)
    video_loss = _video_loss_channels(base, auth, req.timeout_sec)

    fname = f"{ip.replace('.', '_')}_{int(req.http_port)}_ch{ch:02d}.jpg"
    snap_url, snap_dark = _snapshot_for_channel(base, auth, req.timeout_sec, ch, fname)
    title = titles.get(ch) or f"Canal {ch}"
    title_norm = (title or "").strip().lower()
    is_default_title = bool(re.fullmatch(r"canal\s*\d*", title_norm))

    if snap_url and not snap_dark:
        status = "online"
    elif ch in video_loss or snap_dark:
        status = "sem_camera" if is_default_title else "camera_offline"
    else:
        status = "offline"

    row = {
        "source": "dvr",
        "host": ip,
        "http_port": int(req.http_port),
        "channel": ch,
        "ip": f"CH {ch:02d}",
        "mac": mac,
        "fabricante": meta.get("fabricante") or "DVR/NVR",
        "modelo": meta.get("modelo") or "",
        "equip_serial": meta.get("equip_serial") or "",
        "title": title,
        "status": status,
        "video_loss": (ch in video_loss),
        "snapshot_dark": bool(snap_dark),
        "snapshot_url": snap_url,
        "snapshot_file": (f"dvr_snapshot/{Path(snap_url).name}" if snap_url else ""),
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
