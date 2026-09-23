from __future__ import annotations

import json
import shutil
import subprocess
import sys
import ipaddress
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

from fastapi import HTTPException

from app.core.paths import BASE_DIR, INVENTORY_JSON_PATH, SAIDA_DIR, TOOLS_DIR, DATA_DIR, ensure_dirs
from app.models.requests import ScanRequest
from app.services.camsnapshot.device_info import get_snapshot
from app.services.camsnapshot.uploader_imgbb import upload_to_imgbb
from app.services.inventory_json import inventory_row_key, load_inventory_json, save_inventory_json
from app.services.photo_store import (
    attach_snapshot_fields,
    resolve_snapshot_file,
    snapshot_filename_from_ip,
    snapshot_storage_dir,
)
from app.services.db_store import load_app_settings, load_olt_cpe_state, load_switch_mac_state
from app.services import connector_routing_vnat as _vnat

DVR_SNAPSHOT_NAME_RE = re.compile(r"^\d{1,3}(?:_\d{1,3}){3}_\d+_ch\d+\.jpg$", re.IGNORECASE)


def _norm_mac(v: Any) -> str:
    raw = str(v or "").strip().lower()
    # normaliza por hex puro para aceitar:
    # aa:bb:cc:dd:ee:ff / aa-bb-cc-dd-ee-ff / aabb.ccdd.eeff / aabbccddeeff
    hex_only = "".join(ch for ch in raw if ch in "0123456789abcdef")
    if len(hex_only) < 12:
        return ""
    hex_only = hex_only[-12:]
    return ":".join(hex_only[i:i + 2] for i in range(0, 12, 2))


