from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import re
import time
import ipaddress
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.auth import HTTPDigestAuth

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.api.endpoints.cameras import _camera_row_for_ip, _ip_in_inventory, resolve_camera_password
from app.core.paths import BASE_DIR, INVENTORY_JSON_PATH, DVR_INVENTORY_JSON_PATH, NVR_INVENTORY_JSON_PATH, SAIDA_DIR, DATA_DIR
from app.core.tenant_context import get_current_tenant_slug, tenant_recorder_inventory_path, tenant_scoped_path
from app.services.inventory_json import load_inventory_json, save_inventory_json
from app.services.db_store import load_app_settings, save_app_settings, legacy_rows_from_db
from app.services.windows_inventory_service import load_windows_inventory
from app.services.ping_service import _do_ping_sync
from app.services.live_stream_service import register_stream, unregister_stream
from app.services.device_web_proxy import (
    DeviceUnreachable, fetch_device, filter_response_headers, get_cached_scheme, seed_scheme,
)

router = APIRouter(prefix="/api", tags=["maintenance"])


def _netwatch_slug(site: str) -> str:
    s = str(site or "").strip().lower()
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "todos"


def _netwatch_output_name(site: str = "") -> str:
    slug = _netwatch_slug(site)
    return f"netwatch_setup_{slug}.rsc" if slug else "netwatch_setup.rsc"


def _netwatch_output_file(site: str = "") -> Path:
    fname = _netwatch_output_name(site)
    candidates = [
        BASE_DIR / "output" / fname,
        SAIDA_DIR / fname,
        DATA_DIR / fname,
    ]
    existing = [p for p in candidates if p.exists()]
    if existing:
        return sorted(existing, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return candidates[0]


def _netwatch_count_entries(script_content: str) -> int:
    return len(re.findall(r"(?m)^add\s+host=", str(script_content or "")))


def _as_str(v: Any) -> str:
    return str(v or "").strip()


def _is_proxy_allowed_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    return bool(ip.is_private or ip in cgnat)


def _ip_belongs_to_current_tenant(ip: str) -> bool:
    """Confere se este IP pertence a uma camera OU um DVR/NVR cadastrado no
    tenant atual (nao IP arbitrario). Sem isso, um usuario logado em
    QUALQUER cliente consegue abrir a interface web de um IP privado de
    OUTRO cliente so sabendo o IP -- faixas privadas se repetem entre
    tenants neste sistema (mesmo raciocinio de _ip_in_inventory em
    cameras.py, agora cobrindo tambem o inventario de gravador)."""
    return _ip_in_inventory(ip) or _host_in_recorder_inventory(ip)


def _device_http_port(ip: str) -> int:
    """Porta HTTP configurada pra este equipamento (camera ou DVR/NVR),
    ou 80 se nao houver nada salvo/o campo estiver vazio."""
    linha = _camera_row_for_ip(ip) or _recorder_row_for_host(ip)
    try:
        porta = int((linha or {}).get("http_port") or 80)
    except (TypeError, ValueError):
        porta = 80
    return porta if 1 <= porta <= 65535 else 80


_WEB_PROXY_SCHEME_CACHE_PATH = DATA_DIR / "web_proxy_scheme_cache.json"


def _web_proxy_scheme_cache_key(host: str) -> str:
    return f"{get_current_tenant_slug()}:{host}"


def _load_persisted_web_proxy_schemes() -> Dict[str, str]:
    """Esquema (http/https) que ja funcionou para cada host, gravado em disco
    para sobreviver a restart/deploy da API -- arquivo proprio, separado do
    inventario de camera/gravador, pra nao arriscar corromper aquele arquivo
    (que outras rotinas, como a varredura automatica, tambem escrevem)."""
    try:
        dados = json.loads(_WEB_PROXY_SCHEME_CACHE_PATH.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except Exception:
        return {}


def _persist_web_proxy_scheme(host: str, scheme: str) -> None:
    chave = _web_proxy_scheme_cache_key(host)
    dados = _load_persisted_web_proxy_schemes()
    if dados.get(chave) == scheme:
        return
    dados[chave] = scheme
    try:
        _WEB_PROXY_SCHEME_CACHE_PATH.write_text(json.dumps(dados), encoding="utf-8")
    except Exception:
        pass


def _seed_web_proxy_scheme_from_disk(host: str) -> None:
    if get_cached_scheme(host):
        return
    persistido = _load_persisted_web_proxy_schemes().get(_web_proxy_scheme_cache_key(host))
    if persistido:
        seed_scheme(host, persistido)


def _camera_web_target_url(ip: str, path: str = "", query: str = "") -> str:
    host = _as_str(ip)
    if not _is_proxy_allowed_host(host):
        raise HTTPException(status_code=400, detail="proxy web permitido apenas para IP privado/CGNAT")
    if not _ip_belongs_to_current_tenant(host):
        raise HTTPException(status_code=403, detail=f"{host} nao pertence a nenhum equipamento deste cliente")
    clean_path = "/" + str(path or "").lstrip("/")
    if ".." in clean_path.split("/"):
        raise HTTPException(status_code=400, detail="caminho invalido")
    return urlunsplit(("http", host, clean_path, str(query or ""), ""))


def _rewrite_camera_web_content(content: bytes, *, ip: str, content_type: str) -> bytes:
    low_type = str(content_type or "").lower()
    if not any(marker in low_type for marker in ("text/html", "text/css", "javascript", "application/json", "text/xml", "application/xml")):
        return content
    try:
        text = content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="ignore")
        encoding = "latin-1"

    proxy_root = f"/api/maintenance/web/{quote(str(ip), safe='')}/"
    proxy_prefixes = (
        "ISAPI|doc|SDK|cgi-bin|web|js|jsBase|jsCore|css|image|images?|config|System|Streaming|RPC|RPC2|"
        "RPC2_Login|RPC2_Logout|Language|plugin|current_config|custom"
    )
    replacements = [
        (r'(href|src|action)=([\'"])/', rf'\1=\2{proxy_root}'),
        (r'url\(([\'"]?)/', rf'url(\1{proxy_root}'),
        (rf'([\'"])\/({proxy_prefixes})([\/\'"#?])', rf'\1{proxy_root}\2\3'),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text)
    if "text/html" in low_type and "<base " not in text.lower():
        shim = f"""
<base href="{proxy_root}">
<script>
(function(){{
  var root = {json.dumps(proxy_root)};
  var host = {json.dumps(str(ip))};
  function proxify(url) {{
    try {{
      if (!url || typeof url !== 'string') return url;
      if (url.indexOf(root) === 0) return url;
      if (/^https?:\\/\\//i.test(url)) {{
        var a = document.createElement('a');
        a.href = url;
        if (a.hostname === host) return root + a.pathname.replace(/^\\/+/, '') + (a.search || '') + (a.hash || '');
        return url;
      }}
      if (url.charAt(0) === '/' && url.indexOf('/api/maintenance/web/') !== 0) {{
        return root + url.replace(/^\\/+/, '');
      }}
      return url;
    }} catch (e) {{ return url; }}
  }}
  window.__sightopsCameraProxy = proxify;
  if (window.XMLHttpRequest && XMLHttpRequest.prototype.open) {{
    var xhrOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {{
      arguments[1] = proxify(url);
      return xhrOpen.apply(this, arguments);
    }};
  }}
  if (window.fetch) {{
    var oldFetch = window.fetch;
    window.fetch = function(input, init) {{
      if (typeof input === 'string') input = proxify(input);
      else if (input && input.url) input = new Request(proxify(input.url), input);
      return oldFetch.call(this, input, init);
    }};
  }}
  if (window.Element && Element.prototype.setAttribute) {{
    var oldSet = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(name, value) {{
      var n = String(name || '').toLowerCase();
      if (n === 'src' || n === 'href' || n === 'action') value = proxify(value);
      return oldSet.call(this, name, value);
    }};
  }}
}})();
</script>"""
        text = re.sub(r"(?i)<head([^>]*)>", rf"<head\1>{shim}", text, count=1)
    return text.encode(encoding, errors="ignore")


def _proxy_location_header(location: str, *, ip: str) -> str:
    loc = _as_str(location)
    if not loc:
        return loc
    proxy_root = f"/api/maintenance/web/{quote(str(ip), safe='')}/"
    parts = urlsplit(loc)
    if parts.scheme in ("http", "https") and parts.hostname == ip:
        path = parts.path.lstrip("/")
        suffix = path + (("?" + parts.query) if parts.query else "")
        return proxy_root + suffix
    if loc.startswith("/"):
        return proxy_root + loc.lstrip("/")
    return loc


def _camera_cookie_header(request: Request) -> str:
    raw = request.headers.get("cookie") or ""
    if not raw:
        return ""
    kept: list[str] = []
    for part in raw.split(";"):
        item = part.strip()
        if not item:
            continue
        name = item.split("=", 1)[0].strip().lower()
        if name in {"sightops_session"}:
            continue
        kept.append(item)
    return "; ".join(kept)


def _zabbix_tenant_slug() -> str:
    return _as_str(get_current_tenant_slug() or "default").lower() or "default"


def _zabbix_host_safe(value: Any) -> str:
    safe = _as_str(value).upper()
    safe = re.sub(r"[^A-Z0-9_.-]+", "-", safe)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe or "DEFAULT"


def _zabbix_tenant_group(group: str, tenant: str = "") -> str:
    base = _as_str(group) or "Cameras"
    slug = _zabbix_host_safe(tenant or _zabbix_tenant_slug())
    marker = f" - {slug}"
    return base if base.upper().endswith(marker.upper()) else f"{base}{marker}"


def _zabbix_tmp_inventory_path(source: str, mode: str = "", suffix: str = "") -> Path:
    tenant = _zabbix_tenant_slug()
    src = re.sub(r"[^a-z0-9_-]+", "-", _as_str(source).lower() or "ip").strip("-")
    md = re.sub(r"[^a-z0-9_-]+", "-", _as_str(mode).lower()).strip("-")
    extra = re.sub(r"[^a-z0-9_-]+", "-", _as_str(suffix).lower()).strip("-")
    parts = ["zabbix-source-inventory", src]
    if md:
        parts.append(md)
    if extra:
        parts.append(extra)
    return tenant_scoped_path("tmp/" + ".".join(parts) + ".json", tenant)


def _zabbix_host_belongs_to_tenant(host: Dict[str, Any], tenant: str = "") -> bool:
    slug = _zabbix_host_safe(tenant or _zabbix_tenant_slug())
    technical = _as_str(host.get("host"))
    return technical.upper().startswith(f"{slug}-")


def _normalize_zabbix_url(url: str) -> str:
    """Use a Docker-internal Zabbix route when users paste a public/macvlan URL."""
    raw = _as_str(url)
    if not raw:
        return ""
    try:
        candidate = raw if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) else f"http://{raw}"
        parts = urlsplit(candidate)
        host = (parts.hostname or "").strip().lower()
        if host in {"10.10.12.51", "zabbix-web", "zabbix-prod-web"}:
            scheme = parts.scheme or "http"
            internal_host = os.getenv("SIGHTOPS_ZABBIX_WEB_HOST", "zabbix-prod-web").strip() or "zabbix-prod-web"
            internal_port = os.getenv("SIGHTOPS_ZABBIX_WEB_PORT", "8080").strip() or "8080"
            return urlunsplit((scheme, f"{internal_host}:{internal_port}", "/api_jsonrpc.php", "", ""))
    except Exception:
        pass
    return raw


def _zabbix_default_url_candidates() -> list[str]:
    configured = [
        os.getenv("SIGHTOPS_ZABBIX_URL"),
        os.getenv("ZBX_URL"),
        os.getenv("ZABBIX_URL"),
    ]
    defaults = [
        "http://zabbix-prod-web:8080/api_jsonrpc.php",
        "http://zabbix-web:8080/api_jsonrpc.php",
    ]
    out: list[str] = []
    for value in configured + defaults:
        url = _normalize_zabbix_url(_as_str(value))
        if url and url not in out:
            out.append(url)
    return out


def _zabbix_default_user() -> str:
    return (
        _as_str(os.getenv("SIGHTOPS_ZABBIX_USER"))
        or _as_str(os.getenv("ZBX_USER"))
        or _as_str(os.getenv("ZABBIX_USER"))
        or "Admin"
    )


def _zabbix_default_pass() -> str:
    return (
        _as_str(os.getenv("SIGHTOPS_ZABBIX_PASS"))
        or _as_str(os.getenv("ZBX_PASS"))
        or _as_str(os.getenv("ZABBIX_PASS"))
        or "zabbix"
    )


