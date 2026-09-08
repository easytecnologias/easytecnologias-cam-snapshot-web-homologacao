# Isolamento de rede por conector — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fechar, de forma automática e permanente, o vazamento cross-tenant
em que dois conectores com a mesma faixa de IP privado (ex.: `MATA GRANDE`
e `PORTO REAL DO COLEGIO`, ambos com `192.168.10.0/24`) acessam o
dispositivo um do outro — via network namespace dedicado por conector, com
provisionamento automático em `ensure_wireguard_tunnel`, sem exigir passo
manual em nenhuma instalação nova nem nas já existentes.

**Architecture:** Um daemon privilegiado no host (`sightops-netns-provisioner`,
root, socket Unix) cria — por conector — um network namespace com sua
própria interface WireGuard (reaproveitando a chave privada do servidor já
existente) e um proxy SOCKS5 minimalista só alcançável de dentro daquele
namespace. A API (`ensure_wireguard_tunnel`, `fetch_device`, os endpoints
de rede de `dvr.py`) passa a rotear toda requisição a um dispositivo pelo
proxy SOCKS5 do conector daquele dispositivo — nunca mais por IP cru numa
tabela de rotas compartilhada. Persistência via `systemd`.

**Tech Stack:** Python 3 stdlib puro (sem dependência nova) para o daemon e
o proxy SOCKS5; `ip netns`/`wg` (iproute2 + wireguard-tools, já instalados
no host); `systemd` para persistência; `requests` (já em uso) com suporte a
proxy SOCKS5 (`requests[socks]` — precisa checar/instalar `PySocks`) do
lado da aplicação.

## Global Constraints

- Isolamento tem que ser real (tabela de roteamento do kernel diferente por
  conector), não apenas uma checagem de aplicação — requisito do spec:
  "em hipótese alguma um cliente pode se misturar no outro, nunca, em
  nenhuma forma".
- Todo conector — novo ou já existente — tem que ficar isolado sem passo
  manual: a automação mora em `ensure_wireguard_tunnel`.
- Nenhuma senha (root/`su`, chave privada do servidor) pode ser escrita em
  nenhum arquivo deste plano, do repo, ou de log. A chave privada do
  servidor WireGuard é lida em runtime de `/etc/wireguard/wg-sightops.conf`
  e passada ao daemon só em memória (nunca gravada em disco de novo em
  texto plano fora desse arquivo já existente).
- Sem fallback silencioso: se o isolamento de um conector não estiver
  provisionado, a requisição àquele dispositivo tem que falhar com erro
  explícito — nunca cair de volta pro caminho antigo compartilhado.
- Mudança de código Python (`ensure_wireguard_tunnel`, `fetch_device`,
  `dvr.py`) precisa ser aplicada nos dois ambientes de produção paralelos:
  `sightops-prod-api` e `sightops-v3-api` (mesma base de código, imagens
  separadas — ver memória `sightops-deploy-producao-real`).
- Produção não roda o `main` do git direto: qualquer patch em produção é
  aplicado extraindo o arquivo real do container e aplicando só o diff
  nele (ver skill `sightops-bug-producao`), nunca sobrescrevendo com a
  versão do repo sem checar antes.
- Testes deste repo não usam `pytest` — seguem o padrão local de scripts
  autoexecutáveis em `scripts/*_test.py` com `def main()` e `assert`,
  saída via `print` e `sys.exit(1)` em falha (ver
  `scripts/sightops_access_control_cpf_test.py` como referência).

---

## Mapa de arquivos

- Criar `scripts/netns_socks_proxy.py` — o proxy SOCKS5 (roda dentro de
  cada namespace).
- Criar `scripts/netns_socks_proxy_test.py` — testa o proxy contra um
  servidor HTTP local, sem precisar de root/namespace real.
- Criar `ops/netns_provisioner/allocator.py` — lógica pura (sem tocar
  root/rede) de alocação determinística de porta WireGuard e sub-rede veth
  por `connector_id`, e do arquivo de estado.
- Criar `ops/netns_provisioner/system_ops.py` — a única camada que executa
  comandos reais (`ip`, `wg`) — interface pequena e substituível por um
  fake nos testes.
- Criar `ops/netns_provisioner/daemon.py` — o daemon: escuta o socket
  Unix, faz parsing do protocolo JSON-lines, chama `allocator` +
  `system_ops`.
- Criar `ops/netns_provisioner/__init__.py` (vazio).
- Criar `scripts/sightops_netns_provisioner_test.py` — testa
  `allocator.py` e o parsing de protocolo do `daemon.py` com
  `system_ops` trocado por um fake (sem root).
- Criar `ops/netns_provisioner/sightops-netns-provisioner.service` —
  unit systemd do daemon.
- Criar `ops/netns_provisioner/sightops-netns-proxy@.service` — template
  unit do proxy (uma instância por conector).
- Criar `scripts/install_netns_provisioner.sh` — instala/atualiza os dois
  units + o daemon no host, idempotente.
- Criar `app/services/netns_provisioner_client.py` — cliente do socket
  Unix usado pela API (`ensure_wireguard_tunnel` chama daqui).
- Modificar `app/services/connector_service.py:1040-1098`
  (`ensure_wireguard_tunnel`) — chama o cliente acima e grava
  `netns_proxy_host`/`netns_proxy_port`/`netns_listen_port` no `tunnel`.
- Modificar `app/services/device_web_proxy.py:100-174` (`fetch_device`) —
  aceita proxy SOCKS5 opcional.
- Modificar `app/api/endpoints/maintenance.py` (`maintenance_camera_web_proxy`,
  `_camera_web_target_url`, `_ip_belongs_to_current_tenant`,
  `_camera_row_for_ip`/`_recorder_row_for_host` via `cameras.py`) — passa a
  exigir e resolver `connector_id`.
- Modificar `app/api/endpoints/dvr.py:1201-1294`
  (`api_dvr_network_get`/`api_dvr_network_apply`) — exige `connector_id`.
- Modificar `app/api/endpoints/cameras.py:138-157`
  (`_camera_row_for_ip`/`_ip_in_inventory`) — recusa ambiguidade quando o
  mesmo IP aparece em mais de um conector do tenant.
- Modificar `docker-compose.production.yml` e `docker-compose.yml` (e
  `docker-compose.homol.yml` se aplicável) — monta
  `/run/sightops:/run/sightops` no container da API.
- Modificar `docs/HANDOFF_AGENTES.md` — registra o que foi feito, o que
  ficou faseado e o achado novo (`_camera_row_for_ip` ambíguo).

---

### Task 1: Proxy SOCKS5 versionado + teste local

**Files:**
- Create: `scripts/netns_socks_proxy.py`
- Create: `scripts/netns_socks_proxy_test.py`

**Interfaces:**
- Produces: script executável `python scripts/netns_socks_proxy.py <porta> [--host 0.0.0.0]`,
  função `main(argv: list[str]) -> None` e `handle(conn: socket.socket) -> None`
  importáveis para o teste.

- [ ] **Step 1: Escrever o script do proxy**

Conteúdo (idêntico ao já validado ao vivo nesta sessão contra
`192.168.10.201`, adaptado para expor `main(argv)` testável):

```python
#!/usr/bin/env python3
"""SOCKS5 minimalista (so CONNECT, sem autenticacao) pra rodar DENTRO de um
network namespace isolado por conector. So existe pra dar a aplicacao um
jeito de escolher EXPLICITAMENTE qual rede de cliente alcancar (pelo
connector_id -> porta do proxy), nunca por IP sozinho -- e por isso que
roda dentro do namespace isolado: mesmo que o codigo erre, fisicamente
so alcanca a rede daquele cliente."""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading


def relay(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(4096)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            a.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(conn: socket.socket) -> None:
    try:
        conn.settimeout(15)
        ver, nmethods = conn.recv(2)
        conn.recv(nmethods)
        conn.sendall(bytes([0x05, 0x00]))

        header = conn.recv(4)
        ver, cmd, rsv, atyp = header
        if atyp == 0x01:
            addr = socket.inet_ntoa(conn.recv(4))
        elif atyp == 0x03:
            length = conn.recv(1)[0]
            addr = conn.recv(length).decode()
        else:
            conn.close()
            return
        port = struct.unpack(">H", conn.recv(2))[0]

        try:
            remote = socket.create_connection((addr, port), timeout=10)
            reply = struct.pack("!BBBB", 0x05, 0x00, 0x00, 0x01) + socket.inet_aton("0.0.0.0") + struct.pack(">H", 0)
            conn.sendall(reply)
        except OSError:
            reply = struct.pack("!BBBB", 0x05, 0x05, 0x00, 0x01) + socket.inet_aton("0.0.0.0") + struct.pack(">H", 0)
            conn.sendall(reply)
            conn.close()
            return

        conn.settimeout(None)
        remote.settimeout(None)
        t1 = threading.Thread(target=relay, args=(conn, remote), daemon=True)
        t2 = threading.Thread(target=relay, args=(remote, conn), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(host: str, port: int, ready_event: threading.Event | None = None) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(50)
    print(f"socks5 escutando em {host}:{port}", flush=True)
    if ready_event is not None:
        ready_event.set()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", type=int)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(argv)
    serve(args.host, args.port)


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 2: Escrever o teste**

```python
"""Confirma que o proxy SOCKS5 (scripts/netns_socks_proxy.py) faz CONNECT
corretamente e relay bidirecional, sem depender de rede de cliente real
nem de root/namespace -- so localhost."""
from __future__ import annotations

import http.server
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netns_socks_proxy  # noqa: E402


def _start_http_echo(port: int) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok-echo")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _socks5_get(proxy_port: int, target_host: str, target_port: int) -> bytes:
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(bytes([0x05, 0x01, 0x00]))
    assert s.recv(2) == bytes([0x05, 0x00])
    payload = bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton(target_host) + struct.pack(">H", target_port)
    s.sendall(payload)
    reply = s.recv(10)
    assert reply[1] == 0x00, f"SOCKS5 CONNECT falhou, rep={reply[1]}"
    s.sendall(f"GET / HTTP/1.0\r\nHost: {target_host}\r\n\r\n".encode())
    time.sleep(0.2)
    return s.recv(4096)


