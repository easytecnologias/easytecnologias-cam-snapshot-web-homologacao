from __future__ import annotations

import json
import re
import asyncio
import ipaddress
import time
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import AUTH_COOKIE_NAME
from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug, tenant_snapshot_dir
from app.services.ws_scan_service import run_ws_scan
from app.services.maintenance_ping_service import maintenance_ping_hub
from app.services.auth_store import auth_enabled, get_user_by_token
from app.services.camsnapshot.device_info import get_snapshot
from app.services.photo_store import ip_to_stem

router = APIRouter()

ROLE_RANK = {
    "viewer": 10,
    "operator": 20,
    "admin": 30,
    "owner": 40,
}


def _ip_to_stem(ip: str) -> str:
    """Wrapper compativel para manter o mesmo padrao entre servicos."""
    return ip_to_stem(ip)


def _split_tokens(raw: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\s;]+", raw or "") if t.strip()]


def _expand_host_token(token: str) -> list[str]:
    token = (token or "").strip()
    if not token:
        return []

    # CIDR (ex.: 10.0.0.0/24)
    if "/" in token:
        try:
            net = ipaddress.ip_network(token, strict=False)
            return [str(ip) for ip in net.hosts()]
        except Exception:
            return [token]

    # Faixa IPv4 (ex.: 10.0.0.10-10.0.0.20 ou 10.0.0.10-20)
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