def _zabbix_effective_sync_config(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    base = dict(cfg or {})
    url = _normalize_zabbix_url(base.get("url"))
    if not url:
        url = (_zabbix_default_url_candidates() or [""])[0]
    user = _as_str(base.get("user")) or _zabbix_default_user()
    password = _as_str(base.get("pass") or base.get("password")) or _zabbix_default_pass()
    return {
        **base,
        "enabled": True,
        "url": url,
        "user": user,
        "pass": password,
        "group": _as_str(base.get("group")) or "Cameras",
        "template": _as_str(base.get("template")) or "Template Module ICMP Ping",
        "template_dvr": _as_str(base.get("template_dvr")) or "Template Cam-Snapshot DVR Channel",
        "site": _as_str(base.get("site")),
        "inv_mode": _normalize_ip_inventory_mode(base.get("inv_mode") or base.get("mode") or "all"),
        "tenant_slug": _zabbix_tenant_slug(),
    }


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def _load_settings() -> Dict[str, Any]:
    return load_app_settings()


def _save_settings(s: Dict[str, Any]) -> None:
    save_app_settings(s or {})


def _bool_ok(resp: requests.Response | None) -> bool:
    if resp is None:
        return False
    return resp.status_code in (200, 201, 202, 204)


def _request_with_auth(url: str, user: str, password: str, timeout: int = 8) -> tuple[bool, str]:
    last_err = ""
    for auth in (HTTPDigestAuth(user, password), (user, password)):
        try:
            r = requests.get(url, auth=auth, timeout=timeout, verify=False, headers={"Accept": "*/*"})
            if _bool_ok(r):
                return True, ""
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    return False, last_err or "falha de comunicacao"


def _persist_ip_change(old_ip: str, new_ip: str) -> None:
    old_ip = _as_str(old_ip)
    new_ip = _as_str(new_ip)
    if not old_ip or not new_ip or old_ip == new_ip:
        return

    rows = load_inventory_json() or []
    changed = False
    for r in rows:
        ip = _as_str(r.get("ip") or r.get("IP"))
        if ip == old_ip:
            if "ip" in r:
                r["ip"] = new_ip
            elif "IP" in r:
                r["IP"] = new_ip
            else:
                r["ip"] = new_ip
            changed = True
            break

    if changed:
        save_inventory_json(rows)


def _valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(_as_str(value))
        return True
    except Exception:
        return False


def _valid_netmask(mask: str) -> bool:
    try:
        ipaddress.IPv4Network(f"0.0.0.0/{_as_str(mask)}")
        return True
    except Exception:
        return False


def _camera_network_guard(new_ip: str, mask: str, gateway: str) -> str:
    if not _valid_ipv4(new_ip):
        return "new_ip invalido"
    if not _valid_netmask(mask):
        return "mascara invalida"
    if not _valid_ipv4(gateway):
        return "gateway invalido"

    addr = ipaddress.IPv4Address(new_ip)
    cameras_net = ipaddress.IPv4Network("10.10.8.0/22")
    if addr in cameras_net and (mask != "255.255.252.0" or gateway != "10.10.10.1"):
        return "rede 10.10.8.0/22 exige mascara 255.255.252.0 e gateway 10.10.10.1"
    return ""


def _change_ip_one(
    ip: str,
    new_ip: str,
    mask: str,
    gateway: str,
    dns1: str,
    dns2: str,
    user: str,
    password: str,
) -> Dict[str, Any]:
    ip = _as_str(ip)
    new_ip = _as_str(new_ip)
    user = _as_str(user)
    password = _as_str(password)

    if not ip or not new_ip:
        return {"ok": False, "ip": ip, "new_ip": new_ip, "error": "ip e new_ip sao obrigatorios"}
    if not user or not password:
        return {"ok": False, "ip": ip, "new_ip": new_ip, "error": "user e pass sao obrigatorios"}
    if not _as_str(mask) or not _as_str(gateway):
        return {"ok": False, "ip": ip, "new_ip": new_ip, "error": "mascara e gateway sao obrigatorios"}
    guard = _camera_network_guard(new_ip, _as_str(mask), _as_str(gateway))
    if guard:
        return {"ok": False, "ip": ip, "new_ip": new_ip, "error": guard}

    params = [f"Network.eth0.IPAddress={quote(new_ip)}"]
    params.append(f"Network.eth0.SubnetMask={quote(_as_str(mask))}")
    params.append(f"Network.eth0.DefaultGateway={quote(_as_str(gateway))}")
    if _as_str(dns1):
        params.append(f"Network.eth0.DnsServers[0]={quote(_as_str(dns1))}")
    if _as_str(dns2):
        params.append(f"Network.eth0.DnsServers[1]={quote(_as_str(dns2))}")

    q = "&".join(params)
    urls = [
        f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}",
        f"https://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}",
    ]

    last_err = ""
    for url in urls:
        ok, err = _request_with_auth(url, user, password, timeout=8)
        if ok:
            _persist_ip_change(ip, new_ip)
            return {"ok": True, "ip": ip, "new_ip": new_ip, "url": url}
        last_err = err or "falha"

    return {"ok": False, "ip": ip, "new_ip": new_ip, "error": last_err or "falha ao trocar IP"}


def _set_ntp_one(ip: str, user: str, password: str, address: str, port: int, timezone: int, update_period: int) -> Dict[str, Any]:
    ip = _as_str(ip)
    user = _as_str(user)
    password = _as_str(password)
    address = _as_str(address)

    if not ip or not user or not password or not address:
        return {"ok": False, "ip": ip, "error": "ip/user/pass/address sao obrigatorios"}

    q = "&".join(
        [
            "NTP.Enable=true",
            f"NTP.Address={quote(address)}",
            f"NTP.Port={int(port)}",
            f"NTP.TimeZone={int(timezone)}",
            f"NTP.UpdatePeriod={int(update_period)}",
        ]
    )

    urls = [
        f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}",
        f"https://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}",
    ]

    last_err = ""
    for url in urls:
        ok, err = _request_with_auth(url, user, password, timeout=8)
        if ok:
            return {"ok": True, "ip": ip, "url": url}
        last_err = err or "falha"

    return {"ok": False, "ip": ip, "error": last_err or "falha ao configurar NTP"}


def _set_datetime_one(ip: str, user: str, password: str, dt: str) -> Dict[str, Any]:
    ip = _as_str(ip)
    user = _as_str(user)
    password = _as_str(password)
    dt = _as_str(dt)
    if not ip or not user or not password or not dt:
        return {"ok": False, "ip": ip, "error": "ip/user/pass/datetime sao obrigatorios"}

    dt_norm = dt.replace("T", " ").strip()
    if len(dt_norm) == 16:
        dt_norm += ":00"
    q = "&".join(
        [
            "Time.SyncMode=0",
            f"Time.LocalTime={quote(dt_norm)}",
            f"Time.SystemTime={quote(dt_norm)}",
        ]
    )
    urls = [
        f"http://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}",
        f"https://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}",
    ]

    last_err = ""
    for url in urls:
        ok, err = _request_with_auth(url, user, password, timeout=8)
        if ok:
            return {"ok": True, "ip": ip, "url": url}
        last_err = err or "falha"
    return {"ok": False, "ip": ip, "error": last_err or "falha ao configurar data/hora"}


def _change_password_one(ip: str, user: str, old_pass: str, new_pass: str) -> Dict[str, Any]:
    ip = _as_str(ip)
    user = _as_str(user)
    old_pass = _as_str(old_pass)
    new_pass = _as_str(new_pass)

    if not ip or not user or not old_pass or not new_pass:
        return {"ok": False, "ip": ip, "error": "ip/user/old_pass/new_pass sao obrigatorios"}

    q = (
        "action=modifyPassword"
        f"&name={quote(user)}"
        f"&pwdOld={quote(old_pass)}"
        f"&pwdNew={quote(new_pass)}"
    )

    urls = [
        f"http://{ip}/cgi-bin/userManager.cgi?{q}",
        f"https://{ip}/cgi-bin/userManager.cgi?{q}",
    ]

    last_err = ""
    for url in urls:
        ok, err = _request_with_auth(url, user, old_pass, timeout=8)
        if ok:
            return {"ok": True, "ip": ip, "url": url}
        last_err = err or "falha"

    return {"ok": False, "ip": ip, "error": last_err or "falha ao trocar senha"}


def _run_script(script_path: Path, env: Dict[str, str], args: List[str] | None = None) -> tuple[bool, str, str, str]:
    args = args or []
    cmd = [sys.executable, str(script_path), *args]
    merged_env = os.environ.copy()
    merged_env.update(env)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            check=False,
            env=merged_env,
        )
    except Exception as e:
        return False, "", "", str(e)

    ok = proc.returncode == 0
    err = "" if ok else (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
    return ok, proc.stdout or "", proc.stderr or "", err


def _normalize_ip_inventory_mode(mode: str = "") -> str:
    raw = _as_str(mode).lower()
    if raw in {"all", "todos", "tudo", "*"}:
        return "all"
    if raw in {"switch", "sw", "via_switch", "via-switch"}:
        return "switch"
    if raw in {"basic", "basico", "básico", "base"}:
        return "basic"
    return "olt"


def _load_ip_rows_by_mode(site: str = "", mode: str = "olt") -> Dict[str, list[dict[str, Any]]]:
    norm_mode = _normalize_ip_inventory_mode(mode)
    modes = ["olt", "basic", "switch"] if norm_mode == "all" else [norm_mode]
    out: Dict[str, list[dict[str, Any]]] = {}
    olt_ip_set: set[str] = set()
    for item_mode in modes:
        rows = load_inventory_json(site=site, mode=item_mode) or []
        ip_set = {_as_str(row.get("ip") or row.get("IP")) for row in rows if isinstance(row, dict)}
        ip_set.discard("")
        if norm_mode == "all" and item_mode == "olt":
            olt_ip_set = set(ip_set)
        elif norm_mode == "all" and item_mode != "olt" and olt_ip_set and ip_set == olt_ip_set:
            # load_inventory_json tem fallback legado para OLT quando o modo
            # ainda nao foi salvo; no agregado, isso viraria contagem duplicada.
            rows = []
        out[item_mode] = rows
    return out


def _flatten_ip_rows_for_zabbix(site: str = "", mode: str = "olt") -> list[dict[str, Any]]:
    rows_by_mode = _load_ip_rows_by_mode(site=site, mode=mode)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item_mode in ("basic", "olt", "switch"):
        for row in rows_by_mode.get(item_mode, []):
            if not isinstance(row, dict):
                continue
            ip = _as_str(row.get("ip") or row.get("IP"))
            if not ip or ip in seen:
                continue
            enriched = dict(row)
            enriched.setdefault("inventory_mode", item_mode)
            out.append(enriched)
            seen.add(ip)
    return out


def _load_rows_for_source(source: str, site: str = "", mode: str = "olt") -> list[dict[str, Any]]:
    src = _as_str(source).lower()
    site_name = _as_str(site)
    if src == "windows":
        rows = load_windows_inventory()
        if site_name:
            s = site_name.strip().lower()
            rows = [
                r for r in rows
                if isinstance(r, dict)
                and (
                    _as_str(r.get("site")).lower() == s
                    or _as_str(r.get("site_name")).lower() == s
                    or _as_str(r.get("local")).lower() == s
                )
            ]
        return rows
    if src in ("dvr", "nvr"):
        db_rows = legacy_rows_from_db(src, site=site_name)
        if db_rows:
            return db_rows
        p = tenant_recorder_inventory_path(src) if get_current_tenant_slug() else Path(DVR_INVENTORY_JSON_PATH if src == "dvr" else NVR_INVENTORY_JSON_PATH)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "[]")
            rows = data if isinstance(data, list) else []
            if site_name:
                s = site_name.strip().lower()
                rows = [
                    r for r in rows
                    if isinstance(r, dict)
                    and (
                        _as_str(r.get("site")).lower() == s
                        or _as_str(r.get("site_name")).lower() == s
                        or _as_str(r.get("local")).lower() == s
                    )
                ]
            return rows
        except Exception:
            return []
    inv_mode = _normalize_ip_inventory_mode(mode)
    if src == "ip" and inv_mode == "all":
        return _flatten_ip_rows_for_zabbix(site=site_name, mode=inv_mode)
    return load_inventory_json(site=site_name, mode=inv_mode) or []