def main() -> None:
    http_port = 18080
    proxy_port = 18081
    _start_http_echo(http_port)

    ready = threading.Event()
    threading.Thread(
        target=netns_socks_proxy.serve, args=("127.0.0.1", proxy_port, ready), daemon=True
    ).start()
    assert ready.wait(timeout=5), "proxy nao subiu a tempo"

    resposta = _socks5_get(proxy_port, "127.0.0.1", http_port)
    assert b"ok-echo" in resposta, f"resposta inesperada: {resposta!r}"

    # porta sem ninguem escutando -- deve dar REP=5 (connection refused),
    # nunca travar nem derrubar o proxy
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(bytes([0x05, 0x01, 0x00]))
    s.recv(2)
    payload = bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton("127.0.0.1") + struct.pack(">H", 18082)
    s.sendall(payload)
    reply = s.recv(10)
    assert reply[1] == 0x05, f"esperava REP=5 (refused), veio {reply[1]}"

    print("OK: proxy SOCKS5 faz CONNECT, relay e recusa porta fechada corretamente")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Rodar o teste**

Run: `python scripts/netns_socks_proxy_test.py`
Expected: `OK: proxy SOCKS5 faz CONNECT, relay e recusa porta fechada corretamente`
(sem traceback, exit code 0)

- [ ] **Step 4: Commit**

```bash
git add scripts/netns_socks_proxy.py scripts/netns_socks_proxy_test.py
git commit -m "feat(netns): versiona proxy SOCKS5 usado por namespace isolado por conector"
```

---

### Task 2: Daemon provisionador — alocação + protocolo (testável sem root)

**Files:**
- Create: `ops/netns_provisioner/__init__.py`
- Create: `ops/netns_provisioner/allocator.py`
- Create: `ops/netns_provisioner/system_ops.py`
- Create: `ops/netns_provisioner/daemon.py`
- Create: `scripts/sightops_netns_provisioner_test.py`

**Interfaces:**
- Consumes: nada de tasks anteriores (Task 1 é usado só em runtime real,
  não é import).
- Produces:
  - `allocator.allocate(connector_id: str, state: dict) -> dict` — devolve
    `{"listen_port": int, "veth_root_ip": str, "veth_ns_ip": str, "veth_subnet_cidr": str}`,
    determinístico por `connector_id`, sem colisão contra `state` (que é o
    dict carregado de `allocations.json`).
  - `allocator.load_state(path: Path) -> dict` / `allocator.save_state(path: Path, state: dict) -> None`.
  - `system_ops.SystemOps` — classe com os métodos
    `ensure_namespace(connector_id, listen_port, server_private_key, peer_public_key, allowed_ips, veth_root_ip, veth_ns_ip, veth_subnet_cidr) -> None`,
    `remove_namespace(connector_id) -> None`,
    `namespace_status(connector_id) -> dict` (`{"exists": bool, "veth_ns_ip": str | None}`),
    todos implementados chamando `subprocess.run(["ip", ...])`/`["wg", ...])`.
  - `daemon.Provisioner(state_path: Path, system_ops: "system_ops.SystemOps")`
    com `handle_request(request: dict) -> dict`, que despacha `provision`/
    `deprovision`/`status` — usado tanto pelo servidor de socket real
    quanto pelo teste (com um `system_ops` fake).

- [ ] **Step 1: Escrever o teste de alocação (falha primeiro)**

