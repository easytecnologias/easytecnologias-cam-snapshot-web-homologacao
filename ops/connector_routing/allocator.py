"""Alocacao deterministica de recursos de rede por conector (tabelas separadas).

Objetivo: cada conector (site) ganha a SUA propria interface WireGuard, tabela
de roteamento e IP de origem no servidor -- assim dois clientes com o MESMO IP
privado (ex.: 192.168.10.5 nos dois) nunca se cruzam, porque o pacote de cada
um sai por uma interface/tabela diferente.

Esta camada e PURA: nao toca em `ip`/`wg`, nao le/escreve rede. Ela so decide,
de forma estavel e sem colisao, QUAL numero de tabela / nome de interface /
porta UDP / /31 de transito / IP de origem cada conector usa. A execucao real
(criar interface, subir rota, por a regra) fica noutra camada (system_ops),
substituivel por um fake nos testes -- mesmo padrao do namespace provisioner,
mas SEM namespace e SEM proxy SOCKS5.

Regra de ouro: uma vez que um conector recebe um `index`, ele NUNCA muda nem e
reaproveitado por outro -- todos os demais recursos derivam do index, entao o
estado fica estavel entre execucoes (persistido em JSON pelo chamador).
"""
from __future__ import annotations

import ipaddress
from typing import Any, Dict, List

# --- faixas/bases reservadas (fora do caminho de qualquer coisa em uso) ---

# Tabelas de rota do kernel: 0, 253 (default), 254 (main), 255 (local) sao
# reservadas. Ficamos bem longe delas.
TABLE_BASE = 1000
# Nome de interface Linux tem limite de 15 chars -- "wgc" + numero cabe folgado.
IFNAME_PREFIX = "wgc"
# Cada interface WireGuard escuta numa porta UDP propria.
LISTEN_PORT_BASE = 52000
# Rede de transito (/31 por conector) -- ponto-a-ponto servidor<->roteador do
# site. NAO deve colidir com nenhuma LAN de cliente nem com a wg-sightops atual.
TRANSFER_BLOCK = "10.201.0.0/16"

# fwmark: usado pelo modelo A (NAT 1:1 por IP virtual) pra marcar o pacote no
# host e mandar pra tabela certa (o container nao consegue amarrar server_ip).
FWMARK_BASE = 0x5100

# Espaco de IP VIRTUAL (modelo A): cada conector ganha um /18 proprio aqui. O app
# (em container) fala com o IP virtual; o host faz NETMAP virtual->real + SNAT pra
# origem isolada. Raiz escolhida por estar livre no host (so 10.200.0.0/23 e
# 10.201.x usados na regiao). /18 = 16384 enderecos, cabe LAN grande (/20) + varias.
VIRTUAL_ROOT = "10.208.0.0/12"
VIRTUAL_SLICE_PREFIX = 18


class AllocationError(ValueError):
    pass


def _ip_to_int(addr: str) -> int:
    return int(ipaddress.ip_address(addr))


def _int_to_ip(value: int) -> str:
    return str(ipaddress.ip_address(value))


def _transfer_pair(index: int) -> Dict[str, str]:
    """/31 do conector: .0 = servidor, .1 = roteador do site (RFC 3021)."""
    net = ipaddress.ip_network(TRANSFER_BLOCK, strict=True)
    base = int(net.network_address)
    offset = index * 2
    if offset + 1 > int(net.broadcast_address) - base:
        raise AllocationError(f"index {index} estoura o bloco de transito {TRANSFER_BLOCK}")
    server_ip = base + offset
    peer_ip = server_ip + 1
    return {
        "transfer_cidr": f"{_int_to_ip(server_ip)}/31",
        "server_ip": _int_to_ip(server_ip),
        "peer_ip": _int_to_ip(peer_ip),
    }


def _virtual_slice(index: int) -> str:
    """/18 virtual do conector, fatiado da raiz VIRTUAL_ROOT."""
    root = ipaddress.ip_network(VIRTUAL_ROOT, strict=True)
    size = 2 ** (32 - VIRTUAL_SLICE_PREFIX)
    base = int(root.network_address) + (index - 1) * size
    if base + size - 1 > int(root.broadcast_address):
        raise AllocationError(f"index {index} estoura o bloco virtual {VIRTUAL_ROOT}")
    return f"{_int_to_ip(base)}/{VIRTUAL_SLICE_PREFIX}"


def virtual_map_for(index: int, real_cidrs: List[str]) -> List[Dict[str, str]]:
    """Mapeia cada CIDR real do conector num sub-CIDR virtual dentro do /18 dele.

    Empacota em ordem, cada bloco alinhado ao proprio tamanho (NETMAP e 1:1, mesmo
    prefixo). Deterministico pra a mesma ordem de entrada -- o gerador das regras
    do host e o mapa do app tem que usar a MESMA ordem, senao divergem.
    """
    slice_net = ipaddress.ip_network(_virtual_slice(index), strict=True)
    cursor = int(slice_net.network_address)
    out: List[Dict[str, str]] = []
    for c in real_cidrs or []:
        try:
            net = ipaddress.ip_network(str(c).strip(), strict=False)
        except Exception:
            continue
        size = net.num_addresses
        if cursor % size:  # alinha ao tamanho do bloco
            cursor += size - (cursor % size)
        if cursor + size - 1 > int(slice_net.broadcast_address):
            raise AllocationError(f"conector {index}: LANs estouram o /18 virtual")
        out.append({"real_cidr": str(net),
                    "virtual_cidr": f"{_int_to_ip(cursor)}/{net.prefixlen}"})
        cursor += size
    return out


def derive(index: int, connector_id: str) -> Dict[str, Any]:
    """Todos os recursos derivados de um index -- funcao pura, sem estado."""
    if index < 1:
        raise AllocationError("index tem que comecar em 1")
    pair = _transfer_pair(index)
    return {
        "connector_id": connector_id,
        "index": index,
        "table_id": TABLE_BASE + index,
        "ifname": f"{IFNAME_PREFIX}{index}",
        "listen_port": LISTEN_PORT_BASE + index,
        "fwmark": FWMARK_BASE + index,
        "virtual_slice": _virtual_slice(index),
        **pair,
    }


def _existing_indices(state: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for cid, rec in (state.get("connectors") or {}).items():
        try:
            out[str(cid)] = int(rec["index"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def allocate(connector_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Devolve a alocacao do conector, criando um index novo se for a 1a vez.

    Muta `state` (adiciona o conector). Idempotente: chamar de novo devolve a
    MESMA alocacao. `state` e um dict simples (carregado de/salvo em JSON pelo
    chamador) no formato {"connectors": {connector_id: {index, ...}}}.
    """
    cid = str(connector_id or "").strip()
    if not cid:
        raise AllocationError("connector_id vazio")
    state.setdefault("connectors", {})
    existing = _existing_indices(state)
    if cid in existing:
        return derive(existing[cid], cid)
    used = set(existing.values())
    index = 1
    while index in used:
        index += 1
    rec = derive(index, cid)
    state["connectors"][cid] = rec
    return rec


def plan_for_connectors(connector_ids: List[str], state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Alocacao (criando o que faltar) para uma lista de conectores.

    Ordena por connector_id so pra a atribuicao de index novo ser estavel
    quando varios chegam de uma vez; conectores ja conhecidos mantem o index.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for cid in sorted(str(c or "").strip() for c in connector_ids if str(c or "").strip()):
        out[cid] = allocate(cid, state)
    return out
