"""Bind da requisicao ao IP de origem do conector -> cai na tabela de rota dele.

Ponte entre o provisionamento (ops/connector_routing, que cria interface+tabela+
regra `ip rule from <server_ip> table N`) e a API: quando a API vai falar com um
aparelho de um conector, ela usa uma sessao `requests` **amarrada no `server_ip`
daquele conector**. A regra do kernel manda esse trafego pra tabela certa, e o
mesmo IP privado de dois clientes nunca se cruza.

GATED de proposito: se o conector NAO tem alocacao no estado de roteamento
(todo mundo hoje, e todo o v2), o helper devolve uma sessao normal -- byte a byte
o comportamento atual. So muda para conectores ja provisionados (v3).

Le so o JSON que o provisionador escreve (`server_ip` ja esta gravado la), sem
depender do pacote `ops/` estar no deploy da API.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

STATE_PATH = os.getenv("CONNECTOR_ROUTING_STATE", "/etc/sightops/connector_routing_state.json")
_CACHE_TTL = 5.0  # segundos -- evita ler o disco a cada request

_lock = threading.Lock()
_cache: dict = {"mtime": None, "at": 0.0, "map": {}}


def _load_source_map() -> dict:
    """{connector_id: server_ip} lido do estado, com cache curto por mtime."""
    now = time.time()
    try:
        mtime = os.path.getmtime(STATE_PATH)
    except OSError:
        return {}
    with _lock:
        if _cache["map"] and _cache["mtime"] == mtime and (now - _cache["at"]) < _CACHE_TTL:
            return _cache["map"]
    out: dict = {}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        for cid, rec in (data.get("connectors") or {}).items():
            ip = str((rec or {}).get("server_ip") or "").strip()
            if ip:
                out[str(cid)] = ip
    except Exception:
        out = {}
    with _lock:
        _cache.update({"mtime": mtime, "at": now, "map": out})
    return out


def source_ip_for_connector(connector_id: Optional[str]) -> Optional[str]:
    """IP de origem que a API deve usar pra falar com aparelhos deste conector.

    None = conector sem tabela dedicada -> caminho antigo (compartilhado)."""
    cid = str(connector_id or "").strip()
    if not cid:
        return None
    return _load_source_map().get(cid)


class _SourceAddressAdapter(HTTPAdapter):
    """Adapter que faz o socket sair por um IP de origem fixo (source_address)."""

    def __init__(self, source_ip: str, **kw):
        self._source = (source_ip, 0)
        super().__init__(**kw)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = self._source
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["source_address"] = self._source
        return super().proxy_manager_for(*args, **kwargs)


def bound_session(connector_id: Optional[str] = None, source_ip: Optional[str] = None) -> requests.Session:
    """Sessao `requests` amarrada ao IP de origem do conector (se houver alocacao).

    Sem alocacao -> Sessao normal, comportamento identico ao de hoje. Passe
    `source_ip` direto pra pular a resolucao por connector_id (ex.: testes)."""
    src = source_ip or source_ip_for_connector(connector_id)
    session = requests.Session()
    if src:
        adapter = _SourceAddressAdapter(src)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session
