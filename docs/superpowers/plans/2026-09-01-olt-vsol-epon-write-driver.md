# Driver OLT VSOL EPON — Autorizar/Excluir/Reiniciar ONU — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer a OLT VSOL EPON (cliente RADS, conector Japaratinga) autorizar,
excluir e reiniciar ONU de verdade — hoje só descobre/consulta/coleta MACs.
Inclui corrigir um bug real já presente no driver: o código de exclusão que já
existe (`build_delete_onu_vsol_command`) monta o comando errado.

**Architecture:** Estende o driver já existente
(`app/cli/tools/olt_vsol_epon.py`) com três funções novas, seguindo o mesmo
padrão de sessão SSH interativa já usado no arquivo (`_com_sessao_vsol`,
`_entra_na_pon`, `_manda`, `parse_onu_auth_info`). Encaixa no `olt_service.py`
compartilhado com `elif _is_vsol(req):`, mesmo padrão já usado pra 4840E.
Frontend ganha um terceiro conjunto de campos por painel (`...FieldsVsol`),
generalizando o mecanismo de dois-drivers (GPON/EPON) já construído nesta
sessão pra três (GPON/EPON/VSOL).

**Tech Stack:** Python (backend, sem framework de teste — scripts standalone
`scripts/sightops_*_test.py`), JavaScript vanilla (frontend, sem framework).

## Global Constraints

- **Nunca sobrescrever o modo de autenticação da PON.** Se `onu mac-auth add`
  vier com erro, devolver erro claro — nunca tentar `onu auth-mode` sozinho.
- **Excluir usa `no onu auth onuid <onuid>`, nunca `deregister`.** O comando
  `deregister` só desconecta (a ONU volta sozinha); confirmado no manual
  oficial "UPLINK EP Series OLT CLI User Manual v1.2", seções 17.1.2 e
  17.1.3.
- **Reiniciar não pede confirmação y/n nesta OLT** (diferente da 4840E) —
  `onu <onuid> ctc reset` roda direto. Confirmar isso ao vivo antes de ir
  pra produção (não é suposição segura só pelo manual).
- **`_rotulo_da_pon`/`_entra_na_pon`/`_manda`/`_espera_prompt`/`_volta_ao_topo`
  não são tocadas** — funções de sessão já validadas ao vivo, usar como
  estão.
- **Sem posicionamento manual de ONU** — a OLT atribui o `onu_id` sozinha ao
  autorizar; não expor escolha manual de posição na tela.
- **Sem framework de teste** (nem pytest nem unittest) — scripts standalone
  com `check()`/lista de falhas, monkeypatch direto nas funções do módulo
  (ver `scripts/sightops_olt_vsol_cpe_mac_test.py` como referência exata de
  estilo pra ESTE arquivo específico).
- **Deploy em produção não é task deste plano** — fica pra quando o usuário
  pedir, depois da validação ao vivo (ver nota final).

---

### Task 1: `add_onu_vsol` — autorizar ONU por MAC

**Files:**
- Modify: `app/cli/tools/olt_vsol_epon.py`
- Test: `scripts/sightops_olt_vsol_add_onu_test.py` (novo arquivo)

**Interfaces:**
- Consumes: `_com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)`,
  `_entra_na_pon(chan, pon, timeout)`, `_manda(chan, texto, alvos, timeout)`,
  `parse_onu_auth_info(saida) -> List[Dict[str, Any]]` (cada item tem
  `onu_id`, `pon`, `onu_mac`, `oper_status`), `_norm_mac(v) -> str`,
  `_rotulo_da_pon(valor) -> str` — todas já existem no arquivo, nenhuma
  muda de assinatura.
- Produces: `_comando_falhou(saida: str) -> bool` (nova função auxiliar,
  extraída do critério de erro já usado em `_entra_na_pon`) e
  `add_onu_vsol(olt_ip: str, user: str, password: str, pon: str, mac: str, port: int = 22, timeout: float = 15.0) -> Dict[str, Any]`
  — devolve `{"ok": bool, "pon": str, "onu_id": str, "onu_mac": str, "pending": bool}`
  no sucesso (ou `{"ok": False, "error": str}` na falha). `onu_id` fica `""`
  e `pending` fica `True` se a OLT ainda não tiver processado o registro
  MPCP na hora da leitura — usado pela Task 4 (`olt_service.py`) e pelo
  frontend (Task 5).

- [ ] **Step 1: Escrever o teste que falha**

Criar `scripts/sightops_olt_vsol_add_onu_test.py`:

