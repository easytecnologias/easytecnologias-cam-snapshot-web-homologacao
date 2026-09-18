from __future__ import annotations

import json
import ipaddress
import time
from typing import Any, Dict

import anyio
from fastapi import WebSocket

from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
from app.models.requests import ScanRequest
from app.services.connector_service import (
    create_job,
    get_connector,
    list_connectors,
    list_jobs,
    register_connector_known_targets,
)
from app.services.inventory_json import inventory_row_key, load_inventory_json, save_inventory_json
from app.services.scan_service import run_http_scan
from app.services import connector_routing_vnat as _vnat


async def _ws_send(ws: WebSocket, obj: Dict[str, Any]) -> None:
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


def _devirtualize_inventory(connector_id: str, inventory_mode: str) -> int:
    """Reescreve IPs virtuais (10.208.x, do modelo A) de volta pra reais no
    inventario deste conector -- a varredura direta proba o virtual mas o
    operador tem que ver o IP REAL. Fora do range virtual -> nao mexe."""
    if not _vnat.has_mapping(connector_id):
        return 0
    raw = str(inventory_mode or "olt").strip().lower()
    if raw in {"switch", "sw", "via_switch", "via-switch"}:
        inventory_mode = "switch"
    elif raw in {"basic", "basico", "básico", "base"}:
        inventory_mode = "basic"
    else:
        inventory_mode = "olt"
    rows = load_inventory_json(mode=inventory_mode) or []
    if not isinstance(rows, list):
        return 0
    changed = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = "ip" if r.get("ip") is not None else ("IP" if r.get("IP") is not None else "ip")
        ip = str(r.get(key) or "").strip()
        if not ip:
            continue
        real = _vnat.real_ip_for(connector_id, ip)
        if real and real != ip:
            r[key] = real
            r.setdefault("remote_connector_id", connector_id)
            changed += 1
    if changed:
        save_inventory_json(rows, mode=inventory_mode)
    return rows  # devolve as linhas REAIS pro chamador refletir no result da WS


def _run_scan_in_tenant(req: ScanRequest, tenant_slug: str = "", devirtualize_connector: str = "") -> Dict[str, Any]:
    ctx = set_current_tenant_slug(tenant_slug)
    try:
        result = run_http_scan(req)
        if devirtualize_connector:
            rows = _devirtualize_inventory(devirtualize_connector, str(getattr(req, "inventory_mode", "olt") or "olt"))
            # O result devolvido pela WS tem que carregar os IPs REAIS -- o front
            # usa result["inventory"] pra pingar/capturar; com o IP virtual dava 404.
            if isinstance(rows, list):
                result["inventory"] = rows
                result["inventory_count"] = len(rows)
        return result
    finally:
        reset_current_tenant_slug(ctx)


def _expand_remote_targets(raw: str, limit: int = 1024) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace("\n", ",").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            if "/" in item:
                net = ipaddress.ip_network(item, strict=False)
                for ip in net.hosts():
                    out.append(str(ip))
                    if len(out) >= limit:
                        return list(dict.fromkeys(out))
                continue
            if "-" in item:
                left, right = [p.strip() for p in item.split("-", 1)]
                start = ipaddress.ip_address(left)
                end = ipaddress.ip_address(right if "." in right else f"{left.rsplit('.', 1)[0]}.{right}")
                first, last = sorted((int(start), int(end)))
                for value in range(first, last + 1):
                    out.append(str(ipaddress.ip_address(value)))
                    if len(out) >= limit:
                        return list(dict.fromkeys(out))
                continue
            ipaddress.ip_address(item)
            out.append(item)
        except Exception:
            if all(ch.isalnum() or ch in ".:-_" for ch in item):
                out.append(item)
        if len(out) >= limit:
            return list(dict.fromkeys(out))
    return list(dict.fromkeys(out))