def _extract_olt_rows(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        if isinstance(obj.get("cpes"), list):
            return [x for x in obj.get("cpes", []) if isinstance(x, dict)]
        if isinstance(obj.get("items"), list):
            return [x for x in obj.get("items", []) if isinstance(x, dict)]
    return []


def _extract_switch_rows(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        if isinstance(obj.get("rows"), list):
            return [x for x in obj.get("rows", []) if isinstance(x, dict)]
        if isinstance(obj.get("items"), list):
            return [x for x in obj.get("items", []) if isinstance(x, dict)]
    return []


def _enrich_inventory_with_olt(rows: List[Dict[str, Any]], olt_json_path: Path) -> tuple[List[Dict[str, Any]], int]:
    if not rows:
        return rows, 0

    olt_obj: Any = {}
    # DB-first
    try:
        olt_obj = load_olt_cpe_state() or {}
    except Exception:
        olt_obj = {}
    # fallback JSON
    if not isinstance(olt_obj, dict) or not olt_obj:
        if not olt_json_path.exists():
            return rows, 0
        try:
            olt_obj = json.loads(olt_json_path.read_text(encoding="utf-8"))
        except Exception:
            return rows, 0

    olt_rows = _extract_olt_rows(olt_obj)
    if not olt_rows:
        return rows, 0

    olt_by_mac: dict[str, dict[str, Any]] = {}
    for r in olt_rows:
        mac = _norm_mac(r.get("cpe_mac") or r.get("mac") or r.get("MAC"))
        if mac:
            olt_by_mac[mac] = r

    def pick(src: dict[str, Any], *keys: str) -> str:
        for k in keys:
            v = str(src.get(k) or "").strip()
            if v:
                return v
        return ""

    changed = 0
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        mac = _norm_mac(cam.get("mac") or cam.get("MAC"))
        if not mac:
            continue
        g = olt_by_mac.get(mac)
        if not g:
            continue

        updates = {
            "pon": pick(g, "pon", "PON"),
            "onu_id": pick(g, "onu_id", "onuId", "ONU_ID", "onu"),
            "onu_name": pick(g, "onu_name", "onuName", "ONU_NAME"),
            "onu_serial": pick(g, "onu_serial", "serial", "ONU_SERIAL"),
            "onu_model": pick(g, "onu_model", "model", "ONU_MODEL"),
            "olt_ip": pick(g, "olt_ip", "OLT_IP"),
            "olt_name": pick(g, "olt_name", "OLT_NAME"),
            "vlan": pick(g, "vlan", "VLAN"),
        }

        cam_changed = False
        for k, v in updates.items():
            if str(cam.get(k) or "").strip() != v:
                cam[k] = v
                cam_changed = True
        if cam_changed:
            changed += 1

    return rows, changed


def _enrich_inventory_with_switch(rows: List[Dict[str, Any]], switch_json_path: Path) -> tuple[List[Dict[str, Any]], int]:
    if not rows:
        return rows, 0

    switch_obj: Any = {}
    try:
        switch_obj = load_switch_mac_state() or {}
    except Exception:
        switch_obj = {}
    if not isinstance(switch_obj, dict) or not switch_obj:
        if not switch_json_path.exists():
            return rows, 0
        try:
            switch_obj = json.loads(switch_json_path.read_text(encoding="utf-8"))
        except Exception:
            return rows, 0

    switch_rows = _extract_switch_rows(switch_obj)
    if not switch_rows:
        return rows, 0

    switch_by_mac: dict[str, list[dict[str, Any]]] = {}
    port_load: dict[tuple[str, str], int] = {}
    port_vlans: dict[tuple[str, str], set[str]] = {}
    for r in switch_rows:
        switch_ip = str(r.get("switch_ip") or "").strip().lower()
        port = str(r.get("port") or r.get("switch_port") or "").strip().lower()
        if switch_ip and port:
            key = (switch_ip, port)
            port_load[key] = port_load.get(key, 0) + 1
            vlan = str(r.get("vlan") or r.get("switch_vlan") or "").strip()
            if vlan:
                port_vlans.setdefault(key, set()).add(vlan)

    for r in switch_rows:
        mac = _norm_mac(r.get("mac") or r.get("MAC"))
        if mac:
            switch_by_mac.setdefault(mac, []).append(r)

    def pick(src: dict[str, Any], *keys: str) -> str:
        for k in keys:
            v = str(src.get(k) or "").strip()
            if v:
                return v
        return ""

    def _is_uplink(src: dict[str, Any]) -> bool:
        switch_ip = str(src.get("switch_ip") or "").strip().lower()
        port = str(src.get("port") or src.get("switch_port") or "").strip().lower()
        load = port_load.get((switch_ip, port), 999999)
        vlan_count = len(port_vlans.get((switch_ip, port), set()))
        return bool(src.get("is_uplink_candidate")) or vlan_count > 1 or load >= 32

    def pick_best_candidate(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        # Uplink = trafego de varios equipamentos atras do switch (outro
        # segmento), nao a porta fisica real da camera. Se so existe candidato
        # via uplink, nao vale mostrar -- e melhor deixar switch/porta vazio do
        # que dar a falsa impressao de que a camera esta ligada nessa porta.
        candidates = [it for it in items if not _is_uplink(it)]
        if not candidates:
            return None

        def candidate_key(src: dict[str, Any]) -> tuple[int, int, str, str]:
            switch_ip = str(src.get("switch_ip") or "").strip().lower()
            port = str(src.get("port") or src.get("switch_port") or "").strip().lower()
            load = port_load.get((switch_ip, port), 999999)
            vlan_count = len(port_vlans.get((switch_ip, port), set()))
            vlan = str(src.get("vlan") or src.get("switch_vlan") or "").strip()
            return (load, vlan_count, switch_ip, vlan)

        return sorted(candidates, key=candidate_key)[0]

    changed = 0
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        mac = _norm_mac(cam.get("mac") or cam.get("MAC"))
        if not mac:
            continue
        g = pick_best_candidate(switch_by_mac.get(mac) or [])
        if not g:
            continue

        updates = {
            "switch_ip": pick(g, "switch_ip"),
            "switch_name": pick(g, "switch_name"),
            "switch_port": pick(g, "port", "switch_port"),
            "switch_vlan": pick(g, "vlan", "switch_vlan"),
        }
        if not str(cam.get("vlan") or "").strip():
            updates["vlan"] = updates["switch_vlan"]

        cam_changed = False
        for k, v in updates.items():
            if not v:
                continue
            if str(cam.get(k) or "").strip() == "":
                cam[k] = v
                cam_changed = True
        if cam_changed:
            changed += 1

    return rows, changed


def _merge_inventory_rows(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # merge seguro para SaaS: conector+IP quando remoto; legado continua por IP/MAC.
    def norm_mac(m: Any) -> str:
        return _norm_mac(m)

    def norm_ip(i: Any) -> str:
        return str(i or "").strip()

    def norm_site(row: dict[str, Any]) -> str:
        return str(row.get("site") or row.get("site_name") or row.get("local") or "").strip().lower()

    def is_remote(row: dict[str, Any]) -> bool:
        return bool(str(row.get("remote_connector_id") or row.get("connector_id") or "").strip())

    # Nome de fabrica nao e nome: a Hikvision entrega "IP CAMERA" e a Intelbras
    # variantes parecidas. Sem isso aqui, cada varredura lia o nome de fabrica
    # do equipamento e sobrescrevia o nome que o usuario cadastrou -- o trabalho
    # de nomear 48 cameras sumia no scan seguinte.
    TITULOS_DE_FABRICA = {
        "IP CAMERA", "IPCAMERA", "IP-CAMERA", "CAMERA", "NETWORK CAMERA",
        "IPC", "DVR", "NVR", "SEM NOME", "NO NAME",
    }

    def is_placeholder_title(row: dict[str, Any], value: Any) -> bool:
        title = str(value or "").strip()
        if not title:
            return True
        ip = norm_ip(row.get("ip") or row.get("IP") or row.get("host"))
        if ip and title == ip:
            return True
        return title.upper() in TITULOS_DE_FABRICA

    merged: list[dict[str, Any]] = [dict(r) for r in (old_rows or [])]

    def find_match(nr: dict[str, Any]) -> int | None:
        nip = norm_ip(nr.get("ip") or nr.get("IP"))
        nmac = norm_mac(nr.get("mac") or nr.get("MAC"))
        nsite = norm_site(nr)

        def site_conflita(r: dict[str, Any]) -> bool:
            # Faixa privada se repete entre sites. Se as duas linhas dizem de
            # que site sao e os sites diferem, NAO e a mesma camera.
            rsite = norm_site(r)
            return bool(nsite and rsite and rsite != nsite)

        nkey = inventory_row_key(nr)
        for i, r in enumerate(merged):
            if site_conflita(r):
                continue
            if inventory_row_key(r) == nkey:
                return i

        # Linhas remotas nunca casam somente por IP/MAC: redes privadas se repetem
        # entre clientes e sites. O unico match aceito e site+IP, que ja identifica
        # o local; sem site, a linha e sempre nova.
        if is_remote(nr):
            if nip and nsite:
                for i, r in enumerate(merged):
                    if norm_site(r) == nsite and norm_ip(r.get("ip") or r.get("IP")) == nip:
                        return i
            return None

        # 1) IP+MAC
        for i, r in enumerate(merged):
            if site_conflita(r):
                continue
            if norm_ip(r.get("ip") or r.get("IP")) == nip and norm_mac(r.get("mac") or r.get("MAC")) == nmac and (nip or nmac):
                return i
        # 2) IP apenas
        if nip:
            for i, r in enumerate(merged):
                if site_conflita(r):
                    continue
                if norm_ip(r.get("ip") or r.get("IP")) == nip:
                    return i
        # 3) MAC apenas
        if nmac:
            for i, r in enumerate(merged):
                if site_conflita(r):
                    continue
                if norm_mac(r.get("mac") or r.get("MAC")) == nmac:
                    return i
        return None

    for nr in new_rows or []:
        if not isinstance(nr, dict):
            continue
        j = find_match(nr)
        if j is None:
            merged.append(dict(nr))
        else:
            # novos valores sobrescrevem e mantem extras antigos
            for k, v in nr.items():
                if v is None:
                    continue
                s = str(v or "").strip()
                if not s:
                    continue
                if k in {"status", "health", "remote", "remote_connector_id", "remote_connector_name", "site", "site_name", "local"}:
                    merged[j][k] = v
                    continue
                if k in {"titulo", "title"} and is_placeholder_title(nr, v) and not is_placeholder_title(merged[j], merged[j].get("titulo") or merged[j].get("title")):
                    continue
                if is_remote(nr) and str(merged[j].get(k) or "").strip() and k not in {"snapshot_url", "imgbb_url", "imgbb_thumb_url"}:
                    continue
                merged[j][k] = v
            if str(nr.get("status") or "").strip().lower() == "online":
                merged[j].pop("error", None)

    return merged


def _capture_snapshots(
    rows: List[Dict[str, Any]],
    user: str,
    password: str,
    only_ips: set[str] | None = None,
) -> List[Dict[str, Any]]:
    if not rows:
        return rows

    snap_dir = snapshot_storage_dir()

    def _one(row: Dict[str, Any]) -> tuple[str, str | None]:
        ip = str(row.get("ip") or row.get("IP") or "").strip()
        if not ip:
            return "", None
        try:
            out = get_snapshot(ip, user, password, output_dir=str(snap_dir))
            return ip, str(out) if out else None
        except Exception:
            return ip, None

    by_ip: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        ip = str(r.get("ip") or r.get("IP") or "").strip()
        if only_ips is not None and ip not in only_ips:
            continue
        status = str(r.get("status") or "").strip().lower()
        # Evita bater credencial/snapshot em massa para offline/auth_failed.
        # Mantemos tentativa apenas para online (ou status vazio legado).
        if ip and (status in ("", "online")):
            by_ip[ip] = r

    max_workers = min(8, max(1, len(by_ip)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_one, row) for row in by_ip.values()]
        for fut in as_completed(futures):
            ip, saved = fut.result()
            if not ip:
                continue

            row = by_ip.get(ip)
            if not row:
                continue

            final_name = snapshot_filename_from_ip(ip)
            final_path = snap_dir / final_name

            legacy_path: Path | None = None
            try:
                if saved and str(saved).strip():
                    legacy_path = Path(str(saved))
                else:
                    legacy_path = snap_dir / f"{ip}.jpg"
            except Exception:
                legacy_path = None

            try:
                if legacy_path and legacy_path.exists() and legacy_path.is_file():
                    same_target = False
                    try:
                        same_target = legacy_path.resolve() == final_path.resolve()
                    except Exception:
                        same_target = str(legacy_path) == str(final_path)

                    if not same_target:
                        if final_path.exists():
                            try:
                                final_path.unlink()
                            except Exception:
                                pass
                        shutil.move(str(legacy_path), str(final_path))
            except Exception:
                pass

            if final_path.exists() and final_path.is_file():
                attach_snapshot_fields(row, ip, final_name)

    return rows


def _apply_default_local(
    rows: List[Dict[str, Any]],
    local_value: str,
    only_ips: set[str] | None = None,
) -> tuple[List[Dict[str, Any]], int]:
    if not rows:
        return rows, 0

    local_norm = str(local_value or "").strip()
    if not local_norm:
        return rows, 0

    changed = 0
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        # So carimba o site nas linhas que ESTA varredura achou.
        if only_ips is not None:
            ip = str(cam.get("ip") or cam.get("IP") or "").strip()
            if ip not in only_ips:
                continue
        current = str(cam.get("local") or cam.get("LOCAL") or "").strip()
        if current:
            continue
        cam["local"] = local_norm
        changed += 1

    return rows, changed


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


def _resolve_snapshot_path(cam: Dict[str, Any]) -> Path | None:
    ip = str(cam.get("ip") or cam.get("IP") or "").strip()
    path_hint = (
        str(cam.get("snapshot_path") or cam.get("snapshotPath") or "").strip()
        or str(cam.get("snapshot_url") or "").strip()
        or str(cam.get("thumb_url") or "").strip()
    )
    return resolve_snapshot_file(path_hint=path_hint, ip=ip)


def _upload_imgbb_for_inventory(
    rows: List[Dict[str, Any]],
    only_ips: set[str] | None = None,
) -> tuple[List[Dict[str, Any]], int, str]:
    if not rows:
        return rows, 0, ""

    api_key = _load_imgbb_key()
    if not api_key:
        return rows, 0, "ImgBB API key nao configurada."

    paths: list[Path] = []
    name_map: dict[str, str] = {}
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        if str(cam.get("source") or "").strip().lower() == "dvr":
            continue
        ip = str(cam.get("ip") or cam.get("IP") or "").strip()
        if not ip:
            continue
        if only_ips is not None and ip not in only_ips:
            continue
        try:
            ipaddress.ip_address(ip)
        except Exception:
            continue
        p = _resolve_snapshot_path(cam)
        if p is None:
            continue

        p_norm = str(p).replace("\\", "/").lower()
        if "/dvr_snapshot/" in p_norm:
            continue

        # Isola o fluxo IP: só aceita snapshot canônico derivado do IP. Em
        # conector isolado a captura fala com a câmera pelo IP VIRTUAL (vnat) e
        # grava o arquivo com esse nome (10_210_66_51.jpg), enquanto o
        # inventário guarda o IP real (192.168.18.51) -- por isso o nome do
        # virtual também vale, senão nenhuma foto de cliente isolado subia
        # ("Nenhum snapshot local encontrado para upload").
        expected = {snapshot_filename_from_ip(ip).lower()}
        cid = str(cam.get("remote_connector_id") or cam.get("connector_id") or "").strip()
        if cid:
            try:
                virt = _vnat.virtual_ip_for(cid, ip)
            except Exception:
                virt = ""
            if virt:
                expected.add(snapshot_filename_from_ip(virt).lower())
        if p.name.lower() not in expected:
            continue

        paths.append(p)
        name_map[str(p)] = ip

    uniq_files: list[str] = []
    seen: set[str] = set()
    for p in paths:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        uniq_files.append(s)

    if not uniq_files:
        return rows, 0, "Nenhum snapshot local encontrado para upload."

    try:
        uploads = upload_to_imgbb(uniq_files, api_key=api_key, name_prefix="cam", name_map=name_map)
    except Exception as e:
        return rows, 0, str(e)
    if not uploads:
        return rows, 0, "ImgBB nao retornou URL para os snapshots enviados."

    by_file: dict[str, dict[str, Any]] = {}
    for u in uploads or []:
        file_path = str(u.get("file") or "").strip()
        if file_path:
            by_file[file_path] = u

    changed = 0
    for cam in rows:
        if not isinstance(cam, dict):
            continue
        p = _resolve_snapshot_path(cam)
        if p is None:
            continue
        u = by_file.get(str(p))
        if not u:
            continue

        url = str(u.get("url") or "").strip()
        thumb = str(u.get("thumbnail_url") or url).strip()
        if not url:
            continue

        before_url = str(cam.get("imgbb_url") or "").strip()
        before_thumb = str(cam.get("imgbb_thumb_url") or "").strip()
        cam_changed = False
        if before_url != url:
            cam["imgbb_url"] = url
            cam_changed = True
        if thumb and before_thumb != thumb:
            cam["imgbb_thumb_url"] = thumb
            cam_changed = True
        if cam_changed:
            changed += 1

    err = ""
    if changed < len(uniq_files):
        err = f"{changed}/{len(uniq_files)} snapshots receberam URL do ImgBB."
    return rows, changed, err


def _cleanup_misplaced_dvr_snapshots() -> int:
    snap_dir = DATA_DIR / "snapshot"
    dvr_dir = DATA_DIR / "dvr_snapshot"
    if not snap_dir.exists():
        return 0
    dvr_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for p in snap_dir.glob("*.jpg"):
        if not DVR_SNAPSHOT_NAME_RE.match(p.name):
            continue
        dst = dvr_dir / p.name
        try:
            if dst.exists():
                try:
                    same_size = dst.stat().st_size == p.stat().st_size
                except Exception:
                    same_size = False
                if same_size:
                    p.unlink(missing_ok=True)
                    moved += 1
                    continue
                p.unlink(missing_ok=True)
                moved += 1
                continue
            shutil.move(str(p), str(dst))
            moved += 1
        except Exception:
            continue
    return moved


def _generate_inventory_xlsx() -> tuple[bool, str]:
    script = TOOLS_DIR / "json_to_xlsx.py"
    if not script.exists():
        return False, "json_to_xlsx.py nao encontrado em tools/"

    if not INVENTORY_JSON_PATH.exists():
        return False, "Inventario JSON nao encontrado para gerar XLSX."

    out_xlsx = SAIDA_DIR / "cam-inventory.xlsx"
    cmd = [
        sys.executable,
        str(script),
        "--json",
        str(INVENTORY_JSON_PATH),
        "--xlsx",
        str(out_xlsx),
    ]
    proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        return False, (msg[:1000] or f"Falha ao gerar XLSX (rc={proc.returncode}).")
    if not out_xlsx.exists():
        return False, "XLSX nao foi criado."
    return True, ""


def run_http_scan(req: ScanRequest) -> Dict[str, Any]:
    """HTTP /api/scan - compat legado, sem quebrar o front.
    Refatoracao incremental: movemos para service para router dedicado.
    """
    ensure_dirs()
    SAIDA_DIR.mkdir(parents=True, exist_ok=True)
    dvr_snapshots_moved = _cleanup_misplaced_dvr_snapshots()

    mode = "scan"
    if getattr(req, "reuse_inventory", False) and not (getattr(req, "alvo", "") or "").strip():
        mode = "update"

    alvo = (getattr(req, "alvo", "") or "").strip()
    if mode == "scan" and (not alvo) and (not getattr(req, "reuse_inventory", False)):
        raise HTTPException(400, "Campo 'alvo' e obrigatorio.")

    inventory_mode_raw = str(getattr(req, "inventory_mode", "olt") or "olt").strip().lower()
    if inventory_mode_raw in {"switch", "sw", "via_switch", "via-switch"}:
        inventory_mode = "switch"
        inv_json = DATA_DIR / "cam-inventory-switch.json"
    elif inventory_mode_raw in {"basic", "basico", "básico", "base"}:
        inventory_mode = "basic"
        inv_json = DATA_DIR / "cam-inventory-basic.json"
    else:
        inventory_mode = "olt"
        inv_json = INVENTORY_JSON_PATH

    inv_cmd: list[str] | None = None
    current_scan_ips: set[str] | None = None
    discovered_count = 0
    if mode == "scan":
        old_rows_for_merge: list[dict[str, Any]] = []
        tmp_out: Path | None = None

        try:
            loaded = load_inventory_json(mode=inventory_mode) or []
            if isinstance(loaded, list):
                old_rows_for_merge = loaded
        except Exception:
            old_rows_for_merge = []

        # Comportamento seguro: nova varredura nao apaga historico por padrao.
        # O inventario anterior e mesclado com os novos resultados.
        # Para apagar tudo, use o endpoint/botao "Apagar inventario".
        should_merge = bool(old_rows_for_merge)

        out_path = inv_json
        if should_merge:
            tmp_out = inv_json.with_suffix(".tmp.json")
            out_path = tmp_out

        script = TOOLS_DIR / "inventory_dry.py"
        if not script.exists():
            raise HTTPException(500, "inventory_dry.py n??o encontrado em tools/")

        inv_cmd = [
            sys.executable,
            str(script),
            "--alvo",
            alvo,
            "--usuario",
            getattr(req, "usuario", "admin") or "admin",
            "--senha",
            getattr(req, "senha", "admin") or "admin",
            "--out",
            str(out_path),
        ]

        inv_proc = subprocess.run(inv_cmd, cwd=str(BASE_DIR), capture_output=True, text=True, check=False)

        if inv_proc.returncode != 0:
            msg = inv_proc.stderr.strip() or inv_proc.stdout.strip() or "falha desconhecida"
            raise HTTPException(500, f"inventory_dry.py retornou c??digo {inv_proc.returncode}: {msg[:4000]}")

        try:
            scan_out_path = tmp_out if should_merge and tmp_out is not None else out_path
            new_rows: list[dict[str, Any]] = []
            if scan_out_path.exists():
                with scan_out_path.open("r", encoding="utf-8") as f:
                    loaded_rows = json.load(f)
                if isinstance(loaded_rows, list):
                    new_rows = [r for r in loaded_rows if isinstance(r, dict)]

            current_scan_ips = {
                str((r or {}).get("ip") or (r or {}).get("IP") or "").strip()
                for r in new_rows
                if isinstance(r, dict) and str((r or {}).get("ip") or (r or {}).get("IP") or "").strip()
            }
            discovered_count = len(current_scan_ips) or len(new_rows)


            if should_merge:
                new_rows = _merge_inventory_rows(old_rows_for_merge, new_rows)

            # Sempre passa pelo serviço de inventário para manter JSON e json_state
            # sincronizados, inclusive depois de "Apagar inventário".
            save_inventory_json(new_rows, mode=inventory_mode)
        finally:
            try:
                if tmp_out is not None and tmp_out.exists():
                    tmp_out.unlink()
            except Exception:
                pass

    # ====== Etapas opcionais via tools (compat) ======
    # A maior parte dessas etapas ja existe no legado e continua em main.py para os outros endpoints.
    # No v3 mantemos o /api/scan funcionando e retornando inventario atualizado.
    inventory_rows = load_inventory_json(mode=inventory_mode)
    local_applied = 0
    if mode == "scan" and bool(getattr(req, "set_local", False)) and inventory_rows:
        inventory_rows, local_applied = _apply_default_local(
            inventory_rows, getattr(req, "local", ""), only_ips=current_scan_ips
        )
        if local_applied > 0:
            save_inventory_json(inventory_rows, mode=inventory_mode)

    olt_enriched = 0
    if getattr(req, "olt_enrich", False) and inventory_rows:
        inventory_rows, olt_enriched = _enrich_inventory_with_olt(inventory_rows, SAIDA_DIR / "olt-cpe-macs.json")
        if olt_enriched > 0:
            save_inventory_json(inventory_rows, mode=inventory_mode)
    switch_enriched = 0
    if getattr(req, "switch_enrich", False) and inventory_rows:
        inventory_rows, switch_enriched = _enrich_inventory_with_switch(inventory_rows, DATA_DIR / "switch-mac-table.json")
        if switch_enriched > 0:
            save_inventory_json(inventory_rows, mode=inventory_mode)
    do_snapshot = bool(getattr(req, "capture_snapshot", False) or getattr(req, "snapshot", False))
    if do_snapshot and inventory_rows:
        inventory_rows = _capture_snapshots(
            inventory_rows,
            getattr(req, "usuario", "admin") or "admin",
            getattr(req, "senha", "admin") or "admin",
            only_ips=current_scan_ips if mode == "scan" else None,
        )
        save_inventory_json(inventory_rows, mode=inventory_mode)

    imgbb_uploaded = 0
    imgbb_error = ""
    if bool(getattr(req, "imgbb", False)) and inventory_rows:
        inventory_rows, imgbb_uploaded, imgbb_error = _upload_imgbb_for_inventory(
            inventory_rows,
            only_ips=current_scan_ips if mode == "scan" else None,
        )
        if imgbb_uploaded > 0:
            save_inventory_json(inventory_rows, mode=inventory_mode)

    excel_generated = False
    excel_error = ""
    if bool(getattr(req, "excel", False)):
        if inventory_rows:
            excel_generated, excel_error = _generate_inventory_xlsx()
        else:
            excel_error = "Inventario vazio; XLSX nao gerado."

    auth_failed_count = 0
    online_count = 0
    for r in inventory_rows or []:
        st = str((r or {}).get("status") or "").strip().lower()
        if st == "auth_failed":
            auth_failed_count += 1
        if st == "online":
            online_count += 1

    auth_warning = ""
    if auth_failed_count > 0 and online_count == 0:
        auth_warning = "Credencial rejeitada para os dispositivos ativos. Ajuste usuario/senha e rode novamente."

    def _row_ip(row: dict[str, Any]) -> str:
        return str((row or {}).get("ip") or (row or {}).get("IP") or "").strip()

    def _has_snapshot(row: dict[str, Any]) -> bool:
        return any(str((row or {}).get(k) or "").strip() for k in ("snapshot_url", "thumb_url", "snapshot_path", "snapshot_file"))

    scoped_rows = [r for r in inventory_rows or [] if isinstance(r, dict)]
    if current_scan_ips:
        scoped_rows = [r for r in scoped_rows if _row_ip(r) in current_scan_ips]
    snapshot_count = sum(1 for r in scoped_rows if _has_snapshot(r))
    inventory_count = len(inventory_rows or [])
    if mode == "update":
        discovered_count = inventory_count

    errors: list[str] = []
    if imgbb_error:
        errors.append(f"ImgBB: {imgbb_error}")
    if excel_error:
        errors.append(f"Excel: {excel_error}")
    if auth_warning:
        errors.append(auth_warning)

    return {
        "ok": True,
        "success": True,
        "mode": mode,
        "cmd": " ".join(inv_cmd) if inv_cmd else "",
        "target": alvo,
        "inventory_count": inventory_count,
        "discovered_count": discovered_count,
        "snapshot_count": snapshot_count,
        "errors": errors,
        "local_applied": local_applied,
        "olt_enriched": olt_enriched,
        "switch_enriched": switch_enriched,
        "imgbb_uploaded": imgbb_uploaded,
        "imgbb_error": imgbb_error,
        "excel_generated": excel_generated,
        "excel_error": excel_error,
        "auth_failed_count": auth_failed_count,
        "auth_warning": auth_warning,
        "dvr_snapshots_moved": dvr_snapshots_moved,
        "inventory": inventory_rows,
    }