```python
"""Testa autorizar ONU por MAC no driver VSOL EPON (whitelist add).

Sem OLT: a sessao e simulada por um canal falso que responde comando a
comando, mesmo padrao de scripts/sightops_olt_vsol_cpe_mac_test.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.cli.tools.olt_vsol_epon as vsol

AUTH_INFO_COM_NOVA_ONU = """show onu auth-info
ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description
EPON0/1:1   2      online    98:2a:0a:a0:26:19   446     N/A
EPON0/1:9   3      online    aa:bb:cc:dd:ee:ff   500     N/A
epon-olt(config-pon-0/1)#"""

AUTH_INFO_SEM_A_NOVA_ONU = """show onu auth-info
ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description
EPON0/1:1   2      online    98:2a:0a:a0:26:19   446     N/A
epon-olt(config-pon-0/1)#"""


class CanalFalso:
    """Responde a cada comando com a saida gravada, como a OLT responderia."""

    def __init__(self, auth_info: str, mac_auth_falha: bool = False) -> None:
        self.auth_info = auth_info
        self.mac_auth_falha = mac_auth_falha
        self.comandos: list[str] = []

    def resposta(self, cmd: str) -> str:
        self.comandos.append(cmd)
        if cmd.startswith("interface epon 0/1"):
            return "epon-olt(config-pon-0/1)#"
        if cmd.startswith("interface epon"):
            return "epon-olt(config)#"          # PON inexistente: contexto nao muda
        if cmd == "configure terminal":
            return "epon-olt(config)#"
        if cmd.startswith("onu mac-auth add"):
            if self.mac_auth_falha:
                return "% invalid parameter\nepon-olt(config-pon-0/1)#"
            return "epon-olt(config-pon-0/1)#"
        if cmd == "show onu auth-info":
            return self.auth_info
        return "epon-olt#"


def _instala_sessao_falsa(canal: CanalFalso) -> None:
    vsol._manda = lambda chan, texto, alvos, timeout=20.0: chan.resposta(texto)
    vsol._espera_prompt = lambda chan, alvos, timeout=20.0: "epon-olt#"
    vsol._volta_ao_topo = lambda chan, timeout=20.0: None
    vsol._com_sessao_vsol = lambda ip, u, p, port, timeout, tarefa: tarefa(canal)
    vsol.time.sleep = lambda *_: None  # nao esperar de verdade nos testes


def falhas() -> list[str]:
    erros: list[str] = []

    # 1) autorizacao feliz: a ONU ja aparece em auth-info na primeira leitura
    canal = CanalFalso(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.add_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", mac="aa:bb:cc:dd:ee:ff")
    if not r.get("ok"):
        erros.append(f"autorizacao feliz: ok=False, esperava True ({r})")
    if r.get("onu_id") != "9":
        erros.append(f"autorizacao feliz: onu_id={r.get('onu_id')!r}, esperava '9'")
    if r.get("pending"):
        erros.append("autorizacao feliz: pending=True, esperava False (ja apareceu)")
    if not any(c.startswith("onu mac-auth add aa:bb:cc:dd:ee:ff") for c in canal.comandos):
        erros.append(f"autorizacao feliz: nao mandou 'onu mac-auth add', comandos={canal.comandos}")

    # 2) OLT recusa o MAC (erro de sintaxe/parametro)
    canal = CanalFalso(AUTH_INFO_COM_NOVA_ONU, mac_auth_falha=True)
    _instala_sessao_falsa(canal)
    r = vsol.add_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", mac="aa:bb:cc:dd:ee:ff")
    if r.get("ok"):
        erros.append("OLT recusou o mac-auth add, mas a funcao devolveu ok=True")

    # 3) autorizada, mas ainda nao apareceu em auth-info depois das tentativas
    #    -- nao pode travar nem falhar, so avisar que esta pendente
    canal = CanalFalso(AUTH_INFO_SEM_A_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.add_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", mac="aa:bb:cc:dd:ee:ff")
    if not r.get("ok"):
        erros.append(f"registro pendente: ok=False, esperava True ({r})")
    if not r.get("pending"):
        erros.append("registro pendente: pending=False, esperava True (nunca apareceu)")
    if r.get("onu_id") not in ("", None):
        erros.append(f"registro pendente: onu_id={r.get('onu_id')!r}, esperava vazio")
    tentativas_auth_info = sum(1 for c in canal.comandos if c == "show onu auth-info")
    if tentativas_auth_info != 3:
        erros.append(f"registro pendente: esperava 3 tentativas de 'show onu auth-info', veio {tentativas_auth_info}")

    # 4) MAC normalizado (maiusculo/hifen) antes de mandar pra OLT e antes de comparar
    canal = CanalFalso(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.add_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", mac="AA-BB-CC-DD-EE-FF")
    if not r.get("ok") or r.get("onu_id") != "9":
        erros.append(f"MAC nao normalizado corretamente: {r}")

    return erros


def main() -> int:
    erros = falhas()
    for e in erros:
        print("FALHOU:", e)
    if not erros:
        print("OK: sightops_olt_vsol_add_onu_test")
    return 1 if erros else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: `AttributeError: module 'app.cli.tools.olt_vsol_epon' has no attribute 'add_onu_vsol'` (ou similar — a função ainda não existe).

- [ ] **Step 3: Implementar `_comando_falhou` e `add_onu_vsol`**

Em `app/cli/tools/olt_vsol_epon.py`, adicionar logo depois de
`build_delete_onu_vsol_command` (antes da seção `# ------------------- sessao`,
perto de `_prompt_da_pon`) — ou em qualquer ponto do arquivo após
`parse_onu_auth_info` e `_norm_mac` já estarem definidos, já que a nova
função depende dos dois:

```python
def _comando_falhou(saida: str) -> bool:
    """Mesmo criterio de erro ja usado em _entra_na_pon, como funcao
    reutilizavel para os comandos novos de autorizar/excluir/reiniciar."""
    baixo = (saida or "").lower()
    return "unknown command" in baixo or "% invalid" in baixo


def add_onu_vsol(
    olt_ip: str, user: str, password: str, pon: str, mac: str,
    port: int = 22, timeout: float = 15.0,
) -> Dict[str, Any]:
    """Autoriza uma ONU pelo MAC (whitelist). A OLT atribui o onu-id sozinha
    -- tenta ler de volta ('show onu auth-info') ate 3 vezes, 2s entre
    tentativas, antes de desistir de informar a posicao. A autorizacao ja
    aconteceu de qualquer jeito nesse caso; so a posicao fica pendente."""
    alvo = _rotulo_da_pon(pon)
    mac_norm = _norm_mac(mac)

    def tarefa(chan):
        _entra_na_pon(chan, alvo, timeout=timeout)
        saida = _manda(chan, f"onu mac-auth add {mac_norm}", ["config-pon"], timeout=timeout)
        if _comando_falhou(saida):
            return {"ok": False, "error": f"a OLT recusou autorizar o MAC {mac_norm}: {saida.strip()[:300]}"}

        onu_id = ""
        for _ in range(3):
            time.sleep(2)
            auth = _manda(chan, "show onu auth-info", ["config-pon"], timeout=max(20.0, timeout))
            achado = next(
                (l for l in parse_onu_auth_info(auth) if _norm_mac(l.get("onu_mac")) == mac_norm),
                None,
            )
            if achado:
                onu_id = achado.get("onu_id", "")
                break

        return {
            "ok": True,
            "pon": alvo,
            "onu_id": onu_id,
            "onu_mac": mac_norm,
            "pending": not onu_id,
        }

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: `OK: sightops_olt_vsol_add_onu_test`

- [ ] **Step 5: Compilar o módulo**

Run: `python -m py_compile app/cli/tools/olt_vsol_epon.py`
Expected: sem erro.

- [ ] **Step 6: Commit**

```bash
git add app/cli/tools/olt_vsol_epon.py scripts/sightops_olt_vsol_add_onu_test.py
git commit -m "feat(olt-vsol): autorizar ONU por MAC (add_onu_vsol)"
```

---

### Task 2: corrigir exclusão — `no onu auth onuid`, não `deregister`

**Files:**
- Modify: `app/cli/tools/olt_vsol_epon.py`
- Test: `scripts/sightops_olt_vsol_add_onu_test.py` (mesmo arquivo da Task 1,
  acrescentar funções de teste)

**Interfaces:**
- Consumes: `_com_sessao_vsol`, `_entra_na_pon`, `_manda`, `_rotulo_da_pon`,
  `_comando_falhou` (da Task 1).
- Produces: `build_delete_onu_vsol_command(pon: str, onu_id: Any) -> List[str]`
  (assinatura **simplificada** — a antiga aceitava `onu_id="" `/`mac=""`
  opcionais porque montava dois comandos possíveis; o comando correto do
  manual só aceita `onu_id`, então `mac` sai da assinatura) e
  `delete_onu_vsol(olt_ip: str, user: str, password: str, pon: str, onu_id: Any, port: int = 22, timeout: float = 15.0) -> Dict[str, Any]`
  — devolve `{"ok": bool, "pon": str, "onu_id": str}` no sucesso ou
  `{"ok": False, "error": str}` na falha.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar ao final de `scripts/sightops_olt_vsol_add_onu_test.py`, antes
da função `falhas()` existente NÃO — acrescentar DENTRO da função `falhas()`
já criada na Task 1, antes do `return erros`:

```python
    # 5) build_delete_onu_vsol_command monta o comando CERTO (nao "deregister",
    #    que so desconecta e a ONU volta sozinha -- confirmado no manual)
    comandos = vsol.build_delete_onu_vsol_command("0/1", 9)
    if "no onu auth onuid 9" not in comandos:
        erros.append(f"delete: comando certo nao apareceu, veio {comandos}")
    if any("deregister" in c for c in comandos):
        erros.append(f"delete: ainda monta 'deregister' (so desconecta, nao remove): {comandos}")

    # 6) sem onu_id, tem que falhar explicito (nesta OLT nao da pra excluir so por MAC)
    try:
        vsol.build_delete_onu_vsol_command("0/1", "")
        erros.append("delete: aceitou onu_id vazio sem levantar erro")
    except ValueError:
        pass

    # 7) delete_onu_vsol feliz
    canal = CanalFalso(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.delete_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=9)
    if not r.get("ok"):
        erros.append(f"delete_onu_vsol: ok=False, esperava True ({r})")
    if not any(c == "no onu auth onuid 9" for c in canal.comandos):
        erros.append(f"delete_onu_vsol: nao mandou 'no onu auth onuid 9', comandos={canal.comandos}")

    # 8) delete_onu_vsol com erro da OLT
    class CanalFalsoComErro(CanalFalso):
        def resposta(self, cmd: str) -> str:
            self.comandos.append(cmd)
            if cmd == "no onu auth onuid 9":
                return "% invalid parameter\nepon-olt(config-pon-0/1)#"
            return super().resposta(cmd)

    canal = CanalFalsoComErro(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.delete_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=9)
    if r.get("ok"):
        erros.append("delete_onu_vsol: OLT recusou o comando, mas devolveu ok=True")
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: falha nos itens 5-8 (comando errado ainda presente / `delete_onu_vsol`
não existe).

