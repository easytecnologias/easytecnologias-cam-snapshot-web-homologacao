#!/usr/bin/env python3
"""Audita a autorizacao por papel das rotas HTTP do SightOps.

POR QUE ISTO EXISTE
-------------------
A exigencia de papel vive numa LISTA DE PREFIXOS escrita a mao
(`ApiAuthMiddleware._role_rules`). Ate 20/09/2026 o que nao estava na lista
caia em "basta estar logado" -- rota nova nascia no nivel mais permissivo e
ninguem percebia. Havia 62 rotas de ESCRITA sem papel, entre elas abrir porta
do controle de acesso.

Hoje o default e NEGAR escrita sem papel declarado. Este script existe para
que isso continue verdadeiro: ele lista o que esta descoberto e, com --teto,
falha em CI quando alguem adiciona rota sem declarar papel.

USO
---
    python scripts/audita_autorizacao.py            # relatorio completo
    python scripts/audita_autorizacao.py --resumo   # so os numeros
    python scripts/audita_autorizacao.py --teto 0   # falha se houver qualquer uma
"""
from __future__ import annotations

import argparse
import glob
import importlib
import os
import re
import sys
from collections import Counter

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

ESCRITA = ("POST", "PUT", "PATCH", "DELETE")


def rotas_por_import(pasta):
    """Rotas REAIS, importando cada router; None se o ambiente nao permitir.

    Preferir isto ao regex: o regex nao enxerga rota declarada com lista de
    metodos -- foi o caso do proxy `/api/maintenance/web/{ip}/`, cujos
    PUT/PATCH/DELETE teriam sido negados ao virar o default.
    """
    achadas = []
    for arq in sorted(glob.glob(os.path.join(pasta, "*.py"))):
        nome = os.path.basename(arq)[:-3]
        if nome.startswith("_"):
            continue
        try:
            mod = importlib.import_module("app.api.endpoints." + nome)
        except Exception:
            return None
        router = getattr(mod, "router", None)
        if router is None:
            continue
        for rota in router.routes:
            caminho = getattr(rota, "path", "")
            if not caminho.startswith("/api/"):
                continue
            for met in (getattr(rota, "methods", None) or []):
                achadas.append((met.upper(), caminho, nome + ".py"))
    return sorted(set(achadas))


def rotas_por_regex(pasta):
    """Reserva para quando importar nao for possivel. Nao ve tudo."""
    achadas = []
    for arq in sorted(glob.glob(os.path.join(pasta, "*.py"))):
        try:
            txt = open(arq, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        m = re.search(r'APIRouter\((?:[^)]*?)prefix\s*=\s*["\']([^"\']+)["\']', txt, re.S)
        prefix = m.group(1) if m else ""
        for met, caminho in re.findall(r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']', txt):
            full = (prefix + caminho) or "/"
            if full.startswith("/api/"):
                achadas.append((met.upper(), full, os.path.basename(arq)))
    return sorted(set(achadas))


def main():
    ap = argparse.ArgumentParser(description="Audita autorizacao por papel das rotas")
    ap.add_argument("--resumo", action="store_true", help="so os numeros")
    ap.add_argument("--teto", type=int, default=None,
                    help="falha se houver MAIS rotas de escrita sem papel que este numero")
    args = ap.parse_args()

    from app.core.security import ApiAuthMiddleware
    from app.core.settings import get_settings

    mw = ApiAuthMiddleware(None, get_settings())
    pasta = os.path.join(RAIZ, "app", "api", "endpoints")

    rotas = rotas_por_import(pasta)
    origem = "routers importados"
    if rotas is None:
        rotas = rotas_por_regex(pasta)
        origem = "regex (reserva -- pode nao ver tudo)"

    publicas, com_papel, sem_papel = [], [], []
    for met, full, arq in rotas:
        if mw._is_public_path(full):
            publicas.append((met, full, arq))
        elif mw._match_role_rule(full, met):
            com_papel.append((met, full, arq))
        else:
            sem_papel.append((met, full, arq))

    escrita_sem_papel = [r for r in sem_papel if r[0] in ESCRITA]

    if not args.resumo:
        print("=== ROTAS PUBLICAS (sem login) ===")
        for met, full, arq in publicas:
            print("   %-7s %-56s [%s]" % (met, full, arq))
        print("\n=== ESCRITA SEM PAPEL EXIGIDO ===")
        if not escrita_sem_papel:
            print("   (nenhuma)")
        for met, full, arq in escrita_sem_papel:
            print("   %-7s %-56s [%s]" % (met, full, arq))
        por_arq = Counter(arq for _, _, arq in escrita_sem_papel)
        if por_arq:
            print("\n   concentracao por arquivo:")
            for arq, n in por_arq.most_common():
                print("      %3d  %s" % (n, arq))

    print("\nfonte                  : %s" % origem)
    print("rotas /api/ analisadas : %d" % len(rotas))
    print("  publicas             : %d" % len(publicas))
    print("  com papel exigido    : %d" % len(com_papel))
    print("  ESCRITA sem papel    : %d" % len(escrita_sem_papel))

    if args.teto is not None and len(escrita_sem_papel) > args.teto:
        print("\nFALHOU: %d rotas de escrita sem papel (teto=%d)" % (len(escrita_sem_papel), args.teto))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