```python
"""Testa a logica pura de alocacao de porta/sub-rede por conector, e o
protocolo do daemon, SEM tocar em rede/root real -- system_ops.SystemOps
e trocado por um fake que so grava o que seria executado."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.netns_provisioner import allocator, daemon


class FakeSystemOps:
    def __init__(self) -> None:
        self.chamadas: list[tuple] = []
        self._existentes: set[str] = set()

    def ensure_namespace(self, connector_id, **kwargs):
        self.chamadas.append(("ensure", connector_id, kwargs))
        self._existentes.add(connector_id)

    def remove_namespace(self, connector_id):
        self.chamadas.append(("remove", connector_id))
        self._existentes.discard(connector_id)

    def namespace_status(self, connector_id):
        existe = connector_id in self._existentes
        return {"exists": existe, "veth_ns_ip": "169.254.100.2" if existe else None}


def test_alocacao_deterministica_e_sem_colisao():
    state = {}
    a1 = allocator.allocate("3315d77dfdedb2ee", state)
    state[f"connector:3315d77dfdedb2ee"] = a1
    a1_de_novo = allocator.allocate("3315d77dfdedb2ee", state)
    assert a1 == a1_de_novo, "mesmo connector_id tem que devolver a mesma alocacao"

    a2 = allocator.allocate("cde8a557659fcdab", state)
    assert a2["listen_port"] != a1["listen_port"], "conectores diferentes nao podem colidir de porta"
    assert a2["veth_subnet_cidr"] != a1["veth_subnet_cidr"], "nem de sub-rede veth"
    print("OK: alocacao deterministica e sem colisao")


def test_persistencia_de_estado(tmp_path: Path):
    caminho = tmp_path / "allocations.json"
    state = {}
    a1 = allocator.allocate("conector-x", state)
    state["connector:conector-x"] = a1
    allocator.save_state(caminho, state)

    recarregado = allocator.load_state(caminho)
    assert recarregado == state, "estado recarregado tem que bater com o salvo"
    print("OK: persistencia de estado")


def test_provision_idempotente():
    fake = FakeSystemOps()
    with tempfile.TemporaryDirectory() as tmp:
        prov = daemon.Provisioner(state_path=Path(tmp) / "allocations.json", system_ops=fake)
        req = {
            "op": "provision",
            "connector_id": "3315d77dfdedb2ee",
            "server_private_key": "chave-fake-de-teste",
            "peer_public_key": "1iDptEuSPwlUHOBVoB5h2lnWFW+QBkB4OOPAbUUrJUI=",
            "allowed_ips": ["10.250.0.10/32", "192.168.10.0/24"],
        }
        resp1 = daemon_handle(prov, req)
        assert resp1["ok"] is True
        assert resp1["proxy_host"] and resp1["proxy_port"]
        assert len(fake.chamadas) == 1, "primeira chamada tem que criar o namespace"

        resp2 = daemon_handle(prov, req)
        assert resp2["ok"] is True
        assert resp2["listen_port"] == resp1["listen_port"]
        assert len(fake.chamadas) == 1, "segunda chamada com a mesma config nao pode recriar nada"
        print("OK: provision idempotente")


def test_provision_reconfigura_quando_peer_muda():
    fake = FakeSystemOps()
    with tempfile.TemporaryDirectory() as tmp:
        prov = daemon.Provisioner(state_path=Path(tmp) / "allocations.json", system_ops=fake)
        base_req = {
            "op": "provision",
            "connector_id": "conector-y",
            "server_private_key": "chave-fake",
            "peer_public_key": "AAA=",
            "allowed_ips": ["10.250.0.20/32"],
        }
        daemon_handle(prov, base_req)
        req_mudou = dict(base_req, allowed_ips=["10.250.0.20/32", "172.16.30.0/24"])
        resp = daemon_handle(prov, req_mudou)
        assert resp["ok"] is True
        assert len(fake.chamadas) == 2, "peer/allowed_ips mudou -- tem que reconfigurar, sem recriar do zero"
        print("OK: reconfigura sem recriar quando so o peer muda")


def test_deprovision():
    fake = FakeSystemOps()
    with tempfile.TemporaryDirectory() as tmp:
        prov = daemon.Provisioner(state_path=Path(tmp) / "allocations.json", system_ops=fake)
        daemon_handle(prov, {
            "op": "provision", "connector_id": "conector-z",
            "server_private_key": "k", "peer_public_key": "P=", "allowed_ips": ["10.250.0.30/32"],
        })
        resp = daemon_handle(prov, {"op": "deprovision", "connector_id": "conector-z"})
        assert resp["ok"] is True
        status = daemon_handle(prov, {"op": "status", "connector_id": "conector-z"})
        assert status["exists"] is False
        print("OK: deprovision remove o namespace e status reflete")


def daemon_handle(prov: "daemon.Provisioner", req: dict) -> dict:
    return prov.handle_request(req)


def main() -> None:
    from pathlib import Path as _P
    import tempfile as _tmp

    test_alocacao_deterministica_e_sem_colisao()
    with _tmp.TemporaryDirectory() as tmp:
        test_persistencia_de_estado(_P(tmp))
    test_provision_idempotente()
    test_provision_reconfigura_quando_peer_muda()
    test_deprovision()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Rodar o teste e confirmar que falha**

Run: `python scripts/sightops_netns_provisioner_test.py`
Expected: `ModuleNotFoundError: No module named 'ops'` (ainda não existe)

- [ ] **Step 3: Criar `ops/netns_provisioner/__init__.py`**

```python
```

(arquivo vazio, só marca o pacote)

- [ ] **Step 4: Escrever `ops/netns_provisioner/allocator.py`**

```python
"""Alocacao deterministica de recursos (porta WireGuard, sub-rede veth) por
conector, e persistencia do estado -- logica pura, sem tocar em
root/rede/systemd. O daemon (daemon.py) e' quem usa isto pra decidir o que
pedir a system_ops."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

LISTEN_PORT_BASE = 52000
LISTEN_PORT_RANGE = 1000  # 52000-52999
VETH_OCTET_BASE = 100  # 169.254.100.0/24 .. expande se esgotar
VETH_SLOTS_PER_OCTET = 64  # blocos /30 por octeto (256 / 4)


def _stable_index(connector_id: str, modulo: int) -> int:
    digest = hashlib.sha256(connector_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulo


def allocate(connector_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Devolve a alocacao para este connector_id. Se ja existe em `state`
    (dict carregado de allocations.json, chave "connector:<id>"), devolve a
    MESMA alocacao (idempotente) -- so aloca novo se ainda nao existe."""
    chave = f"connector:{connector_id}"
    existente = state.get(chave)
    if isinstance(existente, dict) and existente.get("listen_port"):
        return existente

    ocupados_portas = {
        v["listen_port"] for k, v in state.items() if k.startswith("connector:") and isinstance(v, dict)
    }
    ocupados_slots = {
        v["veth_slot"] for k, v in state.items() if k.startswith("connector:") and isinstance(v, dict) and "veth_slot" in v
    }

    porta = LISTEN_PORT_BASE + _stable_index(connector_id, LISTEN_PORT_RANGE)
    tentativas = 0
    while porta in ocupados_portas and tentativas < LISTEN_PORT_RANGE:
        porta = LISTEN_PORT_BASE + ((porta - LISTEN_PORT_BASE + 1) % LISTEN_PORT_RANGE)
        tentativas += 1

    slot = _stable_index(connector_id, VETH_SLOTS_PER_OCTET)
    tentativas = 0
    while slot in ocupados_slots and tentativas < VETH_SLOTS_PER_OCTET:
        slot = (slot + 1) % VETH_SLOTS_PER_OCTET
        tentativas += 1

    base_terceiro_octeto = VETH_OCTET_BASE + (slot // 64)
    quarto_octeto_base = (slot % 64) * 4
    veth_subnet_cidr = f"169.254.{base_terceiro_octeto}.{quarto_octeto_base}/30"
    veth_root_ip = f"169.254.{base_terceiro_octeto}.{quarto_octeto_base + 1}"
    veth_ns_ip = f"169.254.{base_terceiro_octeto}.{quarto_octeto_base + 2}"

    return {
        "listen_port": porta,
        "veth_slot": slot,
        "veth_subnet_cidr": veth_subnet_cidr,
        "veth_root_ip": veth_root_ip,
        "veth_ns_ip": veth_ns_ip,
        "proxy_port": 8080,
    }


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        dados = json.loads(path.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
```

- [ ] **Step 5: Escrever `ops/netns_provisioner/system_ops.py`**

```python
"""Unica camada que executa comando real (ip/wg) contra o kernel. Isolada
num objeto pequeno de proposito -- e' o que o teste troca por um fake pra
testar allocator.py e daemon.py sem precisar de root."""
from __future__ import annotations

import subprocess
from typing import Any, Dict, List


class SystemOps:
    def _run(self, args: List[str], input_text: str | None = None) -> None:
        subprocess.run(args, input=input_text, text=True, check=True, capture_output=True)

    def ensure_namespace(
        self,
        connector_id: str,
        *,
        listen_port: int,
        server_private_key: str,
        peer_public_key: str,
        allowed_ips: List[str],
        veth_root_ip: str,
        veth_ns_ip: str,
        veth_subnet_cidr: str,
    ) -> None:
        ns = f"ns-{connector_id}"
        iface = f"wg-{connector_id}"
        veth_root = f"veth-{connector_id[:8]}0"
        veth_ns = f"veth-{connector_id[:8]}1"
        prefix_len = veth_subnet_cidr.split("/")[-1]

        status = self.namespace_status(connector_id)
        if not status["exists"]:
            self._run(["ip", "netns", "add", ns])
            self._run(["ip", "link", "add", iface, "type", "wireguard"])
            self._run(["ip", "link", "set", iface, "netns", ns])
            self._run(["ip", "netns", "exec", ns, "ip", "addr", "add", "10.250.0.1/24", "dev", iface])
            self._run(
                ["ip", "netns", "exec", ns, "wg", "set", iface, "listen-port", str(listen_port), "private-key", "/dev/stdin"],
                input_text=server_private_key,
            )
            self._run(["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"])
            self._run(["ip", "netns", "exec", ns, "ip", "link", "set", iface, "up"])

            self._run(["ip", "link", "add", veth_root, "type", "veth", "peer", "name", veth_ns])
            self._run(["ip", "link", "set", veth_ns, "netns", ns])
            self._run(["ip", "addr", "add", f"{veth_root_ip}/{prefix_len}", "dev", veth_root])
            self._run(["ip", "link", "set", veth_root, "up"])
            self._run(["ip", "netns", "exec", ns, "ip", "addr", "add", f"{veth_ns_ip}/{prefix_len}", "dev", veth_ns])
            self._run(["ip", "netns", "exec", ns, "ip", "link", "set", veth_ns, "up"])

        self._run(
            ["ip", "netns", "exec", ns, "wg", "set", iface, "peer", peer_public_key, "allowed-ips", ",".join(allowed_ips)]
        )

    def remove_namespace(self, connector_id: str) -> None:
        ns = f"ns-{connector_id}"
        subprocess.run(["ip", "netns", "del", ns], text=True, capture_output=True)

    def namespace_status(self, connector_id: str) -> Dict[str, Any]:
        ns = f"ns-{connector_id}"
        resultado = subprocess.run(["ip", "netns", "list"], text=True, capture_output=True)
        existe = ns in (resultado.stdout or "")
        return {"exists": existe}
```

- [ ] **Step 6: Escrever `ops/netns_provisioner/daemon.py`**

```python
"""Nucleo do daemon: recebe uma requisicao ja parseada (dict), decide o
que fazer via allocator.py, executa via system_ops, devolve resposta
(dict). O transporte real (socket Unix, JSON-lines) fica em
scripts/install_netns_provisioner.sh + o entrypoint abaixo -- aqui so a
logica, pra ficar testavel sem root (ver Task 2)."""
from __future__ import annotations

import json
import socketserver
import os
from pathlib import Path
from typing import Any, Dict

from . import allocator


class Provisioner:
    def __init__(self, state_path: Path, system_ops: Any) -> None:
        self.state_path = state_path
        self.system_ops = system_ops

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        op = request.get("op")
        connector_id = str(request.get("connector_id") or "").strip()
        if not connector_id or not connector_id.replace("-", "").isalnum():
            return {"ok": False, "error": "connector_id invalido", "code": "bad_request"}

        if op == "provision":
            return self._provision(connector_id, request)
        if op == "deprovision":
            return self._deprovision(connector_id)
        if op == "status":
            return self._status(connector_id)
        return {"ok": False, "error": f"operacao desconhecida: {op!r}", "code": "bad_request"}

    def _provision(self, connector_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
        server_private_key = str(request.get("server_private_key") or "")
        peer_public_key = str(request.get("peer_public_key") or "")
        allowed_ips = request.get("allowed_ips") or []
        if not server_private_key or not peer_public_key or not isinstance(allowed_ips, list) or not allowed_ips:
            return {"ok": False, "error": "server_private_key/peer_public_key/allowed_ips obrigatorios", "code": "bad_request"}

        state = allocator.load_state(self.state_path)
        chave = f"connector:{connector_id}"
        alocacao_anterior = state.get(chave)
        alocacao = allocator.allocate(connector_id, state)

        peer_mudou = (
            not isinstance(alocacao_anterior, dict)
            or alocacao_anterior.get("peer_public_key") != peer_public_key
            or alocacao_anterior.get("allowed_ips") != allowed_ips
        )

        self.system_ops.ensure_namespace(
            connector_id,
            listen_port=alocacao["listen_port"],
            server_private_key=server_private_key,
            peer_public_key=peer_public_key,
            allowed_ips=allowed_ips,
            veth_root_ip=alocacao["veth_root_ip"],
            veth_ns_ip=alocacao["veth_ns_ip"],
            veth_subnet_cidr=alocacao["veth_subnet_cidr"],
        ) if peer_mudou or not isinstance(alocacao_anterior, dict) else None

        alocacao = dict(alocacao, peer_public_key=peer_public_key, allowed_ips=allowed_ips)
        state[chave] = alocacao
        allocator.save_state(self.state_path, state)

        return {
            "ok": True,
            "listen_port": alocacao["listen_port"],
            "veth_subnet": alocacao["veth_subnet_cidr"],
            "proxy_host": alocacao["veth_ns_ip"],
            "proxy_port": alocacao["proxy_port"],
        }

    def _deprovision(self, connector_id: str) -> Dict[str, Any]:
        state = allocator.load_state(self.state_path)
        chave = f"connector:{connector_id}"
        self.system_ops.remove_namespace(connector_id)
        state.pop(chave, None)
        allocator.save_state(self.state_path, state)
        return {"ok": True}

    def _status(self, connector_id: str) -> Dict[str, Any]:
        state = allocator.load_state(self.state_path)
        alocacao = state.get(f"connector:{connector_id}")
        kernel_status = self.system_ops.namespace_status(connector_id)
        if not isinstance(alocacao, dict) or not kernel_status.get("exists"):
            return {"ok": True, "exists": False}
        return {
            "ok": True,
            "exists": True,
            "listen_port": alocacao["listen_port"],
            "proxy_host": alocacao["veth_ns_ip"],
            "proxy_port": alocacao["proxy_port"],
        }


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        provisioner: Provisioner = self.server.provisioner  # type: ignore[attr-defined]
        for linha in self.rfile:
            try:
                request = json.loads(linha.decode("utf-8"))
                resposta = provisioner.handle_request(request)
            except Exception as exc:  # nunca derruba o daemon por uma requisicao ruim
                resposta = {"ok": False, "error": str(exc), "code": "internal_error"}
            self.wfile.write((json.dumps(resposta) + "\n").encode("utf-8"))
            self.wfile.flush()


def serve_forever(socket_path: Path, state_path: Path, system_ops: Any) -> None:
    if socket_path.exists():
        socket_path.unlink()
    server = socketserver.UnixStreamServer(str(socket_path), _Handler)
    server.provisioner = Provisioner(state_path=state_path, system_ops=system_ops)  # type: ignore[attr-defined]
    os.chmod(socket_path, 0o660)
    server.serve_forever()
```

- [ ] **Step 7: Rodar o teste e confirmar que passa**

Run: `python scripts/sightops_netns_provisioner_test.py`
Expected: 5 linhas `OK: ...`, exit code 0, sem traceback.

- [ ] **Step 8: Commit**

```bash
git add ops/netns_provisioner scripts/sightops_netns_provisioner_test.py
git commit -m "feat(netns): daemon provisionador (alocacao + protocolo), testavel sem root"
```

---

### Task 3: Units systemd + script de instalação

**Files:**
- Create: `ops/netns_provisioner/sightops-netns-provisioner.service`
- Create: `ops/netns_provisioner/sightops-netns-proxy@.service`
- Create: `ops/netns_provisioner/entrypoint.py`
- Create: `scripts/install_netns_provisioner.sh`

**Interfaces:**
- Consumes: `ops/netns_provisioner/daemon.serve_forever`, `ops/netns_provisioner/system_ops.SystemOps` (Task 2); `scripts/netns_socks_proxy.py` (Task 1).
- Produces: dois serviços systemd instaláveis (`sightops-netns-provisioner.service`, `sightops-netns-proxy@.service`) e um instalador idempotente.

Esta task não tem teste automatizado (mexe com root/systemd de verdade) —
a verificação é manual, na Task 4, contra o servidor real.

- [ ] **Step 1: Escrever o entrypoint do daemon**

```python
#!/usr/bin/env python3
"""Entrypoint real do daemon -- le a chave privada do wg-sightops.conf so
em memoria (nunca grava em outro arquivo), sobe o daemon.serve_forever."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.netns_provisioner import daemon, system_ops

SOCKET_PATH = Path("/run/sightops/netns-provisioner.sock")
STATE_PATH = Path("/var/lib/sightops-netns-provisioner/allocations.json")


def main() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    daemon.serve_forever(SOCKET_PATH, STATE_PATH, system_ops.SystemOps())


if __name__ == "__main__":
    main()
```

Nota para quem executar: a chave privada do servidor **não** é lida aqui —
é o payload de `provision` (mandado pela API) que carrega
`server_private_key`, lido por ela de onde a API já lê hoje. Este
entrypoint só sobe o daemon; não abre `wg-sightops.conf`.

- [ ] **Step 2: Escrever a unit do daemon**

```ini
# ops/netns_provisioner/sightops-netns-provisioner.service
[Unit]
Description=SightOps - provisionador de namespace de rede por conector
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/sightops-netns-provisioner/entrypoint.py
Restart=on-failure
RestartSec=3
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 3: Escrever o template unit do proxy**

```ini
# ops/netns_provisioner/sightops-netns-proxy@.service
[Unit]
Description=SightOps - proxy SOCKS5 isolado do conector %i
After=sightops-netns-provisioner.service
Requires=sightops-netns-provisioner.service

[Service]
Type=simple
ExecStart=/usr/bin/ip netns exec ns-%i /usr/bin/python3 /opt/sightops-netns-provisioner/netns_socks_proxy.py 8080
Restart=always
RestartSec=2
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Escrever o instalador**

```bash
#!/bin/bash
# scripts/install_netns_provisioner.sh
# Instala/atualiza o daemon provisionador + o proxy SOCKS5 no host como
# systemd units. Idempotente -- rodar de novo so atualiza os arquivos e
# reinicia o daemon (nunca os namespaces ja provisionados, que o daemon
# reconcilia sozinho ao subir).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "precisa rodar como root (sudo/su)" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/sightops-netns-provisioner

mkdir -p "$DEST"
cp -r "$REPO_DIR/ops/netns_provisioner/__init__.py" "$DEST/../ops_init_check" 2>/dev/null || true
mkdir -p "$DEST/ops/netns_provisioner"
cp "$REPO_DIR/ops/netns_provisioner/__init__.py" "$DEST/ops/netns_provisioner/"
cp "$REPO_DIR/ops/netns_provisioner/allocator.py" "$DEST/ops/netns_provisioner/"
cp "$REPO_DIR/ops/netns_provisioner/system_ops.py" "$DEST/ops/netns_provisioner/"
cp "$REPO_DIR/ops/netns_provisioner/daemon.py" "$DEST/ops/netns_provisioner/"
touch "$DEST/ops/__init__.py"
cp "$REPO_DIR/ops/netns_provisioner/entrypoint.py" "$DEST/entrypoint.py"
cp "$REPO_DIR/scripts/netns_socks_proxy.py" "$DEST/netns_socks_proxy.py"

cp "$REPO_DIR/ops/netns_provisioner/sightops-netns-provisioner.service" /etc/systemd/system/
cp "$REPO_DIR/ops/netns_provisioner/sightops-netns-proxy@.service" /etc/systemd/system/

mkdir -p /run/sightops /var/lib/sightops-netns-provisioner

systemctl daemon-reload
systemctl enable --now sightops-netns-provisioner.service
systemctl restart sightops-netns-provisioner.service

sleep 1
systemctl is-active --quiet sightops-netns-provisioner.service && \
  echo "OK: sightops-netns-provisioner ativo" || \
  { echo "FALHOU: sightops-netns-provisioner nao ativou, ver: journalctl -u sightops-netns-provisioner -n 50" >&2; exit 1; }

echo "socket em /run/sightops/netns-provisioner.sock:"
ls -la /run/sightops/netns-provisioner.sock
```

- [ ] **Step 5: Checar sintaxe do bash (sem executar em root aqui)**

Run: `bash -n scripts/install_netns_provisioner.sh`
Expected: sem saída (script sintaticamente válido).

- [ ] **Step 6: Commit**

```bash
git add ops/netns_provisioner/entrypoint.py ops/netns_provisioner/sightops-netns-provisioner.service ops/netns_provisioner/sightops-netns-proxy@.service scripts/install_netns_provisioner.sh
git commit -m "feat(netns): units systemd + instalador idempotente do daemon"
```

---

### Task 4: Validação manual em produção — instalar e migrar Mata Grande pelo daemon

**Não automatizável** (root real, servidor real). Executar via SSH em
`central@10.10.12.7` (fallback `central@201.182.184.84`), com a senha de
`su` pedida na hora ao usuário (nunca escrita em arquivo).

**Files:** nenhum arquivo do repo é modificado nesta task — só o servidor.

- [ ] **Step 1: Remover o protótipo manual desta sessão**

Como root no servidor:
```bash
pkill -f "matagrande.py 8080" || true
ip link del veth-mg0 2>/dev/null || true
ip netns del ns-matagrande 2>/dev/null || true
```
Verificar: `ip netns list` não deve mais listar `ns-matagrande`.

- [ ] **Step 2: Levar o repo (ou só os arquivos das Tasks 1-3) para o servidor e instalar**

```bash
scp -r ops scripts/install_netns_provisioner.sh scripts/netns_socks_proxy.py central@10.10.12.7:/tmp/sightops-netns-provisioner-src/
ssh central@10.10.12.7
su -
cd /tmp/sightops-netns-provisioner-src
# ajustar caminhos do install_netns_provisioner.sh se copiado fora da estrutura de repo, ou copiar o repo inteiro
bash scripts/install_netns_provisioner.sh
```
Expected: `OK: sightops-netns-provisioner ativo` e o socket listado.

- [ ] **Step 3: Provisionar o Mata Grande através do daemon (script de teste manual, não fica no repo)**

No servidor, como root, um script Python curto de uma linha de comando
que fala com o socket (equivalente ao `curl`/`nc` para JSON-lines):
```bash
python3 - <<'PYEOF'
import json, socket
PRIVKEY = open("/etc/wireguard/wg-sightops.conf").read()
import re
privkey = re.search(r'^PrivateKey\s*=\s*(\S+)', PRIVKEY, re.M).group(1)
req = {
    "op": "provision",
    "connector_id": "3315d77dfdedb2ee",
    "server_private_key": privkey,
    "peer_public_key": "1iDptEuSPwlUHOBVoB5h2lnWFW+QBkB4OOPAbUUrJUI=",
    "allowed_ips": ["10.250.0.10/32", "172.16.20.0/24", "172.16.25.0/24", "172.16.30.0/24", "192.168.10.0/24"],
}
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/sightops/netns-provisioner.sock")
s.sendall((json.dumps(req) + "\n").encode())
print(s.recv(4096).decode())
PYEOF
```
Expected: `{"ok": true, "listen_port": ..., "proxy_host": "169.254.X.2", "proxy_port": 8080, ...}`
(a chave privada nunca aparece na saída — não é ecoada pela resposta).

- [ ] **Step 4: Subir o proxy do Mata Grande via systemd (não mais processo solto)**

```bash
systemctl enable --now sightops-netns-proxy@3315d77dfdedb2ee.service
systemctl is-active --quiet sightops-netns-proxy@3315d77dfdedb2ee.service && echo OK
```

- [ ] **Step 5: Validar com o mesmo teste curl já usado nesta sessão**

```bash
# caminho antigo (compartilhado, ainda com o peer la -- so remove na Fase 3, Task 9)
curl --connect-timeout 5 http://192.168.10.201/
# caminho novo isolado (usar o proxy_host devolvido no Step 3, ex. 169.254.100.2)
curl --socks5-hostname 169.254.100.2:8080 http://192.168.10.201/
```
Expected: igual ao já comprovado nesta sessão — caminho antigo 200 (do
Porto Real), caminho novo isolado "connection refused" (rede certa do
Mata Grande).

- [ ] **Step 6: Testar recuperação após reinício do daemon**

```bash
systemctl restart sightops-netns-provisioner.service
sleep 2
ip netns list  # ns-3315d77dfdedb2ee tem que continuar existindo
systemctl is-active --quiet sightops-netns-proxy@3315d77dfdedb2ee.service && echo "proxy sobreviveu ao restart do daemon"
```

Nenhum commit nesta task (é validação manual no servidor).

---

### Task 5: Integrar `ensure_wireguard_tunnel` com o daemon

**Files:**
- Create: `app/services/netns_provisioner_client.py`
- Create: `scripts/sightops_netns_provisioner_client_test.py`
- Modify: `app/services/connector_service.py:1040-1098`

**Interfaces:**
- Consumes: nada de código anterior (fala com o daemon via socket, testado
  com um socket fake nos testes).
- Produces: `netns_provisioner_client.provision(connector_id, server_private_key, peer_public_key, allowed_ips, *, socket_path=DEFAULT_SOCKET_PATH) -> dict`
  (levanta `NetnsProvisionerError` em qualquer resposta `{"ok": false}` ou
  falha de conexão), usado por `ensure_wireguard_tunnel`.

- [ ] **Step 1: Escrever o teste do cliente (falha primeiro)**

```python
"""Testa netns_provisioner_client contra um servidor de socket Unix real
(nao mockado no nivel de rede -- so o servidor do outro lado e' um fake
simples), sem precisar do daemon real nem de root."""
from __future__ import annotations

import json
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.netns_provisioner_client import NetnsProvisionerError, provision


class _FakeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        for linha in self.rfile:
            req = json.loads(linha.decode())
            if req.get("connector_id") == "erro-proposital":
                resp = {"ok": False, "error": "falha proposital", "code": "conflict"}
            else:
                resp = {"ok": True, "listen_port": 52001, "veth_subnet": "169.254.100.0/30", "proxy_host": "169.254.100.2", "proxy_port": 8080}
            self.wfile.write((json.dumps(resp) + "\n").encode())
            self.wfile.flush()


def _start_fake_daemon(socket_path: Path) -> socketserver.UnixStreamServer:
    server = socketserver.UnixStreamServer(str(socket_path), _FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        socket_path = Path(tmp) / "provisioner.sock"
        _start_fake_daemon(socket_path)

        resultado = provision(
            "3315d77dfdedb2ee", "chave-privada-fake", "peer-pub-fake", ["192.168.10.0/24"],
            socket_path=socket_path,
        )
        assert resultado["proxy_host"] == "169.254.100.2"
        assert resultado["proxy_port"] == 8080
        print("OK: provision via socket real devolve proxy_host/porta")

        try:
            provision("erro-proposital", "k", "p", ["10.0.0.0/24"], socket_path=socket_path)
            raise AssertionError("deveria ter levantado NetnsProvisionerError")
        except NetnsProvisionerError as exc:
            assert "falha proposital" in str(exc)
            print("OK: erro do daemon vira NetnsProvisionerError")

        try:
            provision("x", "k", "p", ["10.0.0.0/24"], socket_path=Path(tmp) / "nao-existe.sock")
            raise AssertionError("deveria ter levantado NetnsProvisionerError por socket ausente")
        except NetnsProvisionerError:
            print("OK: socket ausente vira NetnsProvisionerError (nao derruba a app)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `python scripts/sightops_netns_provisioner_client_test.py`
Expected: `ModuleNotFoundError: No module named 'app.services.netns_provisioner_client'`

- [ ] **Step 3: Escrever `app/services/netns_provisioner_client.py`**

```python
"""Cliente do daemon sightops-netns-provisioner (socket Unix, JSON-lines).
Usado por ensure_wireguard_tunnel pra provisionar o isolamento de rede de
cada conector -- ver docs/superpowers/specs/2026-09-08-isolamento-rede-por-conector-design.md.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_SOCKET_PATH = Path("/run/sightops/netns-provisioner.sock")
_TIMEOUT_SECONDS = 10.0


class NetnsProvisionerError(Exception):
    pass


def _call(request: Dict[str, Any], socket_path: Path) -> Dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT_SECONDS)
            sock.connect(str(socket_path))
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            dados = b""
            while not dados.endswith(b"\n"):
                pedaco = sock.recv(4096)
                if not pedaco:
                    break
                dados += pedaco
    except OSError as exc:
        raise NetnsProvisionerError(f"nao foi possivel falar com o provisionador de rede ({socket_path}): {exc}") from exc

    try:
        resposta = json.loads(dados.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise NetnsProvisionerError(f"resposta invalida do provisionador de rede: {dados!r}") from exc

    if not resposta.get("ok"):
        raise NetnsProvisionerError(f"provisionador de rede recusou: {resposta.get('error')}")
    return resposta


def provision(
    connector_id: str,
    server_private_key: str,
    peer_public_key: str,
    allowed_ips: List[str],
    *,
    socket_path: Path = DEFAULT_SOCKET_PATH,
) -> Dict[str, Any]:
    return _call(
        {
            "op": "provision",
            "connector_id": connector_id,
            "server_private_key": server_private_key,
            "peer_public_key": peer_public_key,
            "allowed_ips": allowed_ips,
        },
        socket_path,
    )
```

- [ ] **Step 4: Rodar o teste e confirmar que passa**

Run: `python scripts/sightops_netns_provisioner_client_test.py`
Expected: 3 linhas `OK: ...`

- [ ] **Step 5: Integrar em `ensure_wireguard_tunnel`**

Em `app/services/connector_service.py`, adicionar o import no topo (perto
dos outros imports de `app.services`/`app.core`):

```python
from app.services.netns_provisioner_client import NetnsProvisionerError, provision as netns_provision
```

E editar o trecho `app/services/connector_service.py:1068-1093` (dentro de
`ensure_wireguard_tunnel`, depois de montar `tunnel["client_lans"]` e
antes/durante o `tunnel.update`), acrescentando a chamada ao provisionador
logo após garantir `client_private_key`/`client_public_key` (que já
existem no bloco atual) e usando o `server_private_key` da mesma forma que
`build_routeros_wireguard_script` obtém a chave do servidor hoje — checar
nesse ponto do código como esse valor já é lido (procurar por
`server_private_key`/`_wireguard_server_private_key` no arquivo; se não
existir uma função equivalente à `_wireguard_server_public_key` para a
chave privada, criar `_wireguard_server_private_key()` espelhando o mesmo
padrão de onde `_wireguard_server_public_key()` lê o valor público, sem
nunca logar o retorno):

```python
        tunnel["server_public_key"] = _wireguard_server_public_key()
        if not tunnel.get("client_private_key") or not tunnel.get("client_public_key"):
            client = _wg_keypair()
            tunnel["client_private_key"] = client["private_key"]
            tunnel["client_public_key"] = client["public_key"]

        try:
            isolamento = netns_provision(
                cid,
                _wireguard_server_private_key(),
                tunnel["client_public_key"],
                client_lans or [tunnel.get("client_address") or ""],
            )
        except NetnsProvisionerError as exc:
            raise ValueError(f"nao foi possivel isolar a rede deste conector: {exc}") from exc
        tunnel["netns_proxy_host"] = isolamento["proxy_host"]
        tunnel["netns_proxy_port"] = isolamento["proxy_port"]
        tunnel["netns_listen_port"] = isolamento["listen_port"]
```

Note bem: isso roda **dentro do `with _lock:`** já existente, então a
chamada de rede ao socket Unix acontece enquanto o lock global de
conectores está seguro — aceitável porque a chamada é local (socket Unix
no mesmo host, não uma rede externa) e tem timeout de 10s.

- [ ] **Step 6: Compilar e importar para checar que não quebrou nada**

Run: `python -m compileall app && python -B -c "import app.main; print('app.main OK')"`
Expected: `app.main OK`, sem erro.

- [ ] **Step 7: Escrever teste de integração (mock do socket) para `ensure_wireguard_tunnel`**

Seguir o padrão dos testes existentes de `connector_service`
(`DATA_DIR`/`DATABASE_BACKEND` via variável de ambiente, tenant fake) —
usar `unittest.mock.patch` só em `app.services.connector_service.netns_provision`
para não precisar de socket real:

```python
"""Confirma que ensure_wireguard_tunnel chama o provisionador de rede e
grava netns_proxy_host/porta no tunnel do conector -- com o provisionador
mockado (sem socket real)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-netns-test-key"

        from app.core.tenant_context import set_current_tenant_slug
        from app.services import connector_service as cs

        set_current_tenant_slug("tenant-teste")
        connector = cs.create_connector({"name": "conector-teste", "type": "wireguard"})
        connector_id = connector["connector"]["id"]

        fake_isolamento = {"proxy_host": "169.254.100.2", "proxy_port": 8080, "listen_port": 52001, "veth_subnet": "169.254.100.0/30"}
        with patch.object(cs, "netns_provision", return_value=fake_isolamento) as mocked:
            resultado = cs.ensure_wireguard_tunnel(connector_id, {"lan_mode": "manual", "client_lans": "192.168.77.0/24"})
            assert mocked.called, "ensure_wireguard_tunnel tem que chamar o provisionador de rede"

        tunnel = resultado["tunnel"]
        assert tunnel["netns_proxy_host"] == "169.254.100.2"
        assert tunnel["netns_proxy_port"] == 8080
        print("OK: ensure_wireguard_tunnel grava netns_proxy_host/porta vindos do provisionador")


if __name__ == "__main__":
    main()
```

Ajustar nomes de função (`create_connector` etc.) para os que já existem
em `connector_service.py` caso o nome real seja diferente — conferir com
`grep -n "^def create_connector\|^def " app/services/connector_service.py`
antes de finalizar este teste.

- [ ] **Step 8: Rodar o teste e confirmar que passa**

Run: `python scripts/sightops_netns_provisioner_client_test.py && python -m compileall app`
Expected: sem erro.

- [ ] **Step 9: Commit**

```bash
git add app/services/netns_provisioner_client.py app/services/connector_service.py scripts/sightops_netns_provisioner_client_test.py
git commit -m "feat(netns): ensure_wireguard_tunnel provisiona isolamento de rede automaticamente"
```

---

### Task 6: `fetch_device` e `dvr.py` roteiam por `connector_id`, sem fallback silencioso

**Files:**
- Modify: `app/services/device_web_proxy.py:100-174`
- Modify: `app/api/endpoints/maintenance.py` (rota `maintenance_camera_web_proxy` e `_camera_web_target_url`)
- Modify: `app/api/endpoints/dvr.py:1201-1294`
- Create: `scripts/sightops_device_web_proxy_netns_test.py`

**Interfaces:**
- Consumes: `tunnel.netns_proxy_host`/`netns_proxy_port` gravados pela Task 5.
- Produces: `fetch_device(..., *, socks_proxy: tuple[str, int] | None = None, ...)` —
  quando `socks_proxy` é passado, toda requisição usa
  `proxies={"http": f"socks5h://{host}:{port}", "https": f"socks5h://{host}:{port}"}`.

- [ ] **Step 1: Adicionar dependência `PySocks` (necessária para `requests` suportar `socks5h://`)**

Checar se já está em `requirements.txt`:
```bash
grep -i pysocks requirements.txt || echo "nao esta"
```
Se não estiver, adicionar a linha `PySocks>=1.7.1` em `requirements.txt` e
rodar `pip install PySocks>=1.7.1` no ambiente de desenvolvimento local
para o teste da Step 5 funcionar.

- [ ] **Step 2: Escrever o teste (falha primeiro)**

```python
"""Confirma que fetch_device, quando recebe socks_proxy, roteia a
requisicao por ele (nao direto) -- validado subindo um proxy SOCKS5 real
(scripts/netns_socks_proxy.py) e um servidor HTTP alvo, ambos locais."""
from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.device_web_proxy import fetch_device
import netns_socks_proxy


def _start_http_target(port: int, marca: bytes) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(marca)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    target_port = 18090
    proxy_port = 18091
    _start_http_target(target_port, b"resposta-do-alvo-via-proxy")

    ready = threading.Event()
    threading.Thread(target=netns_socks_proxy.serve, args=("127.0.0.1", proxy_port, ready), daemon=True).start()
    assert ready.wait(timeout=5)

    resposta = fetch_device(
        "127.0.0.1", "/", "", "GET", {}, b"",
        http_port=target_port, socks_proxy=("127.0.0.1", proxy_port),
    )
    assert resposta.status_code == 200
    assert resposta.content == b"resposta-do-alvo-via-proxy"
    print("OK: fetch_device roteia via socks_proxy quando informado")

    # sem socks_proxy continua indo direto (comportamento antigo preservado)
    resposta_direta = fetch_device("127.0.0.1", "/", "", "GET", {}, b"", http_port=target_port)
    assert resposta_direta.status_code == 200
    print("OK: fetch_device sem socks_proxy continua indo direto (retrocompativel)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Rodar e confirmar que falha**

Run: `python scripts/sightops_device_web_proxy_netns_test.py`
Expected: `TypeError: fetch_device() got an unexpected keyword argument 'socks_proxy'`

- [ ] **Step 4: Editar `fetch_device` em `app/services/device_web_proxy.py`**

Adicionar o parâmetro e usá-lo nas duas chamadas de `sessao.request(...)`
(linhas 141-145 e 163-168 do arquivo atual):

```python
def fetch_device(
    host: str,
    path: str,
    query: str,
    method: str,
    headers: Dict[str, str],
    body: bytes,
    username: str = "",
    password: str = "",
    *,
    http_port: int = 80,
    timeout: Tuple[float, float] = (4.0, 25.0),
    socks_proxy: Optional[Tuple[str, int]] = None,
) -> requests.Response:
    """...(docstring existente sem mudanca)...

    socks_proxy: quando informado (host, porta), toda requisicao passa
    OBRIGATORIAMENTE por esse proxy SOCKS5 (o namespace isolado do
    conector) -- nunca sai direto pro host. Ver
    docs/superpowers/specs/2026-09-08-isolamento-rede-por-conector-design.md.
    """
    auth = HTTPBasicAuth(username, password) if (username and password) else None
    ultimo_erro: Optional[Exception] = None
    resposta: Optional[requests.Response] = None
    scheme_usado = ""
    sessao = _session_for(host)
    proxies = None
    if socks_proxy is not None:
        proxy_url = f"socks5h://{socks_proxy[0]}:{socks_proxy[1]}"
        proxies = {"http": proxy_url, "https": proxy_url}

    try:
        schemes = _tentar_schemes(host)
        for scheme in schemes:
            url = build_target_url(scheme, host, path, query, http_port)
            eh_as_cegas = scheme == "https" and _scheme_cache.get(host) != "https"
            tentativa_timeout = (_PROBE_CONNECT_TIMEOUT, timeout[1]) if eh_as_cegas else timeout
            try:
                resposta = sessao.request(
                    method, url, headers=headers,
                    data=body if body else None,
                    timeout=tentativa_timeout, allow_redirects=False, verify=False, auth=auth,
                    proxies=proxies,
                )
                scheme_usado = scheme
                break
            except requests.exceptions.ConnectionError as exc:
                ultimo_erro = exc
                continue

        if resposta is None:
            raise DeviceUnreachable(f"{host} nao respondeu em https nem http: {ultimo_erro}")

        _scheme_cache[host] = scheme_usado

        if (
            resposta.status_code == 401
            and username and password
            and "digest" in (resposta.headers.get("WWW-Authenticate") or "").lower()
        ):
            url = build_target_url(scheme_usado, host, path, query, http_port)
            resposta = sessao.request(
                method, url, headers=headers,
                data=body if body else None,
                timeout=timeout, allow_redirects=False, verify=False,
                auth=HTTPDigestAuth(username, password),
                proxies=proxies,
            )
    except DeviceUnreachable:
        raise
    except requests.exceptions.RequestException as exc:
        raise DeviceUnreachable(f"{host} deu erro de rede: {exc}") from exc

    return resposta
```

Nota importante: `sessao` é uma `requests.Session` cacheada por `host` em
`_sessions` (linhas 47-66) — como o mesmo host físico nunca troca de
conector, isso continua seguro; mas se dois conectores diferentes algum
dia apontarem pro MESMO IP com proxies diferentes, a Session cacheada
ignoraria isso (o cache é só de conexão TCP, `proxies` é passado por
requisição, então não há vazamento de proxy entre chamadas — `requests`
aplica o `proxies` do `.request()` atual, não um da sessão).

- [ ] **Step 5: Rodar o teste e confirmar que passa**

Run: `python scripts/sightops_device_web_proxy_netns_test.py`
Expected: 2 linhas `OK: ...`

- [ ] **Step 6: Exigir `connector_id` na rota de proxy web e recusar sem isolamento**

Em `app/api/endpoints/maintenance.py`, alterar a assinatura da rota
(linha 1339-1341) para receber `connector_id` como query param
obrigatório, e `_camera_web_target_url`/a chamada a `fetch_device` para
resolver e exigir o proxy. Consultar antes como o frontend
(`frontend/js/*.js`, procurar por `/maintenance/web/`) hoje monta essa URL
— ela precisa passar a incluir `?connector_id=...` (o dado já está
disponível na tela, pois toda linha de câmera/DVR já carrega qual
conector/site pertence). Localizar a chamada com
`grep -rn "maintenance/web/" frontend/js/` antes de editar o backend, para
o parâmetro combinar com o nome que o frontend vai mandar.

```python
@router.api_route("/maintenance/web/{ip}/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/maintenance/web/{ip}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def maintenance_camera_web_proxy(ip: str, request: Request, path: str = "", connector_id: str = ""):
    """Proxy HTTP da interface web da camera/DVR/NVR via servidor/WireGuard."""
    connector_id = str(connector_id or "").strip()
    if not connector_id:
        raise HTTPException(status_code=400, detail="connector_id obrigatorio")
    socks_proxy = _connector_netns_proxy(connector_id)
    if socks_proxy is None:
        raise HTTPException(status_code=409, detail="este conector ainda nao tem isolamento de rede provisionado")

    _camera_web_target_url(ip, path, str(request.url.query or ""), connector_id=connector_id)
    ...
```

E adicionar, perto de `_device_http_port` (linha ~106), a função que lê o
proxy do conector:

```python
def _connector_netns_proxy(connector_id: str) -> tuple[str, int] | None:
    from app.services.connector_service import get_connector

    row = get_connector(connector_id, enforce_tenant=True) or {}
    tunnel = row.get("tunnel") if isinstance(row.get("tunnel"), dict) else {}
    host = tunnel.get("netns_proxy_host")
    port = tunnel.get("netns_proxy_port")
    if not host or not port:
        return None
    return (str(host), int(port))
```

E alterar `_camera_web_target_url` (linha 156-165) para receber
`connector_id` e confirmar, além do que já confirma hoje (IP pertence ao
tenant), que o IP pertence especificamente **àquele** conector — usar
`get_connector(connector_id).inventory` ou o helper equivalente já
existente para achar dispositivos por conector (o mesmo usado por
`_lookup_in_connector` em `dvr.py`, que o spec já confirmou ser
corretamente escopado):

```python
def _camera_web_target_url(ip: str, path: str = "", query: str = "", *, connector_id: str = "") -> str:
    host = _as_str(ip)
    if not _is_proxy_allowed_host(host):
        raise HTTPException(status_code=400, detail="proxy web permitido apenas para IP privado/CGNAT")
    if not _ip_belongs_to_connector(host, connector_id):
        raise HTTPException(status_code=403, detail=f"{host} nao pertence a nenhum equipamento deste conector")
    clean_path = "/" + str(path or "").lstrip("/")
    if ".." in clean_path.split("/"):
        raise HTTPException(status_code=400, detail="caminho invalido")
    return urlunsplit(("http", host, clean_path, str(query or ""), ""))
```

E a chamada de `fetch_device` (linhas 1401-1407) passa a incluir
`socks_proxy=socks_proxy`:

```python
        upstream = await loop.run_in_executor(
            _device_proxy_executor,
            lambda: fetch_device(
                ip, path, query, request.method, headers, body,
                username=username, password=password, http_port=_device_http_port(ip),
                socks_proxy=socks_proxy,
            ),
        )
```

A função `_ip_belongs_to_connector` (nova, substitui o uso de
`_ip_belongs_to_current_tenant` só nesta rota — as outras rotas que
usavam `_ip_belongs_to_current_tenant`, como comandos de hardware,
ficam de fora desta task e entram na varredura da Task 7) deve reusar a
mesma fonte de inventário por conector que `dvr.py::_lookup_in_connector`
já usa hoje (que o spec já confirmou corretamente escopada) — implementar
como um grep + leitura direta daquela função antes de escrever esta, para
não duplicar lógica com um comportamento sutilmente diferente.

- [ ] **Step 7: Exigir `connector_id` nos endpoints de rede de `dvr.py`**

Em `app/api/endpoints/dvr.py`, adicionar `connector_id: str` como
parâmetro obrigatório em `api_dvr_network_get` (linha 1218) e no
`DVRNetworkApplyRequest` (linha 1201-1214, novo campo
`connector_id: str`), e trocar as chamadas a `_get_text`/o POST cru para
passarem pelo `socks_proxy` resolvido do mesmo jeito que na Task 6 Step 6
(`_connector_netns_proxy`, movida para um módulo compartilhado se for
usada nos dois arquivos — considerar mover para
`app/services/connector_service.py` como função pública
`connector_netns_proxy(connector_id)` para não duplicar entre
`maintenance.py` e `dvr.py`):

```python
@router.get("/network")
def api_dvr_network_get(
    ip: str,
    connector_id: str,
    user: str = "admin",
    password: str = "",
    http_port: int = 80,
    timeout_sec: float = 8.0,
) -> Dict[str, Any]:
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip obrigatorio")
    socks_proxy = connector_netns_proxy(connector_id)
    if socks_proxy is None:
        raise HTTPException(status_code=409, detail="este conector ainda nao tem isolamento de rede provisionado")

    base = _base(ip, http_port)
    auth = HTTPDigestAuth(user, password)
    eth0_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network.eth0", auth, timeout_sec, socks_proxy=socks_proxy)
    net_txt = _get_text(f"{base}/cgi-bin/configManager.cgi?action=getConfig&name=Network", auth, timeout_sec, socks_proxy=socks_proxy)
    ...
```

`_get_text` (linha 342) precisa do mesmo parâmetro `socks_proxy` repassado
para a chamada HTTP que ela faz internamente — ler a implementação atual
de `_get_text` antes de editar (`grep -n "def _get_text" -A 15 app/api/endpoints/dvr.py`)
e aplicar `proxies={"http": ..., "https": ...}` do mesmo jeito que na
Task 6 Step 4. O mesmo vale para `api_dvr_network_apply` (linha 1261),
que também faz uma chamada HTTP direta (`params`/POST) que precisa do
`socks_proxy`.

- [ ] **Step 8: Compilar**

Run: `python -m compileall app`
Expected: sem erro.

- [ ] **Step 9: Checar sintaxe do JS alterado, se o frontend mudou nesta task**

Run: `node -e "new Function(require('fs').readFileSync('frontend/js/<arquivo tocado>.js','utf8'))"`
Expected: sem erro.

- [ ] **Step 10: Commit**

```bash
git add app/services/device_web_proxy.py app/api/endpoints/maintenance.py app/api/endpoints/dvr.py app/services/connector_service.py scripts/sightops_device_web_proxy_netns_test.py requirements.txt frontend/js
git commit -m "fix(netns): fetch_device e endpoints de rede do DVR exigem connector_id e roteiam pelo namespace isolado"
```

---

### Task 6b: Recusar IP ambíguo dentro do mesmo tenant em `_camera_row_for_ip`

Achado durante este plano (não estava na varredura original): mesmo
depois do isolamento de rede, `app/api/endpoints/cameras.py:138-146`
(`_camera_row_for_ip`) e `app/api/endpoints/maintenance.py:1258-1269`
(`_recorder_row_for_host`) resolvem credenciais/metadados de um IP
buscando em **todo o inventário do tenant** (todos os sites/conectores
juntos) e devolvendo a **primeira linha que bate o IP** — sem checar se
existe mais de uma. Isso significa que, mesmo roteando pela rede certa,
o sistema pode aplicar a senha/porta salva do site errado quando dois
sites do MESMO tenant têm IP igual (caso real: Mata Grande e Porto Real
do Colégio são os dois tenant `rads`). É a mesma classe de bug do vazamento
original, num nível diferente (dado de aplicação, não rota de rede).

**Files:**
- Modify: `app/api/endpoints/cameras.py:138-157`
- Create: `scripts/sightops_camera_row_ambiguous_ip_test.py`

**Interfaces:**
- Produces: `_camera_row_for_ip(ip: str, *, connector_id: str = "") -> dict | None` —
  quando `connector_id` não é informado E existe mais de uma linha com o
  mesmo IP no tenant atual, levanta `AmbiguousDeviceError` em vez de
  devolver silenciosamente a primeira.

- [ ] **Step 1: Escrever o teste (falha primeiro)**

```python
"""Confirma que _camera_row_for_ip recusa IP ambiguo (mesmo IP em mais de
um site/conector do mesmo tenant) em vez de devolver a primeira linha
silenciosamente -- reproduz em miniatura o padrao real do vazamento
(Mata Grande / Porto Real do Colegio, ambos tenant 'rads', ambos com
192.168.10.0/24)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_BACKEND"] = "sqlite"
        os.environ["SIGHTOPS_SECRET_KEY"] = "sightops-ambiguous-ip-test-key"

        from app.core.tenant_context import set_current_tenant_slug
        from app.services.inventory_json import save_inventory_json
        from app.api.endpoints.cameras import AmbiguousDeviceError, _camera_row_for_ip

        set_current_tenant_slug("rads")
        save_inventory_json([
            {"ip": "192.168.10.201", "site": "MATA GRANDE", "connector_id": "3315d77dfdedb2ee"},
            {"ip": "192.168.10.201", "site": "PORTO REAL DO COLEGIO", "connector_id": "cde8a557659fcdab"},
            {"ip": "10.10.10.5", "site": "MATA GRANDE", "connector_id": "3315d77dfdedb2ee"},
        ])

        try:
            _camera_row_for_ip("192.168.10.201")
            raise AssertionError("deveria ter levantado AmbiguousDeviceError")
        except AmbiguousDeviceError:
            print("OK: IP ambiguo sem connector_id levanta AmbiguousDeviceError")

        linha = _camera_row_for_ip("192.168.10.201", connector_id="3315d77dfdedb2ee")
        assert linha["site"] == "MATA GRANDE"
        print("OK: com connector_id, desambigua e devolve a linha certa")

        linha_unica = _camera_row_for_ip("10.10.10.5")
        assert linha_unica["site"] == "MATA GRANDE"
        print("OK: IP nao-ambiguo continua funcionando sem connector_id")


if __name__ == "__main__":
    main()
```

Ajustar o nome real da função de gravação do inventário
(`save_inventory_json` é um palpite) conferindo antes com
`grep -n "^def save_inventory_json\|^def.*inventory" app/services/inventory_json.py`
— usar a função real de escrita que o arquivo expõe.

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `python scripts/sightops_camera_row_ambiguous_ip_test.py`
Expected: `ImportError: cannot import name 'AmbiguousDeviceError'`

- [ ] **Step 3: Editar `app/api/endpoints/cameras.py:138-157`**

```python
class AmbiguousDeviceError(Exception):
    """Mesmo IP aparece em mais de um site/conector do tenant atual --
    resolver sem indicar qual gera risco de aplicar credencial/config do
    site errado (mesma classe do vazamento cross-tenant, agora dentro de
    um unico tenant com varios conectores)."""


def _camera_row_for_ip(ip: str, *, connector_id: str = "") -> dict | None:
    try:
        inv = load_inventory_json() or []
    except Exception:
        inv = []
    candidatos = [r for r in inv if isinstance(r, dict) and str(r.get("ip") or "").strip() == ip]
    if connector_id:
        candidatos = [r for r in candidatos if str(r.get("connector_id") or "") == connector_id]
        return candidatos[0] if candidatos else None
    if len(candidatos) > 1:
        sites = sorted({str(r.get("site") or r.get("connector_id") or "?") for r in candidatos})
        raise AmbiguousDeviceError(f"{ip} aparece em mais de um site deste tenant ({', '.join(sites)}); informe connector_id")
    return candidatos[0] if candidatos else None


def _ip_in_inventory(ip: str, *, connector_id: str = "") -> bool:
    try:
        return _camera_row_for_ip(ip, connector_id=connector_id) is not None
    except AmbiguousDeviceError:
        return True
```

Nota: `_ip_in_inventory` continua devolvendo `True` no caso ambíguo (o IP
*pertence* ao tenant, só não pode ser resolvido sem `connector_id`) — ela
é usada hoje só como um "posso deixar passar" antes de comando de
hardware; qualquer chamador que precisar da linha de verdade tem que
tratar `AmbiguousDeviceError` explicitamente, nunca ignorá-la.

- [ ] **Step 4: Rodar o teste e confirmar que passa**

Run: `python scripts/sightops_camera_row_ambiguous_ip_test.py`
Expected: 3 linhas `OK: ...`

- [ ] **Step 5: Checar quem chama `_camera_row_for_ip`/`_ip_in_inventory` sem `connector_id` e ajustar**

```bash
grep -rn "_camera_row_for_ip\|_ip_in_inventory" app/api/endpoints/*.py
```
Para cada chamador que já tem `connector_id` disponível no escopo (rotas
que recebem o parâmetro), passar adiante. Para os que não têm ainda
(rotas antigas sem esse conceito), registrar em
`docs/HANDOFF_AGENTES.md` (Task 9, Step final) em vez de reformular a
rota inteira aqui — mantém esta task com escopo fechado (a correção do
ponto de dado em si), deixando o replumbing de UI/rota mais amplo como
item explícito de acompanhamento.

- [ ] **Step 6: Compilar**

Run: `python -m compileall app && python -B -c "import app.main; print('app.main OK')"`

- [ ] **Step 7: Commit**

```bash
git add app/api/endpoints/cameras.py scripts/sightops_camera_row_ambiguous_ip_test.py
git commit -m "fix(inventario): recusa IP ambiguo entre conectores do mesmo tenant em vez de escolher o primeiro"
```

---

### Task 7: Varredura dirigida por outros pontos de acesso direto por IP

**Files:**
- Nenhuma modificação de código obrigatória nesta task — o resultado é uma
  lista, registrada em `docs/HANDOFF_AGENTES.md` (seção nova, datada).

**Interfaces:** nenhuma (task de investigação).

- [ ] **Step 1: Grep dirigido pelos padrões já confirmados como arriscados**

```bash
grep -rn "requests\.\(get\|post\|put\|delete\|request\)(" app/api/endpoints app/services app/cli | grep -v "socks_proxy\|_test.py"
grep -rn "f\"http://{.*}\"" app/api/endpoints app/services app/cli
grep -rn "def.*_row_for_ip\|def.*_row_for_host\|def.*_by_ip\b" app/api/endpoints app/services
```

- [ ] **Step 2: Para cada resultado, classificar**

Perguntar de cada um: "esse dispositivo é alcançado via IP sozinho, sem
nunca considerar qual conector/túnel deveria ser usado?" — anotar
arquivo:linha e uma frase do porquê é ou não é risco (ex.: chama
`fetch_device`/`_get_text` já corrigidos nas Tasks 6/6b → OK; é uma
chamada nova e direta ainda sem `connector_id` → risco real).

- [ ] **Step 3: Registrar o resultado em `docs/HANDOFF_AGENTES.md`**

Nova seção datada (`## 2026-09-08 — Varredura de acesso direto por IP
(isolamento de rede por conector)`), listando: os pontos já corrigidos
neste plano (Tasks 5, 6, 6b), qualquer ponto novo encontrado com risco
real (arquivo:linha, o que falta), e uma recomendação explícita de abrir
um plano seguinte se a lista não for trivial — não implementar
correções adicionais improvisadas dentro desta task (mantém o escopo do
plano fechado, conforme a Task 6b já fez para o replumbing de rota).

- [ ] **Step 4: Commit**

```bash
git add docs/HANDOFF_AGENTES.md
git commit -m "docs: registra varredura de acesso direto por IP fora do isolamento de rede"
```

---

### Task 8: Deploy nos dois ambientes de produção

**Não automatizável** — segue o processo real de deploy (skill
`sightops-bug-producao` / memória `sightops-deploy-producao-real`), não o
`docker-compose` do repo direto, porque produção não roda o `main` do git.

**Files:**
- Modify: `docker-compose.production.yml` (adicionar
  `- /run/sightops:/run/sightops` aos `volumes` do serviço
  `cam-snapshot-api`, para o container enxergar o socket do daemon do
  host) — e o mesmo para o compose usado pelo ambiente `v3`, se for um
  arquivo separado (`grep -rn "sightops-v3-api" docker-compose*.yml` para
  localizar).

- [ ] **Step 1: Confirmar qual arquivo de compose real está em uso nos dois ambientes, no servidor**

```bash
ssh central@10.10.12.7 "docker inspect sightops-prod-api --format '{{json .Mounts}}' | python3 -m json.tool"
ssh central@10.10.12.7 "docker inspect sightops-v3-api --format '{{json .Mounts}}' | python3 -m json.tool"
```
Confirmar se já existe (não deveria) um mount de `/run/sightops` — se
não existir, os dois containers precisam ser recriados (não só o processo
reiniciado) para o volume novo pegar.

- [ ] **Step 2: Extrair os arquivos alterados do container real e aplicar o diff**

Seguir literalmente o processo de `sightops-bug-producao`: `docker cp` os
arquivos atuais de dentro de `sightops-prod-api` (`connector_service.py`,
`device_web_proxy.py`, `maintenance.py`, `dvr.py`, `cameras.py`,
`netns_provisioner_client.py` é novo — esse só precisa ser copiado por
inteiro), comparar contra a versão base que o patch deste plano parte
(pode não ser idêntica ao `main`, checar diffs primeiro), aplicar só as
mudanças reais.

- [ ] **Step 3: Construir imagem candidata e validar `import app.main` antes de trocar**

```bash
docker build -t sightops-prod-api:$(date +%Y%m%d)-isolamento-rede .
docker run --rm -e DATABASE_BACKEND=sqlite sightops-prod-api:$(date +%Y%m%d)-isolamento-rede python -c "import app.main; print('OK')"
```

- [ ] **Step 4: Adicionar o mount do socket e recriar os dois containers**

Editar (no servidor, no arquivo de compose real em uso — comparar contra
`docker-compose.production.yml` deste repo antes de assumir que são
idênticos) o serviço da API para incluir
`- /run/sightops:/run/sightops` em `volumes`. Depois:

```bash
docker compose -f <arquivo-em-uso> up -d --no-deps cam-snapshot-api
```
(comando isolado, sem misturar com outros no mesmo bloco — evita o
classificador de segurança do Claude Code travar o passo, como já
observado nesta sessão). Repetir para o ambiente `v3`.

- [ ] **Step 5: Provar com número real de antes/depois**

```bash
# antes de reconfigurar o docker-compose com o mount, esta chamada tinha que estar
# retornando 409 (isolamento nao provisionado ainda dentro do container)
curl -s "https://sightops.easytecnologias.com.br/api/dvr/network?ip=192.168.10.201&connector_id=3315d77dfdedb2ee&user=admin&password=..." | head -c 300
```
Confirmar 200 com o dado do Mata Grande, e a mesma chamada com
`connector_id=cde8a557659fcdab` (Porto Real) devolvendo o dado do Porto
Real — nunca o mesmo dado nos dois.

- [ ] **Step 6: Checar logs por erro**

```bash
docker logs --since 5m sightops-prod-api | grep -iE "traceback|exception"
docker logs --since 5m sightops-v3-api | grep -iE "traceback|exception"
```
Expected: nada relacionado às mudanças deste plano.

Nenhum commit de código nesta task (só o `docker-compose.production.yml`
do repo, se for editado — commitar separadamente):

```bash
git add docker-compose.production.yml
git commit -m "chore(netns): monta socket do provisionador de rede no container da API"
```

---

### Task 9: Migração faseada dos conectores reais

**Não automatizável** — produção real, executar via SSH, um conector por
vez, com validação entre cada um.

- [ ] **Step 1: Mata Grande + Porto Real do Colégio, mantendo os dois caminhos vivos**

Mata Grande já foi provisionado via daemon na Task 4. Repetir o mesmo
processo (Task 4, Step 3-4) para Porto Real do Colégio
(`connector_id=cde8a557659fcdab`, `peer_public_key=X5x4V1ZluBJoc24korNFCDB8gzbiL58oRy/Nq7rnE2M=`,
`allowed_ips=["10.250.0.9/32", "10.45.0.0/24", "192.168.10.0/24"]`).

- [ ] **Step 2: Validar os dois isolados, lado a lado**

```bash
curl --socks5-hostname 169.254.100.2:8080 http://192.168.10.201/    # Mata Grande -- so deve achar algo se existir mesmo la
curl --socks5-hostname <proxy_do_porto_real>:8080 http://192.168.10.201/   # Porto Real -- deve achar o dispositivo real dele
```
Confirmar que os dois dão resultados **diferentes e corretos** (cada um
reflete a rede real daquele cliente).

- [ ] **Step 3: Remover os dois peers da interface compartilhada `wg-sightops`**

```bash
wg set wg-sightops peer 1iDptEuSPwlUHOBVoB5h2lnWFW+QBkB4OOPAbUUrJUI= remove
wg set wg-sightops peer X5x4V1ZluBJoc24korNFCDB8gzbiL58oRy/Nq7rnE2M= remove
wg show wg-sightops | grep -A2 "peer:"   # confirmar que so restam os outros 7
```

- [ ] **Step 4: Validar de novo, agora sem o caminho antigo**

Repetir os mesmos `curl` do Step 2 — resultado tem que ser idêntico (a
aplicação já usa só o caminho isolado desde a Task 8).

- [ ] **Step 5: Migrar os 7 conectores restantes, um de cada vez**

Para cada um (usar `ensure_wireguard_tunnel` já provisiona
automaticamente desde a Task 5 — não precisa mais do passo manual da
Task 4; basta que o conector exista e a rota que chama
`ensure_wireguard_tunnel` seja executada de novo, ex. reenviar o
formulário de túnel na tela, ou chamar a rota correspondente):
1. Confirmar (`status` no daemon) que o namespace foi criado.
2. `curl` via o proxy isolado desse conector contra um IP conhecido dele.
3. Remover o peer da interface compartilhada (`wg set wg-sightops peer ... remove`).
4. Confirmar de novo via `curl`.

- [ ] **Step 6: Confirmar a interface compartilhada vazia**

```bash
wg show wg-sightops | grep -c "peer:"
```
Expected: `0`.

- [ ] **Step 7: Atualizar `docs/HANDOFF_AGENTES.md` com o estado final**

Registrar: todos os 9 conectores migrados, interface `wg-sightops`
compartilhada vazia (mantida como watchdog — qualquer peer que reaparecer
nela é sinal de regressão), e o achado da Task 6b/7 (varredura) como
próximo passo se ainda houver pendência.

```bash
git add docs/HANDOFF_AGENTES.md
git commit -m "docs: registra migracao completa dos 9 conectores para isolamento de rede por namespace"
```

---

## Self-review (cobertura do spec)

- Daemon privilegiado via socket Unix → Task 2, 3.
- Protocolo `provision`/`deprovision`/`status`, idempotência,
  reconfiguração sem recriar → Task 2 (testado).
- Alocação determinística de porta/sub-rede → Task 2 (`allocator.py`,
  testado).
- Persistência systemd (daemon + proxy por conector) → Task 3.
- Integração automática em `ensure_wireguard_tunnel` (sem gambiarra pra
  conector novo) → Task 5.
- `fetch_device`/`dvr.py` roteando por `connector_id`, sem fallback
  silencioso → Task 6.
- Achado extra (mesmo tenant, IPs colidentes entre sites) → Task 6b, não
  estava no spec original mas é a mesma classe de risco — incluído.
- Varredura de outros pontos → Task 7.
- Migração faseada (Fases 2-5 do spec) → Task 9.
- Resposta pra troca de servidor/VPS → já é conteúdo do spec (seção
  dedicada), não gera task de código — é operacional/documentação, já
  coberta pela natureza portátil do desenho (chave do servidor + `connectors.json`
  + reinstalação do daemon), sem ação de código pendente.
- Auditoria/log → coberto de forma leve (log de aplicação existente +
  nota no spec); não abriu task própria porque não há mecanismo de log
  novo a construir além do que `provision`/`deprovision` já expõe via
  resposta — se o usuário quiser um log append-only dedicado do daemon
  além disso, é um adendo pequeno à Task 2 (`daemon.py`) a pedido.