- [ ] **Step 3: Corrigir `build_delete_onu_vsol_command` e implementar `delete_onu_vsol`**

Em `app/cli/tools/olt_vsol_epon.py`, **substituir** a função
`build_delete_onu_vsol_command` inteira (hoje monta `deregister onu auth
onuid`/`deregister onu unauth`) por:

```python
def build_delete_onu_vsol_command(pon: str, onu_id: Any) -> List[str]:
    """Comandos para remover a autorizacao de uma ONU.

    'no onu auth onuid' revoga a autorizacao (a ONU nao volta sozinha).
    'deregister' (usado numa versao anterior deste driver) so desconecta --
    confirmado no manual oficial desta OLT, secoes 17.1.2 e 17.1.3. Devolve
    a sequencia em vez de executar: quem chama decide se roda, e o comando
    fica visivel no log antes de tocar em cliente ativo."""
    pon = str(pon or "").strip()
    if not pon:
        raise ValueError("pon e obrigatorio para excluir ONU")
    if onu_id in ("", None):
        raise ValueError("onu_id e obrigatorio para excluir ONU nesta OLT")
    return ["end", "configure terminal", f"interface epon {pon}", f"no onu auth onuid {int(onu_id)}"]


def delete_onu_vsol(
    olt_ip: str, user: str, password: str, pon: str, onu_id: Any,
    port: int = 22, timeout: float = 15.0,
) -> Dict[str, Any]:
    """Remove a autorizacao de uma ONU (nao apenas desconecta -- ver
    build_delete_onu_vsol_command)."""
    alvo = _rotulo_da_pon(pon)
    if onu_id in ("", None):
        return {"ok": False, "error": "onu_id e obrigatorio para excluir ONU nesta OLT"}

    def tarefa(chan):
        _entra_na_pon(chan, alvo, timeout=timeout)
        saida = _manda(chan, f"no onu auth onuid {int(onu_id)}", ["config-pon"], timeout=timeout)
        if _comando_falhou(saida):
            return {"ok": False, "error": f"a OLT recusou excluir a ONU {onu_id}: {saida.strip()[:300]}"}
        return {"ok": True, "pon": alvo, "onu_id": str(onu_id)}

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)
```

Note: `delete_onu_vsol` não usa `build_delete_onu_vsol_command` internamente
(a função nova manda o comando final direto pela mesma sessão, igual as
outras funções do arquivo) — `build_delete_onu_vsol_command` fica disponível
como utilitário standalone (útil pra logar o comando antes de rodar, se
algum chamador futuro quiser), mas não é dependência de `delete_onu_vsol`.
Os testes da Task 2 cobrem as duas funções separadamente por esse motivo.

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: `OK: sightops_olt_vsol_add_onu_test`

- [ ] **Step 5: Compilar o módulo**

Run: `python -m py_compile app/cli/tools/olt_vsol_epon.py`
Expected: sem erro.

- [ ] **Step 6: Commit**

```bash
git add app/cli/tools/olt_vsol_epon.py scripts/sightops_olt_vsol_add_onu_test.py
git commit -m "fix(olt-vsol): excluir ONU usa 'no onu auth onuid', nao 'deregister'"
```

---

### Task 3: `reboot_onu_vsol` — reiniciar ONU

**Files:**
- Modify: `app/cli/tools/olt_vsol_epon.py`
- Test: `scripts/sightops_olt_vsol_add_onu_test.py` (mesmo arquivo)

**Interfaces:**
- Consumes: `_com_sessao_vsol`, `_entra_na_pon`, `_manda`, `_rotulo_da_pon`,
  `_comando_falhou`.
- Produces: `reboot_onu_vsol(olt_ip: str, user: str, password: str, pon: str, onu_id: Any, port: int = 22, timeout: float = 15.0) -> Dict[str, Any]`
  — devolve `{"ok": bool, "pon": str, "onu_id": str}` no sucesso ou
  `{"ok": False, "error": str}` na falha.

- [ ] **Step 1: Escrever os testes que falham**

Acrescentar dentro de `falhas()`, antes do `return erros`:

