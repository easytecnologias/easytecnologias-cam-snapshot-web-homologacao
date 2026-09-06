from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

from app.core.paths import DVR_INVENTORY_JSON_PATH, NVR_INVENTORY_JSON_PATH
from app.core.tenant_context import get_current_tenant_slug, tenant_recorder_inventory_path
from app.services.db_store import decorate_legacy_rows, legacy_rows_from_db

def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_slug(value: Any) -> str:
    raw = _safe_text(value).lower()
    raw = re.sub(r"[^a-z0-9_.-]+", "_", raw)
    return raw.strip("_") or "item"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "null")
    except Exception:
        pass
    return default


def _parse_dt(value: Any) -> datetime:
    raw = _safe_text(value)
    if not raw:
        raise ValueError("data/hora obrigatoria")
    raw = raw.replace("T", " ")
    if len(raw) == 16:
        raw += ":00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("formato de data invalido; use YYYY-MM-DD HH:MM:SS")


def _read_recorder_inventory(source: str) -> List[Dict[str, Any]]:
    """Le o inventario de gravadores do tenant atual.

    Quando ha tenant no contexto, le SO o arquivo tenant-scoped e nunca cai no
    fallback do banco global (`legacy_rows_from_db`) -- essa tabela nao tem
    coluna de tenant, entao qualquer leitura dela (mesmo so pra "enriquecer"
    com nome de site) podia vazar dado de outro cliente. Tenant sem gravador
    proprio cadastrado ve lista vazia, nao a lista de outro tenant.

    O fallback pro banco global so roda quando NAO ha tenant no contexto --
    mantido para o deployment single-tenant/legado sem essa camada de auth.
    """
    tenant_slug = get_current_tenant_slug()
    if tenant_slug:
        tenant_path = tenant_recorder_inventory_path(source)
        obj = _load_json(tenant_path, [])
        rows = [r for r in obj if isinstance(r, dict)] if isinstance(obj, list) else []
        return rows

    try:
        rows = legacy_rows_from_db(source)
    except Exception:
        rows = []
    if not rows:
        fallback_path = NVR_INVENTORY_JSON_PATH if source == "nvr" else DVR_INVENTORY_JSON_PATH
        try:
            obj = _load_json(Path(fallback_path), [])
            rows = obj if isinstance(obj, list) else []
        except Exception:
            rows = []
    try:
        rows = decorate_legacy_rows(source, rows)
    except Exception:
        pass
    return [r for r in rows if isinstance(r, dict)]


def _read_nvr_inventory() -> List[Dict[str, Any]]:
    """Le o inventario de gravadores NVR e DVR juntos -- para a busca por IA,
    ambos sao apenas "gravador com canal", nao ha motivo pra distinguir."""
    seen: set[tuple[str, int, int]] = set()
    rows: List[Dict[str, Any]] = []
    for source in ("nvr", "dvr"):
        for row in _read_recorder_inventory(source):
            host = _safe_text(row.get("host") or row.get("ip"))
            ch = int(row.get("channel") or 0)
            port = int(row.get("http_port") or 80)
            if host and ch > 0:
                key = (host, port, ch)
                if key in seen:
                    continue
                seen.add(key)
            row.setdefault("recorder_source", source)
            rows.append(row)
    return rows


def list_nvr_targets() -> Dict[str, Any]:
    rows = _read_nvr_inventory()
    targets: List[Dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for r in rows:
        host = _safe_text(r.get("host") or r.get("ip"))
        ch = int(r.get("channel") or 0)
        port = int(r.get("http_port") or 80)
        site = _safe_text(r.get("site") or r.get("site_name") or r.get("local"))
        local = _safe_text(r.get("local"))
        if not host or ch <= 0:
            continue
        key = (host, port, ch)
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            {
                "host": host,
                "http_port": port,
                "channel": ch,
                "title": _safe_text(r.get("title") or r.get("titulo") or f"Canal {ch:02d}"),
                "recorder_name": _safe_text(r.get("recorder_name")),
                "site": site,
                "local": local or site,
                "modelo": _safe_text(r.get("modelo") or r.get("recorder_model") or r.get("camera_model")),
                "fabricante": _safe_text(r.get("fabricante") or r.get("recorder_vendor")),
                "camera_ip": _safe_text(r.get("camera_ip")),
                "status": _safe_text(r.get("status")),
            }
        )
    targets.sort(key=lambda x: (_safe_text(x.get("site")), _safe_text(x.get("host")), int(x.get("channel") or 0)))
    return {"ok": True, "targets": targets, "count": len(targets)}