def _parse_ports(raw: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for token in _split_tokens(raw):
        if "-" in token:
            a, b = token.split("-", 1)
            try:
                start = int(a.strip())
                end = int(b.strip())
                if end < start:
                    start, end = end, start
                for p in range(start, end + 1):
                    if 1 <= p <= 65535 and p not in seen:
                        seen.add(p)
                        out.append(p)
            except Exception:
                continue
        else:
            try:
                p = int(token)
            except Exception:
                continue
            if 1 <= p <= 65535 and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _guess_service(port: int) -> str:
    common: dict[int, str] = {
        21: "ftp",
        22: "ssh",
        23: "telnet",
        25: "smtp",
        53: "dns",
        80: "http",
        81: "http-alt",
        88: "http-alt",
        110: "pop3",
        123: "ntp",
        139: "netbios-ssn",
        143: "imap",
        161: "snmp",
        389: "ldap",
        443: "https",
        445: "microsoft-ds",
        554: "rtsp",
        587: "smtp-submission",
        8291: "mikrotik-winbox",
        8000: "http-alt",
        8080: "http-proxy",
        8443: "https-alt",
        37777: "dahua",
    }
    return common.get(port, "")


def _role_allows(user_role: str, required_role: str) -> bool:
    have = ROLE_RANK.get(str(user_role or "").strip().lower(), 0)
    need = ROLE_RANK.get(str(required_role or "").strip().lower(), 10**9)
    return have >= need


def _effective_tenant_slug(user: Dict[str, Any] | None) -> str:
    row = user or {}
    return str(row.get("effective_tenant_slug") or row.get("tenant_slug") or "").strip().lower()


async def _accept_ws_session(
    ws: WebSocket,
    *,
    min_role: str = "viewer",
) -> tuple[bool, Dict[str, Any] | None, Dict[str, Any] | None]:
    """Accept the socket and authenticate with a first-frame token.

    Query-string token is still accepted for older clients, but current UI sends
    {"type":"auth","token":"..."} as the first frame to keep secrets out of URLs.
    If the first frame is an application payload containing "token", it is
    returned so the caller can process it without waiting for another message.
    """
    await ws.accept()
    if not auth_enabled():
        return True, None, None

    raw_token = str(ws.query_params.get("token") or "").strip()
    if not raw_token:
        raw_token = str(ws.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    first_payload: Dict[str, Any] | None = None

    if not raw_token:
        try:
            raw = await ws.receive_text()
            payload = json.loads(raw or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except WebSocketDisconnect:
            return False, None, None
        except Exception:
            await ws.send_text(json.dumps({"type": "error", "message": "autenticacao obrigatoria"}, ensure_ascii=False))
            await ws.close(code=4401)
            return False, None, None

        raw_token = str(payload.get("token") or "").strip()
        if str(payload.get("type") or "").strip().lower() != "auth":
            first_payload = payload

    if not raw_token:
        await ws.send_text(json.dumps({"type": "error", "message": "autenticacao obrigatoria"}, ensure_ascii=False))
        await ws.close(code=4401)
        return False, None, None

    current_user = get_user_by_token(raw_token)
    if not current_user:
        await ws.send_text(json.dumps({"type": "error", "message": "token invalido ou expirado"}, ensure_ascii=False))
        await ws.close(code=4401)
        return False, None, None

    if min_role and not _role_allows(str(current_user.get("role") or ""), min_role):
        await ws.send_text(
            json.dumps({"type": "error", "message": f"permissao insuficiente: requer perfil {min_role}"}, ensure_ascii=False)
        )
        await ws.close(code=4403)
        return False, None, None

    return True, current_user, first_payload


async def _iter_pairs(hosts: Iterable[str], ports: Iterable[int]) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for h in hosts:
        for p in ports:
            pairs.append((h, p))
    return pairs


@router.websocket("/ws/scan")
async def ws_scan(ws: WebSocket) -> None:
    ok, current_user, first_payload = await _accept_ws_session(ws, min_role="operator")
    if not ok:
        return
    tenant_slug = _effective_tenant_slug(current_user)
    ctx_token = set_current_tenant_slug(tenant_slug)
    try:
        if first_payload is not None:
            payload = first_payload
        else:
            data = await ws.receive_text()
            try:
                payload = json.loads(data)
            except Exception:
                await ws.send_text(json.dumps({"type": "error", "message": "Payload invalido (JSON)."}))
                return
        await run_ws_scan(ws, payload, tenant_slug=tenant_slug)
    except WebSocketDisconnect:
        return
    except Exception as e:
        # Nunca envie mensagem vazia (algumas exceÃ§Ãµes como CancelledError tÃªm str(e)=="" no Windows)
        msg = str(e) or repr(e) or "Erro interno no WebSocket."
        try:
            await ws.send_text(json.dumps({"type": "error", "message": msg}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        reset_current_tenant_slug(ctx_token)


@router.websocket("/ws/olt-console")
async def ws_olt_console(ws: WebSocket) -> None:
    """WebSocket consumido pela UI de OLT para log em tempo real.

    Se nÃ£o existir, o browser acusa erro de handshake (403). Nesta versÃ£o,
    aceitamos e mantemos vivo. O streaming de logs pode ser adicionado depois.
    """
    ok, _, _ = await _accept_ws_session(ws, min_role="operator")
    if not ok:
        return
    try:
        await ws.send_text(json.dumps({"type": "status", "message": "OLT console conectado."}, ensure_ascii=False))
        while True:
            msg = await ws.receive_text()
            if (msg or "").strip().lower() in ("ping", "keepalive"):
                await ws.send_text(json.dumps({"type": "pong"}))
            else:
                await ws.send_text(json.dumps({"type": "ack"}))
    except WebSocketDisconnect:
        return
    except Exception:
        return

@router.websocket("/ws/snapshot")
async def ws_snapshot(ws: WebSocket) -> None:
    """WebSocket usado pela UI de Snapshot/Preview.

    Recebe JSON: {"ip": "...", "usuario": "...", "senha": "..."}.
    Captura um snapshot e salva em /data/snapshot/<ip>.jpg.
    """
    ok, current_user, first_payload = await _accept_ws_session(ws, min_role="operator")
    if not ok:
        return
    ctx_token = set_current_tenant_slug(_effective_tenant_slug(current_user))
    try:
        await ws.send_text(json.dumps({"type": "status", "message": "Snapshot WS conectado."}, ensure_ascii=False))
        while True:
            if first_payload is not None:
                payload = first_payload
                first_payload = None
            else:
                raw = await ws.receive_text()
                low = (raw or "").strip().lower()
                if low in ("ping", "keepalive"):
                    await ws.send_text(json.dumps({"type": "pong"}))
                    continue

                try:
                    payload = json.loads(raw)
                except Exception:
                    await ws.send_text(json.dumps({"type": "error", "message": "Payload invalido (JSON)."}, ensure_ascii=False))
                    continue

            ip = (payload.get("ip") or "").strip()
            usuario = (payload.get("usuario") or payload.get("user") or "").strip()
            senha = (payload.get("senha") or payload.get("pass") or payload.get("password") or "").strip()

            if not ip:
                await ws.send_text(json.dumps({"type": "error", "message": "IP nÃ£o informado."}, ensure_ascii=False))
                continue

            snap_dir = tenant_snapshot_dir("ip")
            # Se existir uma pasta bugada com nome *.jpg, remove (bug legado ao passar path errado)
            try:
                if snap_dir.exists() and snap_dir.is_file():
                    snap_dir.unlink()
            except Exception:
                pass
            snap_dir.mkdir(parents=True, exist_ok=True)

            # Nome normalizado para UI (/data/snapshot/<ip_underscore>.jpg)
            out_name = _ip_to_stem(ip) + ".jpg"
            out_path = snap_dir / out_name

            # Se por algum motivo existir um diretÃ³rio com nome do arquivo, remove
            try:
                if out_path.exists() and out_path.is_dir():
                    shutil.rmtree(out_path, ignore_errors=True)
            except Exception:
                pass

            await ws.send_text(json.dumps({"type": "status", "message": f"Capturando snapshot {ip}..."}, ensure_ascii=False))

            try:
                # IMPORTANTE: get_snapshot() espera OUTPUT_DIR (diretÃ³rio), nÃ£o caminho de arquivo.
                saved_path = await asyncio.to_thread(get_snapshot, ip, usuario, senha, str(snap_dir))

                # get_snapshot salva como "<ip>.jpg" (com pontos). Vamos normalizar para <ip_com_underscore>.jpg
                legacy_path = None
                try:
                    if saved_path and str(saved_path).strip():
                        legacy_path = Path(str(saved_path))
                    else:
                        legacy_path = snap_dir / f"{ip}.jpg"
                except Exception:
                    legacy_path = None

                # Se gerou no formato legado, renomeia/copia para o nome normalizado
                try:
                    if legacy_path and legacy_path.exists() and legacy_path.is_file():
                        same_target = False
                        try:
                            same_target = legacy_path.resolve() == out_path.resolve()
                        except Exception:
                            same_target = str(legacy_path) == str(out_path)

                        if not same_target:
                            if out_path.exists():
                                try:
                                    out_path.unlink()
                                except Exception:
                                    pass
                            shutil.move(str(legacy_path), str(out_path))
                except Exception:
                    pass

                # Alguns snapshots podem ser gravados mesmo se a funÃ§Ã£o retornar None (edge cases).
                if out_path.exists() and out_path.is_file():
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "snapshot",
                                "message": f"Snapshot salvo: /data/snapshot/{out_name}",
                                "file": str(out_path),
                                "url": f"/data/snapshot/{out_name}",
                                "path": f"/data/snapshot/{out_name}",
                            },
                            ensure_ascii=False,
                        )
                    )
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "done",
                                "message": "Finalizado.",
                            },
                            ensure_ascii=False,
                        )
                    )
                    continue

                await ws.send_text(
                    json.dumps(
                        {"type": "error", "message": f"NÃ£o foi possÃ­vel gerar snapshot para {ip}."},
                        ensure_ascii=False,
                    )
                )
            except Exception as e:
                msg = str(e) or repr(e) or "Falha ao capturar snapshot."
                await ws.send_text(json.dumps({"type": "error", "message": msg}, ensure_ascii=False))

    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        reset_current_tenant_slug(ctx_token)