```python
    # 9) reboot_onu_vsol feliz
    canal = CanalFalso(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.reboot_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=9)
    if not r.get("ok"):
        erros.append(f"reboot_onu_vsol: ok=False, esperava True ({r})")
    if not any(c == "onu 9 ctc reset" for c in canal.comandos):
        erros.append(f"reboot_onu_vsol: nao mandou 'onu 9 ctc reset', comandos={canal.comandos}")

    # 10) reboot_onu_vsol com erro da OLT
    class CanalFalsoRebootErro(CanalFalso):
        def resposta(self, cmd: str) -> str:
            self.comandos.append(cmd)
            if cmd == "onu 9 ctc reset":
                return "% invalid parameter\nepon-olt(config-pon-0/1)#"
            return super().resposta(cmd)

    canal = CanalFalsoRebootErro(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.reboot_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=9)
    if r.get("ok"):
        erros.append("reboot_onu_vsol: OLT recusou o comando, mas devolveu ok=True")
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: `AttributeError: ... has no attribute 'reboot_onu_vsol'`.

- [ ] **Step 3: Implementar `reboot_onu_vsol`**

Em `app/cli/tools/olt_vsol_epon.py`, adicionar após `delete_onu_vsol`:

```python
def reboot_onu_vsol(
    olt_ip: str, user: str, password: str, pon: str, onu_id: Any,
    port: int = 22, timeout: float = 15.0,
) -> Dict[str, Any]:
    """Reinicia uma ONU ja autorizada. Diferente da 4840E, esta OLT nao pede
    confirmacao y/n pra este comando (a confirmar ao vivo antes de producao)."""
    alvo = _rotulo_da_pon(pon)
    if onu_id in ("", None):
        return {"ok": False, "error": "onu_id e obrigatorio para reiniciar ONU nesta OLT"}

    def tarefa(chan):
        _entra_na_pon(chan, alvo, timeout=timeout)
        saida = _manda(chan, f"onu {int(onu_id)} ctc reset", ["config-pon"], timeout=timeout)
        if _comando_falhou(saida):
            return {"ok": False, "error": f"a OLT recusou reiniciar a ONU {onu_id}: {saida.strip()[:300]}"}
        return {"ok": True, "pon": alvo, "onu_id": str(onu_id)}

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: `OK: sightops_olt_vsol_add_onu_test`

- [ ] **Step 5: Compilar o módulo**

Run: `python -m py_compile app/cli/tools/olt_vsol_epon.py`
Expected: sem erro.

- [ ] **Step 6: Commit**

```bash
git add app/cli/tools/olt_vsol_epon.py scripts/sightops_olt_vsol_add_onu_test.py
git commit -m "feat(olt-vsol): reiniciar ONU (reboot_onu_vsol)"
```

---

### Task 4: encaixar no `olt_service.py` e `olt_capabilities.py`

**Files:**
- Modify: `app/services/olt_service.py`
- Modify: `app/services/olt_capabilities.py`
- Test: `python -m py_compile app/services/olt_service.py app/services/olt_capabilities.py`
  (sem script de teste dedicado — este arquivo compartilhado não tem suite
  própria neste repo; a validação é compilar + rodar os testes das Tasks 1-3
  de novo, já que `olt_service.py` só chama as funções do driver, não
  reimplementa lógica)

**Interfaces:**
- Consumes: `add_onu_vsol`, `delete_onu_vsol`, `reboot_onu_vsol` (Tasks 1-3),
  `_is_vsol(req)` (já existe em `olt_service.py`, linha ~68).
- Produces: nada novo consumido por outra task — última peça de backend.

- [ ] **Step 1: Importar as três funções novas**

Em `app/services/olt_service.py`, o bloco de import de
`app.cli.tools.olt_vsol_epon` hoje é (linhas ~42-48):

```python
from app.cli.tools.olt_vsol_epon import (
    collect_macs_vsol,
    collect_onu_telemetry_vsol,
    discover_onus_vsol,
    find_onu_vsol,
    onu_signal_vsol,
)
```

Acrescentar os três nomes novos, mantendo ordem alfabética (mesmo estilo já
usado neste bloco):

```python
from app.cli.tools.olt_vsol_epon import (
    add_onu_vsol,
    collect_macs_vsol,
    collect_onu_telemetry_vsol,
    delete_onu_vsol,
    discover_onus_vsol,
    find_onu_vsol,
    onu_signal_vsol,
    reboot_onu_vsol,
)
```

- [ ] **Step 2: `add_onu` — ramo VSOL**

Em `app/services/olt_service.py`, a função `add_onu` (linha ~1120) tem hoje:

```python
            if _is_intelbras_4840e(req):
                ports = [{"port": e.port or 1, "vlan": e.vlan} for e in req.services] if req.services else (
                    [{"port": 1, "vlan": req.vlan}] if req.vlan else None
                )
                result = add_onu_4840e(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=req.pon, mac=req.serial, description=req.description,
                    ports=ports, timeout=req.timeout,
                )
            else:
                result = _add_onu_8820i(
```

Trocar por (novo `elif` entre os dois blocos existentes; VSOL só precisa de
`pon`+`serial` — sem portas/VLAN, ver Global Constraints):

```python
            if _is_intelbras_4840e(req):
                ports = [{"port": e.port or 1, "vlan": e.vlan} for e in req.services] if req.services else (
                    [{"port": 1, "vlan": req.vlan}] if req.vlan else None
                )
                result = add_onu_4840e(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=req.pon, mac=req.serial, description=req.description,
                    ports=ports, timeout=req.timeout,
                )
            elif _is_vsol(req):
                vsol_result = add_onu_vsol(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=str(req.pon), mac=req.serial, timeout=req.timeout,
                )
                result = dict(vsol_result)
                if result.get("ok"):
                    result["onu"] = result.get("onu_id")
                    result["slot"] = result.get("onu_id")
            else:
                result = _add_onu_8820i(
```

(`result["onu"]`/`result["slot"]` alimentam o `log_onu_action` logo abaixo,
que já lê `result.get("onu") or result.get("slot")` — sem isso o histórico
ficaria sem a posição pra ONU VSOL recém-autorizada.)

A checagem logo depois (linha ~1156-1159) já tem:

```python
            if result.get("ok"):
                if not _is_intelbras_4840e(req):
                    result["inventory"] = _upsert_onu_inventory(req, result)
                    result["device_sync"] = _sync_authorized_onu_devices(req, result)
```

Trocar a condição pra também excluir VSOL desse sync (que é 8820i-específico,
mesma razão já documentada pra 4840E — ver o comentário original da 4840E
nesta mesma sessão se existir no histórico do arquivo):

