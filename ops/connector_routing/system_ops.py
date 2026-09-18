"""Camada que fala com o kernel: interface WireGuard + tabela + regra por conector.

Separacao proposital em DUAS partes:

  1. `build_provision_steps(...)` -- PURA: devolve a lista ordenada de comandos
     (`ip`/`wg`) que provisionam um conector, sem executar nada. E o que os
     testes conferem (tabela certa, cidrs certos, IP de origem certo, e -- de
     seguranca -- que a chave privada vai por STDIN, nunca no argv/ps/log).

  2. `run_steps(...)` -- executa os passos, alimentando stdin (a chave privada)
     sem passar pela linha de comando. `runner` e injetavel: no teste entra um
     fake que so grava as chamadas; em producao entra o subprocess de verdade.

Idempotencia: cada passo diz se pode "ja existir" (ok_if_exists) -- rodar de
novo nao quebra. A regra `ip rule` usa uma `pref` fixa por conector: apaga a
pref e recria, entao nunca acumula regra duplicada.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable, Dict, List, Optional

# preferencia (prioridade) da regra ip rule, fixa por conector -> idempotente
RULE_PREF_BASE = 10000

# Runner: recebe (argv, stdin) e devolve (returncode, saida_texto).
Runner = Callable[[List[str], Optional[str]], "tuple[int, str]"]

_PRIVATE_KEY_STDIN = "/dev/stdin"


def _step(argv: List[str], stdin: Optional[str] = None, ok_if_exists: bool = False) -> Dict[str, Any]:
    return {"argv": argv, "stdin": stdin, "ok_if_exists": ok_if_exists}


def build_provision_steps(
    alloc: Dict[str, Any],
    private_key: str,
    peer_pubkey: str,
    site_cidrs: List[str],
    keepalive: int = 25,
) -> List[Dict[str, Any]]:
    """Comandos pra deixar o conector `alloc` roteando na SUA tabela.

    `alloc` vem do allocator.derive/allocate. `site_cidrs` sao as LANs do site
    (podem repetir entre conectores -- e o ponto: cada uma na sua tabela).
    """
    ifname = str(alloc["ifname"])
    table = str(alloc["table_id"])
    port = str(alloc["listen_port"])
    server_ip = str(alloc["server_ip"])
    pref = str(RULE_PREF_BASE + int(alloc["index"]))
    cidrs = [c for c in (site_cidrs or []) if c]

    steps: List[Dict[str, Any]] = []
    # 1. interface WireGuard dedicada
    steps.append(_step(["ip", "link", "add", "dev", ifname, "type", "wireguard"], ok_if_exists=True))
    # 2. chave privada + porta -- chave via STDIN (nunca no argv)
    steps.append(_step(["wg", "set", ifname, "private-key", _PRIVATE_KEY_STDIN, "listen-port", port], stdin=private_key))
    # 3. peer (o roteador do site) e as LANs que ele atende
    peer_argv = ["wg", "set", ifname, "peer", peer_pubkey, "allowed-ips", ",".join(cidrs) or "0.0.0.0/32"]
    if keepalive:
        peer_argv += ["persistent-keepalive", str(keepalive)]
    steps.append(_step(peer_argv))
    # 4. endereco ponto-a-ponto e subir a interface
    steps.append(_step(["ip", "address", "add", str(alloc["transfer_cidr"]), "dev", ifname], ok_if_exists=True))
    steps.append(_step(["ip", "link", "set", ifname, "up"]))
    # 5. rotas das LANs do site NA TABELA do conector (replace = idempotente)
    for cidr in cidrs:
        steps.append(_step(["ip", "route", "replace", cidr, "dev", ifname, "table", table]))
    # 6. regra: quem sair com o IP de origem do conector usa a tabela dele.
    #    del+add na mesma pref -> nunca duplica.
    steps.append(_step(["ip", "rule", "del", "pref", pref], ok_if_exists=True))
    steps.append(_step(["ip", "rule", "add", "from", server_ip, "table", table, "pref", pref]))
    return steps


def build_teardown_steps(alloc: Dict[str, Any]) -> List[Dict[str, Any]]:
    ifname = str(alloc["ifname"])
    table = str(alloc["table_id"])
    pref = str(RULE_PREF_BASE + int(alloc["index"]))
    return [
        _step(["ip", "rule", "del", "pref", pref], ok_if_exists=True),
        _step(["ip", "route", "flush", "table", table], ok_if_exists=True),
        _step(["ip", "link", "del", "dev", ifname], ok_if_exists=True),
    ]


def redact_steps(steps: List[Dict[str, Any]]) -> List[List[str]]:
    """Versao segura pra log/dry-run: nunca mostra o conteudo do stdin (chave)."""
    out: List[List[str]] = []
    for s in steps:
        argv = list(s["argv"])
        if s.get("stdin"):
            argv = argv + ["<stdin: REDACTED>"]
        out.append(argv)
    return out


def _real_runner(argv: List[str], stdin: Optional[str]) -> "tuple[int, str]":
    proc = subprocess.run(
        argv,
        input=(stdin.encode("utf-8") if stdin is not None else None),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc.returncode, (proc.stdout or b"").decode("utf-8", "replace")


def run_steps(steps: List[Dict[str, Any]], runner: Optional[Runner] = None) -> List[Dict[str, Any]]:
    """Executa os passos. Aborta no 1o erro de um passo que NAO e ok_if_exists.

    Devolve o log (sem stdin) do que rodou -- util pro provisionador reportar.
    """
    run = runner or _real_runner
    log: List[Dict[str, Any]] = []
    for s in steps:
        rc, out = run(list(s["argv"]), s.get("stdin"))
        entry = {"argv": redact_steps([s])[0], "rc": rc}
        log.append(entry)
        if rc != 0 and not s.get("ok_if_exists"):
            entry["error"] = out.strip()[:400]
            raise RuntimeError(f"comando falhou (rc={rc}): {' '.join(entry['argv'])}\n{entry['error']}")
    return log
