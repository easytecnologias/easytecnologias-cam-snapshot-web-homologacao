"""Reconcilia o roteamento por conector: desejado x o que ja esta no kernel.

Filosofia (igual ao sightops_wireguard_sync): **so soma**. Nunca derruba a
`wg-sightops` compartilhada nem remove nada de um conector -- se algo falta
(interface, tabela, rota, regra), aplica os passos idempotentes do system_ops;
se ja esta tudo la, nao mexe.

Duas metades, como sempre:
  - PURA: `plan()` recebe os conectores desejados, o estado (indices) e um
    "retrato" do que existe no servidor, e diz, por conector, o que falta.
    Testavel sem tocar em nada.
  - BORDA: `read_current()` tira o retrato rodando `wg show`/`ip rule`/`ip route`
    (runner injetavel) e `reconcile()` amarra tudo e aplica.

A chave privada do servidor nao mora aqui: quem chama le de
/etc/wireguard/wg-sightops.conf em runtime e passa pra `reconcile`.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from ops.connector_routing import allocator as _alloc
from ops.connector_routing import system_ops as _ops

# runner de LEITURA: (argv) -> (returncode, saida_texto). Sem stdin.
ReadRunner = Callable[[List[str]], Tuple[int, str]]


def _canon(cidr: str) -> str:
    try:
        return str(ipaddress.ip_network(str(cidr).strip(), strict=False))
    except Exception:
        return str(cidr).strip()


def connectors_from_store_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extrai {connector_id, pubkey, site_cidrs, name} do cadastro de conectores.

    Le do mesmo formato que o sightops_wireguard_sync le: so conector com tunel
    WireGuard habilitado + chave publica; site_cidrs = client_address + client_lans
    (canonizados, /32 pra IP solto). Funcao PURA -> testavel.
    """
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
        if not tunnel.get("enabled") or str(tunnel.get("type") or "").lower() != "wireguard":
            continue
        pubkey = str(tunnel.get("client_public_key") or "").strip()
        cid = str(row.get("id") or "").strip()
        if not pubkey or not cid:
            continue
        raw = [tunnel.get("client_address")] + list(tunnel.get("client_lans") or [])
        cidrs: List[str] = []
        for v in raw:
            v = str(v or "").strip()
            if not v:
                continue
            c = _canon(v)
            if c and c not in cidrs:
                cidrs.append(c)
        if not cidrs:
            continue
        out.append({"connector_id": cid, "pubkey": pubkey, "site_cidrs": cidrs,
                    "name": str(row.get("name") or cid)})
    return out


def diff_connector(alloc: Dict[str, Any], site_cidrs: List[str], current: Dict[str, Any]) -> List[str]:
    """Lista do que falta pra este conector estar roteado. Vazio = ja ok."""
    missing: List[str] = []
    ifname = str(alloc["ifname"])
    table = str(alloc["table_id"])
    pref = _ops.RULE_PREF_BASE + int(alloc["index"])
    src = str(alloc["server_ip"])
    cidrs = [_canon(c) for c in (site_cidrs or [])]

    if ifname not in (current.get("interfaces") or set()):
        missing.append("interface")

    rule = (current.get("rules") or {}).get(pref)
    if not rule or rule.get("from") != src or str(rule.get("table")) != table:
        missing.append("rule")

    have_routes = (current.get("routes") or {}).get(table) or set()
    for c in cidrs:
        if c not in have_routes:
            missing.append(f"route:{c}")

    peer = (current.get("peers") or {}).get(ifname)
    if peer is not None:
        have = {_canon(x) for x in (peer.get("allowed_ips") or [])}
        if not set(cidrs).issubset(have):
            missing.append("peer")

    return missing