```python
            if result.get("ok"):
                if not _is_intelbras_4840e(req) and not _is_vsol(req):
                    result["inventory"] = _upsert_onu_inventory(req, result)
                    result["device_sync"] = _sync_authorized_onu_devices(req, result)
```

- [ ] **Step 3: `delete_onu` — ramo VSOL**

`delete_onu` (linha ~1257) tem hoje:

```python
            if _is_intelbras_4840e(req):
                result = delete_onu_4840e(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=req.pon, onu=req.onu, mac=req.serial, timeout=req.timeout,
                )
            else:
                result = _delete_onu_8820i(
```

Trocar por:

```python
            if _is_intelbras_4840e(req):
                result = delete_onu_4840e(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=req.pon, onu=req.onu, mac=req.serial, timeout=req.timeout,
                )
            elif _is_vsol(req):
                result = delete_onu_vsol(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=str(req.pon), onu_id=req.onu, timeout=req.timeout,
                )
            else:
                result = _delete_onu_8820i(
```

O `if result.get("ok"): result["inventory"] = _remove_onu_inventory(req)`
logo abaixo já NÃO tem guarda de driver (foi removida numa correção
anterior desta mesma sessão pra 4840E) — `_remove_onu_inventory` casa por
`olt_ip`+`pon`+`onu`/serial, que são genéricos, então já funciona pra VSOL
sem mudança nenhuma aqui.

- [ ] **Step 4: `reboot_onu` — ramo VSOL**

`reboot_onu` (linha ~1291) tem hoje:

```python
            if _is_intelbras_4840e(req):
                result = reboot_onu_4840e(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=req.pon, onu=req.onu, timeout=req.timeout,
                )
            else:
                result = _reboot_onu_8820i(
```

Trocar por:

```python
            if _is_intelbras_4840e(req):
                result = reboot_onu_4840e(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=req.pon, onu=req.onu, timeout=req.timeout,
                )
            elif _is_vsol(req):
                result = reboot_onu_vsol(
                    olt_ip=req.olt_ip, user=req.user, password=req.password,
                    pon=str(req.pon), onu_id=req.onu, timeout=req.timeout,
                )
            else:
                result = _reboot_onu_8820i(
```

- [ ] **Step 5: Capabilities**

Em `app/services/olt_capabilities.py`, o bloco `elif driver == "vsol_epon":`
(linha ~83) hoje é:

```python
    elif driver == "vsol_epon":
        caps.update({
            "collect_macs": True,
            "telemetry": True,
            "discover_onus": True,
            "find_onu": True,
            "onu_signal": True,
        })
        label = "VSOL EPON"
        notes = (
            "Homologada para inventario, telemetria, descoberta e consulta de ONU. "
            "Autorizar e excluir ONU seguem bloqueados ate homologacao do provisionamento."
        )
```

Trocar por:

```python
    elif driver == "vsol_epon":
        caps.update({
            "collect_macs": True,
            "telemetry": True,
            "discover_onus": True,
            "find_onu": True,
            "onu_signal": True,
            "add_onu": True,
            "delete_onu": True,
            "reboot_onu": True,
        })
        label = "VSOL EPON"
        notes = "Homologada: inventario, telemetria, descoberta, consulta, autorizar, excluir e reiniciar ONU."
```

- [ ] **Step 6: Compilar e rodar os testes das Tasks 1-3 de novo**

Run: `python -m py_compile app/services/olt_service.py app/services/olt_capabilities.py`
Expected: sem erro.

Run: `python scripts/sightops_olt_vsol_add_onu_test.py`
Expected: `OK: sightops_olt_vsol_add_onu_test` (garante que o import novo em
`olt_service.py` não quebrou nada no driver).

- [ ] **Step 7: Commit**

```bash
git add app/services/olt_service.py app/services/olt_capabilities.py
git commit -m "feat(olt-vsol): liga autorizar/excluir/reiniciar no service layer"
```

---

### Task 5: Frontend — campos dedicados VSOL (Autorizar/Consultar/Reiniciar/Excluir)

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/js/deploy.js`
- Modify: `frontend/js/bootstrap.js` (se um novo listener de botão for
  necessário — ver Step 5)

**Interfaces:**
- Consumes: `onuSelectedRegistryRow()`, `onuIsEpon(row)`, `onuStartTicker`,
  `onuStopTicker`, `onuSetResult`, `esc()`, `api()`, `onuUpdatePonSelectors()`
  (todas já existem em `frontend/js/deploy.js`).
- Produces: `onuIsVsol(row)`, `onuActiveOltKind(row)`,
  `onuToggleDriverFields(gponId, eponId, vsolId)` (substitui
  `onuToggleEponFields`, mesmo nome de uso mas 3 parâmetros — ver Step 2),
  `onuPlaceInlineButton(buttonId, gponWrapId, eponRowId, vsolRowId, kind)`
  (assinatura muda de `(buttonId, gponWrapId, eponRowId, isEpon)` pra 5
  parâmetros — ver Step 2), `onuAddVsol`, `onuQueryVsol`, `onuRebootVsol`,
  `onuDeleteVsol` (novas funções async, mesmo padrão de `onuAddEpon`/
  `onuQueryEpon`/`onuRebootEpon`/`onuDeleteEpon` já existentes).

Esta task generaliza o mecanismo de alternância de campos por driver de
DOIS (GPON/EPON) pra TRÊS (GPON/EPON/VSOL). É a task de maior risco de
regressão visual nos painéis já prontos da 4840E — o Step 8 (checagem
manual no navegador) tem que confirmar que os painéis EPON continuam
exatamente como estavam antes desta task, além dos novos painéis VSOL.

- [ ] **Step 1: Localizar as funções que vão mudar**

Antes de editar, `grep -n "function onuIsEpon\|function onuToggleEponFields\|function onuPlaceInlineButton" frontend/js/deploy.js`
pra confirmar os números de linha atuais (podem ter mudado desde a escrita
deste plano) — usar como ponto de partida, não como número fixo.

- [ ] **Step 2: Generalizar `onuToggleEponFields` → `onuToggleDriverFields` e `onuPlaceInlineButton`**

Em `frontend/js/deploy.js`, **substituir** as três funções:

```javascript
function onuIsEpon(row) {
  return String(row?.driver || '').trim().toLowerCase() === 'intelbras_4840e';
}

function onuToggleEponFields(baseId, eponId) {
  const row = onuSelectedRegistryRow();
  const isEpon = onuIsEpon(row);
  const baseEl = document.getElementById(baseId);
  const eponEl = document.getElementById(eponId);
  if (baseEl) baseEl.classList.toggle('hidden', isEpon);
  if (eponEl) eponEl.classList.toggle('hidden', !isEpon);
  return isEpon;
}