def _targets_for_connector_scan(raw: str, connector: dict[str, Any] | None, limit: int = 1024) -> list[str]:
    targets = _expand_remote_targets(raw, limit=limit)
    if targets or not connector:
        return targets

    values: list[Any] = []
    inventory = connector.get("inventory") if isinstance(connector.get("inventory"), dict) else {}
    tunnel = connector.get("tunnel") if isinstance(connector.get("tunnel"), dict) else {}
    host = connector.get("host") if isinstance(connector.get("host"), dict) else {}
    values.extend(tunnel.get("client_lans") or [])
    values.extend(str(inventory.get("lan_networks") or "").replace(";", ",").split(","))
    values.extend(str(host.get("lan_networks") or "").replace(";", ",").split(","))

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw_cidr = str(value or "").strip()
        if not raw_cidr:
            continue
        try:
            net = ipaddress.ip_network(raw_cidr, strict=False)
        except Exception:
            continue
        if net.version != 4 or net.prefixlen >= 32:
            continue
        if str(net) == "10.250.0.0/24":
            continue
        if not (net.is_private or net.subnet_of(ipaddress.ip_network("100.64.0.0/10"))):
            continue
        for ip in net.hosts():
            text = str(ip)
            if text in seen:
                continue
            out.append(text)
            seen.add(text)
            if len(out) >= limit:
                return out
    return out


def _connector_for_site(site: str) -> dict[str, Any] | None:
    wanted = str(site or "").strip().lower()
    if not wanted:
        return None
    matches: list[dict[str, Any]] = []
    for row in list_connectors().get("connectors") or []:
        if str(row.get("type") or "").lower() != "routeros":
            continue
        keys = [
            str(row.get("site") or "").strip().lower(),
            str(row.get("name") or "").strip().lower(),
            str(row.get("client") or "").strip().lower(),
        ]
        if wanted in keys:
            matches.append(row)
    online = [row for row in matches if row.get("status") == "online"]
    return (online or matches or [None])[0]


def _connector_from_payload(payload: Dict[str, Any]) -> dict[str, Any] | None:
    connector_id = str(payload.get("connector_id") or payload.get("remote_connector_id") or "").strip()
    if connector_id:
        return get_connector(connector_id, include_token=False, enforce_tenant=True)
    return _connector_for_site(str(payload.get("local") or "").strip())


def _connector_has_tunnel(connector: dict[str, Any] | None) -> bool:
    if not connector:
        return False
    # MikroTik reporta o tunel via tunnel/vpn/wireguard.enabled (populado
    # pelo agente RouterOS). O Ruijie nao tem agente -- o tunel OpenVPN
    # persistente e gerenciado fora do processo, por
    # scripts/sightops_ruijie_vpn_sync.py; o sinal disponivel aqui e ter
    # vpn_config salvo no cadastro.
    if str(connector.get("type") or "").strip().lower() == "ruijie":
        return bool(str(connector.get("vpn_config") or "").strip())
    tunnel = connector.get("tunnel") if isinstance(connector, dict) else None
    if isinstance(tunnel, dict) and tunnel.get("enabled"):
        return True
    vpn = connector.get("vpn") if isinstance(connector, dict) else None
    if isinstance(vpn, dict) and vpn.get("enabled"):
        return True
    wireguard = connector.get("wireguard") if isinstance(connector, dict) else None
    return isinstance(wireguard, dict) and bool(wireguard.get("enabled"))


def _pick_probe_targets(alvo: str, sample: int = 24) -> list[str]:
    """Amostra pequena, mas nao so os primeiros IPs da faixa.

    Em ranges reais de CFTV, os primeiros enderecos costumam ser gateway,
    NVR ou buracos. Se a sondagem olhar so .1/.2/.3, uma rede acessivel pode
    parecer indisponivel e o scan cai para o modo limitado via MikroTik.
    """
    targets = _expand_remote_targets(alvo, limit=1024)
    if len(targets) <= sample:
        return targets

    head_count = min(12, sample)
    picked = targets[:head_count]
    remaining = sample - len(picked)
    if remaining <= 0:
        return list(dict.fromkeys(picked))

    span = len(targets) - head_count
    for i in range(1, remaining + 1):
        idx = head_count + round((span - 1) * i / remaining)
        if 0 <= idx < len(targets):
            picked.append(targets[idx])
    return list(dict.fromkeys(picked))[:sample]