def _parse_dahua_response(text: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _segment_overlaps_window(segment: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> bool:
    try:
        seg_start = _parse_dt(segment.get("start_time"))
        seg_end = _parse_dt(segment.get("end_time"))
    except Exception:
        return False
    return seg_start < end_dt and seg_end > start_dt


def dahua_media_find_segments(
    *,
    host: str,
    http_port: int,
    user: str,
    password: str,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
) -> Dict[str, Any]:
    base_url = f"http://{host}:{int(http_port)}"
    auth = HTTPDigestAuth(user, password)
    session = requests.Session()
    timeout = (4, 12)
    token = ""
    try:
        create = session.get(
            f"{base_url}/cgi-bin/mediaFileFind.cgi",
            params={"action": "factory.create"},
            auth=auth,
            timeout=timeout,
        )
        if create.status_code in (401, 403):
            return {"ok": False, "segments": [], "warning": "NVR recusou usuario/senha na API de gravacoes."}
        create.raise_for_status()
        token = _parse_dahua_response(create.text).get("result", "")
        if not token:
            return {"ok": False, "segments": [], "warning": "NVR nao retornou token de busca de gravacoes."}

        # This CGI is picky: when spaces in date/time are encoded as "+",
        # some Intelbras/Dahua firmwares ignore the time and return midnight.
        # Build the query string manually so the space is always "%20".
        find_url = (
            f"{base_url}/cgi-bin/mediaFileFind.cgi"
            f"?action=findFile"
            f"&object={quote(token, safe='')}"
            f"&condition.Channel={int(channel)}"
            f"&condition.StartTime={quote(start_dt.strftime('%Y-%m-%d %H:%M:%S'), safe='')}"
            f"&condition.EndTime={quote(end_dt.strftime('%Y-%m-%d %H:%M:%S'), safe='')}"
            f"&condition.Types%5B0%5D=dav"
        )
        found = session.get(
            find_url,
            auth=auth,
            timeout=timeout,
        )
        if found.status_code in (400, 404):
            return {"ok": False, "segments": [], "warning": "NVR nao aceitou a consulta de gravacao para este canal/horario."}
        if found.status_code in (401, 403):
            return {"ok": False, "segments": [], "warning": "NVR recusou usuario/senha na consulta de gravacoes."}
        found.raise_for_status()

        next_file = session.get(
            f"{base_url}/cgi-bin/mediaFileFind.cgi",
            params={"action": "findNextFile", "object": token, "count": 100},
            auth=auth,
            timeout=timeout,
        )
        next_file.raise_for_status()
        parsed = _parse_dahua_response(next_file.text)
        segments_by_idx: Dict[int, Dict[str, Any]] = {}
        for key, value in parsed.items():
            m = re.match(r"items\[(\d+)\]\.([A-Za-z0-9_]+)$", key)
            if not m:
                continue
            idx = int(m.group(1))
            field = m.group(2)
            segments_by_idx.setdefault(idx, {})[field] = value
        segments: List[Dict[str, Any]] = []
        for idx in sorted(segments_by_idx):
            seg = segments_by_idx[idx]
            start = _safe_text(seg.get("StartTime"))
            end = _safe_text(seg.get("EndTime"))
            if not start or not end:
                continue
            segments.append(
                {
                    "channel": int(seg.get("Channel") or 0),
                    "start_time": start,
                    "end_time": end,
                    "stream": _safe_text(seg.get("VideoStream")),
                    "type": _safe_text(seg.get("Type")),
                    "length": int(seg.get("Length") or 0),
                    "file_path": _safe_text(seg.get("FilePath")),
                }
            )
        segments = [seg for seg in segments if _segment_overlaps_window(seg, start_dt, end_dt)]
        return {"ok": True, "segments": segments, "warning": ""}
    except requests.RequestException as e:
        return {"ok": False, "segments": [], "warning": f"API de gravacoes do NVR indisponivel: {_safe_text(e)[:180]}"}
    finally:
        if token:
            try:
                session.get(
                    f"{base_url}/cgi-bin/mediaFileFind.cgi",
                    params={"action": "destroy", "object": token},
                    auth=auth,
                    timeout=(3, 6),
                )
            except Exception:
                pass


def query_recording_segments(req: Dict[str, Any]) -> Dict[str, Any]:
    host = _safe_text(req.get("host"))
    user = _safe_text(req.get("user") or "admin")
    password = _safe_text(req.get("password"))
    channel = int(req.get("channel") or 0)
    if not host:
        raise ValueError("host obrigatorio")
    if not password:
        raise ValueError("senha obrigatoria")
    if channel <= 0:
        raise ValueError("canal obrigatorio")

    start_dt = _parse_dt(req.get("start_time"))
    end_dt = _parse_dt(req.get("end_time"))
    if end_dt <= start_dt:
        raise ValueError("data final deve ser maior que a inicial")

    vendor = _safe_text(req.get("vendor") or "auto").lower()
    if vendor not in ("", "auto", "dahua", "intelbras", "intelbras/dahua", "dvr"):
        return {
            "ok": True,
            "record_segments": [],
            "nvr_api_warning": "Consulta rapida de gravacoes ainda esta implementada para Intelbras/Dahua.",
            "message": "Use a indexacao por RTSP para este padrao de equipamento.",
        }

    media_find = dahua_media_find_segments(
        host=host,
        http_port=int(req.get("http_port") or 80),
        user=user,
        password=password,
        channel=channel,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    segments = media_find.get("segments") if isinstance(media_find.get("segments"), list) else []
    warning = _safe_text(media_find.get("warning"))
    if segments:
        first = segments[0]
        last = segments[-1]
        message = (
            f"NVR retornou {len(segments)} trecho(s) gravado(s): "
            f"{first.get('start_time')} ate {last.get('end_time')}."
        )
    elif warning:
        message = "Nao foi possivel consultar o indice de gravacoes do NVR."
    else:
        message = "O NVR respondeu: nao existe gravacao nesse canal/horario."
    return {
        "ok": True,
        "record_segments": segments,
        "nvr_api_warning": warning,
        "message": message,
    }


