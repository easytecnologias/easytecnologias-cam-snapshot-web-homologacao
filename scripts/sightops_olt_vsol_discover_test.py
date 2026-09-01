"""Testa a descoberta de ONUs nao autorizadas no driver VSOL EPON.

Achado ao vivo (Japaratinga, 2026-09-01): `show onu discover` lista TODA
ONU com o link OAM/MPCP completo, autorizada ou nao -- nunca foi uma lista
de "so pendentes". Com `onu auth-mode disable` isso nunca aparecia (a ONU
virava autorizada na hora, sem passar tempo nenhum "so descoberta"); ao
ligar `mac-auth` nas 4 PONs, ONUs ja autorizadas e em producao passaram a
aparecer na tela "Descobrir ONUs" como se fossem novas. `discover_onus_vsol`
agora cruza contra `show onu auth-info` (via `_le_pon`) e tira quem ja esta
autorizado antes de devolver.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.cli.tools.olt_vsol_epon as vsol

# ONU ja autorizada (MAC 80:85:44:5f:2e:98) + uma de verdade nova na fibra
# (98:e5:5b:99:99:99), ainda sem `onu mac-auth add`.
AUTH_INFO_PON01 = """show onu auth-info
ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description
EPON0/1:1   3      online    80:85:44:5f:2e:98   798     N/A
epon-olt(config-pon-0/1)#"""

BASIC_INFO_PON01 = """show onu basic-info
ONU-ID      VendorID  Model     ID            hwVer     SwVer        Type   Interface Type
EPON0/1:1   ITBS      R1v2      8085445F2E98  ONUR1_v2  1.3-220719   SFU    1GE
epon-olt(config-pon-0/1)#"""

DISCOVER_PON01 = """show onu discover
list of OLTs --->
	 index  0 device_id 0x0000 hello_state 01 mac 001325.000000
Index  PON-ID    LLID   MAC  Address         Link        CTC OAM Stats
-----  ------    ----   ------------         ----        -------------
1      EPON0/1   3      80:85:44:5f:2e:98    Discovered  COMPELTED
2      EPON0/1   7      98:e5:5b:99:99:99    Discovered  COMPELTED
epon-olt(config-pon-0/1)#"""


class CanalFalso:
    def __init__(self) -> None:
        self.comandos: list[str] = []

    def resposta(self, cmd: str) -> str:
        self.comandos.append(cmd)
        if cmd.startswith("interface epon"):
            return "epon-olt(config-pon-0/1)#"
        if cmd == "show onu auth-info":
            return AUTH_INFO_PON01
        if cmd == "show onu basic-info":
            return BASIC_INFO_PON01
        if cmd == "show onu discover":
            return DISCOVER_PON01
        return "epon-olt#"


def _instala_sessao_falsa(canal: CanalFalso) -> None:
    vsol._manda = lambda chan, texto, alvos, timeout=20.0: chan.resposta(texto)
    vsol._espera_prompt = lambda chan, alvos, timeout=20.0: "epon-olt#"
    vsol._volta_ao_topo = lambda chan, timeout=20.0: None
    vsol._com_sessao_vsol = lambda ip, u, p, port, timeout, tarefa: tarefa(canal)
    vsol._pons_existentes = lambda chan, pon, maximo=8, timeout=15.0: ["0/1"]


def falhas() -> list[str]:
    erros: list[str] = []

    canal = CanalFalso()
    _instala_sessao_falsa(canal)
    r = vsol.discover_onus_vsol("192.168.200.2", "admin", "x", pon="all")

    macs = {item["onu_mac"] for item in r["onus"]}
    if "80:85:44:5f:2e:98" in macs:
        erros.append(f"discover_onus_vsol: ONU ja autorizada nao deveria aparecer, veio {macs}")
    if "98:e5:5b:99:99:99" not in macs:
        erros.append(f"discover_onus_vsol: ONU nova deveria aparecer, veio {macs}")
    if r["total"] != 1:
        erros.append(f"discover_onus_vsol: esperava total=1 (so a ONU nova), veio {r['total']}")

    return erros


def main() -> int:
    erros = falhas()
    for e in erros:
        print("FALHOU:", e)
    if not erros:
        print("OK: sightops_olt_vsol_discover_test")
    return 1 if erros else 0


if __name__ == "__main__":
    raise SystemExit(main())
