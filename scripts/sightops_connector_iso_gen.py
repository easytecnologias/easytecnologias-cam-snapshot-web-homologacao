#!/usr/bin/env python3
"""Gerador do isolamento por conector (modelo A) -- self-contained.

Le os conectores (connectors.json) + o estado de roteamento (indices) e gera,
de forma DETERMINISTICA e consistente entre si:

  * /tmp/connector_iso.sh          -- aplica no HOST (root): interfaces wgc<N>,
    peers com allowed-ips de TODAS as LANs, rotas da tabela, ip rules, rp_filter
    e o NAT 1:1 por IP virtual (NETMAP/MARK/SNAT) de cada LAN.
  * /tmp/connector_vnat_map.json   -- mapa real->virtual que a API (container) le,
    com as MESMAS faixas virtuais do host (senao divergem).

Nao embute nem imprime a chave privada do servidor: o script gerado le
/etc/wireguard/wg-sightops.conf em runtime.

Uso (no servidor, como root):
    docker cp sightops-v3-api:/app/data/connectors.json /tmp/connectors.json
    python3 sightops_connector_iso_gen.py \
        --connectors /tmp/connectors.json \
        --state /etc/sightops/connector_routing_state.json
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys

# --- constantes (iguais ao ops/connector_routing/allocator.py) ---
TABLE_BASE = 1000
LISTEN_PORT_BASE = 52000
TRANSFER_BLOCK = "10.201.0.0/16"
FWMARK_BASE = 0x5100
RULE_PREF_BASE = 10000          # ip rule "from <server_ip>"
VNAT_PREF_BASE = 20000          # ip rule "fwmark"
VIRTUAL_ROOT = "10.208.0.0/12"
VIRTUAL_SLICE_PREFIX = 18


def _ip(v):
    return str(ipaddress.ip_address(v))


def _transfer(index):
    base = int(ipaddress.ip_network(TRANSFER_BLOCK, strict=True).network_address)
    srv = base + index * 2
    return _ip(srv), _ip(srv + 1)


def _virtual_slice(index):
    root = ipaddress.ip_network(VIRTUAL_ROOT, strict=True)
    size = 2 ** (32 - VIRTUAL_SLICE_PREFIX)
    b = int(root.network_address) + (index - 1) * size
    if b + size - 1 > int(root.broadcast_address):
        raise SystemExit(f"index {index} estoura {VIRTUAL_ROOT}")
    return ipaddress.ip_network(f"{_ip(b)}/{VIRTUAL_SLICE_PREFIX}")


def _virtual_map(index, lans, existing=None):
    """Mapeia cada LAN real -> sub-CIDR virtual no /18 do conector.

    PRESERVA os mapeamentos que ja existem (mesmo real ainda presente) pra nao
    mover uma faixa virtual que ja esta em uso (senao quebraria snapshots/rotas
    de quem ja funciona). LAN nova entra no primeiro slot livre alinhado.
    """
    slc = _virtual_slice(index)
    lo, hi = int(slc.network_address), int(slc.broadcast_address)
    kept = {}
    used = []  # [(start,end)] ocupados
    for e in (existing or []):
        real = _canon(e.get("real_cidr"))
        virt = e.get("virtual_cidr")
        if real and real in lans and virt:
            v = ipaddress.ip_network(str(virt), strict=False)
            if lo <= int(v.network_address) and int(v.broadcast_address) <= hi:
                kept[real] = str(v)
                used.append((int(v.network_address), int(v.broadcast_address)))

    def _free(size):
        cur = lo
        while True:
            if cur % size:
                cur += size - (cur % size)
            start, end = cur, cur + size - 1
            if end > hi:
                raise SystemExit(f"conector idx {index}: sem espaco virtual (/18)")
            if not any(not (end < u0 or start > u1) for u0, u1 in used):
                return start, end
            cur = end + 1

    out = []
    for c in lans:
        net = ipaddress.ip_network(str(c).strip(), strict=False)
        if c in kept:
            out.append({"real_cidr": c, "virtual_cidr": kept[c]})
            continue
        start, end = _free(net.num_addresses)
        used.append((start, end))
        out.append({"real_cidr": c, "virtual_cidr": f"{_ip(start)}/{net.prefixlen}"})
    return out


def _canon(c):
    try:
        return str(ipaddress.ip_network(str(c).strip(), strict=False))
    except Exception:
        return None


def _lans_of(row):
    """client_lans canonizadas (sem o /32 do endereco wg, que entra separado)."""
    tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
    out = []
    for v in (tunnel.get("client_lans") or []):
        c = _canon(v)
        if c and c not in out:
            out.append(c)
    return out


def _client_addr32(row):
    tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
    ip = str(tunnel.get("client_address") or "").split("/")[0].strip()
    return f"{ip}/32" if ip else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connectors", required=True)
    ap.add_argument("--state", default="", help="connector_routing_state.json (se existir)")
    ap.add_argument("--index", default="", help="cid=N,cid=N -- indices explicitos se nao houver state")
    ap.add_argument("--existing-map", default="", help="mapa vnat atual (preserva faixas ja em uso)")
    ap.add_argument("--out-sh", default="/tmp/connector_iso.sh")
    ap.add_argument("--out-map", default="/tmp/connector_vnat_map.json")
    args = ap.parse_args()

    rows = json.loads(open(args.connectors, encoding="utf-8").read())
    existing_map = {}
    if args.existing_map:
        try:
            existing_map = json.loads(open(args.existing_map, encoding="utf-8").read()) or {}
        except Exception:
            existing_map = {}
    idx_of = {}
    if args.state and os.path.exists(args.state):
        state = json.loads(open(args.state, encoding="utf-8").read())
        idx_of = {str(k): int(v["index"]) for k, v in (state.get("connectors") or {}).items()
                  if isinstance(v, dict) and "index" in v}
    for pair in args.index.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            idx_of[k.strip()] = int(v.strip())
    if not idx_of:
        raise SystemExit("sem indices: use --state <arquivo> ou --index cid=N,cid=N")

    app_map = {}
    sh_lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
        pubkey = str(tunnel.get("client_public_key") or "").strip()
        if cid not in idx_of or not pubkey:
            continue
        index = idx_of[cid]
        name = str(row.get("name") or cid)
        srv_ip, _peer_ip = _transfer(index)
        ifname = f"wgc{index}"
        table = TABLE_BASE + index
        wg_pref = RULE_PREF_BASE + index
        vnat_pref = VNAT_PREF_BASE + index
        mark = f"0x{FWMARK_BASE + index:x}"
        addr32 = _client_addr32(row)
        lans = _lans_of(row)
        vmap = _virtual_map(index, lans, existing_map.get(cid))
        app_map[cid] = vmap

        allowed = ([addr32] if addr32 else []) + lans
        sh_lines.append(f'\n# ---- {name} ({cid}) index {index} ----')
        sh_lines.append(
            f'wg_iface {ifname} {LISTEN_PORT_BASE + index} {srv_ip}/31 {table} {wg_pref} '
            f'"{pubkey}" "{",".join(allowed)}"'
        )
        for c in ([addr32] if addr32 else []) + lans:
            sh_lines.append(f'route_add {c} {ifname} {table}')
        for m in vmap:
            sh_lines.append(
                f'vnat {m["virtual_cidr"]} {m["real_cidr"]} {ifname} {table} '
                f'{srv_ip} {mark} {vnat_pref}'
            )

    header = r'''#!/bin/sh
# GERADO por sightops_connector_iso_gen.py -- nao editar a mao.
# Isolamento por conector (modelo A): interfaces wgc<N> + tabelas + NAT 1:1 virtual.
set -u
PATH=/usr/sbin:/sbin:/usr/bin:/bin:$PATH
IPT=$(command -v iptables || echo /usr/sbin/iptables)
KEY=$(awk -F'=' 'tolower($1) ~ /privatekey/ {sub(/^[^=]*=/,""); gsub(/^[ \t]+|[ \t]+$/,""); print; exit}' /etc/wireguard/wg-sightops.conf)
echo 2 > /proc/sys/net/ipv4/conf/all/rp_filter 2>/dev/null || true

wg_iface() {  # IF PORT SRV/31 TABLE PREF PEER "allowed,csv"
  IF=$1; PORT=$2; SRV=$3; TABLE=$4; PREF=$5; PEER=$6; ALLOWED=$7
  ip link show "$IF" >/dev/null 2>&1 || ip link add "$IF" type wireguard
  printf '%s\n' "$KEY" | wg set "$IF" listen-port "$PORT" private-key /dev/stdin
  wg set "$IF" peer "$PEER" allowed-ips "$ALLOWED"
  ip addr replace "$SRV" dev "$IF"; ip link set "$IF" up
  echo 2 > /proc/sys/net/ipv4/conf/"$IF"/rp_filter 2>/dev/null || true
  ip rule del pref "$PREF" 2>/dev/null || true
  ip rule add from "${SRV%/*}" table "$TABLE" pref "$PREF"
}
route_add() { ip route replace "$1" dev "$2" table "$3"; }
vnat() {  # VIRT REAL IF TABLE SRC MARK PREF
  V=$1; REAL=$2; IF=$3; TABLE=$4; SRC=$5; MARK=$6; PREF=$7
  $IPT -t mangle -C PREROUTING -d "$V" -j MARK --set-mark "$MARK" 2>/dev/null || $IPT -t mangle -A PREROUTING -d "$V" -j MARK --set-mark "$MARK"
  $IPT -t nat -C PREROUTING -d "$V" -j NETMAP --to "$REAL" 2>/dev/null || $IPT -t nat -A PREROUTING -d "$V" -j NETMAP --to "$REAL"
  $IPT -t nat -C POSTROUTING -o "$IF" -j SNAT --to-source "$SRC" 2>/dev/null || $IPT -t nat -I POSTROUTING 1 -o "$IF" -j SNAT --to-source "$SRC"
  $IPT -C DOCKER-USER -o "$IF" -j ACCEPT 2>/dev/null || $IPT -I DOCKER-USER -o "$IF" -j ACCEPT
  $IPT -C DOCKER-USER -i "$IF" -j ACCEPT 2>/dev/null || $IPT -I DOCKER-USER -i "$IF" -j ACCEPT
  ip rule del fwmark "$MARK" 2>/dev/null || true
  ip rule add fwmark "$MARK" table "$TABLE" pref "$PREF"
}
'''
    open(args.out_sh, "w", encoding="utf-8").write(header + "\n".join(sh_lines) + "\n")
    open(args.out_map, "w", encoding="utf-8").write(json.dumps(app_map, ensure_ascii=False, indent=2))
    print(f"OK: {len(app_map)} conector(es)")
    for cid, vmap in app_map.items():
        print(f"  {cid}: " + ", ".join(f'{m["real_cidr"]}->{m["virtual_cidr"]}' for m in vmap))
    print(f"gerado: {args.out_sh}  e  {args.out_map}")


if __name__ == "__main__":
    main()