def _lan_reachable(targets: list[str], port: int | list[int] | tuple[int, ...] = 80, timeout: float = 1.2) -> bool:
    """Tenta uma conexao TCP rapida num punhado de alvos da rede do cliente.

    Existe porque "conector com VPN" nao significa "servidor consegue falar
    com a LAN do cliente" -- so significa que a VPN alcanca o MikroTik. Se
    alguem cadastrar a rede da camera em client_lans e isso for aplicado de
    verdade (ver scripts/sightops_wireguard_sync.py), o servidor passa a ter
    rota real e o scan local funciona direto, rapido e com snapshot -- foi
    assim que funcionou pra Incoforte. Sem essa rota, o scan local marcaria
    tudo como offline silenciosamente; o caminho seguro nesse caso e perguntar
    ao MikroTik (via ping_many), que sempre alcanca a propria LAN.

    Em vez de assumir um dos dois casos por regra fixa, testamos: se a rede
    tem rota de verdade, ate um host desligado responde RST rapido em alguma
    porta comum -- e ISSO (a resposta, nao a conexao completa) e o sinal de
    que o caminho existe. Timeout curto (poucos segundos no total) porque o
    unico custo de errar pra "sem rota" e cair no caminho mais lento, nunca
    quebrar o scan.
    """
    import socket
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ports = list(port) if isinstance(port, (list, tuple)) else [int(port)]
    extra_ports = [443, 554, 8080, 8000, 8081]
    for p in extra_ports:
        if p not in ports:
            ports.append(p)

    def can_connect(ip: str, tcp_port: int) -> bool:
        try:
            with socket.create_connection((ip, tcp_port), timeout=timeout):
                return True
        except ConnectionRefusedError:
            return True
        except OSError:
            return False

    jobs = [(ip, tcp_port) for ip in targets for tcp_port in ports]
    if not jobs:
        return False
    workers = min(48, len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(can_connect, ip, tcp_port) for ip, tcp_port in jobs]
        for future in as_completed(futures):
            if future.result():
                for pending in futures:
                    pending.cancel()
                return True
    return False


def _decide_remote_only(
    *,
    scan_origin: str,
    connector_id: str,
    connector_has_tunnel: bool,
    remote_only_requested: bool,
    probe_targets: list[str],
    probe_fn=_lan_reachable,
) -> bool:
    """Decide entre o caminho MikroTik (so ping, sem snapshot) e o caminho
    local direto (rapido, com snapshot -- o que funcionou pra Incoforte).

    So chama `probe_fn` (I/O real de rede) quando as regras anteriores nao ja
    decidiram sozinhas -- e por isso da pra testar sem rede nenhuma: injete um
    probe_fn falso e nenhum socket e aberto.

    Antes so sondava em inventory_mode=="olt" -- varreduras basico/switch
    (o caminho mais comum, usado pra cameras) confiavam cegamente que "tunel
    configurado" significava "rota ate a LAN do cliente ja aplicada", e
    voltavam vazias em silencio quando isso nao era verdade ainda (ex:
    conector recem-criado, scripts/sightops_wireguard_sync.py so aplica a
    rota depois de rodar, ate 60s). A sondagem e barata (poucos alvos, timeout
    curto) e vale pra qualquer modo, entao passou a valer sempre.
    """
    if scan_origin == "connector" and not connector_has_tunnel:
        return True
    if connector_id and connector_has_tunnel and probe_targets:
        return not bool(probe_fn(probe_targets))
    if connector_id and remote_only_requested:
        return True
    return False


