#!/usr/bin/env python3
"""Provisiona as tabelas de roteamento por conector -- DRY-RUN por padrao.

Le os conectores reais (mesmo connectors.json do sightops_wireguard_sync),
aloca interface/tabela/porta/IP-de-origem pra cada um e mostra o plano: o que
JA existe no servidor e quais comandos rodariam. So aplica de verdade com
--apply (precisa root e a chave do servidor).

Uso:
    # preview fora do servidor (nao le o host, assume vazio):
    python scripts/sightops_connector_routing_provision.py --offline --connectors /caminho/connectors.json

    # no servidor v3, mostrando o que falta em relacao ao estado real:
    python scripts/sightops_connector_routing_provision.py

    # aplicar de verdade (v3, root):
    sudo python scripts/sightops_connector_routing_provision.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)
sys.path.insert(0, _HERE)  # pra importar sightops_wireguard_sync (resolucao do connectors.json)

from ops.connector_routing import provisioner as P
from ops.connector_routing import state as St

WG_CONF = "/etc/wireguard/wg-sightops.conf"


def _connectors_path(cli_value):
    if cli_value:
        return Path(cli_value)
    try:
        import sightops_wireguard_sync as wg  # reusa a mesma resolucao de caminho
        return wg._connectors_json_path()
    except Exception:
        for p in ("/etc/sightops/connectors.json", "/home/central/sightops-prod-release/connectors.json"):
            if Path(p).exists():
                return Path(p)
    return None


def _read_runner(argv):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except FileNotFoundError as exc:
        return 127, str(exc)


def _run_runner(argv, stdin):
    p = subprocess.run(
        argv,
        input=(stdin.encode("utf-8") if stdin is not None else None),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace")


def _server_privkey():
    for line in Path(WG_CONF).read_text(encoding="utf-8").splitlines():
        if line.strip().lower().startswith("privatekey"):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"PrivateKey nao encontrada em {WG_CONF}")


def _print(results):
    for r in results:
        print(f"== {r['connector_id']}  [{r['action']}]  iface={r['ifname']} tabela={r['table']}")
        if r.get("missing"):
            print("   falta:", ", ".join(r["missing"]))
        for c in r.get("cmds", []):
            print("     " + " ".join(c))
        for e in r.get("log", []):
            print(f"     rc={e['rc']}  " + " ".join(e["argv"]))
    total = len(results)
    need = sum(1 for r in results if r["action"] != "ok")
    print(f"\n{total} conector(es); {need} precisa(m) de mudanca; {total - need} ja ok.")


def main():
    ap = argparse.ArgumentParser(description="Tabelas de roteamento por conector (dry-run por padrao).")
    ap.add_argument("--connectors", help="caminho do connectors.json (senao, resolucao automatica)")
    ap.add_argument("--state", help="caminho do estado de roteamento (senao, padrao)")
    ap.add_argument("--offline", action="store_true", help="nao le o servidor; assume vazio (preview fora do host)")
    ap.add_argument("--save", action="store_true", help="persiste as alocacoes (indices) mesmo em dry-run")
    ap.add_argument("--apply", action="store_true", help=f"APLICA de verdade (precisa root + {WG_CONF})")
    args = ap.parse_args()

    cpath = _connectors_path(args.connectors)
    if not cpath or not Path(cpath).exists():
        raise SystemExit("connectors.json nao encontrado; passe --connectors <caminho>")
    rows = json.loads(Path(cpath).read_text(encoding="utf-8"))
    conns = P.connectors_from_store_rows(rows)
    if not conns:
        print("Nenhum conector com tunel WireGuard habilitado.")
        return

    state = St.load_state(args.state)
    print(f"{len(conns)} conector(es) com WireGuard; estado: {args.state or St.DEFAULT_STATE_PATH}\n")

    if args.apply:
        key = _server_privkey()
        results = P.reconcile(conns, state, private_key=key, read_runner=_read_runner,
                              run_runner=_run_runner, dry_run=False)
        St.save_state(state, args.state)
        _print(results)
        print("\nAPLICADO. Estado salvo. Lembre: cada roteador de site precisa apontar o WireGuard")
        print("pra porta nova dele (listen-port por conector) -- ver a coluna porta na alocacao.")
        return

    read = (lambda argv: (0, "")) if args.offline else _read_runner
    results = P.reconcile(conns, state, private_key="<dry-run>", read_runner=read, dry_run=True)
    if args.save:
        St.save_state(state, args.state)
        print("(alocacoes persistidas)\n")
    _print(results)
    print("\nDRY-RUN: nada foi alterado. Rode com --apply (root, no v3) pra aplicar.")


if __name__ == "__main__":
    main()
