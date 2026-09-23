from __future__ import annotations

import io
import json
import os
import socket
import shutil
import zipfile
import ipaddress
import asyncio
import re
import requests
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.core.paths import (
    BASE_DIR,
    DATA_DIR,
    OUTPUT_DIR,
    SAIDA_DIR,
    INVENTORY_JSON_PATH,
    DVR_INVENTORY_JSON_PATH,
    NVR_INVENTORY_JSON_PATH,
    KMZ_INPUT_DIR,
    KMZ_IMPORTED_PATH,
    KMZ_IMPORTED_GEOJSON_PATH,
    KMZ_OUTPUT_DIR,
)
from app.services.kmz_ops import (
    apply_locations_to_inventory,
    generate_enriched_kmz,
    kmz_to_geojson,
    read_geojson_file,
)
from app.services.camsnapshot.device_info import probe_device
from app.services.db_store import (
    decorate_legacy_rows,
    legacy_rows_from_db,
    replace_ip_inventory_rows,
    replace_recorder_inventory_rows,
    clear_inventory_source,
    load_app_settings,
    save_app_settings,
)
from app.core.tenant_context import (
    get_current_tenant_slug,
    tenant_kmz_imported_geojson_path,
    tenant_kmz_imported_path,
    tenant_kmz_input_dir,
    tenant_kmz_output_dir,
    tenant_locations_apply_report_path,
    tenant_recorder_inventory_path,
    tenant_report_logo_path,
)
from app.services.scan_service import _upload_imgbb_for_inventory
from app.services.scan_service import _enrich_inventory_with_olt, _enrich_inventory_with_switch
from app.services.pdf_inventory_report import build_inventory_pdf_report, build_inventory_preview_image
from app.services.inventory_json import inventory_row_key, load_inventory_json, save_inventory_json

router = APIRouter(prefix="/api", tags=["tools"])


def _report_color(value: Any = "") -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return "#" + raw
    return "#0b2242"

# -----------------
# small utilities
# -----------------

