#!/usr/bin/env python3
"""Audita a autorizacao por papel das rotas HTTP do SightOps.

POR QUE ISTO EXISTE
-------------------
A exigencia de papel vive numa LISTA DE PREFIXOS escrita a mao
(`ApiAuthMiddleware._role_rules`). O que nao esta na lista cai no default, que
hoje e "basta estar logado" -- ou seja, rota nova nasce no nivel mais
permissivo e ninguem percebe. Em 20/09/2026 havia 62 rotas de ESCRITA sem
papel exigido, entre elas abrir porta do controle de acesso.

Este script torna isso visivel e verificavel: roda em qualquer maquina com o
codigo, nao precisa de banco nem de rede, e sai com codigo != 0 quando
encontra rota de escrita desprotegida. Serve como porta de entrada de CI.

USO
---
    python scripts/audita_autorizacao.py            # relatorio completo
    python scripts/audita_autorizacao.py --resumo   # so os numeros
    python scripts/audita_autorizacao.py --teto 0   # falha se houver QUALQUER uma

O parametro --teto permite baixar a divida aos poucos: fixe o numero atual e
diminua a cada correcao; se alguem adicionar rota insegura, o script falha.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections import Counter

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

ESCRITA = ("POST", "PUT", "PATCH", "DELETE")


def rotas_declaradas(pasta: str) -> list[tuple[str, str, str]]:
    """(metodo, caminho completo, arquivo) de cada @router.<metodo> encontrado."""
    achadas: list[tuple[str, str, str]] = []
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Audita autorizacao por papel das rotas")
    ap.add_argument("--resumo", action="store_true", help="so os numeros")
    ap.add_argument("--teto", type=int, default=None,
                    help="falha se houver MAIS rotas de escrita sem papel que este numero")
    args = ap.parse_args()

    # importado aqui pra mensagem de erro ser clara se o ambiente nao tiver deps
    from app.core.security import ApiAuthMiddleware
    from app.core.settings import get_settings

    mw = ApiAuthMiddleware(None, get_settings())
    rotas = rotas_declaradas(os.path.join(RAIZ, "app", "api", "endpoints"))

    publicas, com_papel, sem_papel = [], [], []
    for met, full, arq in rotas:
        if mw._is_public_path(full):
            publicas.append((met, full, arq))
        elif mw._match_role_rule(full, met):
            com_papel.append((met, full, arq, mw._match_role_rule(full, met)))
        else:
            sem_papel.append((met, full, arq))

    escrita_sem_papel = [r for r in sem_papel if r[0] in ESCRITA]

    if not args.resumo:
        print("=== ROTAS PUBLICAS (sem login) ===")
        for met, full, arq in publicas:
            print(f"   {met:<7} {full:<56} [{arq}]")
        print(f"\n=== ESCRITA SEM PAPEL EXIGIDO (qualquer logado, ate viewer) ===")
        for met, full, arq in escrita_sem_papel:
            print(f"   {met:<7} {full:<56} [{arq}]")
        por_arq = Counter(arq for _, _, arq in escrita_sem_papel)
        if por_arq:
            print("\n   concentracao por arquivo:")
            for arq, n in por_arq.most_common():
                print(f"      {n:>3}  {arq}")

    print(f"\nrotas /api/ analisadas : {len(rotas)}")
    print(f"  publicas             : {len(publicas)}")
    print(f"  com papel exigido    : {len(com_papel)}")
    print(f"  ESCRITA sem papel    : {len(escrita_sem_papel)}")

    if args.teto is not None and len(escrita_sem_papel) > args.teto:
        print(f"\nFALHOU: {len(escrita_sem_papel)} rotas de escrita sem papel (teto={args.teto})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