def plan(connectors: List[Dict[str, Any]], state: Dict[str, Any], current: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Por conector: alocacao + o que falta + se precisa aplicar.

    `connectors`: lista de {connector_id, pubkey, site_cidrs}.
    Muta `state` (aloca indices novos pra conectores ineditos).
    """
    out: List[Dict[str, Any]] = []
    for conn in connectors:
        cid = str(conn.get("connector_id") or "").strip()
        if not cid:
            continue
        alloc = _alloc.allocate(cid, state)
        cidrs = [_canon(c) for c in (conn.get("site_cidrs") or [])]
        missing = diff_connector(alloc, cidrs, current)
        out.append({
            "connector_id": cid,
            "alloc": alloc,
            "pubkey": str(conn.get("pubkey") or "").strip(),
            "site_cidrs": cidrs,
            "missing": missing,
            "needs": bool(missing),
        })
    return out


def build_apply_steps(item: Dict[str, Any], private_key: str) -> List[Dict[str, Any]]:
    if not item.get("needs"):
        return []
    return _ops.build_provision_steps(item["alloc"], private_key, item["pubkey"], item["site_cidrs"])


# ---------- borda: ler o estado real ----------

def _table_ids_from_state(state: Dict[str, Any]) -> List[int]:
    ids: List[int] = []
    for cid, rec in (state.get("connectors") or {}).items():
        try:
            ids.append(_alloc.derive(int(rec["index"]), str(cid))["table_id"])
        except Exception:
            continue
    return ids


def read_current(read_runner: ReadRunner, table_ids: List[int]) -> Dict[str, Any]:
    """Retrato do servidor: interfaces wgc*, regras por pref, rotas por tabela, peers."""
    interfaces: set = set()
    rules: Dict[int, Dict[str, str]] = {}
    routes: Dict[str, set] = {}
    peers: Dict[str, Dict[str, Any]] = {}

    rc, out = read_runner(["wg", "show", "interfaces"])
    if rc == 0:
        interfaces = {w for w in out.split() if w.startswith(_alloc.IFNAME_PREFIX)}

    rc, out = read_runner(["ip", "rule", "show"])
    if rc == 0:
        for m in re.finditer(r"^(\d+):\s+from (\S+)\s+lookup (\S+)", out, re.M):
            rules[int(m.group(1))] = {"from": m.group(2), "table": m.group(3)}

    for t in table_ids:
        rc, out = read_runner(["ip", "route", "show", "table", str(t)])
        s: set = set()
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if line:
                    s.add(_canon(line.split()[0]))
        routes[str(t)] = s

    for ifn in interfaces:
        rc, out = read_runner(["wg", "show", ifn, "allowed-ips"])
        if rc == 0:
            ips: set = set()
            for line in out.splitlines():
                toks = line.split()
                for tok in toks[1:]:  # 1o token e a pubkey do peer
                    if "/" in tok:
                        ips.add(_canon(tok))
            peers[ifn] = {"allowed_ips": ips}

    return {"interfaces": interfaces, "rules": rules, "routes": routes, "peers": peers}


def reconcile(
    connectors: List[Dict[str, Any]],
    state: Dict[str, Any],
    private_key: str,
    read_runner: ReadRunner,
    run_runner: Optional[_ops.Runner] = None,
    dry_run: bool = True,
) -> List[Dict[str, Any]]:
    """Amarra tudo: aloca, le o real, planeja, e aplica (ou so mostra se dry_run).

    Padrao dry_run=True de proposito -- rede em servidor nao se toca por engano.
    """
    for conn in connectors:  # garante indices antes de descobrir as tabelas
        cid = str(conn.get("connector_id") or "").strip()
        if cid:
            _alloc.allocate(cid, state)
    current = read_current(read_runner, _table_ids_from_state(state))
    items = plan(connectors, state, current)

    results: List[Dict[str, Any]] = []
    for it in items:
        steps = build_apply_steps(it, private_key)
        base = {"connector_id": it["connector_id"], "ifname": it["alloc"]["ifname"],
                "table": it["alloc"]["table_id"], "missing": it["missing"]}
        if not steps:
            results.append({**base, "action": "ok"})
        elif dry_run:
            results.append({**base, "action": "would-apply", "cmds": _ops.redact_steps(steps)})
        else:
            log = _ops.run_steps(steps, runner=run_runner)
            results.append({**base, "action": "applied", "log": log})
    return results
