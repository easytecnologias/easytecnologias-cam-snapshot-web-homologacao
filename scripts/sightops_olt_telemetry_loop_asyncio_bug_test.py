"""Prova o bug real de 2026-09-04: `_olt_telemetry_loop` (app/main.py) chamava
`api_olt_registry_telemetry` via `asyncio.to_thread`, porque essa funcao era
sincrona quando o loop foi escrito. Em 2026-09-01/02 ela virou `async def`
(pra criar um job em background e devolver na hora, sem travar o botao
manual "Atualizar telemetria" na tela). `asyncio.to_thread(func, *args)`
so executa `func(*args)` numa thread -- se `func` for uma corrotina, a
chamada dentro da thread so CRIA o objeto corrotina, nunca roda ela (e o
Python emite "coroutine ... was never awaited"). O loop automatico parou de
coletar telemetria de verdade, silenciosamente, sem nenhum erro no log.

Este teste reproduz o padrao com uma funcao de exemplo em vez da real (nao
precisa de app FastAPI nem OLT), pra provar a diferenca entre os dois jeitos
de chamar.
"""

from __future__ import annotations

import asyncio
import gc
import warnings


async def funcao_async_de_exemplo(valor: int) -> dict:
    """Equivalente de api_olt_registry_telemetry: async, roda rapido, sem
    bloquear (o trabalho pesado de verdade fica num asyncio.create_task
    separado, fora deste teste)."""
    executou["rodou"] = True
    return {"ok": True, "valor": valor}


executou: dict[str, bool] = {}


async def jeito_errado(valor: int):
    """O bug: to_thread numa funcao async so cria a corrotina, nunca espera."""
    return await asyncio.to_thread(funcao_async_de_exemplo, valor)


async def jeito_certo(valor: int):
    """O fix: await direto, porque a funcao ja e async e nao bloqueia."""
    return await funcao_async_de_exemplo(valor)


def falhas() -> list[str]:
    erros: list[str] = []

    # 1) jeito errado: nunca roda o corpo da funcao, devolve o objeto
    #    corrotina cru (nao o dict), e emite RuntimeWarning quando o coletor
    #    de lixo percebe que ninguem nunca deu await nela
    executou.clear()
    with warnings.catch_warnings(record=True) as capturadas:
        warnings.simplefilter("always")
        resultado = asyncio.run(jeito_errado(42))
        eh_corrotina = asyncio.iscoroutine(resultado)
        # o warning "was never awaited" so dispara quando o coletor de lixo
        # recolhe a corrotina nao-aguardada -- forca isso aqui dentro do
        # bloco de captura em vez de deixar pro acaso
        del resultado
        gc.collect()
    if executou.get("rodou"):
        erros.append("jeito_errado: o corpo da funcao rodou, mas o bug deveria impedir isso (a funcao async mudou?)")
    if not eh_corrotina:
        erros.append("jeito_errado: esperava receber a corrotina crua (nao executada)")
    avisos_corrotina = [w for w in capturadas if "was never awaited" in str(w.message)]
    if not avisos_corrotina:
        erros.append("jeito_errado: esperava o RuntimeWarning 'coroutine ... was never awaited', nao apareceu")

    # 2) jeito certo: await direto roda a funcao de verdade e devolve o dict
    executou.clear()
    resultado2 = asyncio.run(jeito_certo(42))
    if not executou.get("rodou"):
        erros.append("jeito_certo: o corpo da funcao deveria ter rodado, nao rodou")
    if resultado2 != {"ok": True, "valor": 42}:
        erros.append(f"jeito_certo: esperava {{'ok': True, 'valor': 42}}, veio {resultado2!r}")

    return erros


def main() -> int:
    erros = falhas()
    for e in erros:
        print("FALHOU:", e)
    if not erros:
        print("OK: sightops_olt_telemetry_loop_asyncio_bug_test")
    return 1 if erros else 0


if __name__ == "__main__":
    raise SystemExit(main())