function onuPlaceInlineButton(buttonId, gponWrapId, eponRowId, isEpon) {
  const btn = document.getElementById(buttonId);
  const target = document.getElementById(isEpon ? eponRowId : gponWrapId);
  if (btn && target && btn.parentElement !== target) target.appendChild(btn);
  document.getElementById(gponWrapId)?.classList.toggle('hidden', isEpon);
}
```

por:

```javascript
function onuIsEpon(row) {
  return String(row?.driver || '').trim().toLowerCase() === 'intelbras_4840e';
}

function onuIsVsol(row) {
  return String(row?.driver || '').trim().toLowerCase() === 'vsol_epon';
}

function onuActiveOltKind(row) {
  if (onuIsEpon(row)) return 'epon';
  if (onuIsVsol(row)) return 'vsol';
  return 'gpon';
}

function onuToggleDriverFields(gponId, eponId, vsolId) {
  const kind = onuActiveOltKind(onuSelectedRegistryRow());
  const gponEl = gponId ? document.getElementById(gponId) : null;
  const eponEl = eponId ? document.getElementById(eponId) : null;
  const vsolEl = vsolId ? document.getElementById(vsolId) : null;
  if (gponEl) gponEl.classList.toggle('hidden', kind !== 'gpon');
  if (eponEl) eponEl.classList.toggle('hidden', kind !== 'epon');
  if (vsolEl) vsolEl.classList.toggle('hidden', kind !== 'vsol');
  return kind;
}

function onuPlaceInlineButton(buttonId, gponWrapId, eponRowId, vsolRowId, kind) {
  const btn = document.getElementById(buttonId);
  const targetId = kind === 'epon' ? eponRowId : (kind === 'vsol' ? vsolRowId : gponWrapId);
  const target = document.getElementById(targetId);
  if (btn && target && btn.parentElement !== target) target.appendChild(btn);
  document.getElementById(gponWrapId)?.classList.toggle('hidden', kind !== 'gpon');
}
```

`onuIsEpon` fica exatamente igual (não muda) — só as outras duas mudam de
forma/assinatura.

- [ ] **Step 3: Atualizar os chamadores em `onuApplyRegisteredOlt`**

No mesmo arquivo, localizar a função `onuApplyRegisteredOlt` (`grep -n
"function onuApplyRegisteredOlt" frontend/js/deploy.js`). Ela tem hoje,
perto do fim:

```javascript
  onuToggleEponFields('onuAddFieldsGpon', 'onuAddFieldsEpon');
  onuToggleEponFields(null, 'onuAddHintEpon');
  onuPlaceInlineButton('btnOnuQuery', 'onuQueryBtnWrapGpon', 'onuQueryBtnSlotEpon',
    onuToggleEponFields('onuQueryFieldsGpon', 'onuQueryFieldsEpon'));
  onuPlaceInlineButton('btnOnuReboot', 'onuRebootBtnWrapGpon', 'onuRebootBtnSlotEpon',
    onuToggleEponFields('onuRebootFieldsGpon', 'onuRebootFieldsEpon'));
  onuPlaceInlineButton('btnOnuDelete', 'onuDeleteBtnWrapGpon', 'onuDeleteBtnSlotEpon',
    onuToggleEponFields('onuDeleteFieldsGpon', 'onuDeleteFieldsEpon'));
  onuToggleEponFields('onuDiscoverResult', 'onuDiscoverResultEpon');
```

Trocar por:

```javascript
  onuToggleDriverFields('onuAddFieldsGpon', 'onuAddFieldsEpon', 'onuAddFieldsVsol');
  onuToggleDriverFields(null, 'onuAddHintEpon', null);
  onuPlaceInlineButton('btnOnuQuery', 'onuQueryBtnWrapGpon', 'onuQueryBtnSlotEpon', 'onuQueryBtnSlotVsol',
    onuToggleDriverFields('onuQueryFieldsGpon', 'onuQueryFieldsEpon', 'onuQueryFieldsVsol'));
  onuPlaceInlineButton('btnOnuReboot', 'onuRebootBtnWrapGpon', 'onuRebootBtnSlotEpon', 'onuRebootBtnSlotVsol',
    onuToggleDriverFields('onuRebootFieldsGpon', 'onuRebootFieldsEpon', 'onuRebootFieldsVsol'));
  onuPlaceInlineButton('btnOnuDelete', 'onuDeleteBtnWrapGpon', 'onuDeleteBtnSlotEpon', 'onuDeleteBtnSlotVsol',
    onuToggleDriverFields('onuDeleteFieldsGpon', 'onuDeleteFieldsEpon', 'onuDeleteFieldsVsol'));
  onuToggleDriverFields('onuDiscoverResult', 'onuDiscoverResultEpon', null);
```

Note: os IDs `onuQueryBtnSlotVsol`/`onuRebootBtnSlotVsol`/`onuDeleteBtnSlotVsol`
e `onuAddFieldsVsol` são criados no Step 4 (HTML) — se este step rodar
antes do Step 4, os `document.getElementById(...)` desses IDs simplesmente
devolvem `null` e as linhas correspondentes de `onuToggleDriverFields`/
`onuPlaceInlineButton` viram no-op até o HTML existir (não quebra nada).

- [ ] **Step 4: Adicionar os blocos HTML novos**

Em `frontend/index.html`, dentro da sanfona de ONU (`grep -n
'id="onuStepAdd"\|id="onuStepQuery"\|id="onuStepReboot"\|id="onuStepDelete"'
frontend/index.html` pra achar os 4 painéis).

**Painel "2. Autorizar (adicionar) ONU"** — logo depois do bloco que fecha
`id="onuAddFieldsEpon"` (procurar `</div>` que fecha essa div, antes da
`<div style="display:flex;align-items:center;justify-content:space-between;...">`
que já envolve `btnOnuAdd`/hint/aviso — esse botão e essa div são
compartilhados entre os 3 drivers e NÃO mudam), inserir:

```html
                <div id="onuAddFieldsVsol" class="hidden">
                  <div class="form-row">
                    <div class="form-group">
                      <label for="onuAddPonVsol">PON</label>
                      <select id="onuAddPonVsol"></select>
                    </div>
                    <div class="form-group">
                      <label>MAC da ONU
                        <input type="text" id="onuAddMacVsol" placeholder="aa:bb:cc:dd:ee:ff">
                      </label>
                    </div>
                  </div>
                </div>
