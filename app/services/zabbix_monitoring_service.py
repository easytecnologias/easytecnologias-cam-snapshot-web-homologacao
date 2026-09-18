from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List
import os
from urllib.parse import urlsplit, urlunsplit

import requests

from app.core.tenant_context import get_current_tenant_slug
from app.services.db_store import _conn, load_app_settings
from app.services.monitoring_service import list_entities


def _text(value: Any) -> str:
    return str(value or "").strip()


def _api_url(raw: Any) -> str:
    value = _text(raw)
    if not value:
        return ""
    parts = urlsplit(value)
    if (parts.hostname or "").lower() in {"10.10.12.51", "zabbix-web", "zabbix-prod-web"}:
        host = os.getenv("SIGHTOPS_ZABBIX_WEB_HOST", "zabbix-prod-web").strip() or "zabbix-prod-web"
        port = os.getenv("SIGHTOPS_ZABBIX_WEB_PORT", "8080").strip() or "8080"
        return urlunsplit((parts.scheme or "http", f"{host}:{port}", "/api_jsonrpc.php", "", ""))
    return value


def _default_zabbix_cfg(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    base = dict(cfg or {})
    url = _api_url(base.get("url"))
    if not url:
        url = (
            _text(os.getenv("SIGHTOPS_ZABBIX_URL"))
            or _text(os.getenv("ZBX_URL"))
            or _text(os.getenv("ZABBIX_URL"))
            or "http://zabbix-prod-web:8080/api_jsonrpc.php"
        )
    user = (
        _text(base.get("user"))
        or _text(os.getenv("SIGHTOPS_ZABBIX_USER"))
        or _text(os.getenv("ZBX_USER"))
        or _text(os.getenv("ZABBIX_USER"))
        or "Admin"
    )
    password = (
        _text(base.get("pass") or base.get("password"))
        or _text(os.getenv("SIGHTOPS_ZABBIX_PASS"))
        or _text(os.getenv("ZBX_PASS"))
        or _text(os.getenv("ZABBIX_PASS"))
        or "zabbix"
    )
    return {**base, "url": _api_url(url), "user": user, "pass": password}


def _call(url: str, method: str, params: Any, auth: str | None = None, req_id: int = 1) -> Any:
    body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
    if auth:
        body["auth"] = auth
    response = requests.post(url, json=body, timeout=45)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"{method}: {data['error']}")
    return data.get("result")


