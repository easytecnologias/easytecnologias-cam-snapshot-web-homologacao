"""Traducao IP real -> IP virtual do conector (NAT 1:1 no host).

Complementa `connector_routing_bind`. Como a API roda em container (rede bridge
do Docker), ela NAO consegue amarrar o `server_ip` do conector -- esse IP vive na
netns do HOST, junto das interfaces `wgc<N>`. Em vez de amarrar a origem, a API
fala com um **IP virtual unico por conector**; o host faz `NETMAP virtual->real`,
marca o pacote pra cair na tabela de rota daquele conector e faz `SNAT` pra
origem isolada (ver `/opt/sightops/scripts/connector_vnat.sh`). Assim o mesmo IP
privado de dois clientes (ex.: 192.168.10.5 nos dois) nunca se cruza, e funciona
de dentro do container -- provado em 2026-09-15.

GATED de proposito: conector SEM mapa (todo o v2, e qualquer conector ainda nao
provisionado) -> devolve o IP real intacto, byte a byte o comportamento de hoje.

Le so o JSON que o gerador escreve -- nao depende do pacote `ops/` no deploy:
    {connector_id: [{"real_cidr": "192.168.10.0/24", "virtual_cidr": "10.208.0.0/24"}, ...]}
"""
from __future__ import annotations

import contextvars
import ipaddress
import json
import os
import threading
import time
from typing import List, Optional, Tuple

MAP_PATH = os.getenv("CONNECTOR_VNAT_MAP", "/app/data/connector_vnat_map.json")
_CACHE_TTL = 5.0  # segundos -- evita ler o disco a cada request

_lock = threading.Lock()
_cache: dict = {"mtime": None, "at": 0.0, "map": {}}

# {connector_id: [(rede_real, rede_virtual), ...]}
_Pair = Tuple[ipaddress._BaseNetwork, ipaddress._BaseNetwork]


def _parse(data: dict) -> dict:
    out: dict = {}
    for cid, entries in (data or {}).items():
        pairs: List[_Pair] = []
        for e in entries or []:
            try:
                real = ipaddress.ip_network(str(e["real_cidr"]), strict=False)
                virt = ipaddress.ip_network(str(e["virtual_cidr"]), strict=False)
            except Exception:
                continue
            if real.prefixlen == virt.prefixlen:  # NETMAP e 1:1, mesmos bits de host
                pairs.append((real, virt))
        if pairs:
            out[str(cid)] = pairs
    return out


def _load_map() -> dict:
    now = time.time()
    try:
        mtime = os.path.getmtime(MAP_PATH)
    except OSError:
        return {}
    with _lock:
        if _cache["map"] and _cache["mtime"] == mtime and (now - _cache["at"]) < _CACHE_TTL:
            return _cache["map"]
    parsed: dict = {}
    try:
        with open(MAP_PATH, encoding="utf-8") as fh:
            parsed = _parse(json.load(fh))
    except Exception:
        parsed = {}
    with _lock:
        _cache.update({"mtime": mtime, "at": now, "map": parsed})
    return parsed


def virtual_ip_for(connector_id: Optional[str], real_ip: Optional[str]) -> Optional[str]:
    """IP que a API deve usar pra falar com `real_ip` deste conector.

    Sem mapa/alocacao (ou IP fora das LANs mapeadas) -> devolve `real_ip` intacto.
    Preserva os bits de host: 192.168.10.50 -> 10.208.0.50 se a LAN mapeia
    192.168.10.0/24 -> 10.208.0.0/24.
    """
    cid = str(connector_id or "").strip()
    raw = str(real_ip or "").strip()
    if not cid or not raw:
        return real_ip
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return real_ip
    for real_net, virt_net in _load_map().get(cid, []):
        if ip in real_net:
            offset = int(ip) - int(real_net.network_address)
            return str(ipaddress.ip_address(int(virt_net.network_address) + offset))
    return real_ip


def real_ip_for(connector_id: Optional[str], virtual_ip: Optional[str]) -> Optional[str]:
    """Inverso de `virtual_ip_for`: IP virtual -> IP real do conector.

    Serve pra "des-virtualizar" o que a varredura direta descobriu, pra guardar o
    IP REAL no inventario. Fora do range virtual -> devolve intacto."""
    cid = str(connector_id or "").strip()
    raw = str(virtual_ip or "").strip()
    if not cid or not raw:
        return virtual_ip
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return virtual_ip
    for real_net, virt_net in _load_map().get(cid, []):
        if ip in virt_net:
            offset = int(ip) - int(virt_net.network_address)
            return str(ipaddress.ip_address(int(real_net.network_address) + offset))
    return virtual_ip


def has_mapping(connector_id: Optional[str]) -> bool:
    """True se o conector tem alocacao virtual (ou seja, esta isolado)."""
    return bool(_load_map().get(str(connector_id or "").strip()))


# --- Contexto de coleta OLT: o conector "ativo" da operacao em curso ---
# Os drivers de OLT (app/cli/tools/olt_*) abrem SSH pelo IP que recebem e NAO
# conhecem o conector. Em vez de propagar connector_id por dezenas de call-sites,
# o olt_service seta este contextvar no inicio da operacao e o ponto de conexao
# de cada driver chama reach_olt_ip(host) -- assim SO a conexao vira IP virtual;
# o IP real continua no que o driver grava no inventario.
_olt_reach_cid = contextvars.ContextVar("olt_reach_connector", default="")


def set_olt_reach_connector(connector_id: Optional[str]) -> None:
    """Marca o conector da operacao OLT atual (thread/contexto isolado)."""
    try:
        _olt_reach_cid.set(str(connector_id or "").strip())
    except Exception:
        pass


def reach_olt_ip(olt_ip: Optional[str]) -> Optional[str]:
    """IP a conectar de fato na OLT: virtual (vnat) se o conector do contexto for
    isolado, senao o real intacto. Idempotente (IP ja virtual volta igual)."""
    try:
        return virtual_ip_for(_olt_reach_cid.get(), olt_ip) or olt_ip
    except Exception:
        return olt_ip


def _virtualize_token(cid: str, token: str) -> str:
    token = token.strip()
    if not token:
        return token
    if "/" in token:  # CIDR
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            return token
        v = virtual_ip_for(cid, str(net.network_address))
        return token if v == str(net.network_address) else f"{v}/{net.prefixlen}"
    if "-" in token:  # range "a-b" (b pode ser IP inteiro ou so o ultimo octeto)
        a, _, b = token.partition("-")
        va = virtual_ip_for(cid, a.strip())
        if va == a.strip():
            return token
        b = b.strip()
        return f"{va}-{virtual_ip_for(cid, b)}" if "." in b else f"{va}-{b}"
    return virtual_ip_for(cid, token)  # IP unico


def virtualize_target(connector_id: Optional[str], target: Optional[str]) -> Optional[str]:
    """Reescreve o alvo do scan (IP / range / CIDR / lista) real -> virtual.

    GATED: conector sem mapa -> alvo intacto. Preserva as formas (CIDR vira CIDR
    virtual, range vira range virtual) pra alimentar o inventory_dry.py igual."""
    cid = str(connector_id or "").strip()
    if not cid or not target or not _load_map().get(cid):
        return target
    parts = [p for p in str(target).split(",")]
    return ",".join(_virtualize_token(cid, p) for p in parts)