def _parse_routeros_ping(result: Any) -> dict[str, bool]:
    text = str((result or {}).get("routeros_ping") or "") if isinstance(result, dict) else str(result or "")
    parsed: dict[str, bool] = {}
    for item in text.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        sep = ":" if ":" in item else "="
        target, ok = (item.split(sep, 1) + [""])[:2]
        target = target.strip()
        if target:
            parsed[target] = ok.strip().lower() in {"1", "true", "ok", "online"}
    return parsed


async def ping_via_connector(connector_id: str, ip: str, timeout_s: float = 45.0) -> bool | None:
    """Pinga um unico IP atraves do job ping_many de um conector MikroTik.

    Usado como fallback pro "Testar ping" de uma camera remota (atras de um
    conector, sem rota direta do servidor ate a LAN do cliente) -- mesmo
    primitivo que `_remote_inventory_via_connector` usa pra varredura, so que
    pra um alvo so. Mais lento que ping direto (depende do ciclo de polling
    do agente RouterOS, ate ~1min), por isso NAO substitui o ping direto --
    so entra quando ele falhar.

    Retorna True/False se o conector respondeu a tempo, None se nao
    respondeu dentro do timeout (job nunca saiu de "queued"/"running") --
    nesse caso quem chamou deve tratar como "nao foi possivel confirmar",
    nao como "offline".
    """
    target = str(ip or "").strip()
    connector_id = str(connector_id or "").strip()
    if not connector_id or not target:
        return None
    job = create_job({"connector_id": connector_id, "type": "ping_many", "payload": {"targets": [target]}}).get("job") or {}
    job_id = str(job.get("id") or "")
    if not job_id:
        return None
    deadline = time.time() + timeout_s
    final_job: dict[str, Any] | None = None
    while time.time() < deadline:
        await anyio.sleep(2)
        jobs = list_jobs(connector_id).get("jobs") or []
        final_job = next((item for item in jobs if str(item.get("id") or "") == job_id), None)
        if final_job and final_job.get("status") in {"done", "failed"}:
            break
    if not final_job or final_job.get("status") != "done":
        return None
    return bool(_parse_routeros_ping(final_job.get("result") or {}).get(target))


def _tag_rows_for_connector(payload: Dict[str, Any], result: Dict[str, Any], tenant_slug: str = "") -> Dict[str, Any]:
    site = str(payload.get("local") or "").strip()
    connector = _connector_from_payload(payload)
    if not connector:
        return result
    targets = set(_expand_remote_targets(str(payload.get("alvo") or "")))
    if not targets:
        return result
    mode = str(payload.get("inventory_mode") or "olt").strip().lower() or "olt"
    ctx = set_current_tenant_slug(str(tenant_slug or "").strip().lower())
    try:
        rows = load_inventory_json(mode=mode) or []
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            ip = str(row.get("ip") or row.get("IP") or "").strip()
            if ip not in targets:
                continue
            # Nao re-etiquetar linha que ja pertence a OUTRO conector -- senao um
            # scan de Porto Real re-tagueia o .201 da Mata Grande, as chaves ficam
            # iguais e uma sobrescreve a outra (mesmo IP, tenants/sites diferentes).
            _ec = str(row.get("remote_connector_id") or "").strip()
            if _ec and _ec != str(connector.get("id") or ""):
                continue
            # Site autoritativo do CONECTOR (nao do campo "Local" da tela, que pode
            # estar com o valor de outro site) -- senao a camera de um conector sai
            # rotulada com o site de outro, como aconteceu com o .201 de Mata Grande.
            _cs = str(connector.get("site") or connector.get("client") or connector.get("name") or site).strip()
            if _cs:
                row["local"] = _cs
                row["site"] = _cs
                row["site_name"] = _cs
            row["remote"] = True
            row["remote_connector_id"] = connector.get("id")
            row["remote_connector_name"] = connector.get("name") or site
            if str(row.get("status") or "").strip().lower() == "online":
                row.pop("error", None)
            changed = True
        if changed:
            save_inventory_json(rows, mode=mode)
            result["inventory"] = rows
            result["inventory_count"] = len(rows)
    finally:
        reset_current_tenant_slug(ctx)
    return result