@router.websocket("/ws/portscan")
async def ws_portscan(ws: WebSocket) -> None:
    ok, current_user, first_payload = await _accept_ws_session(ws, min_role="operator")
    if not ok:
        return
    ctx_token = set_current_tenant_slug(_effective_tenant_slug(current_user))
    cancel_event = asyncio.Event()

    async def _send(obj: Dict[str, Any]) -> None:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))

    async def _control_listener() -> None:
        while not cancel_event.is_set():
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                cancel_event.set()
                return
            except Exception:
                cancel_event.set()
                return
            try:
                msg = json.loads(raw or "{}")
            except Exception:
                continue
            t = str(msg.get("type") or "").strip().lower()
            if t == "cancel":
                cancel_event.set()
                return

    try:
        if first_payload is not None:
            payload = first_payload
        else:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                await _send({"type": "error", "message": "Payload invalido (JSON)."})
                return

        targets = _parse_targets(str(payload.get("targets") or ""))
        ports = _parse_ports(str(payload.get("ports") or ""))
        timeout_ms = int(payload.get("timeout_ms") or 700)
        concurrency = int(payload.get("concurrency") or 200)
        detect_service = bool(payload.get("detect_service", True))
        fast_discovery = bool(payload.get("fast_discovery", True))

        timeout_sec = max(0.1, min(timeout_ms / 1000.0, 30.0))
        concurrency = max(1, min(concurrency, 4000))

        if not targets:
            await _send({"type": "error", "message": "Informe ao menos um alvo para escanear."})
            return
        if not ports:
            await _send({"type": "error", "message": "Informe ao menos uma porta para escanear."})
            return

        await _send(
            {
                "type": "status",
                "message": (
                    f"Iniciando scan em {len(targets)} host(s), {len(ports)} porta(s), "
                    f"timeout={int(timeout_ms)}ms, concorrÃªncia={concurrency}"
                    + (" (fast discovery)" if fast_discovery else "")
                ),
            }
        )

        control_task = asyncio.create_task(_control_listener())
        pairs = await _iter_pairs(targets, ports)
        total = len(pairs)
        scanned = 0
        found = 0
        started = time.monotonic()
        sem = asyncio.Semaphore(concurrency)
        counters_lock = asyncio.Lock()

        async def _scan_one(host: str, port: int) -> None:
            nonlocal scanned, found
            if cancel_event.is_set():
                return

            is_open = False
            try:
                async with sem:
                    if cancel_event.is_set():
                        return
                    conn = asyncio.open_connection(host, port)
                    reader, writer = await asyncio.wait_for(conn, timeout=timeout_sec)
                    is_open = True
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
            except Exception:
                is_open = False

            async with counters_lock:
                scanned += 1
                if is_open:
                    found += 1
                    await _send(
                        {
                            "type": "open",
                            "host": host,
                            "port": port,
                            "service": _guess_service(port) if detect_service else "",
                        }
                    )

                elapsed = max(time.monotonic() - started, 1e-6)
                rate = round(scanned / elapsed, 2)
                remaining = max(total - scanned, 0)
                eta_s = int(remaining / rate) if rate > 0 else 0
                await _send(
                    {
                        "type": "progress",
                        "scanned": scanned,
                        "total": total,
                        "found": found,
                        "rate": rate,
                        "eta_s": eta_s,
                    }
                )

        tasks = [asyncio.create_task(_scan_one(h, p)) for h, p in pairs]
        await asyncio.gather(*tasks, return_exceptions=True)

        if not control_task.done():
            control_task.cancel()
            try:
                await control_task
            except BaseException:
                pass

        if cancel_event.is_set():
            await _send(
                {
                    "type": "done",
                    "message": f"Scan cancelado. Testes concluÃ­dos: {scanned}/{total}. Portas abertas: {found}.",
                }
            )
            return

        await _send(
            {
                "type": "done",
                "message": f"Scan concluÃ­do. Testes: {total}. Portas abertas: {found}.",
            }
        )
    except WebSocketDisconnect:
        return
    except Exception as e:
        msg = str(e) or repr(e) or "Erro interno no portscan."
        try:
            await ws.send_text(json.dumps({"type": "error", "message": msg}, ensure_ascii=False))
        except Exception:
            pass
    finally:
        reset_current_tenant_slug(ctx_token)