def _chunks(rows: List[Dict[str, Any]], size: int = 50) -> Iterable[List[Dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def _host_key(tenant: str, row: Dict[str, Any]) -> str:
    digest = hashlib.sha1(_text(row.get("entity_key")).encode("utf-8")).hexdigest()[:16]
    return f"SIGHTOPS.{tenant}.{_text(row.get('entity_type')).upper()}.{digest}"


def _number(value: Any) -> float | None:
    match = re.search(r"-?\d+(?:[.,]\d+)?", _text(value))
    try:
        return float(match.group(0).replace(",", ".")) if match else None
    except ValueError:
        return None


def find_orphan_hostids(
    current_hosts: List[Dict[str, Any]],
    technical_names: Dict[str, Any],
    tenant: str,
    entity_types: Iterable[str],
) -> List[str]:
    """Hosts SIGHTOPS.<tenant>.<TIPO>.* que existem no Zabbix mas cuja entidade
    (ONU/OLT) nao esta mais ativa em `technical_names` -- ficaram orfaos porque
    o sync so criava hosts, nunca removia os que sumiram do inventario."""
    prefixes = tuple(f"SIGHTOPS.{tenant}.{entity_type.upper()}." for entity_type in entity_types)
    return [
        _text(row.get("hostid"))
        for row in current_hosts
        if _text(row.get("host")).startswith(prefixes) and _text(row.get("host")) not in technical_names
    ]


def _ensure_group(url: str, auth: str, name: str, req_id: int) -> str:
    rows = _call(url, "hostgroup.get", {"output": ["groupid"], "filter": {"name": [name]}}, auth, req_id) or []
    if rows:
        return _text(rows[0].get("groupid"))
    created = _call(url, "hostgroup.create", {"name": name}, auth, req_id + 1) or {}
    return _text((created.get("groupids") or [""])[0])


def sync_monitoring_to_zabbix(entity_types: tuple[str, ...] = ("olt", "onu", "camera", "nvr", "dvr", "connector", "access_device", "whatsapp")) -> Dict[str, Any]:
    settings = load_app_settings()
    raw_cfg = settings.get("zabbix_ip_sync") if isinstance(settings.get("zabbix_ip_sync"), dict) else {}
    cfg = _default_zabbix_cfg(raw_cfg)
    url = _api_url(cfg.get("url"))
    user = _text(cfg.get("user"))
    password = _text(cfg.get("pass") or cfg.get("password"))
    if not (url and user and password):
        return {"ok": False, "error": "Zabbix nao configurado automaticamente."}

    tenant = _text(get_current_tenant_slug() or "default").lower()
    entities: List[Dict[str, Any]] = []
    for entity_type in entity_types:
        entities.extend(list_entities(entity_type=entity_type, limit=2000))

    auth = _text(_call(url, "user.login", {"username": user, "password": password}, req_id=1))
    technical_names = {_host_key(tenant, row): row for row in entities}
    current_hosts = _call(
        url, "host.get",
        {"output": ["hostid", "host", "name"], "search": {"host": f"SIGHTOPS.{tenant}."}, "startSearch": True},
        auth, 30,
    ) or []

    # Remove hosts orfaos mesmo quando `entities` fica vazio (ex: todas as ONUs
    # de um tenant foram excluidas).
    orphan_hostids = find_orphan_hostids(current_hosts, technical_names, tenant, entity_types)
    if orphan_hostids:
        _call(url, "host.delete", orphan_hostids, auth, 20)

    if not entities:
        return {
            "ok": True, "tenant": tenant, "total": 0, "created_hosts": 0, "created_items": 0,
            "pushed": 0, "removed_hosts": len(orphan_hostids),
        }

    group_ids = {
        entity_type: _ensure_group(url, auth, f"SIGHTOPS - {tenant.upper()} - {entity_type.upper()}", 10 + idx * 4)
        for idx, entity_type in enumerate(entity_types)
    }
    host_ids = {_text(row.get("host")): _text(row.get("hostid")) for row in current_hosts if _text(row.get("host")) in technical_names}

    create_hosts: List[Dict[str, Any]] = []
    missing_names: List[str] = []
    for technical_name, row in technical_names.items():
        if technical_name in host_ids:
            continue
        entity_type = _text(row.get("entity_type"))
        display_name = _text(row.get("display_name")) or technical_name
        site = _text(row.get("site"))
        visible_name = f"{display_name} - {site} - {technical_name.rsplit('.', 1)[-1][:6]}"
        missing_names.append(technical_name)
        create_hosts.append({
            "host": technical_name,
            "name": visible_name,
            "groups": [{"groupid": group_ids[entity_type]}],
            "tags": [
                {"tag": "sightops_tenant", "value": tenant},
                {"tag": "sightops_type", "value": entity_type},
                {"tag": "sightops_key", "value": _text(row.get("entity_key"))},
                {"tag": "site", "value": site},
            ],
        })
    cursor = 0
    for batch in _chunks(create_hosts):
        created = _call(url, "host.create", batch, auth, 40 + cursor) or {}
        ids = created.get("hostids") or []
        for technical_name, hostid in zip(missing_names[cursor:cursor + len(batch)], ids):
            host_ids[technical_name] = _text(hostid)
        cursor += len(batch)

    all_hostids = [host_ids[name] for name in technical_names if host_ids.get(name)]
    wanted_keys = ["sightops.status", "sightops.onu_rx", "sightops.olt_rx", "sightops.distance"]
    items = _call(
        url, "item.get",
        {"output": ["itemid", "hostid", "key_"], "hostids": all_hostids, "filter": {"key_": wanted_keys}},
        auth, 100,
    ) or []
    item_by_host_key = {(_text(row.get("hostid")), _text(row.get("key_"))): _text(row.get("itemid")) for row in items}
    item_specs = {
        "sightops.status": ("SightOps - Estado operacional", 3, "1=up, 0=down, 2=instavel, 3=desconhecido, 4=manutencao"),
        "sightops.onu_rx": ("SightOps - ONU RX", 0, "Potencia recebida pela ONU em dBm"),
        "sightops.olt_rx": ("SightOps - OLT RX", 0, "Potencia recebida pela OLT em dBm"),
        "sightops.distance": ("SightOps - Distancia", 0, "Distancia da ONU em km"),
    }
    create_items = []
    for technical_name, row in technical_names.items():
        hostid = host_ids.get(technical_name, "")
        keys = ["sightops.status"] + (["sightops.onu_rx", "sightops.olt_rx", "sightops.distance"] if row.get("entity_type") == "onu" else [])
        for key in keys:
            if (hostid, key) in item_by_host_key:
                continue
            name, value_type, description = item_specs[key]
            create_items.append({"hostid": hostid, "name": name, "key_": key, "type": 2, "value_type": value_type, "delay": "0", "history": "30d", "trends": "365d", "description": description})
    cursor = 0
    missing_item_keys = [(row["hostid"], row["key_"]) for row in create_items]
    for batch in _chunks(create_items):
        created = _call(url, "item.create", batch, auth, 110 + cursor) or {}
        ids = created.get("itemids") or []
        for host_key, itemid in zip(missing_item_keys[cursor:cursor + len(batch)], ids):
            item_by_host_key[host_key] = _text(itemid)
        cursor += len(batch)

    status_value = {"up": "1", "down": "0", "unstable": "2", "unknown": "3", "maintenance": "4"}
    push_rows = []
    links = []
    for technical_name, row in technical_names.items():
        hostid = host_ids.get(technical_name, "")
        itemid = item_by_host_key.get((hostid, "sightops.status"), "")
        if hostid:
            links.append((hostid, _text(row.get("entity_key"))))
        if itemid:
            push_rows.append({"itemid": itemid, "value": status_value.get(_text(row.get("status")), "3")})
        try:
            detail = json.loads(_text(row.get("detail_json")) or "{}")
        except Exception:
            detail = {}
        for key, value in (
            ("sightops.onu_rx", _number(detail.get("onu_rx"))),
            ("sightops.olt_rx", _number(detail.get("olt_rx"))),
            ("sightops.distance", _number(detail.get("distance_km"))),
        ):
            metric_itemid = item_by_host_key.get((hostid, key), "")
            if metric_itemid and value is not None:
                push_rows.append({"itemid": metric_itemid, "value": str(value)})
    pushed = 0
    for batch in _chunks(push_rows, 200):
        result = _call(url, "history.push", batch, auth, 200 + pushed) or {}
        pushed += int(result.get("response") == "success") * len(batch) if isinstance(result, dict) else len(batch)

    with _conn() as connection:
        for hostid, entity_key in links:
            connection.execute(
                "UPDATE monitoring_entities SET zabbix_hostid=? WHERE tenant_slug=? AND entity_key=?",
                (hostid, tenant, entity_key),
            )
    return {
        "ok": True, "tenant": tenant, "total": len(entities), "groups": len(group_ids),
        "created_hosts": len(create_hosts), "linked_hosts": len(host_ids),
        "created_items": len(create_items), "pushed": pushed, "removed_hosts": len(orphan_hostids),
    }