def _safe_rmtree(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for member in zf.infolist():
        name = str(member.filename or "").replace("\\", "/")
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise HTTPException(400, "Backup invalido: caminho inseguro dentro do ZIP.")
        dest = (target_dir / name).resolve()
        if root != dest and root not in dest.parents:
            raise HTTPException(400, "Backup invalido: caminho fora do diretorio temporario.")
    zf.extractall(target_dir)


def _safe_name(value: str, fallback: str = "mapa") -> str:
    stem = Path(str(value or fallback)).stem.strip() or fallback
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-")
    return stem[:80] or fallback


def _kmz_layers_dir() -> Path:
    base = tenant_kmz_input_dir().parent if get_current_tenant_slug() else KMZ_INPUT_DIR.parent
    p = base / "kmz_layers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _kmz_generated_layers_dir() -> Path:
    base = tenant_kmz_output_dir().parent if get_current_tenant_slug() else KMZ_OUTPUT_DIR.parent
    p = base / "kmz_generated_layers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _kmz_layer_paths(layer_id: str) -> tuple[Path, Path, Path]:
    safe_id = _safe_name(layer_id, "layer")
    base = _kmz_layers_dir()
    return base / f"{safe_id}.kmz", base / f"{safe_id}.geojson", base / f"{safe_id}.meta.json"


def _kmz_generated_layer_paths(layer_id: str) -> tuple[Path, Path, Path]:
    safe_id = _safe_name(layer_id, "layer")
    base = _kmz_generated_layers_dir()
    return base / f"{safe_id}.kmz", base / f"{safe_id}.geojson", base / f"{safe_id}.meta.json"


def _read_kmz_layer_meta(meta_path: Path) -> dict[str, Any]:
    try:
        if meta_path.exists():
            data = json.loads(meta_path.read_text(encoding="utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _write_kmz_layer_meta(meta_path: Path, meta: dict[str, Any]) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _kmz_layer_download_name(label: str, fallback: str = "mapa.kmz") -> str:
    raw = str(label or "").strip() or fallback
    stem = Path(raw).stem or "mapa"
    return f"{_safe_name(stem, 'mapa')}.kmz"


def _list_kmz_import_layers(include_features: bool = True) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    for meta_path in sorted(_kmz_layers_dir().glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = _read_kmz_layer_meta(meta_path)
        layer_id = str(meta.get("id") or meta_path.stem.replace(".meta", "")).strip()
        kmz_path, geojson_path, _ = _kmz_layer_paths(layer_id)
        if not kmz_path.exists() or not geojson_path.exists():
            continue
        geojson: dict[str, Any] = {}
        features: list[Any] = []
        if include_features:
            geojson = read_geojson_file(geojson_path)
            features = geojson.get("features") or []
        elif geojson_path.exists():
            try:
                features = json.loads(geojson_path.read_text(encoding="utf-8") or "{}").get("features") or []
            except Exception:
                features = []
        layers.append({
            "id": layer_id,
            "label": str(meta.get("label") or meta.get("original_name") or kmz_path.name).replace(".kmz", "").replace(".kml", ""),
            "original_name": meta.get("original_name") or kmz_path.name,
            "filename": kmz_path.name,
            "features_count": len(features),
            "created_at": meta.get("created_at") or "",
            "download_url": f"/api/kmz/import/layers/{layer_id}/download-enriched",
            "raw_download_url": f"/api/kmz/import/layers/{layer_id}/download",
            "update_url": f"/api/kmz/import/layers/{layer_id}",
            **({"features": features, "type": geojson.get("type") or "FeatureCollection"} if include_features else {}),
        })
    return layers


def _list_kmz_generated_layers(include_features: bool = True) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    for meta_path in sorted(_kmz_generated_layers_dir().glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = _read_kmz_layer_meta(meta_path)
        generated_from = str(meta.get("generated_from") or "").strip()
        if generated_from == "imported-layer-download":
            continue
        layer_id = str(meta.get("id") or meta_path.stem.replace(".meta", "")).strip()
        kmz_path, geojson_path, _ = _kmz_generated_layer_paths(layer_id)
        if not kmz_path.exists() or not geojson_path.exists():
            continue
        geojson: dict[str, Any] = {}
        features: list[Any] = []
        if include_features:
            geojson = read_geojson_file(geojson_path)
            features = geojson.get("features") or []
        elif geojson_path.exists():
            try:
                features = json.loads(geojson_path.read_text(encoding="utf-8") or "{}").get("features") or []
            except Exception:
                features = []
        layers.append({
            "id": layer_id,
            "label": str(meta.get("label") or meta.get("original_name") or kmz_path.name).replace(".kmz", "").replace(".kml", ""),
            "original_name": meta.get("original_name") or kmz_path.name,
            "filename": kmz_path.name,
            "features_count": len(features),
            "created_at": meta.get("created_at") or "",
            "source_layer_id": meta.get("source_layer_id") or "",
            "source": meta.get("source") or "generated",
            "mode": meta.get("mode") or "",
            "generated_from": generated_from,
            "download_url": f"/api/kmz/generated/layers/{layer_id}/download",
            "update_url": f"/api/kmz/generated/layers/{layer_id}",
            **({"features": features, "type": geojson.get("type") or "FeatureCollection"} if include_features else {}),
        })
    return layers


def _rename_kmz_layer(layer_id: str, label: str, generated: bool = False) -> dict[str, Any]:
    clean_label = str(label or "").strip()
    if not clean_label:
        raise HTTPException(400, "Nome da camada obrigatorio.")
    paths_fn = _kmz_generated_layer_paths if generated else _kmz_layer_paths
    list_fn = _list_kmz_generated_layers if generated else _list_kmz_import_layers
    kmz_path, geojson_path, meta_path = paths_fn(layer_id)
    if not kmz_path.exists() or not geojson_path.exists():
        raise HTTPException(404, "Camada KMZ nao encontrada.")
    meta = _read_kmz_layer_meta(meta_path)
    meta["id"] = layer_id
    meta["label"] = clean_label
    meta["original_name"] = _kmz_layer_download_name(clean_label, str(meta.get("original_name") or kmz_path.name))
    meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_kmz_layer_meta(meta_path, meta)
    layer = next((item for item in list_fn(include_features=False) if item.get("id") == layer_id), None)
    return layer or {"id": layer_id, "label": clean_label, "original_name": meta["original_name"]}


def _ensure_imported_layer_enriched(layer_id: str, source: str, mode: str = "") -> tuple[Path, str]:
    src = str(source or "ip").strip().lower()
    normalized_mode = _normalize_inventory_mode(mode) if src == "ip" else ""
    import_kmz_path, _, import_meta_path = _kmz_layer_paths(layer_id)
    if not import_kmz_path.exists():
        raise HTTPException(404, "Camada KMZ nao encontrada.")

    rows_raw = _load_rows_by_source(src, mode=normalized_mode)
    rows = _kmz_rows_for_source(src, rows_raw)
    if not rows:
        raise HTTPException(400, "Inventario vazio.")

    meta = _read_kmz_layer_meta(import_meta_path)
    label = str(meta.get("label") or meta.get("original_name") or import_kmz_path.stem).replace(".kmz", "").replace(".kml", "")
    generated_id = f"enriched-{_safe_name(layer_id, 'layer')}-{_safe_name(src, 'ip')}-{_safe_name(normalized_mode or 'all', 'all')}"
    generated_kmz_path, generated_geojson_path, generated_meta_path = _kmz_generated_layer_paths(generated_id)
    kmz_output_dir = tenant_kmz_output_dir() if get_current_tenant_slug() else KMZ_OUTPUT_DIR

    try:
        out_kmz = generate_enriched_kmz(import_kmz_path, rows, kmz_output_dir)
        geojson = kmz_to_geojson(out_kmz)
        generated_kmz_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out_kmz, generated_kmz_path)
        generated_geojson_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
        generated_meta = {
            "id": generated_id,
            "label": label,
            "original_name": _kmz_layer_download_name(label),
            "created_at": str(meta.get("created_at") or datetime.now().isoformat(timespec="seconds")),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "features_count": len(geojson.get("features") or []),
            "source_layer_id": layer_id,
            "source": src,
            "mode": normalized_mode if src == "ip" else "",
            "generated_from": "imported-layer-download",
            "rows_used": len(rows),
        }
        _write_kmz_layer_meta(generated_meta_path, generated_meta)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Falha ao gerar KMZ enriquecido: {exc}")

    return generated_kmz_path, str(generated_meta.get("original_name") or generated_kmz_path.name)


def _cleanup_kmz_workspace(keep_imported: bool = False) -> None:
    kmz_input_dir = tenant_kmz_input_dir() if get_current_tenant_slug() else KMZ_INPUT_DIR
    kmz_output_dir = tenant_kmz_output_dir() if get_current_tenant_slug() else KMZ_OUTPUT_DIR
    kmz_imported_path = tenant_kmz_imported_path() if get_current_tenant_slug() else KMZ_IMPORTED_PATH
    kmz_geojson_path = tenant_kmz_imported_geojson_path() if get_current_tenant_slug() else KMZ_IMPORTED_GEOJSON_PATH

    # Mantem workspace de KMZ limpo para evitar "sujeira" de imports antigos.
    kmz_input_dir.mkdir(parents=True, exist_ok=True)
    kmz_output_dir.mkdir(parents=True, exist_ok=True)
    kmz_imported_path.parent.mkdir(parents=True, exist_ok=True)

    # Limpa arquivos de input antigos
    for p in kmz_input_dir.glob("*"):
        if p.is_file():
            _safe_unlink(p)

    # Limpa artefatos gerados (mantemos somente a ultima geracao por sessao)
    for p in kmz_output_dir.glob("*"):
        if p.is_file():
            _safe_unlink(p)

    # Limpa import atual/caches quando requisitado
    if not keep_imported:
        _safe_unlink(kmz_imported_path)
        _safe_unlink(kmz_geojson_path)
        _safe_unlink(kmz_imported_path.with_suffix(".meta.json"))


def _zip_add_tree(
    zf: zipfile.ZipFile,
    src_dir: Path,
    arc_prefix: str,
    exclude_prefixes: Optional[List[str]] = None,
) -> None:
    exclude_prefixes = exclude_prefixes or []
    if not src_dir.exists():
        return
    for root, dirs, files in os.walk(src_dir):
        root_p = Path(root)
        rel_root = root_p.relative_to(src_dir)
        dirs[:] = [d for d in dirs if not any(str((rel_root / d)).startswith(x) for x in exclude_prefixes)]
        for fn in files:
            rel = rel_root / fn
            if any(str(rel).startswith(x) for x in exclude_prefixes):
                continue
            full_path = root_p / fn
            arcname = str(Path(arc_prefix) / rel).replace("\\", "/")
            try:
                zf.write(full_path, arcname)
            except Exception:
                continue


def _load_inventory_rows(mode: str = "olt", site: str = "") -> list[dict[str, Any]]:
    return load_inventory_json(site=site, mode=mode) or []


def _save_inventory_rows(rows: list[dict[str, Any]], mode: str = "olt") -> None:
    save_inventory_json(rows, mode=mode)


def _load_dvr_rows() -> list[dict[str, Any]]:
    if get_current_tenant_slug():
        p = tenant_recorder_inventory_path("dvr")
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []
    db_rows = legacy_rows_from_db("dvr")
    if db_rows:
        return db_rows
    p = Path(DVR_INVENTORY_JSON_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_dvr_rows(rows: list[dict[str, Any]]) -> None:
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
    p = Path(DVR_INVENTORY_JSON_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_nvr_rows() -> list[dict[str, Any]]:
    if get_current_tenant_slug():
        p = tenant_recorder_inventory_path("nvr")
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []
    db_rows = legacy_rows_from_db("nvr")
    if db_rows:
        return db_rows
    p = Path(NVR_INVENTORY_JSON_PATH)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_nvr_rows(rows: list[dict[str, Any]]) -> None:
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
    p = Path(NVR_INVENTORY_JSON_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_inventory_mode(mode: str = "") -> str:
    raw = str(mode or "").strip().lower()
    if raw in ("basic", "basico", "básico"):
        return "basico"
    if raw == "switch":
        return "switch"
    return "olt"


def _load_rows_by_source_mode(source: str, mode: str = "") -> tuple[list[dict[str, Any]], str]:
    """Linhas da fonte + o modo de onde elas vieram.

    Cada site vive num modo so (o Demerval, por exemplo, so existe em "switch").
    Com o seletor do mapa em outro modo o inventario vinha vazio e baixar/gerar
    KMZ morria em "Inventario vazio." mesmo com as cameras cadastradas. Aqui o
    modo pedido tem prioridade e os outros servem de reserva; quem grava usa o
    modo EFETIVO, pra nao criar copia da camera num modo que nao e o dela.
    """
    src = str(source or "ip").strip().lower()
    if src == "dvr":
        return _load_dvr_rows(), ""
    if src == "nvr":
        return _load_nvr_rows(), ""
    wanted = _normalize_inventory_mode(mode)
    rows = _load_inventory_rows(mode=wanted)
    if rows:
        return rows, wanted
    for alt in ("basico", "switch", "olt"):
        if alt == wanted:
            continue
        rows = _load_inventory_rows(mode=alt)
        if rows:
            return rows, alt
    return [], wanted


def _load_rows_by_source(source: str, mode: str = "") -> list[dict[str, Any]]:
    return _load_rows_by_source_mode(source, mode=mode)[0]


def _save_rows_by_source(source: str, rows: list[dict[str, Any]], mode: str = "") -> None:
    src = str(source or "ip").strip().lower()
    if src == "dvr":
        _save_dvr_rows(rows)
        return
    if src == "nvr":
        _save_nvr_rows(rows)
        return
    _save_inventory_rows(rows, mode=_normalize_inventory_mode(mode))


def _kmz_rows_for_source(source: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    src = str(source or "ip").strip().lower()
    if src not in ("dvr", "nvr"):
        return rows
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        host = str(r.get("host") or "").strip()
        if not host:
            continue
        ch = int(r.get("channel") or 0)
        ch_txt = f"{ch:02d}" if ch > 0 else "00"
        title = str(r.get("title") or "").strip() or f"Canal {ch_txt}"
        out.append(
            {
                "ip": host,
                "titulo": title,
                "title": title,
                "local": str(r.get("local") or "").strip(),
                "lat": r.get("lat"),
                "lon": r.get("lon"),
                "snapshot_url": str(r.get("imgbb_url") or r.get("snapshot_url") or "").strip(),
                "imgbb_url": str(r.get("imgbb_url") or "").strip(),
            }
        )
    return out


def _guess_service(port: int) -> str:
    common: dict[int, str] = {
        21: "ftp",
        22: "ssh",
        23: "telnet",
        53: "dns",
        80: "http",
        81: "http-alt",
        88: "http-alt",
        443: "https",
        554: "rtsp",
        8000: "http-alt",
        8080: "http-proxy",
        8291: "mikrotik-winbox",
        8443: "https-alt",
        37777: "dahua",
    }
    return common.get(port, "")


def _parse_ports(raw: str) -> list[int]:
    if not raw:
        return [80, 443, 554, 8000, 37777, 8291, 22]
    out: list[int] = []
    seen: set[int] = set()
    for token in [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]:
        if "-" in token:
            try:
                a, b = token.split("-", 1)
                start = int(a.strip())
                end = int(b.strip())
                if end < start:
                    start, end = end, start
                for p in range(start, end + 1):
                    if 1 <= p <= 65535 and p not in seen:
                        out.append(p)
                        seen.add(p)
            except Exception:
                continue
        else:
            try:
                p = int(token)
            except Exception:
                continue
            if 1 <= p <= 65535 and p not in seen:
                out.append(p)
                seen.add(p)
    return out or [80, 443, 554, 8000, 37777, 8291, 22]


def _split_tokens(raw: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\s;]+", raw or "") if t.strip()]


def _expand_host_token(token: str) -> list[str]:
    token = (token or "").strip()
    if not token:
        return []

    if "/" in token:
        try:
            net = ipaddress.ip_network(token, strict=False)
            return [str(ip) for ip in net.hosts()]
        except Exception:
            return [token]

    if "-" in token:
        left, right = token.split("-", 1)
        left = left.strip()
        right = right.strip()
        try:
            ip_left = ipaddress.ip_address(left)
            if ip_left.version == 4:
                if re.fullmatch(r"\d{1,3}", right):
                    parts = left.split(".")
                    start = int(parts[-1])
                    end = int(right)
                    if 0 <= start <= 255 and 0 <= end <= 255:
                        if end < start:
                            start, end = end, start
                        base = ".".join(parts[:-1])
                        return [f"{base}.{i}" for i in range(start, end + 1)]
                ip_right = ipaddress.ip_address(right)
                if ip_right.version == 4:
                    start_i = int(ip_left)
                    end_i = int(ip_right)
                    if end_i < start_i:
                        start_i, end_i = end_i, start_i
                    return [str(ipaddress.ip_address(i)) for i in range(start_i, end_i + 1)]
        except Exception:
            pass

    return [token]


def _parse_targets(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in _split_tokens(raw):
        for host in _expand_host_token(token):
            if host not in seen:
                seen.add(host)
                out.append(host)
    return out


def _quick_scan_open_ports(ip: str, ports: list[int], timeout_s: float) -> list[dict[str, Any]]:
    opened: list[dict[str, Any]] = []
    for port in ports:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            rc = sock.connect_ex((ip, int(port)))
            if rc == 0:
                opened.append({"port": int(port), "service": _guess_service(int(port))})
        except Exception:
            continue
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
    return opened


@router.post("/tools/scan-ip")
async def api_tools_scan_ip(payload: Dict[str, Any]) -> Dict[str, Any]:
    ip = str(payload.get("ip") or "").strip()
    user = str(payload.get("usuario") or payload.get("user") or "").strip()
    password = str(payload.get("senha") or payload.get("password") or payload.get("pass") or "").strip()
    ports_raw = str(payload.get("ports") or "").strip()
    timeout_ms = int(payload.get("timeout_ms") or 900)
    timeout_s = max(0.08, min(timeout_ms / 1000.0, 8.0))

    if not ip:
        raise HTTPException(400, "IP nao informado.")
    try:
        parsed = ipaddress.ip_address(ip)
    except Exception:
        raise HTTPException(400, "IP invalido.")
    if parsed.version != 4:
        raise HTTPException(400, "A ferramenta atual aceita apenas IPv4.")

    ports = _parse_ports(ports_raw)
    open_ports = _quick_scan_open_ports(ip, ports, timeout_s)
    online = bool(open_ports)

    hostname = ""
    try:
        hostname = socket.gethostbyaddr(ip)[0] or ""
    except Exception:
        hostname = ""

    info: Dict[str, Any] = {"ip": ip}
    try:
        if user:
            info = probe_device(ip, user, password, timeout=(timeout_s, max(1.0, timeout_s * 2.2)), retries=0) or {"ip": ip}
    except Exception:
        info = {"ip": ip}

    fabricante = str(info.get("fabricante") or "").strip()
    modelo = str(info.get("modelo") or "").strip()
    titulo = str(info.get("titulo") or "").strip()
    mac = str(info.get("mac") or "").strip().lower()
    status = str(info.get("status") or ("online" if online else "offline")).strip().lower()

    return {
        "ok": True,
        "ip": ip,
        "online": online,
        "hostname": hostname,
        "status": status,
        "fabricante": fabricante,
        "modelo": modelo,
        "titulo": titulo,
        "mac": mac,
        "open_ports": open_ports,
        "ports_checked": ports,
        "auth_used": bool(user),
    }


@router.post("/discovery/run")
async def api_discovery_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    targets = _parse_targets(str(payload.get("targets") or "").strip())
    ports = _parse_ports(str(payload.get("ports") or "").strip())
    timeout_ms = int(payload.get("timeout_ms") or 650)
    timeout_s = max(0.08, min(timeout_ms / 1000.0, 8.0))
    concurrency = max(1, min(int(payload.get("concurrency") or 256), 1024))
    fast_mode = bool(payload.get("fast_mode", True))

    user = str(payload.get("usuario") or payload.get("user") or "").strip()
    password = str(payload.get("senha") or payload.get("password") or payload.get("pass") or "").strip()
    collect_info = bool(payload.get("collect_info", True))

    if not targets:
        raise HTTPException(400, "Informe ao menos um alvo (IP/range/CIDR).")
    if not ports:
        raise HTTPException(400, "Informe ao menos uma porta.")

    sem = asyncio.Semaphore(concurrency)
    rows: list[dict[str, Any]] = []
    checked = 0

    async def _scan_host(ip: str) -> None:
        nonlocal checked
        open_ports: list[int] = []
        async with sem:
            for p in ports:
                try:
                    conn = asyncio.open_connection(ip, int(p))
                    reader, writer = await asyncio.wait_for(conn, timeout=timeout_s)
                    open_ports.append(int(p))
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    if fast_mode:
                        break
                except Exception:
                    continue
            checked += 1

            if not open_ports:
                return

            hostname = ""
            try:
                hostname = socket.gethostbyaddr(ip)[0] or ""
            except Exception:
                hostname = ""

            info: Dict[str, Any] = {"ip": ip}
            if collect_info and user:
                try:
                    info = await asyncio.to_thread(
                        probe_device,
                        ip,
                        user,
                        password,
                        (timeout_s, max(1.0, timeout_s * 2.0)),
                        0,
                    ) or {"ip": ip}
                except Exception:
                    info = {"ip": ip}

            rows.append(
                {
                    "ip": ip,
                    "hostname": hostname,
                    "open_ports": [{"port": int(pp), "service": _guess_service(int(pp))} for pp in open_ports],
                    "fabricante": str(info.get("fabricante") or "").strip(),
                    "modelo": str(info.get("modelo") or "").strip(),
                    "titulo": str(info.get("titulo") or "").strip(),
                    "mac": str(info.get("mac") or "").strip().lower(),
                    "status": str(info.get("status") or "online").strip().lower(),
                }
            )

    tasks = [asyncio.create_task(_scan_host(ip)) for ip in targets]
    await asyncio.gather(*tasks, return_exceptions=True)
    rows.sort(key=lambda r: r.get("ip") or "")

    return {
        "ok": True,
        "targets_total": len(targets),
        "targets_checked": checked,
        "found": len(rows),
        "results": rows,
    }


# -----------------
# Inventory clear/export/import
# -----------------

@router.post("/inventory/clear")
async def api_inventory_clear(payload: Dict[str, Any] | None = None) -> dict[str, Any]:
    removed: list[str] = []
    wiped: dict[str, int] = {}
    db_clear: dict[str, Any] = {}
    data = payload if isinstance(payload, dict) else {}
    site = str(data.get("site") or "").strip()
    mode = str(data.get("mode") or "olt").strip().lower()

    def _row_matches_site(row: Dict[str, Any], wanted_site: str) -> bool:
        ws = str(wanted_site or "").strip().lower()
        if not ws:
            return False
        vals = [
            str(row.get("site") or "").strip(),
            str(row.get("site_name") or "").strip(),
            str(row.get("local") or row.get("LOCAL") or "").strip(),
        ]
        return any(v.lower() == ws for v in vals if v)

    if site:
        rows = _load_inventory_rows(mode=mode)
        kept = [r for r in rows if not (isinstance(r, dict) and _row_matches_site(r, site))]
        removed_count = max(0, len(rows) - len(kept))
        _save_inventory_rows(kept, mode=mode)
        return {
            "ok": True,
            "mode": mode,
            "site": site,
            "scope": "site",
            "removed_rows": removed_count,
            "remaining": len(kept),
            "wiped": {},
            "removed": [],
        }

    try:
        _save_inventory_rows([], mode=mode)
        db_clear = {"ok": True, "mode": mode}
    except Exception:
        db_clear = {"ok": False, "mode": mode}

    if mode == "olt":
        for path in [
            INVENTORY_JSON_PATH,
            SAIDA_DIR / "cam-inventory.ai.csv",
            BASE_DIR / "cam-inventory.json",
        ]:
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            except Exception:
                pass

    def _wipe_tree(dir_path: Path) -> int:
        try:
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                return 0
            try:
                count = sum(1 for p in dir_path.rglob("*") if p.is_file())
            except Exception:
                count = 0
            shutil.rmtree(dir_path, ignore_errors=True)
            dir_path.mkdir(parents=True, exist_ok=True)
            return count
        except Exception:
            return 0

    if mode == "olt":
        for d in [OUTPUT_DIR / "snapshot", OUTPUT_DIR / "thumbs", OUTPUT_DIR / "snapshot_manual"]:
            wiped[str(d)] = _wipe_tree(d)

    return {"ok": True, "mode": mode, "scope": "all", "removed": removed, "wiped": wiped, "db_clear": db_clear}


@router.get("/inventory/export")
async def api_inventory_export(mode: str = "olt") -> FileResponse:
    rows = _load_inventory_rows(mode=mode)
    if not rows:
        raise HTTPException(404, "Inventario nao encontrado.")
    export_mode = str(mode or "olt").strip().lower()
    export_path = (DATA_DIR / "cam-inventory-switch-export.json") if export_mode == "switch" else INVENTORY_JSON_PATH
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"cam-inventory-{export_mode}-backup-{ts}.json"
    return FileResponse(path=export_path, media_type="application/json", filename=fname)


@router.post("/inventory/import")
async def api_inventory_import(file: UploadFile = File(...), mode: str = "olt") -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Arquivo vazio.")

    data: Any = None
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            data = json.loads(raw.decode(enc))
            break
        except Exception:
            continue
    if data is None:
        raise HTTPException(400, "JSON invalido.")

    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("inventory") or data.get("rows") or data.get("data")
    else:
        rows = None

    if not isinstance(rows, list):
        raise HTTPException(400, "Formato invalido: esperado uma lista de cameras.")

    _save_inventory_rows([r for r in rows if isinstance(r, dict)], mode=mode)
    saved = _load_inventory_rows(mode=mode)
    return {"ok": True, "count": len(saved), "inventory": saved}


@router.get("/inventory-last")
async def api_inventory_last(site: str = "", enrich: str = "") -> Dict[str, Any]:
    mode = (enrich or "olt").strip().lower()
    rows = _load_inventory_rows(mode=mode, site=site)
    if mode == "olt":
        rows, _ = _enrich_inventory_with_olt(list(rows), SAIDA_DIR / "olt-cpe-macs.json")
    elif mode == "switch":
        rows, _ = _enrich_inventory_with_switch(list(rows), DATA_DIR / "switch-mac-table.json")
    return {"ok": True, "count": len(rows), "inventory": rows}


def _load_report_rows_and_config(
    site: str, company_name: str, report_color: str, mode: str, ips: str
) -> tuple[list[dict[str, Any]], Dict[str, Any]]:
    report_mode = str(mode or "").strip().lower()
    rows = _load_inventory_rows(mode=(report_mode or "olt"), site=site)
    if report_mode == "olt":
        rows, _ = _enrich_inventory_with_olt(list(rows), SAIDA_DIR / "olt-cpe-macs.json")
    elif report_mode == "switch":
        rows, _ = _enrich_inventory_with_switch(list(rows), DATA_DIR / "switch-mac-table.json")
    selected_ips = {str(part or "").strip() for part in str(ips or "").split(",")}
    selected_ips.discard("")
    if selected_ips:
        rows = [
            r for r in rows
            if isinstance(r, dict) and str(r.get("ip") or r.get("IP") or "").strip() in selected_ips
        ]
    settings = load_app_settings()
    if not isinstance(settings, dict):
        settings = {}
    rep_cfg = settings.get("inventory_pdf_report") if isinstance(settings, dict) else {}
    rep_cfg = rep_cfg if isinstance(rep_cfg, dict) else {}
    company = str(company_name or rep_cfg.get("company_name") or "").strip()
    color = _report_color(report_color or rep_cfg.get("report_color") or "")
    report_logo_path = tenant_report_logo_path("inventory") if get_current_tenant_slug() else (DATA_DIR / "input" / "inventory-report-logo.png")
    logo = report_logo_path if report_logo_path.exists() else None
    cfg = {
        "site": site,
        "company_name": company,
        "logo_path": logo,
        "include_olt": (report_mode == "olt"),
        "include_switch": (report_mode == "switch"),
        "module_label": {"olt": "Cameras IP OLT", "switch": "Cameras IP Switch"}.get(report_mode, "Cameras IP Basico"),
        "report_color": color,
    }
    return rows, cfg


@router.get("/inventory/report.pdf")
async def api_inventory_report_pdf(site: str = "", company_name: str = "", report_color: str = "", mode: str = "", ips: str = "") -> FileResponse:
    rows, cfg = _load_report_rows_and_config(site, company_name, report_color, mode, ips)
    pdf_path = await asyncio.to_thread(
        build_inventory_pdf_report,
        rows,
        include_photos=True,
        **cfg,
    )
    return FileResponse(path=pdf_path, media_type="application/pdf", filename=pdf_path.name, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


# Job assincrono com progresso: gerar o PDF (principalmente a galeria de
# fotos, que le/decodifica/redimensiona uma imagem por camera) pode levar
# de alguns segundos a alguns minutos num inventario grande. Uma unica
# requisicao sincrona nesse caso deixa o navegador parado sem feedback --
# aqui o front dispara o job, recebe um job_id e fica consultando o status
# (done/total/etapa) pra mostrar progresso real, so baixando o arquivo no
# final. Mesmo padrao ja usado pelas auditorias offline de OLT.
_report_jobs: dict[str, dict[str, Any]] = {}
_report_tasks: set[asyncio.Task[Any]] = set()
_REPORT_JOB_TTL_SECONDS = 30 * 60


def _prune_report_jobs() -> None:
    now = datetime.now()
    stale = [
        jid for jid, job in _report_jobs.items()
        if job.get("status") in ("done", "error")
        and (now - job.get("_created", now)).total_seconds() > _REPORT_JOB_TTL_SECONDS
    ]
    for jid in stale:
        job = _report_jobs.pop(jid, None)
        path = job.get("path") if job else None
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


async def _run_inventory_report_job(job_id: str, rows: list[dict[str, Any]], cfg: Dict[str, Any]) -> None:
    job = _report_jobs[job_id]
    job["status"] = "running"

    def _cb(done: int, total: int, stage: str) -> None:
        job["done"] = int(done)
        job["total"] = int(total)
        job["stage"] = stage

    try:
        pdf_path = await asyncio.to_thread(
            build_inventory_pdf_report,
            rows,
            include_photos=True,
            progress_cb=_cb,
            **cfg,
        )
        job["status"] = "done"
        job["path"] = str(pdf_path)
        job["filename"] = pdf_path.name
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc) or repr(exc)


@router.post("/inventory/report/job")
async def api_inventory_report_job_start(site: str = "", company_name: str = "", report_color: str = "", mode: str = "", ips: str = "") -> Dict[str, Any]:
    _prune_report_jobs()
    rows, cfg = _load_report_rows_and_config(site, company_name, report_color, mode, ips)
    job_id = uuid.uuid4().hex
    _report_jobs[job_id] = {
        "status": "queued",
        "done": 0,
        "total": 0,
        "stage": "preparando",
        "total_rows": len(rows),
        "_created": datetime.now(),
    }
    task = asyncio.create_task(_run_inventory_report_job(job_id, rows, cfg), name=f"inv-report-{job_id[:8]}")
    _report_tasks.add(task)
    task.add_done_callback(_report_tasks.discard)
    return {"ok": True, "job_id": job_id, "total_rows": len(rows)}


@router.get("/inventory/report/job/{job_id}/status")
def api_inventory_report_job_status(job_id: str) -> Dict[str, Any]:
    job = _report_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job nao encontrado ou expirado")
    return {
        "ok": True,
        "status": job.get("status"),
        "done": job.get("done", 0),
        "total": job.get("total", 0),
        "stage": job.get("stage", ""),
        "total_rows": job.get("total_rows", 0),
        "error": job.get("error"),
        "ready": job.get("status") == "done",
    }


@router.get("/inventory/report/job/{job_id}/download")
def api_inventory_report_job_download(job_id: str) -> FileResponse:
    job = _report_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job nao encontrado ou expirado")
    if job.get("status") != "done":
        raise HTTPException(status_code=409, detail="relatorio ainda esta sendo gerado")
    path = Path(job.get("path") or "")
    if not path.exists():
        raise HTTPException(status_code=404, detail="arquivo do relatorio nao encontrado")
    return FileResponse(path=path, media_type="application/pdf", filename=job.get("filename") or path.name)


@router.get("/inventory/report/preview")
async def api_inventory_report_preview(site: str = "", company_name: str = "", report_color: str = "", mode: str = "", ips: str = "") -> StreamingResponse:
    rows, cfg = _load_report_rows_and_config(site, company_name, report_color, mode, ips)
    pdf_path = build_inventory_pdf_report(rows, **cfg)
    data = pdf_path.read_bytes()
    headers = {"Content-Disposition": f'inline; filename="{pdf_path.name}"'}
    return StreamingResponse(io.BytesIO(data), media_type="application/pdf", headers=headers)


@router.get("/inventory/report/preview.jpg")
async def api_inventory_report_preview_jpg(site: str = "", company_name: str = "", report_color: str = "", mode: str = "", ips: str = "") -> FileResponse:
    rows, cfg = _load_report_rows_and_config(site, company_name, report_color, mode, ips)
    img_path = build_inventory_preview_image(rows, **cfg)
    return FileResponse(path=img_path, media_type="image/jpeg", filename=img_path.name, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.get("/inventory/report/settings")
async def api_inventory_report_settings() -> Dict[str, Any]:
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("inventory_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    return {
        "ok": True,
        "company_name": str(rep.get("company_name") or "").strip(),
        "report_color": _report_color(rep.get("report_color") or ""),
        "has_logo": bool((tenant_report_logo_path("inventory") if get_current_tenant_slug() else (DATA_DIR / "input" / "inventory-report-logo.png")).exists()),
    }


@router.post("/inventory/report/settings")
async def api_inventory_report_settings_save(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    company = str(data.get("company_name") or "").strip()
    color = _report_color(data.get("report_color") or "")
    obj = load_app_settings()
    if not isinstance(obj, dict):
        obj = {}
    rep = obj.get("inventory_pdf_report")
    rep = rep if isinstance(rep, dict) else {}
    rep["company_name"] = company
    rep["report_color"] = color
    obj["inventory_pdf_report"] = rep
    save_app_settings(obj)
    return {"ok": True, "company_name": company, "report_color": color}


@router.post("/inventory/report/logo")
async def api_inventory_report_logo(file: UploadFile = File(...)) -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Arquivo vazio.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        raise HTTPException(400, "Formato invalido. Use PNG/JPG/WEBP/BMP.")
    report_logo_path = tenant_report_logo_path("inventory") if get_current_tenant_slug() else (DATA_DIR / "input" / "inventory-report-logo.png")
    report_logo_path.parent.mkdir(parents=True, exist_ok=True)
    report_logo_path.write_bytes(raw)
    return {"ok": True, "has_logo": True}


@router.post("/inventory/imgbb/upload")
async def api_inventory_imgbb_upload(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    mode = str(data.get("mode") or "olt").strip().lower()
    rows = _load_inventory_rows(mode=mode)
    if not rows:
        return {"ok": True, "uploaded": 0, "processed": 0, "error": "Inventario vazio.", "inventory": []}

    ips_raw = data.get("ips")
    keys_raw = data.get("keys")
    selected_ips: list[str] = []
    selected_keys: list[str] = []
    if isinstance(keys_raw, list):
        seen_keys: set[str] = set()
        for it in keys_raw:
            key = str(it or "").strip()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            selected_keys.append(key)
    if isinstance(ips_raw, list):
        seen: set[str] = set()
        for it in ips_raw:
            ip = str(it or "").strip()
            if not ip or ip in seen:
                continue
            seen.add(ip)
            selected_ips.append(ip)

    target_rows: list[dict[str, Any]]
    if selected_keys:
        want_keys = set(selected_keys)
        target_rows = [r for r in rows if isinstance(r, dict) and inventory_row_key(r) in want_keys]
        if not target_rows:
            return {"ok": False, "uploaded": 0, "processed": 0, "error": "Nenhuma camera selecionada encontrada no inventario.", "inventory": rows}
    elif selected_ips:
        want = set(selected_ips)
        target_rows = [r for r in rows if isinstance(r, dict) and str(r.get("ip") or r.get("IP") or "").strip() in want]
        if not target_rows:
            return {"ok": False, "uploaded": 0, "processed": 0, "error": "Nenhuma camera selecionada encontrada no inventario.", "inventory": rows}
    else:
        target_rows = [r for r in rows if isinstance(r, dict)]

    max_batch = int(os.getenv("IMGBB_UPLOAD_MAX_BATCH", "6") or "6")
    if len(target_rows) > max_batch:
        return {
            "ok": False,
            "uploaded": 0,
            "processed": 0,
            "error": f"Selecione no maximo {max_batch} cameras por lote. A tela atual envia automaticamente em lotes.",
            "inventory": rows,
        }

    uploaded_rows, uploaded, err = _upload_imgbb_for_inventory(target_rows)
    uploaded_by_key: dict[str, dict[str, Any]] = {}
    for r in uploaded_rows or []:
        if not isinstance(r, dict):
            continue
        key = inventory_row_key(r)
        if key and (str(r.get("imgbb_url") or "").strip() or str(r.get("imgbb_thumb_url") or "").strip()):
            uploaded_by_key[key] = r

    if uploaded_by_key:
        # `rows` foi carregado no INICIO da requisicao, antes do upload real
        # pro ImgBB (rede, um round-trip por camera). O sync automatico de
        # status Zabbix roda a cada 60s e faz a mesma coisa -- carrega tudo,
        # demora com chamadas de rede, salva tudo -- e o "salva tudo" de um
        # por cima do outro apaga silenciosamente o que o outro acabou de
        # gravar (visto em producao: upload confirmava no log mas o
        # imgbb_url sumia minutos depois). Recarrega aqui, bem antes de
        # salvar, e aplica so os campos do upload por chave.
        fresh_rows = _load_inventory_rows(mode=mode)
        changed = False
        for r in fresh_rows:
            if not isinstance(r, dict):
                continue
            u = uploaded_by_key.get(inventory_row_key(r))
            if not u:
                continue
            imgbb_url = str(u.get("imgbb_url") or u.get("url") or u.get("display_url") or "").strip()
            thumb_url = str(u.get("imgbb_thumb_url") or u.get("thumbnail_url") or u.get("thumb_url") or imgbb_url).strip()
            if imgbb_url:
                r["imgbb_url"] = imgbb_url
            if thumb_url:
                r["imgbb_thumb_url"] = thumb_url
            r["imgbb_status"] = "up"
            r["imgbb_updated_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
        if changed:
            _save_inventory_rows(fresh_rows, mode=mode)
            rows = fresh_rows
    err_text = str(err or "").strip()
    skipped_no_snapshot = 0
    if int(uploaded or 0) == 0 and "nenhum snapshot local" in err_text.lower():
        skipped_no_snapshot = len(target_rows)
    ok = int(uploaded or 0) > 0 or skipped_no_snapshot > 0
    if len(target_rows) == 0:
        ok = True
    return {
        "ok": ok,
        "mode": mode,
        "uploaded": int(uploaded or 0),
        "processed": len(target_rows),
        "skipped_no_snapshot": skipped_no_snapshot,
        "error": err_text,
        "inventory": rows,
    }


# -----------------
# Full backup export/import
# -----------------

def _create_full_backup_zip_file() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    backups_dir = OUTPUT_DIR / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = backups_dir / f"cam-snapshot-backup-{ts}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "app": "SightOps Cam Snapshot",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "format": "sightops-full-backup-v1",
            "contains": ["data"] + ([] if OUTPUT_DIR == DATA_DIR else ["output"]),
        }
        zf.writestr("backup-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        _zip_add_tree(zf, DATA_DIR, "data")
        if OUTPUT_DIR != DATA_DIR:
            _zip_add_tree(zf, OUTPUT_DIR, "output", exclude_prefixes=["backups"])

    # keep only last 5
    try:
        zips = sorted(backups_dir.glob("cam-snapshot-backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in zips[5:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass

    return zip_path


@router.get("/backup/export")
async def api_full_backup_export() -> FileResponse:
    try:
        zip_path = _create_full_backup_zip_file()
        return FileResponse(path=zip_path, media_type="application/zip", filename=zip_path.name)
    except Exception as exc:
        raise HTTPException(500, f"Falha ao gerar backup ZIP: {exc}")


@router.post("/backup/import")
async def api_full_backup_import(file: UploadFile = File(...)) -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Arquivo vazio.")

    tmp_root = BASE_DIR / "_tmp_restore"
    _safe_rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        _safe_extract_zip(zf, tmp_root)

    src_data = tmp_root / "data"
    src_output = tmp_root / "output"
    if not src_data.exists() and not src_output.exists():
        _safe_rmtree(tmp_root)
        raise HTTPException(400, "Backup invalido: nao encontrei as pastas 'data/' ou 'output/' dentro do ZIP.")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    unified_store = OUTPUT_DIR == DATA_DIR

    if unified_store:
        merged = tmp_root / "_merged_data"
        merged.mkdir(parents=True, exist_ok=True)
        if src_data.exists():
            shutil.copytree(src_data, merged, dirs_exist_ok=True)
        if src_output.exists():
            shutil.copytree(src_output, merged, dirs_exist_ok=True)

        if DATA_DIR.exists():
            prev_data = DATA_DIR.parent / f"data_prev_{ts}"
            try:
                if prev_data.exists():
                    _safe_rmtree(prev_data)
                shutil.move(str(DATA_DIR), str(prev_data))
            except Exception:
                pass

        if merged.exists():
            shutil.move(str(merged), str(DATA_DIR))
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)

        _safe_rmtree(tmp_root)
        return {
            "ok": True,
            "message": "Backup restaurado.",
            "restored": {"data": True, "output": False, "output_is_alias_of_data": True},
        }

    if DATA_DIR.exists():
        prev_data = DATA_DIR.parent / f"data_prev_{ts}"
        try:
            if prev_data.exists():
                _safe_rmtree(prev_data)
            shutil.move(str(DATA_DIR), str(prev_data))
        except Exception:
            pass

    if OUTPUT_DIR.exists():
        prev_out = OUTPUT_DIR.parent / f"output_prev_{ts}"
        try:
            if prev_out.exists():
                _safe_rmtree(prev_out)
            shutil.move(str(OUTPUT_DIR), str(prev_out))
        except Exception:
            pass

    if src_data.exists():
        shutil.move(str(src_data), str(DATA_DIR))
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    if src_output.exists():
        shutil.move(str(src_output), str(OUTPUT_DIR))
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    _safe_rmtree(tmp_root)
    return {"ok": True, "message": "Backup restaurado.", "restored": {"data": True, "output": True}}


# -----------------
# KMZ endpoints (minimal / compat)
# -----------------

@router.post("/kmz/import")
async def api_kmz_import(file: UploadFile = File(...)) -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Arquivo vazio.")

    kmz_input_dir = tenant_kmz_input_dir() if get_current_tenant_slug() else KMZ_INPUT_DIR
    kmz_imported_path = tenant_kmz_imported_path() if get_current_tenant_slug() else KMZ_IMPORTED_PATH
    kmz_geojson_path = tenant_kmz_imported_geojson_path() if get_current_tenant_slug() else KMZ_IMPORTED_GEOJSON_PATH

    original_name = (file.filename or "import.kmz").strip() or "import.kmz"
    layer_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe_name(original_name)}-{secrets.token_hex(3)}"
    layer_kmz_path, layer_geojson_path, layer_meta_path = _kmz_layer_paths(layer_id)
    tmp_import = layer_kmz_path.with_suffix(".tmp.kmz")
    _safe_unlink(tmp_import)
    layer_kmz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_import.write_bytes(raw)

    try:
        geojson = kmz_to_geojson(tmp_import)
        layer_geojson_path.write_text(
            json.dumps(geojson, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        _safe_unlink(tmp_import)
        raise HTTPException(400, f"Falha ao processar KMZ: {exc}")

    # So persiste o import definitivo apos validar que o KMZ e processavel.
    (kmz_input_dir / f"{layer_id}-{_safe_name(original_name)}.kmz").write_bytes(raw)
    tmp_import.replace(layer_kmz_path)

    layer_meta = {
        "id": layer_id,
        "original_name": original_name,
        "label": Path(original_name).stem,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "features_count": len(geojson.get("features") or []),
    }
    layer_meta_path.write_text(json.dumps(layer_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # Compatibilidade: endpoints antigos seguem apontando para o ultimo importado.
    kmz_imported_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(layer_kmz_path, kmz_imported_path)
    kmz_geojson_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")

    meta_path = kmz_imported_path.with_suffix(".meta.json")
    meta_path.write_text(
        json.dumps({"original_name": original_name, "layer_id": layer_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "id": layer_id,
        "path": str(layer_kmz_path),
        "filename": original_name,
        "features_count": len(geojson.get("features") or []),
        "message": "KMZ importado.",
    }


@router.get("/kmz/import/layers")
async def api_kmz_import_layers(include_features: bool = True) -> Dict[str, Any]:
    layers = _list_kmz_import_layers(include_features=include_features)
    return {"ok": True, "count": len(layers), "layers": layers}


@router.get("/kmz/import/layers/{layer_id}/download")
async def api_kmz_import_layer_download(layer_id: str) -> FileResponse:
    kmz_path, _, meta_path = _kmz_layer_paths(layer_id)
    if not kmz_path.exists():
        raise HTTPException(404, "Camada KMZ nao encontrada.")
    meta = _read_kmz_layer_meta(meta_path)
    filename = str(meta.get("original_name") or kmz_path.name)
    return FileResponse(kmz_path, media_type="application/vnd.google-earth.kmz", filename=filename)


@router.get("/kmz/import/layers/{layer_id}/download-enriched")
async def api_kmz_import_layer_download_enriched(
    layer_id: str,
    source: str = "ip",
    mode: str = "",
) -> FileResponse:
    kmz_path, filename = _ensure_imported_layer_enriched(layer_id, source=source, mode=mode)
    return FileResponse(kmz_path, media_type="application/vnd.google-earth.kmz", filename=filename)


def _editar_ponto_da_camada(layer_id: str, ponto: Dict[str, Any], generated: bool = False) -> Dict[str, Any]:
    """Grava um ponto no KMZ da camada e regenera o geojson que o mapa le.

    A fonte de verdade continua sendo o KMZ: regerar o geojson dele evita que o
    mapa mostre um ponto que o arquivo baixado nao tem.
    """
    from app.services.kmz_ops import editar_ponto_no_kmz, kmz_to_geojson

    paths_fn = _kmz_generated_layer_paths if generated else _kmz_layer_paths
    kmz_path, geojson_path, meta_path = paths_fn(layer_id)
    if not kmz_path.exists():
        raise HTTPException(404, "Camada KMZ nao encontrada.")

    def _coord(chave: str) -> float | None:
        valor = ponto.get(chave)
        if valor in (None, ""):
            return None
        try:
            return float(valor)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Valor invalido para {chave}.")

    try:
        resultado = editar_ponto_no_kmz(
            kmz_path,
            nome=str(ponto.get("nome") or ""),
            lat=_coord("lat"),
            lon=_coord("lon"),
            descricao=str(ponto.get("descricao") or ""),
            remover=bool(ponto.get("remover")),
            novo_nome=str(ponto.get("novo_nome") or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    geojson_path.write_text(
        json.dumps(kmz_to_geojson(kmz_path), ensure_ascii=False), encoding="utf-8"
    )

    meta = _read_kmz_layer_meta(meta_path)
    meta["id"] = layer_id
    meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_kmz_layer_meta(meta_path, meta)

    # A copia enriquecida foi gerada do KMZ antigo e ficaria desatualizada.
    if not generated:
        _apagar_geradas_do_mapa(layer_id)

    return resultado


@router.patch("/kmz/import/layers/{layer_id}")
async def api_kmz_import_layer_update(layer_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload.get("ponto"), dict):
        return {"ok": True, **_editar_ponto_da_camada(layer_id, payload["ponto"])}
    layer = _rename_kmz_layer(layer_id, str(payload.get("label") or ""))
    return {"ok": True, "layer": layer}


def _apagar_geradas_do_mapa(layer_id: str) -> list[str]:
    """Remove as camadas geradas a partir do mapa importado `layer_id`.

    Sem isso a copia enriquecida fica orfa no disco e reaparece na tela assim
    que o importado que a escondia deixa de existir.
    """
    alvo = str(layer_id or "").strip()
    if not alvo:
        return []
    removidas: list[str] = []
    for meta_path in _kmz_generated_layers_dir().glob("*.meta.json"):
        try:
            meta = _read_kmz_layer_meta(meta_path)
        except Exception:
            continue
        if str(meta.get("source_layer_id") or "").strip() != alvo:
            continue
        gid = str(meta.get("id") or meta_path.stem.replace(".meta", "")).strip()
        k, g, m = _kmz_generated_layer_paths(gid)
        _safe_unlink(k)
        _safe_unlink(g)
        _safe_unlink(m)
        removidas.append(gid)
    return removidas


@router.delete("/kmz/import/layers/{layer_id}")
async def api_kmz_import_layer_delete(layer_id: str) -> Dict[str, Any]:
    kmz_path, geojson_path, meta_path = _kmz_layer_paths(layer_id)
    existed = kmz_path.exists() or geojson_path.exists() or meta_path.exists()
    _safe_unlink(kmz_path)
    _safe_unlink(geojson_path)
    _safe_unlink(meta_path)
    # a copia enriquecida vai junto: se ficar, reaparece na tela sem a origem
    geradas = _apagar_geradas_do_mapa(layer_id)
    return {
        "ok": True,
        "removed": bool(existed),
        "id": layer_id,
        "generated_removed": len(geradas),
        "generated_ids": geradas,
    }


@router.get("/kmz/import/geojson")
async def api_kmz_import_geojson() -> Dict[str, Any]:
    kmz_geojson_path = tenant_kmz_imported_geojson_path() if get_current_tenant_slug() else KMZ_IMPORTED_GEOJSON_PATH
    return read_geojson_file(kmz_geojson_path)


@router.get("/kmz/import/download")
async def api_kmz_import_download() -> FileResponse:
    kmz_imported_path = tenant_kmz_imported_path() if get_current_tenant_slug() else KMZ_IMPORTED_PATH
    if not kmz_imported_path.exists():
        raise HTTPException(404, "Nenhum KMZ importado.")

    filename = kmz_imported_path.name
    meta_path = kmz_imported_path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8") or "{}")
            filename = str(meta.get("original_name") or filename)
        except Exception:
            pass
    return FileResponse(kmz_imported_path, media_type="application/vnd.google-earth.kmz", filename=filename)


@router.post("/kmz/import/locations/apply")
async def api_kmz_import_locations_apply(payload: Dict[str, Any]) -> Dict[str, Any]:
    dry_run = bool(payload.get("dry_run", False))
    overwrite = bool(payload.get("overwrite", False))
    source = str(payload.get("source") or "ip").strip().lower()
    mode = _normalize_inventory_mode(str(payload.get("mode") or ""))
    # Mapa escolhido na tela; sem escolha, o ultimo importado (comportamento
    # antigo). Herdar o ultimo import sem dizer nada foi o que deixou alguem
    # aplicar um mapa achando que era outro.
    layer_id = str(payload.get("layer_id") or "").strip()
    if layer_id:
        _, layer_geojson_path, _ = _kmz_layer_paths(layer_id)
        geojson = read_geojson_file(layer_geojson_path)
        if not geojson:
            raise HTTPException(400, "Mapa nao encontrado. Recarregue a lista de mapas.")
    else:
        kmz_geojson_path = tenant_kmz_imported_geojson_path() if get_current_tenant_slug() else KMZ_IMPORTED_GEOJSON_PATH
        geojson = read_geojson_file(kmz_geojson_path)
    if not geojson:
        raise HTTPException(400, "Nenhum KMZ importado/convertido para aplicar.")

    rows, effective_mode = _load_rows_by_source_mode(source, mode=mode)
    if not rows:
        raise HTTPException(400, "Inventario vazio.")

    rows_for_apply = rows
    dvr_idx_map: dict[int, int] = {}
    if source in ("dvr", "nvr"):
        mapped: list[dict[str, Any]] = []
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            host = str(r.get("host") or "").strip()
            ch = int(r.get("channel") or 0)
            ch_txt = f"{ch:02d}" if ch > 0 else "00"
            title = str(r.get("title") or "").strip() or f"Canal {ch_txt}"
            mapped.append(
                {
                    "__idx": i,
                    "ip": host,
                    "titulo": title,
                    "local": str(r.get("local") or "").strip(),
                    "lat": r.get("lat"),
                    "lon": r.get("lon"),
                }
            )
            dvr_idx_map[len(mapped) - 1] = i
        rows_for_apply = mapped

    try:
        new_rows, summary, no_match_rows = apply_locations_to_inventory(
            rows_for_apply,
            geojson,
            dry_run=dry_run,
            overwrite=overwrite,
            site=str(payload.get("site") or "").strip(),
        )
    except ValueError as exc:
        # Mapa de um site aplicado em outro: erro do usuario, com texto que
        # explica o que fazer -- nao 500.
        raise HTTPException(400, str(exc)) from exc
    if not dry_run:
        if source in ("dvr", "nvr"):
            rows_out = [dict(r) for r in rows]
            for mapped_idx, raw_idx in dvr_idx_map.items():
                if mapped_idx >= len(new_rows) or raw_idx >= len(rows_out):
                    continue
                m = new_rows[mapped_idx] if isinstance(new_rows[mapped_idx], dict) else {}
                if "lat" in m:
                    rows_out[raw_idx]["lat"] = m.get("lat")
                if "lon" in m:
                    rows_out[raw_idx]["lon"] = m.get("lon")
            _save_rows_by_source(source, rows_out)
        else:
            # modo EFETIVO: se as cameras vieram de outro modo, e nele que salva
            _save_rows_by_source(source, new_rows, mode=effective_mode or mode)

    report_path = tenant_locations_apply_report_path() if get_current_tenant_slug() else (DATA_DIR / "input" / "locations_apply_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "summary": summary,
                "no_match_rows": no_match_rows,
                "ts": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # A tela precisa saber DE QUE MAPA e DE QUE SITE ela esta falando antes de
    # deixar alguem clicar em Aplicar -- foi a falta disso que deixou o KMZ de um
    # site carimbar cameras de outro sem ninguem perceber. Na previa vao junto os
    # nomes das cameras sem ponto no mapa: numero solto ("323 sem match") nao
    # ajuda ninguem a agir.
    resposta = {
        "ok": True,
        "source": source,
        "mode": mode if source == "ip" else "",
        "layer": _kmz_layer_ativa(layer_id),
        **summary,
    }
    if dry_run:
        resposta["no_match_rows"] = no_match_rows[:60]
    return resposta


def _kmz_layer_ativa(layer_id: str = "") -> Dict[str, Any]:
    """Nome do KMZ que o Aplicar vai usar: o escolhido, ou o ultimo importado."""
    if layer_id:
        _, _, meta_path = _kmz_layer_paths(layer_id)
    else:
        kmz_imported_path = tenant_kmz_imported_path() if get_current_tenant_slug() else KMZ_IMPORTED_PATH
        meta_path = kmz_imported_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    nome = str(meta.get("original_name") or "").strip()
    return {
        "id": str(meta.get("layer_id") or ""),
        "nome": Path(nome).stem if nome else "",
        "arquivo": nome,
    }


@router.post("/kmz/generate")
async def api_kmz_generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = str(payload.get("source") or "ip").strip().lower()
    mode = _normalize_inventory_mode(str(payload.get("mode") or ""))
    label = str(payload.get("label") or payload.get("name") or "Cameras do Inventario").strip() or "Cameras do Inventario"
    source_layer_id = str(payload.get("layer_id") or payload.get("source_layer_id") or "").strip()
    kmz_imported_path = tenant_kmz_imported_path() if get_current_tenant_slug() else KMZ_IMPORTED_PATH
    kmz_output_dir = tenant_kmz_output_dir() if get_current_tenant_slug() else KMZ_OUTPUT_DIR
    source_kmz_path = kmz_imported_path
    if source_layer_id:
        layer_kmz_path, _, _ = _kmz_layer_paths(source_layer_id)
        if layer_kmz_path.exists():
            source_kmz_path = layer_kmz_path
    if not source_kmz_path.exists():
        raise HTTPException(400, "Importe um KMZ antes de gerar.")
    rows_raw = _load_rows_by_source(source, mode=mode)
    rows = _kmz_rows_for_source(source, rows_raw)
    if not rows:
        raise HTTPException(400, "Inventario vazio.")

    try:
        out_kmz = generate_enriched_kmz(source_kmz_path, rows, kmz_output_dir)
        geojson = kmz_to_geojson(out_kmz)
        (kmz_output_dir / "generated.geojson").write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")

        layer_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe_name(label)}-{secrets.token_hex(3)}"
        layer_kmz_path, layer_geojson_path, layer_meta_path = _kmz_generated_layer_paths(layer_id)
        layer_kmz_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out_kmz, layer_kmz_path)
        layer_geojson_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")
        layer_meta = {
            "id": layer_id,
            "label": label,
            "original_name": f"{_safe_name(label)}.kmz",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "features_count": len(geojson.get("features") or []),
            "source_layer_id": source_layer_id,
            "source": source,
            "mode": mode if source == "ip" else "",
        }
        layer_meta_path.write_text(json.dumps(layer_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        raise HTTPException(500, f"Falha ao gerar KMZ: {exc}")

    return {
        "ok": True,
        "id": layer_id,
        "label": label,
        "source": source,
        "mode": mode if source == "ip" else "",
        "source_layer_id": source_layer_id,
        "features_count": len(geojson.get("features") or []),
        "rows_used": len(rows),
        "path": str(layer_kmz_path),
        "latest": layer_kmz_path.name,
        "latest_url": f"/api/kmz/generated/layers/{layer_id}/download",
    }


@router.get("/kmz/generated/layers")
async def api_kmz_generated_layers(include_features: bool = True) -> Dict[str, Any]:
    layers = _list_kmz_generated_layers(include_features=include_features)
    return {"ok": True, "count": len(layers), "layers": layers}


@router.get("/kmz/generated/layers/{layer_id}/download")
async def api_kmz_generated_layer_download(layer_id: str) -> FileResponse:
    kmz_path, _, meta_path = _kmz_generated_layer_paths(layer_id)
    if not kmz_path.exists():
        raise HTTPException(404, "Camada KMZ gerada nao encontrada.")
    meta = _read_kmz_layer_meta(meta_path)
    filename = str(meta.get("original_name") or kmz_path.name)
    return FileResponse(kmz_path, media_type="application/vnd.google-earth.kmz", filename=filename)


@router.patch("/kmz/generated/layers/{layer_id}")
async def api_kmz_generated_layer_update(layer_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload.get("ponto"), dict):
        return {"ok": True, **_editar_ponto_da_camada(layer_id, payload["ponto"], generated=True)}
    layer = _rename_kmz_layer(layer_id, str(payload.get("label") or ""), generated=True)
    return {"ok": True, "layer": layer}


@router.delete("/kmz/generated/layers/{layer_id}")
async def api_kmz_generated_layer_delete(layer_id: str) -> Dict[str, Any]:
    kmz_path, geojson_path, meta_path = _kmz_generated_layer_paths(layer_id)
    existed = kmz_path.exists() or geojson_path.exists() or meta_path.exists()
    _safe_unlink(kmz_path)
    _safe_unlink(geojson_path)
    _safe_unlink(meta_path)
    return {"ok": True, "removed": bool(existed), "id": layer_id}


@router.get("/kmz/generated/geojson")
async def api_kmz_generated_geojson() -> Dict[str, Any]:
    kmz_output_dir = tenant_kmz_output_dir() if get_current_tenant_slug() else KMZ_OUTPUT_DIR
    return read_geojson_file(kmz_output_dir / "generated.geojson")


@router.get("/kmz/generated/download")
async def api_kmz_generated_download() -> FileResponse:
    kmz_output_dir = tenant_kmz_output_dir() if get_current_tenant_slug() else KMZ_OUTPUT_DIR
    if not kmz_output_dir.exists():
        raise HTTPException(404, "Nenhum KMZ gerado.")
    kmzs = sorted(kmz_output_dir.glob("*.kmz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not kmzs:
        raise HTTPException(404, "Nenhum KMZ gerado.")
    latest = kmzs[0]
    return FileResponse(latest, media_type="application/vnd.google-earth.kmz", filename=latest.name)

@router.get("/geo/camera.kml")
async def api_geo_camera_kml() -> FileResponse:
    kmz_output_dir = tenant_kmz_output_dir() if get_current_tenant_slug() else KMZ_OUTPUT_DIR
    kml = kmz_output_dir / "camera.kml"
    if not kml.exists():
        raise HTTPException(404, "KML nao encontrado.")
    return FileResponse(kml, media_type="application/vnd.google-earth.kml+xml", filename="camera.kml")


# -----------------
# Settings: ImgBB
# -----------------

def _load_app_settings() -> Dict[str, Any]:
    return load_app_settings()


def _save_app_settings(settings: Dict[str, Any]) -> None:
    save_app_settings(settings)


def _mask_key(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 4:
        return "****"
    return "****" + k[-4:]


def _settings_imgbb_key(settings: Dict[str, Any]) -> str:
    return str((settings or {}).get("imgbb_key") or (settings or {}).get("imgbb_api_key") or "").strip()


def _imgbb_validate_key(api_key: str) -> tuple[bool, str]:
    api_key = (api_key or "").strip().strip('"')
    if not api_key:
        return False, "API key vazia"
    try:
        import requests
        tiny_png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/6X7b9kAAAAASUVORK5CYII="
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": api_key, "image": tiny_png_b64, "name": "cam_snapshot_web_test"},
            timeout=15,
        )
        payload = {}
        try:
            payload = r.json()
        except Exception:
            payload = {}
        if r.status_code == 200 and payload.get("success") is True:
            return True, "OK"
        err = (
            payload.get("error", {}).get("message")
            or payload.get("error", {}).get("code")
            or payload.get("message")
            or f"HTTP {r.status_code}"
        )
        return False, str(err)
    except Exception as e:
        return False, str(e)


@router.get("/settings/imgbb")
async def api_imgbb_get_settings() -> Dict[str, Any]:
    s = _load_app_settings()
    key = _settings_imgbb_key(s)
    has_key = bool(key)
    return {
        "ok": True,
        "has_key": has_key,
        "configured": has_key,  # alias esperado pelo frontend atual
        "masked": _mask_key(key) if key else "",
    }


@router.post("/settings/imgbb/test")
async def api_imgbb_test(payload: Dict[str, Any]) -> Dict[str, Any]:
    key = (payload.get("key") or payload.get("api_key") or "").strip()
    ok, msg = _imgbb_validate_key(key)
    return {"ok": ok, "message": msg}


@router.post("/settings/imgbb")
async def api_imgbb_set(payload: Dict[str, Any]) -> Dict[str, Any]:
    key = (payload.get("key") or payload.get("api_key") or "").strip()
    validate = bool(payload.get("validate", False))
    if validate:
        ok, msg = _imgbb_validate_key(key)
        if not ok:
            return {"ok": False, "message": msg, "has_key": False, "configured": False, "masked": ""}

    s = _load_app_settings()
    s["imgbb_key"] = key
    s.pop("imgbb_api_key", None)
    _save_app_settings(s)
    has_key = bool(key)
    return {
        "ok": True,
        "has_key": has_key,
        "configured": has_key,  # alias esperado pelo frontend atual
        "masked": _mask_key(key) if key else "",
    }


# -----------------
# Telegram relay (Zabbix -> backend -> Telegram sendPhoto multipart)
# -----------------
@router.post("/telegram/relay_send")
async def api_telegram_relay_send(payload: Dict[str, Any]) -> Dict[str, Any]:
    token = str(payload.get("token") or "").strip()
    chat_id = str(payload.get("chat_id") or payload.get("chat") or "").strip()
    text = str(payload.get("text") or payload.get("message") or "").strip()
    snapshot_url = str(payload.get("snapshot_url") or "").strip()
    map_url = str(payload.get("map_url") or "").strip()
    parse_mode = str(payload.get("parse_mode") or "HTML").strip() or "HTML"
    relay_key = str(payload.get("relay_key") or "").strip()
    expected_key = str(os.getenv("TELEGRAM_RELAY_KEY") or "").strip()

    if expected_key and relay_key != expected_key:
        raise HTTPException(status_code=403, detail="relay_key invalida")
    if not token:
        raise HTTPException(status_code=400, detail="token obrigatorio")
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id obrigatorio")
    if not snapshot_url:
        raise HTTPException(status_code=400, detail="snapshot_url obrigatorio")

    tg_base = f"https://api.telegram.org/bot{token}"

    low = snapshot_url.lower()
    looks_http = low.startswith("http://") or low.startswith("https://")
    looks_img = bool(re.search(r"(\.jpg|\.jpeg|\.png|\.webp)(\?|$)", low)) or ("i.ibb.co/" in low)

    if not (looks_http and looks_img):
        raise HTTPException(status_code=400, detail="snapshot_url invalido para envio de foto")

    # Envia como multipart (binario) para garantir foto embutida e suportar caption.
    try:
        img = requests.get(
            snapshot_url,
            timeout=25,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": "cam-snapshot-relay/1.0"},
        )
        data = img.content if img.status_code == 200 else b""
        ctype = str(img.headers.get("Content-Type") or "").lower()
        if not data:
            raise HTTPException(status_code=502, detail=f"download status={img.status_code}")
        mime = ctype if ctype.startswith("image/") else "image/jpeg"
        files = {"photo": ("snapshot.jpg", data, mime)}
        form = {"chat_id": chat_id}
        if text:
            caption = text if len(text) <= 1024 else (text[:1000] + "...")
            form["caption"] = caption
            form["parse_mode"] = parse_mode
        if map_url.lower().startswith(("http://", "https://")):
            form["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": "Abrir no Google Maps", "url": map_url}]]},
                ensure_ascii=False,
            )
        s = requests.post(f"{tg_base}/sendPhoto", data=form, files=files, timeout=35)
        sobj = {}
        try:
            sobj = s.json()
        except Exception:
            sobj = {"ok": False, "description": s.text[:500]}
        if s.status_code == 200 and sobj.get("ok"):
            return {"ok": True, "method": "sendPhoto_multipart"}
        raise HTTPException(status_code=502, detail=f"sendPhoto falhou: {str(sobj)[:500]}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="download/sendPhoto exception")