def _build_zabbix_rows(source: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    src = _as_str(source).lower()
    if src == "windows":
        out: list[dict[str, Any]] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            ip = _as_str(r.get("ip") or r.get("primary_ipv4"))
            hostname = _as_str(r.get("hostname") or r.get("host") or r.get("nome"))
            if not ip or not hostname:
                continue
            os_info = r.get("os") if isinstance(r.get("os"), dict) else {}
            cpu_info = r.get("cpu") if isinstance(r.get("cpu"), dict) else {}
            remote_access = r.get("remote_access") if isinstance(r.get("remote_access"), dict) else {}
            anydesk = remote_access.get("anydesk") if isinstance(remote_access.get("anydesk"), dict) else {}
            site = _as_str(r.get("site"))
            sector = _as_str(r.get("sector") or r.get("setor"))
            local = " / ".join([x for x in (site, sector) if x])
            mac = _as_str(r.get("mac"))
            if not mac:
                network = r.get("network") if isinstance(r.get("network"), list) else []
                for n in network:
                    if isinstance(n, dict) and _as_str(n.get("mac")):
                        mac = _as_str(n.get("mac"))
                        break
            out.append(
                {
                    "source": "windows",
                    "ip": ip,
                    "host": hostname,
                    "hostname": hostname,
                    "title": hostname,
                    "titulo": hostname,
                    "nome": hostname,
                    "local": local,
                    "site": site,
                    "sector": sector,
                    "mac": mac,
                    "modelo": _as_str(r.get("model")),
                    "manufacturer": _as_str(r.get("manufacturer")),
                    "serial": _as_str(r.get("serial")),
                    "os_name": _as_str(os_info.get("name")),
                    "os_build": _as_str(os_info.get("build")),
                    "logged_user": _as_str(r.get("logged_user")),
                    "cpu": _as_str(cpu_info.get("name")),
                    "ram_gb": _as_str(r.get("ram_gb")),
                    "disk_summary": _as_str(r.get("disk_summary")),
                    "anydesk_id": _as_str(r.get("anydesk_id") or anydesk.get("id")),
                    "zabbix_agent_status": _as_str((r.get("zabbix_agent") or {}).get("service_status")) if isinstance(r.get("zabbix_agent"), dict) else "",
                    "host_key": f"WIN-{hostname}",
                }
            )
        return out
    if src not in ("dvr", "nvr"):
        return rows

    out: list[dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        host_ip = _as_str(r.get("host") or r.get("ip"))
        if not host_ip:
            continue
        ch = int(r.get("channel") or 0)
        http_port = int(r.get("http_port") or 80)
        ch_txt = f"{ch:02d}" if ch > 0 else "00"
        title = _as_str(r.get("title") or r.get("titulo")) or f"CH {ch_txt}"
        map_url = ""
        lat = _as_str(r.get("lat"))
        lon = _as_str(r.get("lon"))
        if lat and lon:
            map_url = f"https://www.google.com/maps?q={lat},{lon}"
        out.append(
            {
                # mk_zabbix_from_inventory já entende o fluxo por "dvr" (host por canal).
                # Para origem NVR, reaproveitamos a mesma modelagem de host por canal.
                "source": "dvr",
                "ip": host_ip,
                "channel": ch,
                "http_port": http_port,
                "title": f"CH {ch_txt} - {title}",
                "titulo": f"CH {ch_txt} - {title}",
                "local": _as_str(r.get("local")),
                "mac": _as_str(r.get("mac")),
                "modelo": _as_str(r.get("modelo")),
                "snapshot_url": _as_str(r.get("imgbb_url") or r.get("snapshot_url")),
                "host_key": f"DVR-{host_ip}-CH{ch_txt}",
                "map_url": map_url,
                "lat": lat,
                "lon": lon,
            }
        )
    return out


@router.post("/maintenance/batch/rename")
def maintenance_batch_rename(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    targets = payload.get("targets") or []

    if not isinstance(targets, list) or not targets:
        return {"ok": False, "error": "targets vazio"}

    results: List[Dict[str, Any]] = []
    for t in targets:
        if not isinstance(t, dict):
            continue

        ip = _as_str(t.get("ip"))
        title = _as_str(t.get("title"))
        if not ip or not title:
            results.append({"ok": False, "ip": ip, "title": title, "error": "ip/title obrigatorios"})
            continue

        r = api_cameras_rename(
            {
                "ip": ip,
                "title": title,
                "user": user,
                "pass": password,
                "port": t.get("port", 80),
                "channel": t.get("channel", 1),
            }
        )
        results.append({"ok": bool(r.get("ok")), "ip": ip, "title": title, "error": r.get("error")})

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {
        "ok": fail_n == 0,
        "message": f"Renomeacao concluida: {ok_n} ok, {fail_n} falhas.",
        "results": results,
    }


@router.post("/maintenance/batch/password")
def maintenance_batch_password(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    old_pass = _as_str(payload.get("old_pass"))
    new_pass = _as_str(payload.get("new_pass"))
    ips = payload.get("ips") or []

    if not user or not old_pass or not new_pass:
        return {"ok": False, "error": "user, old_pass e new_pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}

    results = [_change_password_one(_as_str(ip), user, old_pass, new_pass) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Troca de senha: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/change_ip")
def maintenance_change_ip(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _change_ip_one(
        ip=_as_str(payload.get("ip")),
        new_ip=_as_str(payload.get("new_ip")),
        mask=_as_str(payload.get("mask")),
        gateway=_as_str(payload.get("gateway")),
        dns1=_as_str(payload.get("dns1")),
        dns2=_as_str(payload.get("dns2")),
        user=_as_str(payload.get("user")),
        password=_as_str(payload.get("pass")),
    )


@router.post("/maintenance/batch/ip")
def maintenance_batch_ip(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    items = payload.get("items") or []

    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "items vazio"}

    results: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        results.append(
            _change_ip_one(
                ip=_as_str(it.get("ip")),
                new_ip=_as_str(it.get("new_ip")),
                mask=_as_str(it.get("mask")),
                gateway=_as_str(it.get("gateway")),
                dns1=_as_str(it.get("dns1")),
                dns2=_as_str(it.get("dns2")),
                user=user,
                password=password,
            )
        )

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Troca de IP: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/ntp")
def maintenance_batch_ntp(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    address = _as_str(payload.get("address"))
    if not address:
        address = _as_str(payload.get("ntp_server"))
    datetime_value = _as_str(payload.get("datetime"))
    port = int(payload.get("port") or 123)
    timezone = int(payload.get("timezone") or 22)
    update_period = int(payload.get("update_period") or 60)

    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}

    if datetime_value:
        results = [_set_datetime_one(_as_str(ip), user, password, datetime_value) for ip in ips]
        label = "Data/hora aplicada"
    else:
        if not address:
            return {"ok": False, "error": "address e obrigatorio"}
        results = [_set_ntp_one(_as_str(ip), user, password, address, port, timezone, update_period) for ip in ips]
        label = "NTP aplicado"
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"{label}: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/reboot")
def maintenance_batch_reboot(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []

    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}

    results: List[Dict[str, Any]] = []
    for ip in ips:
        sip = _as_str(ip)
        r = api_cameras_reboot({"ip": sip, "user": user, "pass": password})
        results.append({"ok": bool(r.get("ok")), "ip": sip, "error": r.get("error"), "method": r.get("method")})

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Reboot em lote: {ok_n} ok, {fail_n} falhas.", "results": results}


# ── Helpers para novos endpoints ─────────────────────────────────────────────

def _cam_get(ip: str, user: str, password: str, path: str, timeout: int = 6) -> tuple[bool, str]:
    """GET com digest/basic fallback, http/https fallback. Retorna (ok, body)."""
    for proto in ("http", "https"):
        url = f"{proto}://{ip}{path}"
        for auth in (HTTPDigestAuth(user, password), (user, password)):
            try:
                r = requests.get(url, auth=auth, timeout=timeout, verify=False)
                if r.status_code == 200:
                    return True, r.text
                if r.status_code in (401, 403):
                    return False, f"HTTP {r.status_code} — credenciais inválidas"
            except Exception as e:
                continue
    return False, "Câmera inacessível"


def _set_config_one(ip: str, user: str, password: str, params: list) -> Dict[str, Any]:
    q = "&".join(params)
    for proto in ("http", "https"):
        url = f"{proto}://{ip}/cgi-bin/configManager.cgi?action=setConfig&{q}"
        ok, err = _request_with_auth(url, user, password, timeout=8)
        if ok:
            return {"ok": True, "ip": ip}
    return {"ok": False, "ip": ip, "error": "falha ao configurar"}


def _get_firmware_one(ip: str, user: str, password: str) -> Dict[str, Any]:
    ok, body = _cam_get(ip, user, password, "/cgi-bin/magicBox.cgi?action=getSoftwareVersion")
    if not ok:
        return {"ok": False, "ip": ip, "error": body}
    firmware = next((l.split("=", 1)[1].strip() for l in body.splitlines() if l.startswith("version=")), "")
    return {"ok": True, "ip": ip, "firmware": firmware, "message": f"Firmware: {firmware}"}


def _get_cam_time_one(ip: str, user: str, password: str) -> Dict[str, Any]:
    from datetime import datetime
    ok, body = _cam_get(ip, user, password, "/cgi-bin/global.cgi?action=getCurrentTime")
    if not ok:
        return {"ok": False, "ip": ip, "error": body}
    cam_time = next((l.split("=", 1)[1].strip() for l in body.splitlines() if l.startswith("result=")), "")
    if not cam_time:
        return {"ok": False, "ip": ip, "error": "Hora não obtida"}
    try:
        cam_dt = datetime.strptime(cam_time, "%Y-%m-%d %H:%M:%S")
        diff = abs((datetime.now() - cam_dt).total_seconds())
        if diff < 60:
            status = f"sincronizada (±{int(diff)}s)"
        elif diff < 3600:
            status = f"DEFASADA {int(diff // 60)}min"
        else:
            status = f"DEFASADA {int(diff // 3600)}h{int((diff % 3600) // 60)}min"
    except Exception:
        status = "obtida"
    return {"ok": True, "ip": ip, "cam_time": cam_time, "message": f"{cam_time} — {status}"}


def _set_mirror_one(ip: str, user: str, password: str, mirror: bool, flip: bool) -> Dict[str, Any]:
    r = _set_config_one(ip, user, password, [
        f"VideoInOptions[0].Mirror={'true' if mirror else 'false'}",
        f"VideoInOptions[0].Flip={'true' if flip else 'false'}",
    ])
    if r.get("ok"):
        r["message"] = f"{'Espelhado' if mirror else 'Normal'} / {'Virado' if flip else 'Normal'}"
    return r


def _set_day_night_one(ip: str, user: str, password: str, mode: int) -> Dict[str, Any]:
    # Valores conforme a API HTTP real da Dahua/Intelbras: 0=sempre colorido,
    # 1=automatico (decide pela luminosidade), 2=sempre P&B. Confirmado na
    # documentacao oficial -- nao inventar, o front tinha 0 e 1 invertidos
    # antes (mandava "automatico" quando o operador pedia "colorido forcado").
    labels = {0: "Colorido", 1: "Automático", 2: "Preto e branco"}
    r = _set_config_one(ip, user, password, [f"VideoInOptions[0].DayNightColor={int(mode)}"])
    if r.get("ok"):
        r["message"] = labels.get(mode, f"Modo {mode}")
    return r


def _set_video_quality_one(ip: str, user: str, password: str,
                            bitrate: int | None, fps: int | None, codec: str | None) -> Dict[str, Any]:
    params = []
    if bitrate is not None:
        params += [f"Encode[0].MainFormat[0].Video.BitRate={int(bitrate)}",
                   f"Encode[0].ExtraFormat[0].Video.BitRate={int(max(256, bitrate // 4))}"]
    if fps is not None:
        params += [f"Encode[0].MainFormat[0].Video.FPS={int(fps)}"]
    if codec:
        params += [f"Encode[0].MainFormat[0].Video.Compression={quote(codec)}"]
    if not params:
        return {"ok": False, "ip": ip, "error": "Nenhum parâmetro informado"}
    r = _set_config_one(ip, user, password, params)
    if r.get("ok"):
        parts = []
        if bitrate: parts.append(f"{bitrate} kbps")
        if fps: parts.append(f"{fps} fps")
        if codec: parts.append(codec)
        r["message"] = " · ".join(parts)
    return r


def _force_snapshot_one_mnt(ip: str, user: str, password: str) -> Dict[str, Any]:
    import shutil
    from app.services.photo_store import attach_snapshot_fields, snapshot_storage_dir
    from app.services.camsnapshot.device_info import get_snapshot
    dst_dir = snapshot_storage_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        saved_path = get_snapshot(ip, user, password, str(dst_dir), timeout=(1.5, 8.0), retries=1)
    except Exception as e:
        return {"ok": False, "ip": ip, "error": str(e)}
    safe_ip = ip.replace(":", "__").replace(".", "_").replace("/", "_")
    out_name = f"{safe_ip}.jpg"
    out_path = dst_dir / out_name
    try:
        legacy = Path(str(saved_path)) if saved_path else dst_dir / f"{ip}.jpg"
        if legacy.exists() and legacy.resolve() != out_path.resolve():
            if out_path.exists(): out_path.unlink()
            shutil.move(str(legacy), str(out_path))
    except Exception:
        pass
    if not out_path.exists():
        return {"ok": False, "ip": ip, "error": "Não foi possível capturar snapshot"}
    rows = load_inventory_json() or []
    for cam in rows:
        if isinstance(cam, dict) and str(cam.get("ip") or "").strip() == ip:
            from app.services.photo_store import attach_snapshot_fields
            attach_snapshot_fields(cam, ip, out_name)
            break
    save_inventory_json(rows)
    return {"ok": True, "ip": ip, "message": "Snapshot capturado"}


# ── Stream ao vivo — MJPEG proxy (multipart/x-mixed-replace) ─────────────────

@router.get("/maintenance/stream/{ip}")
def maintenance_mjpeg_stream(ip: str, user: str = "admin", password: str = ""):
    """Proxia MJPEG da câmera como multipart/x-mixed-replace usando requests streaming."""
    from fastapi.responses import StreamingResponse, Response
    import requests as _req
    from requests.auth import HTTPBasicAuth

    cam_urls = [
        f"http://{ip}/cgi-bin/mjpg/video.cgi?channel=1&subtype=1",
        f"http://{ip}/cgi-bin/mjpg/video.cgi?channel=1&subtype=0",
        f"http://{ip}/cgi-bin/mjpg/video.cgi?channel=0&subtype=1",
        f"http://{ip}/cgi-bin/mjpg/video.cgi?channel=0&subtype=0",
    ]

    # Testa conexão e verifica se a câmera envia JPEG real (não H.264 em container multipart)
    active_resp = None
    first_buf = b""
    for cam_url in cam_urls:
        for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
            try:
                r = _req.get(cam_url, auth=auth, stream=True, timeout=(6, 15))
                if r.status_code != 200 or "multipart" not in r.headers.get("Content-Type", ""):
                    r.close()
                    continue
                # Lê o primeiro frame para verificar se é JPEG real (FF D8) ou H.264 NALU (00 00 00 01)
                buf = b""
                for chunk in r.iter_content(chunk_size=4096):
                    buf += chunk
                    if len(buf) >= 8192:
                        break
                m = re.search(rb"Content-Length:\s*(\d+)\r\n\r\n", buf)
                if m:
                    body_start = m.end()
                    body_prefix = buf[body_start:body_start + 4]
                    if body_prefix[:2] != b'\xff\xd8':
                        # Câmera envia H.264/outro dentro do multipart — não suportado via <img>
                        r.close()
                        continue
                active_resp = r
                first_buf = buf
                break
            except Exception:
                continue
        if active_resp:
            break

    if not active_resp:
        return Response(status_code=503, content=b"Camera MJPEG unavailable")

    def generate(resp, initial_buf=b""):
        buf = initial_buf
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                buf += chunk
                while True:
                    # Estratégia 1: Content-Length explícito no header MJPEG
                    m = re.search(rb"Content-Length:\s*(\d+)\r\n\r\n", buf)
                    if m:
                        flen = int(m.group(1))
                        s = m.end()
                        if len(buf) < s + flen:
                            break
                        frame = buf[s:s + flen]
                        buf = buf[s + flen:]
                        if frame[:2] == b'\xff\xd8':
                            yield (
                                b"--myboundary\r\nContent-Type: image/jpeg\r\n"
                                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                                + frame + b"\r\n"
                            )
                    else:
                        # Estratégia 2: detecta frame pelos marcadores JPEG (câmeras sem Content-Length)
                        start = buf.find(b'\xff\xd8')
                        if start < 0:
                            break
                        end = buf.find(b'\xff\xd9', start + 2)
                        if end < 0:
                            break
                        frame = buf[start:end + 2]
                        buf = buf[end + 2:]
                        yield (
                            b"--myboundary\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                            + frame + b"\r\n"
                        )
                if len(buf) > 2_000_000:
                    buf = buf[-200_000:]
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    return StreamingResponse(
        generate(active_resp, first_buf),
        media_type="multipart/x-mixed-replace; boundary=myboundary",
        headers={"Cache-Control": "no-store, no-cache"},
    )


@router.get("/maintenance/live/{ip}")
def maintenance_live_snapshot(ip: str, user: str = "admin", password: str = ""):
    """Snapshot único direto da câmera — fallback quando MJPEG não disponível."""
    from fastapi.responses import Response
    import requests as _req
    from requests.auth import HTTPBasicAuth

    for url in [
        f"http://{ip}/cgi-bin/snapshot.cgi?channel=1",
        f"http://{ip}/cgi-bin/snapshot.cgi?channel=0",
        f"http://{ip}/cgi-bin/snapshot.cgi",
    ]:
        for auth in (HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
            try:
                r = _req.get(url, auth=auth, timeout=5)
                if r.status_code == 200 and r.content[:2] == b'\xff\xd8':
                    return Response(content=r.content, media_type="image/jpeg",
                                    headers={"Cache-Control": "no-store"})
            except Exception:
                continue
    return Response(status_code=503)


def _recorder_row_for_host(host: str) -> Optional[Dict[str, Any]]:
    """Acha a linha do inventario de DVR/NVR (qualquer canal) para este
    host, no tenant atual -- mesmo padrao de _camera_row_for_ip em
    cameras.py, so que para gravador."""
    alvo = str(host or "").strip()
    if not alvo:
        return None
    for fonte in ("dvr", "nvr"):
        for linha in _load_rows_for_source(fonte):
            if isinstance(linha, dict) and str(linha.get("host") or "").strip() == alvo:
                return linha
    return None


def _host_in_recorder_inventory(host: str) -> bool:
    return _recorder_row_for_host(host) is not None


@router.api_route("/maintenance/web/{ip}/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/maintenance/web/{ip}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def maintenance_camera_web_proxy(ip: str, request: Request, path: str = ""):
    """Proxy HTTP da interface web da camera/DVR/NVR via servidor/WireGuard."""
    # so o efeito colateral de validar importa aqui (400 IP publico, 403
    # fora do inventario do tenant) -- a URL de fato usada no fetch vem de
    # fetch_device, que tenta https e http.
    _camera_web_target_url(ip, path, str(request.url.query or ""))

    headers: dict[str, str] = {
        "User-Agent": request.headers.get("user-agent") or "SightOps device web proxy",
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Language": request.headers.get("accept-language") or "pt-BR,pt;q=0.9,en;q=0.8",
    }
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    cookie = _camera_cookie_header(request)
    if cookie:
        headers["Cookie"] = cookie

    # Se o navegador ja mandou Authorization e porque o proprio proxy pediu
    # (via 401 + WWW-Authenticate) e o operador digitou a senha no dialogo
    # nativo -- essa credencial tem prioridade sobre a senha salva no
    # sistema (que pode nao existir ou estar errada, motivo pelo qual o
    # navegador esta mandando Authorization agora). Repassa direto e nao
    # deixa fetch_device tentar "ajudar" com auth=Basic/Digest por cima.
    auth_navegador = request.headers.get("authorization")
    if auth_navegador:
        headers["Authorization"] = auth_navegador
        username, password = "", ""
    else:
        username, password = resolve_camera_password(ip, "", "")

    body = await request.body()

    _seed_web_proxy_scheme_from_disk(ip)
    try:
        # fetch_device faz uma chamada de rede sincrona (requests) que pode
        # levar segundos -- sem to_thread, ela trava a unica thread do event
        # loop, e TODA a API (todo cliente, toda rota) fica parada esperando
        # essa camera responder. E o motivo mais provavel do "demora muito"
        # ao clicar em Web: cada sub-recurso da pagina da camera (JS/CSS/
        # imagem) tinha que esperar o anterior terminar em vez de andar em
        # paralelo.
        upstream = await asyncio.to_thread(
            fetch_device,
            ip, path, str(request.url.query or ""), request.method, headers, body,
            username=username, password=password, http_port=_device_http_port(ip),
        )
    except DeviceUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    esquema_usado = get_cached_scheme(ip)
    if esquema_usado:
        _persist_web_proxy_scheme(ip, esquema_usado)

    resp_headers: dict[str, str] = {
        "Cache-Control": "no-store",
        "X-Frame-Options": "SAMEORIGIN",
        **filter_response_headers(upstream.headers),
    }
    location = upstream.headers.get("location")
    if location:
        resp_headers["Location"] = _proxy_location_header(location, ip=ip)
    set_cookie = upstream.headers.get("set-cookie")
    if set_cookie:
        resp_headers["Set-Cookie"] = set_cookie

    media_type = upstream.headers.get("content-type") or "application/octet-stream"
    content = _rewrite_camera_web_content(upstream.content or b"", ip=ip, content_type=media_type)
    return Response(content=content, status_code=upstream.status_code, media_type=media_type, headers=resp_headers)


@router.post("/maintenance/stream_register/{ip}")
def maintenance_stream_register(ip: str, payload: Dict[str, Any]):
    """Registra a camera no go2rtc (idempotente) e devolve o nome do stream
    para o player MSE conectar em /go2rtc/api/ws?src=<stream_name>.

    Credenciais vem no corpo da requisicao (nao em query string) para nao
    vazar a senha da camera em log de acesso/proxy -- mesma classe de
    problema do vazamento de credenciais do go2rtc corrigido antes."""
    from fastapi.responses import JSONResponse

    # Nao usar HTTP 401 aqui: e o codigo que o wrapper api() do frontend
    # (core.js) trata como "sessao expirada" e desloga o usuario -- e um
    # 401 diferente (senha DESTA camera desconhecida, nao do login).
    user, password = resolve_camera_password(ip, _as_str(payload.get("user")), _as_str(payload.get("password")))
    if not password:
        return {"ok": False, "error": "credential_required"}
    try:
        subtype = int(payload.get("subtype") or 1)
    except (TypeError, ValueError):
        subtype = 1
    vendor = _as_str(payload.get("vendor"))
    model = _as_str(payload.get("model"))

    try:
        stream_name = register_stream(ip=ip, user=user, password=password, subtype=subtype, vendor=vendor, model=model)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return {"ok": True, "stream_name": stream_name}


@router.post("/maintenance/stream_unregister/{ip}")
def maintenance_stream_unregister(ip: str, subtype: int = 1):
    """Desregistra a camera do go2rtc (chamado ao fechar a tela de live view;
    a limpeza automatica periodica cobre o caso de aba fechada sem aviso)."""
    unregister_stream(ip=ip, subtype=subtype)
    return {"ok": True}


# ── PTZ / reboot / rename ─────────────────────────────────────────────────────
# Movidos de app/api/endpoints/cameras.py: nao sao usados pela tela de
# Inventario > Cameras IP (que fala com /api/maintenance/batch/reboot para
# reboot em lote), so pela tela de Manutencao (frontend/js/maintenance.js).

class PTZMoveRequest(BaseModel):
    ip: str
    user: str = ""
    password: str = ""
    direction: str
    channel: int = 1
    speed: int = 4
    duration_ms: int = 350


@router.get("/cameras/ptz_capability", tags=["cameras"])
def api_cameras_ptz_capability(
    ip: str = Query(...),
    user: str = Query(""),
    password: str = Query("", alias="pass"),
    channel: int = Query(1, ge=1, le=32),
) -> Dict[str, Any]:
    import requests
    from requests.auth import HTTPDigestAuth

    ip = (ip or "").strip()
    if not ip:
        return {"ok": False, "error": "ip obrigatorio"}
    if not _ip_in_inventory(ip):
        return {"ok": False, "error": "IP nao encontrado no inventario deste cliente"}
    user, password = resolve_camera_password(ip, user, password)
    if not password:
        return {"ok": False, "error": "credential_required"}

    brand = ""
    model = ""
    title = ""
    try:
        inv = load_inventory_json() or []
        for r in inv:
            if str(r.get("ip") or "").strip() == ip:
                brand = str(r.get("fabricante") or "").strip().lower()
                model = str(r.get("modelo") or r.get("model") or "").strip().lower()
                title = str(r.get("titulo") or r.get("title") or "").strip().lower()
                break
    except Exception:
        pass

    hint_text = " ".join([brand, model, title]).lower()
    hint_is_ptz = any(k in hint_text for k in ["ptz", "speed dome", "speeddome", "sd5", "sd6", "sd49", "sd59"])

    def _try_get(url: str):
        for auth in (HTTPDigestAuth(user, password), (user, password)):
            try:
                r = requests.get(url, auth=auth, timeout=4, verify=False, headers={"Accept": "*/*"})
                if r.status_code == 200:
                    return True, r.status_code
            except Exception:
                continue
        return False, None

    probe_ok = False
    probe_url = ""

    if ("hik" in brand) or ("hilook" in brand):
        probe_url = f"http://{ip}/ISAPI/PTZCtrl/channels/{int(channel)}/capabilities"
        probe_ok, _ = _try_get(probe_url)
    elif ("dahua" in brand) or ("intelbras" in brand):
        probe_url = f"http://{ip}/cgi-bin/ptz.cgi?action=getStatus&channel={max(0, int(channel)-1)}"
        probe_ok, _ = _try_get(probe_url)
    else:
        # Best effort: testa os dois formatos
        probe_url = f"http://{ip}/cgi-bin/ptz.cgi?action=getStatus&channel={max(0, int(channel)-1)}"
        probe_ok, _ = _try_get(probe_url)
        if not probe_ok:
            probe_url = f"http://{ip}/ISAPI/PTZCtrl/channels/{int(channel)}/capabilities"
            probe_ok, _ = _try_get(probe_url)

    capable = bool(probe_ok or hint_is_ptz)
    return {
        "ok": True,
        "capable": capable,
        "probe_ok": bool(probe_ok),
        "hint_is_ptz": bool(hint_is_ptz),
        "brand": brand or "",
        "model": model or "",
        "probe_url": probe_url,
    }


@router.post("/cameras/ptz_capability", tags=["cameras"])
def api_cameras_ptz_capability_post(payload: Dict[str, Any]) -> Dict[str, Any]:
    return api_cameras_ptz_capability(
        ip=str(payload.get("ip") or ""),
        user=str(payload.get("user") or payload.get("username") or ""),
        password=str(payload.get("pass") or payload.get("password") or ""),
        channel=int(payload.get("channel") or 1),
    )


@router.post("/cameras/ptz_move", tags=["cameras"])
def api_cameras_ptz_move(payload: PTZMoveRequest) -> Dict[str, Any]:
    import requests
    from requests.auth import HTTPDigestAuth

    ip = (payload.ip or "").strip()
    direction = (payload.direction or "").strip().lower()
    channel = int(payload.channel or 1)
    speed = max(1, min(8, int(payload.speed or 4)))
    duration_ms = max(80, min(5000, int(payload.duration_ms or 350)))

    if not ip:
        return {"ok": False, "error": "ip obrigatorio"}
    if not _ip_in_inventory(ip):
        return {"ok": False, "error": "IP nao encontrado no inventario deste cliente"}
    user, password = resolve_camera_password(ip, payload.user, payload.password)
    if not password:
        return {"ok": False, "error": "credential_required"}

    brand = ""
    try:
        inv = load_inventory_json() or []
        for r in inv:
            if str(r.get("ip") or "").strip() == ip:
                brand = str(r.get("fabricante") or "").strip().lower()
                break
    except Exception:
        pass

    if direction in ("left", "right", "up", "down"):
        dh_code_map = {"left": "Left", "right": "Right", "up": "Up", "down": "Down"}
        hk_vec_map = {
            "left": (-speed * 10, 0, 0),
            "right": (speed * 10, 0, 0),
            "up": (0, speed * 10, 0),
            "down": (0, -speed * 10, 0),
        }
    elif direction in ("zoomin", "zoomout"):
        dh_code_map = {"zoomin": "ZoomTele", "zoomout": "ZoomWide"}
        hk_vec_map = {
            "zoomin": (0, 0, speed * 10),
            "zoomout": (0, 0, -speed * 10),
        }
    elif direction == "stop":
        dh_code_map = {}
        hk_vec_map = {}
    else:
        return {"ok": False, "error": "direction invalida"}

    def _request(method: str, url: str, data: str | None = None) -> requests.Response | None:
        for auth in (HTTPDigestAuth(user, password), (user, password)):
            try:
                if method == "PUT":
                    r = requests.put(url, auth=auth, timeout=6, verify=False, data=data, headers={"Content-Type": "application/xml"})
                else:
                    r = requests.get(url, auth=auth, timeout=6, verify=False)
                if r.status_code in (200, 201, 202, 204):
                    return r
            except Exception:
                continue
        return None

    # Hikvision/HiLook path
    if ("hik" in brand) or ("hilook" in brand):
        base = f"http://{ip}/ISAPI/PTZCtrl/channels/{int(channel)}/continuous"
        if direction == "stop":
            stop_xml = "<PTZData><pan>0</pan><tilt>0</tilt><zoom>0</zoom></PTZData>"
            r = _request("PUT", base, stop_xml)
            return {"ok": bool(r), "brand": "hikvision", "method": "isapi.stop"}
        pan, tilt, zoom = hk_vec_map.get(direction, (0, 0, 0))
        move_xml = f"<PTZData><pan>{pan}</pan><tilt>{tilt}</tilt><zoom>{zoom}</zoom></PTZData>"
        stop_xml = "<PTZData><pan>0</pan><tilt>0</tilt><zoom>0</zoom></PTZData>"
        r1 = _request("PUT", base, move_xml)
        if not r1:
            return {"ok": False, "error": "falha ao iniciar PTZ (Hikvision)"}
        time.sleep(duration_ms / 1000.0)
        _request("PUT", base, stop_xml)
        return {"ok": True, "brand": "hikvision", "method": "isapi.continuous"}

    # Dahua/Intelbras path
    ch0 = max(0, int(channel) - 1)
    if direction == "stop":
        # stop abrangente
        for code in ("Left", "Right", "Up", "Down", "ZoomTele", "ZoomWide"):
            stop_url = f"http://{ip}/cgi-bin/ptz.cgi?action=stop&channel={ch0}&code={code}&arg1=0&arg2={speed}&arg3=0"
            _request("GET", stop_url)
        return {"ok": True, "brand": "dahua/intelbras", "method": "ptz.stop"}

    code = dh_code_map.get(direction)
    if not code:
        return {"ok": False, "error": "direction invalida para dahua/intelbras"}

    start_url = f"http://{ip}/cgi-bin/ptz.cgi?action=start&channel={ch0}&code={code}&arg1=0&arg2={speed}&arg3=0"
    stop_url = f"http://{ip}/cgi-bin/ptz.cgi?action=stop&channel={ch0}&code={code}&arg1=0&arg2={speed}&arg3=0"
    r1 = _request("GET", start_url)
    if not r1:
        return {"ok": False, "error": "falha ao iniciar PTZ"}
    time.sleep(duration_ms / 1000.0)
    _request("GET", stop_url)
    return {"ok": True, "brand": "dahua/intelbras", "method": "ptz.cgi"}


def _try_http_with_auth(
    method: str,
    url: str,
    user: str,
    password: str,
    *,
    timeout,
    headers: dict | None = None,
    data=None,
    success_codes: tuple[int, ...] = (200, 201, 202, 204),
):
    """Tenta uma URL de camera com Digest e depois Basic auth (nessa ordem --
    e como Hikvision/Dahua/Intelbras costumam aceitar). Se um auth der uma
    resposta de sucesso, devolve (response, None) na hora sem tentar o outro.
    Senao, tenta o proximo auth mesmo assim, e devolve o resultado (resposta
    OU erro) do ULTIMO auth tentado -- e o que reboot/rename/ptz ja faziam
    cada um com sua propria copia deste loop, aqui compartilhado.

    Usado por reboot e rename (que tem outer-loop de multiplas URLs/portas
    em volta disso, com sua propria logica de qual erro final mostrar).
    """
    response = None
    error: str | None = None
    for auth in (HTTPDigestAuth(user, password), (user, password)):
        try:
            if method == "PUT":
                r = requests.put(url, auth=auth, timeout=timeout, verify=False, headers=headers, data=data)
            else:
                r = requests.get(url, auth=auth, timeout=timeout, verify=False, headers=headers)
        except requests.exceptions.ReadTimeout:
            response, error = None, "timeout"
            continue
        except Exception as e:
            response, error = None, str(e)
            continue
        response, error = r, None
        if r.status_code in success_codes:
            return response, error
    return response, error


@router.post("/cameras/reboot", tags=["cameras"])
def api_cameras_reboot(payload: Dict[str, Any]) -> Dict[str, Any]:
    ip = (payload.get("ip") or "").strip()

    if not ip:
        return {"ok": False, "error": "IP obrigatÃ³rio"}
    if not _ip_in_inventory(ip):
        return {"ok": False, "error": "IP nao encontrado no inventario deste cliente"}
    user, password = resolve_camera_password(
        ip, str(payload.get("user") or ""), str(payload.get("pass") or payload.get("password") or "")
    )
    if not password:
        return {"ok": False, "error": "credential_required"}

    brand = ""
    try:
        inv = load_inventory_json() or []
        for r in inv:
            if str(r.get("ip") or "").strip() == ip:
                brand = str(r.get("fabricante") or "").strip().lower()
                break
    except Exception:
        brand = ""

    attempts: list[tuple[str, str, str]] = []

    def add_attempt(name: str, method: str, url: str):
        attempts.append((name, method, url))
        if url.startswith("http://"):
            attempts.append((name + "_https", method, "https://" + url[len("http://"):]))

    is_hik = ("hik" in brand) or ("hilook" in brand)
    is_dahua = ("dahua" in brand) or ("intelbras" in brand)

    if is_hik:
        add_attempt("hikvision_isapi", "PUT", f"http://{ip}/ISAPI/System/reboot")
    if is_dahua:
        add_attempt("magicbox", "GET", f"http://{ip}/cgi-bin/magicBox.cgi?action=reboot")
        add_attempt("configManager", "GET", f"http://{ip}/cgi-bin/configManager.cgi?action=reboot")

    add_attempt("isapi", "PUT", f"http://{ip}/ISAPI/System/reboot")
    add_attempt("magicbox_fallback", "GET", f"http://{ip}/cgi-bin/magicBox.cgi?action=reboot")
    add_attempt("configManager_fallback", "GET", f"http://{ip}/cgi-bin/configManager.cgi?action=reboot")

    last_err = ""
    # Guarda separadamente o ultimo erro que veio de uma resposta HTTP real
    # (a camera respondeu, so recusou) -- isso e muito mais util pro usuario
    # do que "Sem resposta" de uma tentativa https tardia numa porta que
    # nem esta aberta. Sem isso, um 401 real (senha errada/sem permissao no
    # endpoint certo) podia ficar escondido atras do erro de conexao da
    # ultima tentativa da lista.
    best_status_err = ""
    for name, method, url in attempts:
        data = b"" if method == "PUT" else None
        r, _err = _try_http_with_auth(method, url, user, password, timeout=5, headers={"Accept": "application/xml"}, data=data)
        if r is None:
            last_err = f"{name}: Sem resposta"
            continue
        # 401/403 NAO e sucesso: e a camera recusando o comando por
        # credencial errada ou usuario sem permissao de "Gerenciamento
        # do Sistema" no ISAPI (comum na Hikvision) -- contar isso como
        # "ok" fazia o front mostrar "reboot enviado" mesmo quando a
        # camera nunca recebeu um comando autenticado e nao reiniciava.
        if r.status_code in (200, 201, 202, 204):
            return {"ok": True, "method": name, "status": r.status_code}
        last_err = f"{name}: HTTP {r.status_code}"
        if not best_status_err:
            best_status_err = last_err

    return {"ok": False, "error": best_status_err or last_err or "Falha ao reiniciar"}


@router.post("/cameras/rename", tags=["cameras"])
def api_cameras_rename(payload: Dict[str, Any]) -> Dict[str, Any]:
    import urllib.parse

    ip = (payload.get("ip") or "").strip()
    title = (payload.get("title") or payload.get("titulo") or "").strip()

    port = payload.get("port", 80)
    channel = payload.get("channel", 1)

    try:
        port = int(port) if port is not None else 80
    except Exception:
        port = 80

    try:
        channel = int(channel) if channel is not None else 1
    except Exception:
        channel = 1

    if not ip:
        return {"ok": False, "error": "IP obrigatÃ³rio"}
    if not title:
        return {"ok": False, "error": "TÃ­tulo obrigatÃ³rio"}
    if channel < 1:
        return {"ok": False, "error": "Channel deve ser >= 1"}

    def _persist_inventory_title() -> bool:
        try:
            rows = load_inventory_json() or []
            changed = False
            for r in rows:
                if str(r.get("ip") or "").strip() == ip:
                    r["titulo"] = title
                    changed = True
                    break
            if changed:
                save_inventory_json(rows)
            return changed
        except Exception:
            return False

    if not _ip_in_inventory(ip):
        return {"ok": False, "error": "IP nao encontrado no inventario deste cliente", "inventory_updated": False}
    user, password = resolve_camera_password(
        ip, str(payload.get("user") or payload.get("username") or ""), str(payload.get("pass") or payload.get("password") or "")
    )
    if not password:
        return {
            "ok": False,
            "error": "Informe usuÃ¡rio e senha no topo da aba ManutenÃ§Ã£o",
            "ip": ip,
            "title": title,
            "inventory_updated": _persist_inventory_title(),
        }

    # Brand hint from inventory (helps route first attempt for Hikvision/HiLook)
    brand = ""
    try:
        inv = load_inventory_json() or []
        for r in inv:
            if str(r.get("ip") or "").strip() == ip:
                brand = str(r.get("fabricante") or "").strip().lower()
                break
    except Exception:
        brand = ""

    is_hik = ("hik" in brand) or ("hilook" in brand)
    idx0 = channel - 1
    q_title = urllib.parse.quote(title)

    # Candidate ports: requested port first, then common management ports.
    port_candidates: list[int] = []
    for p in [port, 80, 8000, 443]:
        try:
            pp = int(p)
            if 1 <= pp <= 65535 and pp not in port_candidates:
                port_candidates.append(pp)
        except Exception:
            continue

    attempts: list[dict[str, Any]] = []

    def _add_attempt(name: str, method: str, url: str, data: str | None = None, content_type: str | None = None) -> None:
        attempts.append({"name": name, "method": method, "url": url, "data": data, "content_type": content_type})

    # Hikvision/HiLook rename via ISAPI (preferred for Hikvision)
    hik_xml = f"<VideoInputChannel><id>{int(channel)}</id><name>{title}</name></VideoInputChannel>"
    hik_proxy_xml = f"<InputProxyChannel><id>{int(channel)}</id><name>{title}</name></InputProxyChannel>"

    for p in port_candidates:
        for scheme in ("http", "https"):
            # Avoid very common dead combinations that waste time.
            if scheme == "https" and p == 80:
                continue
            if scheme == "http" and p == 443:
                continue

            base = f"{scheme}://{ip}:{p}"
            _add_attempt(
                "hikvision_isapi_videoinput",
                "PUT",
                f"{base}/ISAPI/System/Video/inputs/channels/{int(channel)}",
                hik_xml,
                "application/xml",
            )
            _add_attempt(
                "hikvision_isapi_inputproxy",
                "PUT",
                f"{base}/ISAPI/ContentMgmt/InputProxy/channels/{int(channel)}",
                hik_proxy_xml,
                "application/xml",
            )

            # Dahua/Intelbras style rename (also used as fallback)
            _add_attempt(
                "dahua_configmanager",
                "GET",
                f"{base}/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[{idx0}].Name={q_title}",
            )

    # Try family-specific path first, then generic fallback.
    if is_hik:
        attempts.sort(key=lambda a: 0 if str(a.get("name", "")).startswith("hikvision_") else 1)
    else:
        attempts.sort(key=lambda a: 0 if str(a.get("name", "")).startswith("dahua_") else 1)

    last_err = ""
    for at in attempts:
        method = str(at.get("method") or "GET").upper()
        url = str(at.get("url") or "")
        data = at.get("data")
        ctype = at.get("content_type")
        name = str(at.get("name") or "rename")
        headers = {"Accept": "*/*"}
        if ctype:
            headers["Content-Type"] = ctype

        r, err = _try_http_with_auth(method, url, user, password, timeout=(2.5, 5.5), headers=headers, data=data)
        if r is not None and r.status_code in (200, 201, 202, 204):
            _persist_inventory_title()
            return {"ok": True, "status": r.status_code, "url": url, "method": name}
        last_err = f"{name}: HTTP {r.status_code}" if r is not None else f"{name}: {err}"

    return {"ok": False, "error": last_err or "Falha ao renomear", "inventory_updated": False}


# ── Novos endpoints batch ─────────────────────────────────────────────────────

@router.post("/maintenance/batch/test")
def maintenance_batch_test(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}
    results = [_get_firmware_one(_as_str(ip), user, password) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Teste de acesso: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/snapshot_force")
def maintenance_batch_snapshot_force(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}
    results = [_force_snapshot_one_mnt(_as_str(ip), user, password) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Snapshot forçado: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/time_check")
def maintenance_batch_time_check(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}
    results = [_get_cam_time_one(_as_str(ip), user, password) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Hora verificada: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/mirror")
def maintenance_batch_mirror(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    mirror = bool(payload.get("mirror", False))
    flip = bool(payload.get("flip", False))
    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}
    results = [_set_mirror_one(_as_str(ip), user, password, mirror, flip) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Espelhar/Virar: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/day_night")
def maintenance_batch_day_night(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    mode = int(payload.get("mode", 0))
    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}
    results = [_set_day_night_one(_as_str(ip), user, password, mode) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Modo dia/noite: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/video_quality")
def maintenance_batch_video_quality(payload: Dict[str, Any]) -> Dict[str, Any]:
    user = _as_str(payload.get("user"))
    password = _as_str(payload.get("pass"))
    ips = payload.get("ips") or []
    bitrate = payload.get("bitrate")
    fps = payload.get("fps")
    codec = _as_str(payload.get("codec"))
    if not user or not password:
        return {"ok": False, "error": "user e pass sao obrigatorios"}
    if not isinstance(ips, list) or not ips:
        return {"ok": False, "error": "ips vazio"}
    results = [_set_video_quality_one(_as_str(ip), user, password,
               int(bitrate) if bitrate else None,
               int(fps) if fps else None,
               codec or None) for ip in ips]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"Qualidade de vídeo: {ok_n} ok, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/network_config")
def maintenance_batch_network_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    targets  = payload.get("targets", [])   # [{old_ip, new_ip}]
    mask     = _as_str(payload.get("mask", ""))
    gateway  = _as_str(payload.get("gateway", ""))
    user     = _as_str(payload.get("user", "admin"))
    password = _as_str(payload.get("pass", ""))

    if not targets:
        return {"ok": False, "error": "Nenhuma câmera informada"}

    results = []
    for t in targets:
        old_ip = _as_str(t.get("old_ip", ""))
        new_ip = _as_str(t.get("new_ip", "")) or old_ip
        use_mask    = mask
        use_gateway = gateway
        r = _change_ip_one(
            ip=old_ip,
            new_ip=new_ip,
            mask=use_mask,
            gateway=use_gateway,
            dns1="",
            dns2="",
            user=user,
            password=password,
        )
        results.append(r)

    ok_n   = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"{ok_n} câmeras configuradas, {fail_n} falhas.", "results": results}


@router.post("/maintenance/batch/shift_ips")
def maintenance_batch_shift_ips(payload: Dict[str, Any]) -> Dict[str, Any]:
    prefix     = _as_str(payload.get("prefix", ""))
    start      = int(payload.get("start_octet", 0))
    end        = int(payload.get("end_octet", 0))
    delta      = int(payload.get("delta", 1))
    user       = _as_str(payload.get("user", "admin"))
    password   = _as_str(payload.get("pass", ""))
    mask       = _as_str(payload.get("mask", ""))
    gateway    = _as_str(payload.get("gateway", ""))

    if not prefix or start < 1 or end > 254 or start > end or delta == 0:
        return {"ok": False, "error": "Parâmetros inválidos"}

    octets = list(range(start, end + 1))
    # Shift up → highest first to avoid colisão; shift down → lowest first
    octets = sorted(octets, reverse=(delta > 0))

    results = []
    for octet in octets:
        old_ip  = f"{prefix}{octet}"
        new_oct = octet + delta
        new_ip  = f"{prefix}{new_oct}"
        if new_oct < 1 or new_oct > 254:
            results.append({"ip": old_ip, "new_ip": new_ip, "ok": False, "msg": "Octet fora do intervalo (1–254)"})
            continue
        r = _change_ip_one(
            ip=old_ip,
            new_ip=new_ip,
            mask=mask,
            gateway=gateway,
            dns1="",
            dns2="",
            user=user,
            password=password,
        )
        results.append(r)

    ok_n   = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    return {"ok": fail_n == 0, "message": f"{ok_n} IPs alterados, {fail_n} falhas.", "results": results}


@router.post("/scripts/netwatch")
def scripts_netwatch(payload: Dict[str, Any]) -> Dict[str, Any]:
    token = _as_str(payload.get("token"))
    chat = _as_str(payload.get("chat"))
    interval = _as_str(payload.get("interval")) or "1m"
    timeout = _as_str(payload.get("timeout")) or "2s"
    site = _as_str(payload.get("site"))
    inv_mode = _normalize_ip_inventory_mode(payload.get("inv_mode") or payload.get("mode") or "olt")

    if not token or not chat:
        return {"success": False, "error": "token e chat sao obrigatorios"}

    script = BASE_DIR / "tools" / "mk_netwatch_from_inventory.py"
    args = ["--token", token, "--chat", chat, "--interval", interval, "--timeout", timeout]
    if site:
        args.extend(["--site", site])
    tenant_slug = get_current_tenant_slug()
    if tenant_slug:
        args.extend(["--tenant", tenant_slug])
    ok, stdout, stderr, err = _run_script(script, env={}, args=args)

    out_file = _netwatch_output_file(site)
    script_content = ""
    try:
        if out_file.exists():
            script_content = out_file.read_text(encoding="utf-8")
    except Exception:
        script_content = ""
    cameras = _netwatch_count_entries(script_content)

    if not ok:
        return {"success": False, "error": err, "stdout": stdout, "stderr": stderr, "cameras": cameras}

    return {
        "success": True,
        "cameras": cameras,
        "site": site,
        "filename": out_file.name,
        "download_url": f"/api/scripts/netwatch/download?site={quote(site)}" if site else "/api/scripts/netwatch/download",
        "script": script_content,
        "stdout": stdout,
        "stderr": stderr,
    }


@router.get("/scripts/netwatch/download")
def scripts_netwatch_download(site: str = "") -> FileResponse:
    out_file = _netwatch_output_file(site)
    if not out_file.exists():
        raise HTTPException(status_code=404, detail="Gere o script Netwatch antes de baixar.")
    return FileResponse(
        path=out_file,
        media_type="text/plain",
        filename=out_file.name,
    )


@router.post("/scripts/zabbix")
def scripts_zabbix(payload: Dict[str, Any]) -> Dict[str, Any]:
    effective = _zabbix_effective_sync_config(payload)
    url = _normalize_zabbix_url(payload.get("url") or effective.get("url"))
    user = _as_str(payload.get("user") or effective.get("user"))
    password = _as_str(payload.get("pass") or payload.get("password") or effective.get("pass"))
    tenant_slug = _zabbix_tenant_slug()
    group = _zabbix_tenant_group(_as_str(payload.get("group") or effective.get("group")) or "Cameras", tenant_slug)
    template = _as_str(payload.get("template") or effective.get("template")) or "Template Module ICMP Ping"
    template_dvr = _as_str(payload.get("template_dvr") or effective.get("template_dvr")) or "Template Cam-Snapshot DVR Channel"
    dvr_user = _as_str(payload.get("dvr_user")) or "admin"
    dvr_pass = _as_str(payload.get("dvr_pass"))
    tg_auto = bool(payload.get("tg_auto", False))
    tg_token = _as_str(payload.get("tg_token"))
    tg_chat = _as_str(payload.get("tg_chat"))
    # {"INTERBLOCOS": "-100123", ...}. O que vier na tela manda; o que nao vier,
    # aproveita o que ja estava salvo -- assim sincronizar um site nao apaga a
    # configuracao dos outros.
    _mapa = payload.get("tg_chat_by_site")
    tg_chat_by_site = dict(effective.get("tg_chat_by_site") or {})
    if isinstance(_mapa, dict):
        for _site, _chat in _mapa.items():
            _site = " ".join(_as_str(_site).split()).strip()
            _chat = _as_str(_chat).strip()
            if not _site:
                continue
            if _chat:
                tg_chat_by_site[_site] = _chat
            else:
                tg_chat_by_site.pop(_site, None)   # campo esvaziado = desligar o site
    source = _as_str(payload.get("source") or "ip").lower()
    site = _as_str(payload.get("site"))
    # a tela pode mandar varios sites de uma vez; "site" (texto) continua
    # valendo pra quem chama a API do jeito antigo
    _sel = payload.get("sites")
    sites_sel = [_as_str(x) for x in _sel if _as_str(x)] if isinstance(_sel, list) else []
    if not sites_sel and site:
        sites_sel = [site]
    inv_mode = _normalize_ip_inventory_mode(payload.get("inv_mode") or payload.get("mode") or "olt")

    if not url or not user or not password:
        return {"error": "url, user e pass sao obrigatorios"}
    if source in ("dvr", "nvr") and (not dvr_user or not dvr_pass):
        return {"error": "Para source=dvr/nvr informe dvr_user e dvr_pass"}

    script = BASE_DIR / "tools" / "mk_zabbix_from_inventory.py"
    if len(sites_sel) > 1:
        # varios sites: carrega tudo e filtra pelo conjunto escolhido
        _todas = _load_rows_for_source(source, mode=inv_mode) or []
        _alvos = {x.strip().lower() for x in sites_sel}

        def _e_dos_escolhidos(linha):
            for chave in ("site", "site_name", "local", "LOCAL"):
                if str(linha.get(chave) or "").strip().lower() in _alvos:
                    return True
            return False

        _linhas = [r for r in _todas if isinstance(r, dict) and _e_dos_escolhidos(r)]
    else:
        _linhas = _load_rows_for_source(source, site=(sites_sel[0] if sites_sel else ""), mode=inv_mode)
    inv_rows = _build_zabbix_rows(source, _linhas)
    tmp_inv = _zabbix_tmp_inventory_path(source, inv_mode)
    try:
        tmp_inv.parent.mkdir(parents=True, exist_ok=True)
        tmp_inv.write_text(json.dumps(inv_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"error": f"falha ao preparar inventario para Zabbix: {e}"}

    env = {
        "INV_PATH": str(tmp_inv),
        "ZBX_URL": url,
        "ZBX_USER": user,
        "ZBX_PASS": password,
        "ZBX_GROUP": group,
        # Prefixa o nome de todo host no Zabbix pelo tenant -- sem isso, dois
        # clientes com camera no mesmo IP privado colidem no mesmo host e um
        # sincronismo sobrescreve os dados do outro (ver build_host_name em
        # tools/mk_zabbix_from_inventory.py).
        "ZBX_TENANT": tenant_slug,
        # Poda so quando o sync e do inventario COMPLETO. Com filtro de
        # site a lista enviada e parcial, e remover "o que nao veio"
        # apagaria os hosts dos outros sites.
        # Tambem nao poda com inventario vazio: zero linhas quase sempre e
        # fonte/filtro errado, e a poda apagaria o grupo inteiro.
        "ZBX_PRUNE": "0" if (sites_sel or not inv_rows) else "1",
        "ZBX_LEGACY_DEFAULT_HOSTNAMES": "1" if tenant_slug == "default" else "0",
        "ZBX_TEMPLATE": template,
        "ZBX_TEMPLATE_DVR": template_dvr,
        "ZBX_DVR_USER": dvr_user,
        "ZBX_DVR_PASS": dvr_pass,
        "ZBX_TG_AUTO": "1" if tg_auto else "0",
        "ZBX_TG_TOKEN": tg_token,
        "ZBX_TG_CHAT": tg_chat,
        "ZBX_TG_CHAT_BY_SITE": json.dumps(tg_chat_by_site, ensure_ascii=False),
    }

    ok, stdout, stderr, err = _run_script(script, env=env, args=[])
    if not ok:
        return {"error": err, "stdout": stdout, "stderr": stderr}

    # Persistimos a última configuração para sincronismo automático de status.
    if source in ("ip", "dvr", "nvr"):
        try:
            s = _load_settings()
            key = "zabbix_ip_sync" if source == "ip" else "zabbix_dvr_sync"
            s[key] = {
                "enabled": True,
                "url": url,
                "user": user,
                "pass": password,
                "group": group,
                "template": template,
                "template_dvr": template_dvr,
                "dvr_user": dvr_user,
                "dvr_pass": dvr_pass,
                "site": site,
                "inv_mode": inv_mode,
                "tg_token": tg_token,
                "tg_chat": tg_chat,
                "tg_chat_by_site": tg_chat_by_site,
                "tenant_slug": tenant_slug,
            }
            _save_settings(s)
        except Exception:
            pass

    return {"ok": True, "source": source, "site": site, "mode": inv_mode, "rows_used": len(inv_rows), "stdout": stdout, "stderr": stderr}


def _zabbix_api_call(url: str, method: str, params: Any, auth: str | None = None, req_id: int = 1) -> Any:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
    if auth:
        payload["auth"] = auth
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"{method}: {data['error']}")
    return data.get("result")


def _zabbix_login(url: str, user: str, password: str) -> str:
    return _as_str(_zabbix_api_call(url, "user.login", {"username": user, "password": password}, req_id=1))


@router.post("/scripts/zabbix/status-sync")
def scripts_zabbix_status_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    source = _as_str(payload.get("source") or "ip").lower()
    if source != "ip":
        return {"ok": False, "error": "Por enquanto o status-sync Zabbix automatico e para Cameras IP."}

    settings = _load_settings()
    cfg_saved = settings.get("zabbix_ip_sync") if isinstance(settings.get("zabbix_ip_sync"), dict) else {}
    cfg = _zabbix_effective_sync_config(cfg_saved)
    tenant_slug = _zabbix_tenant_slug()
    url = _normalize_zabbix_url(payload.get("url") or cfg.get("url"))
    user = _as_str(payload.get("user") or cfg.get("user"))
    password = _as_str(payload.get("pass") or cfg.get("password") or cfg.get("pass"))
    if "site" in payload:
        site = _as_str(payload.get("site"))
    else:
        site = _as_str(cfg.get("site"))
    mode = _normalize_ip_inventory_mode(payload.get("mode") or payload.get("inv_mode") or cfg.get("inv_mode") or "olt")
    validate_offline = bool(payload.get("validate_offline", True))

    rows_by_mode = _load_ip_rows_by_mode(site="", mode=mode)
    wanted_site = site.strip().lower()

    def _row_matches_site(row: Dict[str, Any]) -> bool:
        if not wanted_site:
            return True
        vals = [
            _as_str(row.get("site")).lower(),
            _as_str(row.get("site_name")).lower(),
            _as_str(row.get("local") or row.get("LOCAL")).lower(),
        ]
        return any(v == wanted_site for v in vals if v)

    ip_to_targets: Dict[str, List[tuple[str, int]]] = {}
    for item_mode, rows in rows_by_mode.items():
        for idx, row in enumerate(rows):
            if not _row_matches_site(row):
                continue
            ip = _as_str(row.get("ip") or row.get("IP"))
            if ip:
                ip_to_targets.setdefault(ip, []).append((item_mode, idx))
    if not ip_to_targets:
        return {"ok": True, "source": "zabbix", "total": 0, "online": 0, "offline": 0, "unknown": 0, "updated": 0}

    if not url or not user or not password:
        return {
            "ok": False,
            "error": "Zabbix nao configurado automaticamente: defina SIGHTOPS_ZABBIX_URL/USER/PASS ou suba o container zabbix-prod-web.",
        }

    ensure_hosts_raw = payload.get("ensure_hosts", True)
    ensure_hosts = str(ensure_hosts_raw).strip().lower() not in {"0", "false", "no", "nao", "não", "off"}
    bootstrapped = False
    bootstrap_rows = 0
    if ensure_hosts:
        bootstrap = scripts_zabbix(
            {
                "source": "ip",
                "mode": mode,
                "inv_mode": mode,
                "site": site,
                "url": url,
                "user": user,
                "pass": password,
                "group": cfg.get("group") or "Cameras",
                "template": cfg.get("template") or "Template Module ICMP Ping",
            }
        )
        if not bootstrap.get("ok"):
            return {
                "ok": False,
                "error": "Falha ao garantir hosts das Cameras IP no Zabbix.",
                "bootstrap_error": bootstrap.get("error") or bootstrap.get("stderr") or bootstrap.get("stdout"),
            }
        bootstrapped = True
        bootstrap_rows = int(bootstrap.get("rows_used") or 0)

    try:
        auth = _zabbix_login(url, user, password)
        hosts = _zabbix_api_call(
            url,
            "host.get",
            {
                "output": ["hostid", "host", "name", "available"],
                "selectInterfaces": ["interfaceid", "ip", "available", "error"],
                "search": {"host": f"{_zabbix_host_safe(tenant_slug)}-"},
                "startSearch": True,
                "monitored_hosts": True,
            },
            auth,
            req_id=2,
        ) or []
        host_by_ip: Dict[str, Dict[str, Any]] = {}
        hostids: List[str] = []
        def _collect_matching_hosts(zbx_hosts: Any, require_tenant_prefix: bool = True) -> None:
            for host in zbx_hosts or []:
                if not isinstance(host, dict):
                    continue
                if require_tenant_prefix and not _zabbix_host_belongs_to_tenant(host, tenant_slug):
                    continue
                hid = _as_str(host.get("hostid"))
                if hid:
                    hostids.append(hid)
                for iface in host.get("interfaces") or []:
                    ip = _as_str((iface or {}).get("ip"))
                    if ip in ip_to_targets:
                        host_by_ip[ip] = {"host": host, "interface": iface or {}}

        _collect_matching_hosts(hosts, require_tenant_prefix=True)

        used_legacy_hosts = False
        if not host_by_ip and tenant_slug == "default":
            legacy_hosts = _zabbix_api_call(
                url,
                "host.get",
                {
                    "output": ["hostid", "host", "name", "available"],
                    "selectInterfaces": ["interfaceid", "ip", "available", "error"],
                    "monitored_hosts": True,
                },
                auth,
                req_id=22,
            ) or []
            hostids.clear()
            _collect_matching_hosts(legacy_hosts, require_tenant_prefix=False)
            used_legacy_hosts = bool(host_by_ip)

        host_status: Dict[str, str] = {}
        if hostids:
            items = _zabbix_api_call(
                url,
                "item.get",
                {
                    "hostids": hostids,
                    "output": ["hostid", "key_", "lastvalue", "lastclock", "name"],
                    "search": {"key_": "icmpping"},
                    "sortfield": "key_",
                },
                auth,
                req_id=3,
            ) or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = _as_str(item.get("key_"))
                if not key.startswith("icmpping"):
                    continue
                hid = _as_str(item.get("hostid"))
                if hid in host_status and key != "icmpping":
                    continue
                last = _as_str(item.get("lastvalue"))
                if last == "1":
                    host_status[hid] = "online"
                elif last == "0":
                    host_status[hid] = "offline"

        now = datetime.now(timezone.utc).isoformat()
        updated = online = offline = unknown = validated_online = 0
        resolved: List[Dict[str, Any]] = []
        offline_probe_ips: List[str] = []

        for ip, targets in ip_to_targets.items():
            info = host_by_ip.get(ip)
            if not info:
                unknown += len(targets)
                continue
            host = info.get("host") or {}
            iface = info.get("interface") or {}
            hid = _as_str(host.get("hostid"))
            status = host_status.get(hid)
            if not status:
                available = _as_str(iface.get("available") or host.get("available"))
                if available == "1":
                    status = "online"
                elif available == "2":
                    status = "offline"
            if not status:
                unknown += 1
                continue

            if status == "offline" and validate_offline:
                offline_probe_ips.append(ip)
            resolved.append({"ip": ip, "targets": targets, "status": status, "host": host, "hid": hid})

        probe_by_ip: Dict[str, Dict[str, Any]] = {}
        if offline_probe_ips:
            def _probe_offline_ip(target: str) -> tuple[str, Dict[str, Any]]:
                try:
                    return target, _do_ping_sync(target, 3, "auto", [80, 554, 8000, 8080, 37777, 8554])
                except Exception:
                    return target, {"online": False, "method": "auto", "error": "validacao falhou"}

            with ThreadPoolExecutor(max_workers=min(24, len(offline_probe_ips))) as pool:
                futures = [pool.submit(_probe_offline_ip, ip) for ip in offline_probe_ips]
                for fut in as_completed(futures):
                    ip, probe = fut.result()
                    probe_by_ip[ip] = probe

        touched_modes: set[str] = set()
        for item in resolved:
            ip = _as_str(item.get("ip"))
            targets = item.get("targets") if isinstance(item.get("targets"), list) else []
            status = _as_str(item.get("status"))
            host = item.get("host") or {}
            hid = _as_str(item.get("hid"))
            status_source = "zabbix"
            probe: Dict[str, Any] = {}
            if status == "offline" and validate_offline:
                probe = probe_by_ip.get(ip) or {"online": False, "method": "auto"}
                if bool(probe.get("online")):
                    status = "online"
                    status_source = "zabbix+tcp"
                    validated_online += 1

            for item_mode, idx in targets:
                rows = rows_by_mode.get(item_mode) or []
                if idx < 0 or idx >= len(rows):
                    continue
                row = rows[idx]
                row["status"] = status
                row["status_source"] = status_source
                row["status_checked_at"] = now
                row["zabbix_hostid"] = hid
                row["zabbix_host"] = _as_str(host.get("name") or host.get("host"))
                if status_source == "zabbix+tcp" or (status == "offline" and validate_offline):
                    row["status_detail"] = (
                        "zabbix_icmp_offline_but_tcp_online"
                        if bool(probe.get("online"))
                        else "zabbix_icmp_offline_tcp_unreachable"
                    )
                    row["status_check_method"] = _as_str(probe.get("method"))
                updated += 1
                touched_modes.add(item_mode)
                if status == "online":
                    online += 1
                else:
                    offline += 1

        # Este sync carrega o inventario inteiro no inicio da funcao e so
        # grava aqui no final -- no meio, ha chamadas de rede pro Zabbix
        # (login, host.get, as vezes bootstrap de host) que levam segundos, e
        # o loop de fundo roda para todos os tenants/modos a cada 60s. Se
        # outro request (ex.: upload de foto pro ImgBB) salvar o inventario
        # nesse meio tempo, um save_inventory_json(rows_by_mode...) direto
        # sobrescreveria o arquivo inteiro com esta copia em memoria, que ja
        # esta desatualizada, e apagaria o que o outro processo acabou de
        # gravar. Foi exatamente isso: upload confirmava no log ("OK") mas o
        # imgbb_url sumia minutos depois. Por isso: recarrega o inventario
        # atual bem antes de salvar e aplica so os campos de status por IP,
        # preservando qualquer outro campo que tenha mudado nesse intervalo.
        _STATUS_SYNC_FIELDS = (
            "status", "status_source", "status_checked_at", "zabbix_hostid",
            "zabbix_host", "status_detail", "status_check_method",
        )
        for item_mode in touched_modes:
            updates_by_ip: Dict[str, Dict[str, Any]] = {}
            for row in rows_by_mode.get(item_mode) or []:
                ip = _as_str(row.get("ip") or row.get("IP"))
                if ip:
                    updates_by_ip[ip] = {k: row[k] for k in _STATUS_SYNC_FIELDS if k in row}

            fresh_rows = load_inventory_json(site="", mode=item_mode) or []
            changed = False
            for row in fresh_rows:
                upd = updates_by_ip.get(_as_str(row.get("ip") or row.get("IP")))
                if upd:
                    row.update(upd)
                    changed = True
            if changed:
                save_inventory_json(fresh_rows, mode=item_mode)

        return {
            "ok": True,
            "source": "zabbix",
            "mode": mode,
            "site": site,
            "bootstrapped": bootstrapped,
            "bootstrap_rows": bootstrap_rows,
            "total": sum(len(v) for v in ip_to_targets.values()),
            "matched": len(host_by_ip),
            "updated": updated,
            "online": online,
            "offline": offline,
            "unknown": unknown,
            "validated_online": validated_online,
            "legacy_hosts_used": used_legacy_hosts,
        }
    except Exception as e:
        return {"ok": False, "error": f"Falha ao consultar Zabbix: {e}"}


@router.post("/scripts/grafana")
def scripts_grafana(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = _as_str(payload.get("url"))
    api_key = _as_str(payload.get("api_key"))
    folder_uid = _as_str(payload.get("folder_uid"))
    overwrite = bool(payload.get("overwrite", True))

    if not url or not api_key:
        return {"error": "url e api_key sao obrigatorios"}

    script = BASE_DIR / "tools" / "mk_grafana_import_dashboard.py"
    env = {
        "GRAFANA_URL": url,
        "GRAFANA_API_KEY": api_key,
        "GRAFANA_FOLDER_UID": folder_uid,
        "GRAFANA_OVERWRITE": "1" if overwrite else "0",
    }

    ok, stdout, stderr, err = _run_script(script, env=env, args=[])
    if not ok:
        return {"error": err, "stdout": stdout, "stderr": stderr}

    return {"ok": True, "stdout": stdout, "stderr": stderr}


@router.post("/scripts/zabbix/preview")
def api_scripts_zabbix_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Diz o que a sincronizacao FARIA, sem escrever nada no Zabbix.

    Aceita o mesmo payload do POST /scripts/zabbix e monta o inventario pelo
    mesmo caminho, entao o que aparece aqui e o que vai acontecer de fato.
    A conta de remocao respeita a mesma regra da poda: sync com filtro de site
    manda uma lista parcial, e remover "o que nao veio" apagaria os hosts dos
    outros sites -- por isso so ha remocao no sync completo.
    """
    import importlib.util as _ilu

    effective = _zabbix_effective_sync_config(payload)
    url = _normalize_zabbix_url(payload.get("url") or effective.get("url"))
    user = _as_str(payload.get("user") or effective.get("user"))
    password = _as_str(payload.get("pass") or payload.get("password") or effective.get("pass"))
    if not url or not user or not password:
        return {"ok": False, "error": "url, user e pass sao obrigatorios"}

    tenant_slug = _zabbix_tenant_slug()
    group = _zabbix_tenant_group(
        _as_str(payload.get("group") or effective.get("group")) or "Cameras", tenant_slug)
    source = _as_str(payload.get("source") or "ip").lower()
    inv_mode = _normalize_ip_inventory_mode(payload.get("inv_mode") or payload.get("mode") or "olt")

    site = _as_str(payload.get("site"))
    _sel = payload.get("sites")
    sites_sel = [_as_str(x) for x in _sel if _as_str(x)] if isinstance(_sel, list) else []
    if not sites_sel and site:
        sites_sel = [site]

    # --- mesmas linhas que o sync usaria
    if len(sites_sel) > 1:
        _todas = _load_rows_for_source(source, mode=inv_mode) or []
        _alvos = {x.strip().lower() for x in sites_sel}

        def _e_dos_escolhidos(linha):
            for chave in ("site", "site_name", "local", "LOCAL"):
                if str(linha.get(chave) or "").strip().lower() in _alvos:
                    return True
            return False

        _linhas = [r for r in _todas if isinstance(r, dict) and _e_dos_escolhidos(r)]
    else:
        _linhas = _load_rows_for_source(source, site=(sites_sel[0] if sites_sel else ""), mode=inv_mode)
    inv_rows = _build_zabbix_rows(source, _linhas)

    # --- nome de host pelo MESMO codigo do sync, senao a previa mente
    script = BASE_DIR / "tools" / "mk_zabbix_from_inventory.py"
    try:
        spec = _ilu.spec_from_file_location("_mkzbx_previa", str(script))
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.ZBX_LEGACY_DEFAULT_HOSTNAMES = (tenant_slug == "default")
        nomes_inv = {mod.build_host_name(tenant_slug, r) for r in inv_rows}
        prefixo = mod._host_safe(tenant_slug).upper() + "-"
    except Exception as e:
        return {"ok": False, "error": "nao consegui calcular os nomes de host: " + str(e)}

    # --- o que ja existe no Zabbix (somente leitura)
    try:
        auth = _zabbix_login(url, user, password)
        grupos = _zabbix_api_call(url, "hostgroup.get", {"filter": {"name": [group]}}, auth)
        if grupos:
            gid = grupos[0]["groupid"]
            atuais = _zabbix_api_call(
                url, "host.get", {"groupids": [gid], "output": ["hostid", "host", "name"]}, auth)
        else:
            atuais = []
    except Exception as e:
        return {"ok": False, "error": "nao consegui consultar o Zabbix: " + str(e)}

    nomes_zbx = {str(h.get("host") or "") for h in atuais}
    criar = sorted(n for n in nomes_inv if n not in nomes_zbx)
    atualizar = sorted(n for n in nomes_inv if n in nomes_zbx)

    poda_ativa = bool(inv_rows) and not sites_sel
    remover = []
    if poda_ativa:
        for h in atuais:
            nome = str(h.get("host") or "")
            if not nome.upper().startswith(prefixo):
                continue                      # criado fora do sistema: nao entra
            if nome in nomes_inv:
                continue
            remover.append({"host": nome, "nome_visivel": _as_str(h.get("name"))})
        remover.sort(key=lambda x: x["host"])

    return {
        "ok": True,
        "grupo": group,
        "sites": sites_sel,
        "cameras": len(inv_rows),
        "hosts_no_grupo": len(atuais),
        "criar": len(criar),
        "atualizar": len(atualizar),
        "remover": len(remover),
        "criar_exemplos": criar[:20],
        "remover_lista": remover[:500],
        "poda_ativa": poda_ativa,
        "poda_motivo": (
            "" if poda_ativa
            else ("esta Fonte nao devolveu nenhuma camera; nada sera criado nem removido"
                  if not inv_rows
                  else "sync por site nao remove nada; escolha 'Todos os sites' para limpar")
        ),
    }


@router.get("/scripts/zabbix/config")
def api_scripts_zabbix_config() -> Dict[str, Any]:
    """Devolve a configuracao ja salva do Zabbix + os sites conhecidos.

    Existe para a tela abrir preenchida. A senha nao volta -- so a informacao
    de que existe uma guardada.
    """
    cfg = (_load_settings().get("zabbix_ip_sync") or {})

    # Alem do nome, quantas cameras cada site tem e quantas estao offline.
    # E o que permite a tela mostrar o peso de cada site antes de sincronizar,
    # em vez de uma lista de nomes onde todo site parece igual.
    sites = set()
    por_site: dict[str, dict[str, int]] = {}
    vistos: set[str] = set()
    for modo in ("olt", "basic", "switch"):
        for row in (_load_rows_for_source("ip", mode=modo) or []):
            if not isinstance(row, dict):
                continue
            nome = ""
            for chave in ("site", "site_name", "local", "LOCAL"):
                valor = str(row.get(chave) or "").strip()
                if valor:
                    nome = valor
                    break
            if not nome:
                continue
            sites.add(nome)
            # a mesma camera aparece em mais de um modo; conta uma vez so
            ip = str(row.get("ip") or row.get("IP") or "").strip()
            chave_unica = nome.lower() + "|" + ip
            if ip and chave_unica in vistos:
                continue
            if ip:
                vistos.add(chave_unica)
            item = por_site.setdefault(nome, {"cameras": 0, "offline": 0})
            item["cameras"] += 1
            if str(row.get("status") or "").strip().lower() != "online":
                item["offline"] += 1

    return {
        "ok": True,
        "url": _as_str(cfg.get("url")),
        "user": _as_str(cfg.get("user")),
        "has_password": bool(_as_str(cfg.get("pass"))),
        "group": _as_str(cfg.get("group")),
        "template": _as_str(cfg.get("template")),
        "site": _as_str(cfg.get("site")),
        "tg_token": _as_str(cfg.get("tg_token")),
        "tg_chat": _as_str(cfg.get("tg_chat")),
        "tg_chat_by_site": dict(cfg.get("tg_chat_by_site") or {}),
        "sites": sorted(sites),
        # sites_info e o que a tela nova usa; "sites" continua pra nao quebrar
        # quem ja consumia o endpoint
        "sites_info": [
            {"name": nome,
             "cameras": por_site.get(nome, {}).get("cameras", 0),
             "offline": por_site.get(nome, {}).get("offline", 0)}
            for nome in sorted(sites)
        ],
    }


@router.get("/scripts/zabbix/groups")
def api_scripts_zabbix_groups(url: str = "", user: str = "", password: str = "") -> Dict[str, Any]:
    """Lista os grupos que existem no Zabbix, pra tela oferecer em vez de digitar."""
    import json as _json
    import urllib.request as _url

    cfg = (_load_settings().get("zabbix_ip_sync") or {})
    alvo = _as_str(url) or _as_str(cfg.get("url"))
    usuario = _as_str(user) or _as_str(cfg.get("user"))
    senha = _as_str(password) or _as_str(cfg.get("pass"))
    if not alvo or not usuario or not senha:
        return {"ok": False, "error": "Zabbix ainda nao configurado", "groups": []}

    def _chamar(metodo, params, token=None):
        req = {"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}
        if token:
            req["auth"] = token
        pedido = _url.Request(alvo, data=_json.dumps(req).encode(),
                              headers={"Content-Type": "application/json-rpc"})
        with _url.urlopen(pedido, timeout=20) as resposta:
            corpo = _json.loads(resposta.read().decode())
        if "error" in corpo:
            raise RuntimeError(corpo["error"].get("data") or corpo["error"])
        return corpo["result"]

    def _so_letras(texto: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(texto or "").upper())

    try:
        token = _chamar("user.login", {"username": usuario, "password": senha})
        grupos = _chamar("hostgroup.get", {"output": ["name"], "sortfield": "name"}, token)
        nomes = [g["name"] for g in grupos]

        # O Zabbix e compartilhado entre clientes. Mostrar os 40+ grupos de
        # todo mundo nao ajuda e ainda arrisca o usuario mandar as cameras dele
        # pro grupo de outro cliente. Filtra pelo tenant da sessao.
        marca = _so_letras(get_current_tenant_slug())
        do_cliente = [n for n in nomes if marca and marca in _so_letras(n)]

        # o grupo ja configurado entra sempre, mesmo que fuja do padrao
        atual = _as_str(cfg.get("group"))
        if atual and atual in nomes and atual not in do_cliente:
            do_cliente.insert(0, atual)

        return {
            "ok": True,
            "groups": do_cliente or nomes,
            "filtrado_por_cliente": bool(do_cliente),
            "total_no_zabbix": len(nomes),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "groups": []}
