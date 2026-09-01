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
    if tentativas_auth_info != 6:
        erros.append(f"registro pendente: esperava 6 tentativas de 'show onu auth-info', veio {tentativas_auth_info}")

    # 4) MAC normalizado (maiusculo/hifen) antes de mandar pra OLT e antes de comparar
    canal = CanalFalso(AUTH_INFO_COM_NOVA_ONU)
    _instala_sessao_falsa(canal)
    r = vsol.add_onu_vsol("192.168.200.2", "admin", "x", pon="0/1", mac="AA-BB-CC-DD-EE-FF")
    if not r.get("ok") or r.get("onu_id") != "9":
        erros.append(f"MAC nao normalizado corretamente: {r}")

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