```

**Painel "3. Consultar sinal e MACs"** — logo depois do bloco que fecha
`id="onuQueryFieldsEpon"` (que já contém `onuQueryBtnSlotEpon` como
terceira coluna), inserir:

```html
                <div id="onuQueryFieldsVsol" class="hidden form-row" style="grid-template-columns:1fr 1fr auto;align-items:end">
                  <div class="form-group">
                    <label>PON
                      <select id="onuQueryPonVsol"></select>
                    </label>
                  </div>
                  <div class="form-group">
                    <label>ONU (posicao)
                      <input type="number" min="1" id="onuQueryOnuNumVsol">
                    </label>
                  </div>
                  <div class="form-group" id="onuQueryBtnSlotVsol">
                    <label style="visibility:hidden">.</label>
                  </div>
                </div>
```

**Painel "4. Reiniciar ONU"** — logo depois do bloco que fecha
`id="onuRebootFieldsEpon"`, inserir:

```html
                <div id="onuRebootFieldsVsol" class="hidden form-row" style="grid-template-columns:1fr 1fr auto;align-items:end">
                  <div class="form-group">
                    <label>PON
                      <select id="onuRebootPonVsol"></select>
                    </label>
                  </div>
                  <div class="form-group">
                    <label>ONU (posicao)
                      <input type="number" min="1" id="onuRebootOnuNumVsol">
                    </label>
                  </div>
                  <div class="form-group" id="onuRebootBtnSlotVsol">
                    <label style="visibility:hidden">.</label>
                  </div>
                </div>
```

**Painel "5. Excluir ONU"** — logo depois do bloco que fecha
`id="onuDeleteFieldsEpon"`, inserir:

```html
                <div id="onuDeleteFieldsVsol" class="hidden form-row" style="grid-template-columns:1fr 1fr auto;align-items:end">
                  <div class="form-group">
                    <label>PON
                      <select id="onuDeletePonVsol"></select>
                    </label>
                  </div>
                  <div class="form-group">
                    <label>ONU (posicao)
                      <input type="number" min="1" id="onuDeleteOnuNumVsol">
                    </label>
                  </div>
                  <div class="form-group" id="onuDeleteBtnSlotVsol">
                    <label style="visibility:hidden">.</label>
                  </div>
                </div>
```

- [ ] **Step 5: Bumpar o cache-bust do `deploy.js` em `frontend/index.html`**

`grep -n 'deploy\.js?v=' frontend/index.html` pra achar o número atual (ex:
`?v=184`) e trocar pro próximo número inteiro (ex: `?v=185`) — nunca reusar
um número já usado antes nesta sessão.

- [ ] **Step 6: Implementar `onuAddVsol`, `onuQueryVsol`, `onuRebootVsol`, `onuDeleteVsol`**

Em `frontend/js/deploy.js`, localizar `onuAddEpon`/`onuAdd`,
`onuQueryEpon`/`onuQuery`, `onuRebootEpon`/`onuReboot`,
`onuDeleteEpon`/`onuDelete`/`onuConfirmDelete` (`grep -n "async function
onuAddEpon\|async function onuAdd\b\|async function onuQueryEpon\|async
function onuQuery\b\|async function onuRebootEpon\|async function
onuReboot\b\|async function onuDeleteEpon\|async function onuDelete\b\|
async function onuConfirmDelete" frontend/js/deploy.js`).

Cada função dispatcher (`onuAdd`, `onuQuery`, `onuReboot`, `onuDelete`) já
tem uma linha `if (onuIsEpon(onuSelectedRegistryRow())) { return
onuXxxEpon(olt); }` logo no início — acrescentar, logo antes dessa linha,
uma equivalente pra VSOL:

```javascript
  if (onuIsVsol(onuSelectedRegistryRow())) { return onuXxxVsol(olt); }
  if (onuIsEpon(onuSelectedRegistryRow())) { return onuXxxEpon(olt); }
```

(substituir `Xxx` por `Add`/`Query`/`Reboot`/`Delete` em cada dispatcher —
a ordem entre os dois `if` não importa, já que um único driver nunca é
EPON e VSOL ao mesmo tempo).

Implementar as 4 funções novas, no mesmo arquivo, próximas às suas
equivalentes `...Epon`:

```javascript
async function onuAddVsol(olt) {
  const pon = Number(document.getElementById('onuAddPonVsol')?.value || '0');
  const mac = document.getElementById('onuAddMacVsol')?.value.trim() || '';
  if (!pon || !mac) { showToast('Informe PON e MAC da ONU.', true); return; }

  const ticker = onuStartTicker('onuAddResult', 'Autorizando ONU na OLT');
  const res = await api('/api/olt/add-onu', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, serno_id: 0, vlan: 0, serial: mac, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuAddResult', esc(data?.error || 'Falha ao autorizar ONU.'), true);
    return;
  }
  loadOnuHistory();
  const posicao = data.pending
    ? 'aguardando a OLT registrar a posicao (atualize o historico em alguns segundos)'
    : `posicao atribuida: ONU ${esc(data.onu_id)}`;
  onuSetResult('onuAddResult', `
    <div><b>PON ${esc(pon)}</b> - MAC ${esc(mac)} autorizado</div>
    <div style="margin-top:4px">${posicao}</div>
  `);
}

async function onuQueryVsol(olt) {
  const pon = Number(document.getElementById('onuQueryPonVsol')?.value || '0');
  const onuNum = Number(document.getElementById('onuQueryOnuNumVsol')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  const ticker = onuStartTicker('onuQueryResult', 'Consultando sinal da ONU');
  const res = await api('/api/olt/onu-signal', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuQueryResult', esc(data?.error || 'Falha ao consultar sinal.'), true);
    return;
  }
  onuSetResult('onuQueryResult', `
    <div><b>PON ${esc(data.pon)} / ONU ${esc(data.onu_id)}</b> - MAC ${esc(data.onu_mac || '-')}</div>
    <div>Estado: ${esc(data.oper_status || '-')} / Distancia: ${esc(data.distance_km ?? '-')} km</div>
    <div>RX: ${esc(data.onu_rx ?? '-')} dBm</div>
  `);
}

async function onuRebootVsol(olt) {
  const pon = Number(document.getElementById('onuRebootPonVsol')?.value || '0');
  const onuNum = Number(document.getElementById('onuRebootOnuNumVsol')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  const ticker = onuStartTicker('onuRebootResult', 'Reiniciando ONU na OLT (equipamento vivo)');
  const res = await api('/api/olt/reboot-onu', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  loadOnuHistory();
  if (!res?.ok || data?.ok === false) {
    onuSetResult('onuRebootResult', esc(data?.error || 'Falha ao reiniciar ONU.'), true);
    return;
  }
  onuSetResult('onuRebootResult', `<div><b>PON ${esc(pon)} / ONU ${esc(onuNum)}</b> reiniciada.</div>`);
}

async function onuDeleteVsol(olt) {
  const pon = Number(document.getElementById('onuDeletePonVsol')?.value || '0');
  const onuNum = Number(document.getElementById('onuDeleteOnuNumVsol')?.value || '0');
  if (!pon || !onuNum) { showToast('Informe PON e numero da ONU.', true); return; }

  _onuDeleteTarget = { olt, pon, onu: onuNum, vlanHint: '' };
  const panoramaEl = document.getElementById('onuDeletePanorama');
  const confirmBtn = document.getElementById('confirmOnuDelete');
  if (confirmBtn) confirmBtn.disabled = true;
  openOnuDeleteModal();

  const ticker = onuStartTicker('onuDeletePanorama', 'Consultando dados da ONU na OLT');
  const res = await api('/api/olt/onu-signal', {
    method: 'POST',
    body: JSON.stringify({
      olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
      olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
      pon, onu: onuNum, site: olt.site || '', olt_name: olt.olt_name || '',
      connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '',
    }),
  });
  onuStopTicker(ticker);
  const data = await res?.json().catch(() => ({}));
  if (!panoramaEl) return;
  if (!res?.ok || data?.ok === false) {
    panoramaEl.innerHTML = `<p>Sem informacoes para essa ONU (PON ${esc(pon)} / posicao ${esc(onuNum)}) -- ${esc(data?.error || 'nao respondeu')}.</p>`;
    return;
  }
  _onuDeleteTarget.mac = data.onu_mac || '';
  if (confirmBtn) confirmBtn.disabled = false;
  panoramaEl.innerHTML = `
    <p>Voce esta prestes a excluir:</p>
    <div style="margin:8px 0;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-soft)">
      <div><b>PON ${esc(pon)} / ONU ${esc(onuNum)}</b> - MAC ${esc(data.onu_mac || '-')}</div>
      <div style="margin-top:4px">Estado: ${esc(data.oper_status || '-')}</div>
    </div>
    <p style="color:var(--danger);font-size:13px;margin:0">Isso remove a autorizacao e desliga o servico dela AGORA na OLT.</p>
  `;
}
```