def _merge_remote_rows(existing: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [dict(row) for row in existing if isinstance(row, dict)]
    by_key = {inventory_row_key(row, fallback=f"ROW:{idx}"): row for idx, row in enumerate(merged)}

    def norm_site(row: dict[str, Any]) -> str:
        return str(row.get("site") or row.get("site_name") or row.get("local") or "").strip().lower()

    def find_site_ip(row: dict[str, Any]) -> dict[str, Any] | None:
        ip = str(row.get("ip") or "").strip()
        site = norm_site(row)
        if not ip or not site:
            return None
        for current in merged:
            if norm_site(current) == site and str(current.get("ip") or "").strip() == ip:
                return current
        return None

    def placeholder_title(row: dict[str, Any], value: Any) -> bool:
        title = str(value or "").strip()
        ip = str(row.get("ip") or "").strip()
        return not title or bool(ip and title == ip)

    for row in new_rows:
        ip = str(row.get("ip") or "").strip()
        if not ip:
            continue
        row_key = inventory_row_key(row)
        current = by_key.get(row_key) or find_site_ip(row)
        if current:
            for key, value in row.items():
                if key in {"status", "local", "site", "site_name", "remote", "remote_connector_id", "remote_connector_name"}:
                    current[key] = value
                elif key in {"title", "titulo"} and placeholder_title(row, value):
                    continue
                elif not str(current.get(key) or "").strip():
                    current[key] = value
            by_key[row_key] = current
        else:
            merged.append(row)
            by_key[row_key] = row
    return merged


async def _remote_inventory_via_connector(ws: WebSocket, payload: Dict[str, Any], result: Dict[str, Any], tenant_slug: str = "") -> Dict[str, Any]:
    site = str(payload.get("local") or "").strip()
    connector = _connector_from_payload(payload)
    if not connector:
        return result
    if connector.get("status") != "online":
        await _ws_send(ws, {"type": "status", "message": f"Conector {connector.get('name') or site} offline. Coleta remota nao executada."})
        return result
    targets = _expand_remote_targets(str(payload.get("alvo") or ""))
    if not targets:
        return result

    connector_name = connector.get("name") or site or connector.get("id")
    await _ws_send(ws, {"type": "status", "message": f"{connector_name} via conector: testando {len(targets)} alvo(s)..."})

    online_targets: list[str] = []
    connector_id = str(connector.get("id") or "")
    chunks = [targets[i:i + 50] for i in range(0, len(targets), 50)] or [targets]
    for idx, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            await _ws_send(ws, {"type": "status", "message": f"{connector_name} via conector: lote {idx}/{len(chunks)} ({len(chunk)} alvo(s))..."})
        job = create_job({"connector_id": connector_id, "type": "ping_many", "payload": {"targets": chunk}}).get("job") or {}
        job_id = str(job.get("id") or "")
        final_job: dict[str, Any] | None = None
        deadline = time.time() + 150
        while time.time() < deadline:
            await anyio.sleep(3)
            jobs = list_jobs(connector_id).get("jobs") or []
            final_job = next((item for item in jobs if str(item.get("id") or "") == job_id), None)
            if final_job and final_job.get("status") in {"done", "failed"}:
                break

        if not final_job or final_job.get("status") != "done":
            await _ws_send(ws, {"type": "status", "message": f"Conector nao devolveu o lote {idx}/{len(chunks)} dentro do tempo."})
            continue

        for target, ok in _parse_routeros_ping(final_job.get("result") or {}).items():
            if ok:
                online_targets.append(target)

    online_targets = list(dict.fromkeys(online_targets))
    if not online_targets:
        await _ws_send(ws, {"type": "status", "message": "Conector respondeu, mas nenhum alvo ficou online."})
        return result

    mode = str(payload.get("inventory_mode") or "olt").strip().lower() or "olt"
    ctx = set_current_tenant_slug(str(tenant_slug or "").strip().lower())
    try:
        existing = load_inventory_json(mode=mode) or []
        rows = [
            {
                "ip": ip,
                "host": ip,
                "http_port": 80,
                "title": ip,
                "local": site or connector.get("site") or connector.get("client") or "",
                "site": site or connector.get("site") or connector.get("client") or "",
                "site_name": site or connector.get("site") or connector.get("client") or "",
                "status": "online",
                "remote": True,
                "remote_connector_id": connector.get("id"),
                "remote_connector_name": connector.get("name"),
            }
            for ip in online_targets
        ]
        merged = _merge_remote_rows(existing, rows)
        save_inventory_json(merged, mode=mode)
    finally:
        reset_current_tenant_slug(ctx)

    result["inventory"] = merged
    result["inventory_count"] = len(merged)
    result["discovered_count"] = len(online_targets)
    result["remote_discovered"] = len(online_targets)
    await _ws_send(ws, {"type": "status", "message": f"Conector {connector.get('name') or site}: {len(online_targets)} IP(s) ativo(s) gravado(s) no inventario."})
    return result


async def run_ws_scan(ws: WebSocket, payload: Dict[str, Any], tenant_slug: str = "") -> None:
    """
    WS /ws/scan

    Importante (Windows): Uvicorn costuma usar WindowsSelectorEventLoopPolicy com --reload,
    e asyncio.create_subprocess_exec pode lançar NotImplementedError.
    Por isso, aqui executamos o scan via run_http_scan() em thread (subprocess.run),
    mantendo compatibilidade e evitando travar o loop.
    """
    alvo = (payload.get("alvo") or "").strip()
    usuario = (payload.get("usuario") or "admin").strip() or "admin"
    senha = (payload.get("senha") or "admin").strip() or "admin"
    scan_origin = str(payload.get("scan_origin") or "").strip().lower()
    connector_id = str(payload.get("connector_id") or payload.get("remote_connector_id") or "").strip()
    connector = _connector_from_payload(payload) if connector_id or scan_origin == "connector" else None
    auto_targets = _targets_for_connector_scan(alvo, connector)
    effective_alvo = alvo or ",".join(auto_targets)

    # Modelo A (isolamento): no caminho DIRETO o container fala com o IP VIRTUAL do
    # conector (o host faz NETMAP -> real + origem isolada, sem cruzar IP igual de
    # outro cliente). GATED: conector sem alocacao -> direct_alvo == effective_alvo.
    # O alvo REAL (effective_alvo) segue sendo usado pra tag/known_targets/agente;
    # depois do scan direto, _run_scan_in_tenant des-virtualiza o inventario.
    direct_alvo = _vnat.virtualize_target(connector_id, effective_alvo) or effective_alvo

    # Compatibilidade de payload:
    # - Front atual usa: snapshot/imgbb/excel/olt_enrich/ia
    # - Alguns clientes usam: capture_snapshot/upload_imgbb/generate_spreadsheet/enrich_with_olt/run_image_health_ai
    snapshot = bool(payload.get("snapshot", payload.get("capture_snapshot", False)))
    imgbb = bool(payload.get("imgbb", payload.get("upload_imgbb", False)))
    excel = bool(payload.get("excel", payload.get("generate_spreadsheet", False)))
    olt_enrich = bool(payload.get("olt_enrich", payload.get("enrich_with_olt", False)))
    switch_enrich = bool(payload.get("switch_enrich", payload.get("enrich_with_switch", False)))
    ia = bool(payload.get("ia", payload.get("run_image_health_ai", False)))

    req = ScanRequest(
        alvo=direct_alvo,
        usuario=usuario,
        senha=senha,
        capture_snapshot=snapshot,
        snapshot=snapshot,
        imgbb=imgbb,
        excel=excel,
        olt_enrich=olt_enrich,
        switch_enrich=switch_enrich,
        ia=ia,
        append_inventory=bool(payload.get("append_inventory", False)),
        reuse_inventory=bool(payload.get("reuse_inventory", False)),
        nat_mode=bool(payload.get("nat_mode", False)),
        set_local=bool(payload.get("set_local", False)),
        local=(payload.get("local") or ""),
        inventory_mode=str(payload.get("inventory_mode") or "olt"),
        scan_origin=str(payload.get("scan_origin") or ""),
        connector_id=str(payload.get("connector_id") or ""),
        remote_connector_id=str(payload.get("remote_connector_id") or payload.get("connector_id") or ""),
    )

    connector_has_tunnel = _connector_has_tunnel(connector)

    if scan_origin == "connector" and not connector_id:
        await _ws_send(ws, {"type": "error", "message": "Selecione um conector MikroTik para executar esta varredura remota."})
        return
    if scan_origin == "connector" and not alvo and auto_targets:
        await _ws_send(ws, {"type": "status", "message": f"Usando a LAN detectada do conector: {len(auto_targets)} alvo(s)."})

    # "Ter VPN" so significa que o servidor alcanca o MikroTik, nao que alcanca
    # a LAN do cliente atras dele -- isso depende de client_lans estar de fato
    # aplicado (ver scripts/sightops_wireguard_sync.py). Em vez de assumir,
    # _decide_remote_only testa a rede na hora (poucos segundos) quando ha
    # ambiguidade real: se responde, caminho local direto -- rapido, com
    # snapshot, igual funcionou pra Incoforte; se nao, caminho MikroTik --
    # mais lento, so descoberta, mas nunca marca tudo como offline por engano.
    probe_targets = _pick_probe_targets(direct_alvo)
    expanded_targets = _expand_remote_targets(effective_alvo)
    if connector_id and expanded_targets:
        try:
            register_connector_known_targets(connector_id, expanded_targets)
        except ValueError:
            pass
    if connector_id and connector_has_tunnel and probe_targets:
        await _ws_send(ws, {"type": "status", "message": "Testando se a rede do cliente responde direto..."})
    remote_only = await anyio.to_thread.run_sync(
        lambda: _decide_remote_only(
            scan_origin=scan_origin,
            connector_id=connector_id,
            connector_has_tunnel=connector_has_tunnel,
            remote_only_requested=bool(payload.get("remote_only")),
            probe_targets=probe_targets,
        )
    )

    if remote_only:
        await _ws_send(ws, {"type": "status", "message": "Executando via conector (MikroTik)..."})
    elif scan_origin == "connector" and connector_has_tunnel:
        await _ws_send(ws, {"type": "status", "message": "Rede do cliente respondeu -- executando direto pela VPN..."})
    else:
        await _ws_send(ws, {"type": "status", "message": "Executando inventory_scan..."})

    try:
        # roda em thread para não bloquear o loop e não depender de subprocess async
        if remote_only:
            result: Dict[str, Any] = {"ok": True, "inventory": [], "inventory_count": 0}
            remote_payload = dict(payload)
            remote_payload["alvo"] = effective_alvo
            result = await _remote_inventory_via_connector(ws, remote_payload, result, str(tenant_slug or "").strip().lower())
        else:
            result = await anyio.to_thread.run_sync(_run_scan_in_tenant, req, str(tenant_slug or "").strip().lower(), connector_id)
            tag_payload = dict(payload)
            tag_payload["alvo"] = effective_alvo
            result = _tag_rows_for_connector(tag_payload, result, str(tenant_slug or "").strip().lower())
    except Exception as e:
        msg = str(e) or repr(e) or "Erro interno no scan."
        await _ws_send(ws, {"type": "error", "message": msg})
        return

    # Se chegou aqui, o inventário foi atualizado
    await _ws_send(ws, {"type": "inventory_updated"})
    await _ws_send(ws, {"type": "done", "message": "Scan concluído. Inventário atualizado.", "result": result})