@router.websocket("/ws/maintenance_ping")
async def ws_maintenance_ping(ws: WebSocket) -> None:
    ok, current_user, first_payload = await _accept_ws_session(ws, min_role="viewer")
    if not ok:
        return
    tenant_slug = _effective_tenant_slug(current_user)
    ctx_token = set_current_tenant_slug(tenant_slug)
    await maintenance_ping_hub.subscribe(ws, tenant_slug=tenant_slug)
    try:
        await ws.send_text(json.dumps({"type": "status", "message": "maintenance ping conectado."}, ensure_ascii=False))
        await ws.send_text(json.dumps(maintenance_ping_hub.snapshot(limit=5000, tenant_slug=tenant_slug), ensure_ascii=False))
        while True:
            if first_payload is not None:
                payload = first_payload
                first_payload = None
            else:
                raw = await ws.receive_text()
                low = (raw or "").strip().lower()
                if low in ("ping", "keepalive"):
                    await ws.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    await ws.send_text(json.dumps({"type": "error", "message": "Payload invalido."}, ensure_ascii=False))
                    continue
            msg_type = str(payload.get("type") or "").strip().lower()
            if msg_type in ("subscribe", "prioritize", "visible"):
                visible_ips = payload.get("visible_ips") or payload.get("ips") or []
                maintenance_ping_hub.prioritize(visible_ips, tenant_slug=tenant_slug)
                await ws.send_text(json.dumps({
                    "type": "ack",
                    "prioritized": len([ip for ip in visible_ips if str(ip or "").strip()]),
                    "summary": maintenance_ping_hub.summary(tenant_slug),
                }, ensure_ascii=False))
            elif msg_type == "snapshot":
                await ws.send_text(json.dumps(maintenance_ping_hub.snapshot(limit=5000, tenant_slug=tenant_slug), ensure_ascii=False))
            else:
                await ws.send_text(json.dumps({"type": "ack"}, ensure_ascii=False))
    except WebSocketDisconnect:
        return
    except Exception:
        return
    finally:
        await maintenance_ping_hub.unsubscribe(ws)
        reset_current_tenant_slug(ctx_token)