- [ ] **Step 7: Ligar `onuConfirmDelete` pro caso VSOL**

`onuConfirmDelete` (a função que roda quando o usuário confirma no modal)
já tem, hoje:

```javascript
  const isEpon = onuIsEpon(onuSelectedRegistryRow());
  const payload = isEpon
    ? { olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password,
        olt_vendor: olt.olt_vendor, olt_model: olt.olt_model,
        pon, onu, serial: mac || '', vlan_hint: vlanHint || '', site: olt.site || '',
        connector_id: olt.connector_id || '', remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '' }
    : { olt_id: olt.olt_id || null, olt_ip: olt.olt_ip, user: olt.user, password: olt.password, pon, onu,
        vlan_hint: vlanHint || '', site: olt.site || '', connector_id: olt.connector_id || '',
        remote_connector_id: olt.remote_connector_id || '', connector_name: olt.connector_name || '' };
```

Como os dois ramos já montam praticamente o mesmo payload (a diferença
real entre GPON e EPON aqui é só `olt_vendor`/`olt_model`/`serial` estarem
presentes ou não — e o backend em `olt_service.py` ignora campos que não
usa), o caso VSOL **não precisa de um terceiro ramo**: o payload do ramo
GPON (`else`) já contém `pon`+`onu`, que é tudo que `delete_onu_vsol`
consome. Nenhuma mudança necessária neste step além de confirmar (visual,
Step 8) que a exclusão VSOL de fato manda `pon`+`onu` corretos.

- [ ] **Step 8: Checar sintaxe e testar manualmente no navegador**

Run: `node -e "new Function(require('fs').readFileSync('frontend/js/deploy.js','utf8'))"`
Expected: sem erro.

Run: `node -e "require('fs').readFileSync('frontend/index.html','utf8')"`
Expected: sem erro (checagem básica de leitura; a validação de estrutura
acontece visualmente no navegador a seguir).

Abrir a tela de Implantação > ONU (local ou apontando pro servidor de
desenvolvimento já configurado no repo), selecionar uma OLT cadastrada com
driver `intelbras_4840e` e confirmar que os 4 painéis (Autorizar, Consultar,
Reiniciar, Excluir) continuam exatamente como antes desta task — nenhuma
regressão visual. Depois, se houver uma OLT `vsol_epon` cadastrada no
ambiente de teste, selecioná-la e confirmar que aparecem os campos PON+MAC
(Autorizar) e PON+número (Consultar/Reiniciar/Excluir), com os botões
alinhados à altura das caixas ao lado (mesmo padrão já corrigido pra EPON).

- [ ] **Step 9: Commit**

```bash
git add frontend/index.html frontend/js/deploy.js
git commit -m "feat(olt-vsol): campos dedicados na tela pra autorizar/consultar/reiniciar/excluir"
```

---

## Nota final — fora do escopo das tasks

**Deploy em produção**: não é parte deste plano. Quando o usuário pedir,
seguir o mesmo padrão já usado pra 4840E nesta sessão: extrair
`olt_vsol_epon.py`, `olt_service.py` e `olt_capabilities.py` de dentro do
container real (`docker cp`) antes de aplicar qualquer mudança — os dois
últimos têm histórico de bastante drift entre repo e produção — construir
imagem nova, validar com `python -c "import app.main"` dentro da imagem,
publicar com `deploy_api.py`/`deploy_api_v3.py` (nunca `docker compose up
--no-deps` direto), e atualizar `.env.production`/`.env.v3` com a tag nova
depois. Frontend via `pscp` pros dois `frontend/` de produção
(`sightops-prod-release` e `sightops-v3-release`), com backup antes.

**Validação em equipamento real**: também fora das tasks — depois que a
Task 5 passar (testes automatizados + checagem visual local), testar o
fluxo completo (autorizar → consultar → reiniciar → excluir → reautorizar)
contra a OLT real de Japaratinga (192.168.200.2, usuário `admin`), numa
ONU que o usuário escolha e autorize explicitamente pra cada ação
destrutiva — mesmo padrão já usado pra 4840E (SANTANA → BARRA DE SÃO
MIGUEL). Confirmar em especial, nesse teste ao vivo, que `onu <onuid> ctc
reset` realmente não pede confirmação y/n (suposição do manual, ainda não
provada contra hardware real).