@router.websocket("/ws/web-tunnel/{ip}")
async def ws_web_tunnel(ws: WebSocket, ip: str) -> None:
    """Ponte WebSocket<->TCP pro AGENTE local: o agente abre 127.0.0.1:porta no
    PC do usuario e canaliza os bytes por este WS ate o dispositivo, alcancado
    pelo IP VIRTUAL do conector (vnat/wgc). Assim a UI da camera/NVR abre direta,
    sem reconstrucao de HTML e sem expor porta publica. Auth pela mesma sessao
    (token na query, sem consumir frame). `?port=` = porta web do dispositivo."""
    ok, current_user, _first = await _accept_ws_session(ws, min_role="operator")
    if not ok:
        return
    ctx_token = set_current_tenant_slug(_effective_tenant_slug(current_user))
    writer = None
    try:
        try:
            port = int(ws.query_params.get("port") or 80)
        except Exception:
            port = 80
        if port < 1 or port > 65535:
            await ws.close(code=4400)
            return
        from app.api.endpoints.maintenance import (
            _is_proxy_allowed_host,
            _ip_belongs_to_current_tenant,
            _reach,
        )
        host = str(ip or "").strip()
        # mesma politica do proxy web: so IP privado/CGNAT e do tenant atual
        if not _is_proxy_allowed_host(host) or not _ip_belongs_to_current_tenant(host):
            await ws.close(code=4403)
            return
        # GRAVADOR nao vive no inventario de cameras, e o _reach so resolve o
        # conector por la -- sem este hint ele devolveria o IP real e o
        # container nao alcanca a LAN do conector isolado (camera abria, NVR
        # dava ERR_EMPTY_RESPONSE). Cai pro inventario de gravadores.
        rec_cid = ""
        try:
            from app.api.endpoints.nvr import _recorder_connector_for_host
            rec_cid = _recorder_connector_for_host(host)
        except Exception:
            rec_cid = ""
        reach = _reach(host, rec_cid)  # conector isolado -> IP virtual
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(reach, port), timeout=10.0
            )
        except Exception:
            await ws.close(code=4502)
            return

        cancel = asyncio.Event()

        async def _ws_to_tcp() -> None:
            try:
                while not cancel.is_set():
                    data = await ws.receive_bytes()
                    if not data:
                        continue
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                cancel.set()

        async def _tcp_to_ws() -> None:
            try:
                while not cancel.is_set():
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except Exception:
                pass
            finally:
                cancel.set()

        await asyncio.gather(_ws_to_tcp(), _tcp_to_ws())
    finally:
        try:
            if writer is not None:
                writer.close()
        except Exception:
            pass
        reset_current_tenant_slug(ctx_token)
        try:
            await ws.close()
        except Exception:
            pass


