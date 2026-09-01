"""Testa a leitura de potencia optica real do driver VSOL EPON.

`show onu <id> ctc pon monitor_status` (usado antes) so informa se o
monitoramento periodico esta ligado/desligado -- na OLT de Japaratinga ele
sempre voltou "disable", entao onu_rx nunca aparecia. `show onu opm-diag`
e o comando certo: traz temperatura/tensao/bias/TX/RX de toda a PON numa
tabela so. Saida de exemplo capturada ao vivo (PON 0/1, 2026-09-01).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.cli.tools.olt_vsol_epon as vsol

OPM_DIAG_PON01 = (
    "show onu opm-diag\r\n\r\n"
    "ONU-ID      Temperature(C)    Supply Voltage(V)   TX Bias Current(mA)   TX Power(dBm)   RX Power(dBm)\r\n"
    "------      --------------    -----------------   -------------------   -------------   -------------\r\n"
    "EPON0/1:1   42.96             3.24                15.65                 4.25            -17.12\r\n"
    "EPON0/1:2   41.19             3.28                14.45                 5.14            -10.74\r\n"
    "EPON0/1:4   41.55             3.24                15.80                 3.29            -14.88\r\n"
    "epon-olt(config-pon-0/1)# "
)

AUTH_INFO_PON01 = """show onu auth-info
ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description
EPON0/1:2   3      online    98:2a:0a:a0:25:b9   672     N/A
epon-olt(config-pon-0/1)#"""

AUTH_INFO_PON01_OFFLINE = """show onu auth-info
ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description
EPON0/1:2   3      offline   98:2a:0a:a0:25:b9   0       N/A
epon-olt(config-pon-0/1)#"""

MAC_TABLE_ONU2 = """show onu 2 mac-address-table
 Mac Address Table
----------------------------------------------------------
Index   VLAN   MAC  Address         PON       ONU    Aging(s)
1       1000   54:6c:ac:25:e6:cf    EPON0/1   2      255
2       1000   80:2a:a8:11:22:33    EPON0/1   2      255

 Total Addresses Found in System :2
epon-olt(config-pon-0/1)#"""


class CanalFalso:
    def __init__(self, opm: str, auth: str, mac_table: str = "") -> None:
        self.opm = opm
        self.auth = auth
        self.mac_table = mac_table
        self.comandos: list[str] = []

    def resposta(self, cmd: str) -> str:
        self.comandos.append(cmd)
        if cmd.startswith("interface epon"):
            return "epon-olt(config-pon-0/1)#"
        if cmd == "show onu opm-diag":
            return self.opm
        if cmd == "show onu auth-info":
            return self.auth
        if cmd == "show onu 2 mac-address-table":
            return self.mac_table
        return "epon-olt#"


def _instala_sessao_falsa(canal: CanalFalso) -> None:
    vsol._manda = lambda chan, texto, alvos, timeout=20.0: chan.resposta(texto)
    vsol._espera_prompt = lambda chan, alvos, timeout=20.0: "epon-olt#"
    vsol._volta_ao_topo = lambda chan, timeout=20.0: None
    vsol._com_sessao_vsol = lambda ip, u, p, port, timeout, tarefa: tarefa(canal)


def falhas() -> list[str]:
    erros: list[str] = []

    # 1) parse_onu_opm_diag extrai temperatura/tensao/bias/tx/rx por onu_id,
    #    inclusive RX negativo (sempre e, em dBm)
    tabela = vsol.parse_onu_opm_diag(OPM_DIAG_PON01)
    if set(tabela.keys()) != {"1", "2", "4"}:
        erros.append(f"parse_onu_opm_diag: chaves esperadas {{'1','2','4'}}, veio {set(tabela.keys())}")
    onu2 = tabela.get("2", {})
    if onu2.get("onu_rx") != "-10.74":
        erros.append(f"parse_onu_opm_diag: onu_rx da ONU 2 esperava '-10.74', veio {onu2.get('onu_rx')!r}")
    if onu2.get("onu_tx") != "5.14":
        erros.append(f"parse_onu_opm_diag: onu_tx da ONU 2 esperava '5.14', veio {onu2.get('onu_tx')!r}")
    if onu2.get("temperatura") != "41.19":
        erros.append(f"parse_onu_opm_diag: temperatura da ONU 2 esperava '41.19', veio {onu2.get('temperatura')!r}")

    # 2) linha fora do padrao (cabecalho, prompt) nao vira entrada espuria
    if len(tabela) != 3:
        erros.append(f"parse_onu_opm_diag: esperava 3 linhas de dados, veio {len(tabela)}")

    # 3) onu_signal_vsol usa show onu opm-diag (nao mais monitor_status) e
    #    devolve onu_rx de verdade pra ONU consultada
    canal = CanalFalso(OPM_DIAG_PON01, AUTH_INFO_PON01)
    _instala_sessao_falsa(canal)
    r = vsol.onu_signal_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=2)
    if r.get("onu_rx") != "-10.74":
        erros.append(f"onu_signal_vsol: onu_rx esperava '-10.74', veio {r.get('onu_rx')!r}")
    if not any(c == "show onu opm-diag" for c in canal.comandos):
        erros.append(f"onu_signal_vsol: nao mandou 'show onu opm-diag', comandos={canal.comandos}")
    if any("monitor_status" in c for c in canal.comandos):
        erros.append(f"onu_signal_vsol: ainda manda o comando antigo 'monitor_status', comandos={canal.comandos}")

    # 4) ONU online: onu_signal_vsol devolve os MACs das cameras atras dela
    canal = CanalFalso(OPM_DIAG_PON01, AUTH_INFO_PON01, MAC_TABLE_ONU2)
    _instala_sessao_falsa(canal)
    r = vsol.onu_signal_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=2)
    macs = r.get("macs") or []
    if len(macs) != 2:
        erros.append(f"onu_signal_vsol: esperava 2 MACs, veio {len(macs)} ({macs})")
    macs_encontrados = {m.get("mac") for m in macs}
    if macs_encontrados != {"54:6c:ac:25:e6:cf", "80:2a:a8:11:22:33"}:
        erros.append(f"onu_signal_vsol: MACs errados, veio {macs_encontrados}")
    if not any("1000" in (m.get("interface") or "") for m in macs):
        erros.append(f"onu_signal_vsol: interface deveria citar a VLAN, veio {macs}")

    # 5) ONU offline: nao tenta ler a tabela de MAC (nao ha luz pra aprender nada)
    canal = CanalFalso(OPM_DIAG_PON01, AUTH_INFO_PON01_OFFLINE, MAC_TABLE_ONU2)
    _instala_sessao_falsa(canal)
    r = vsol.onu_signal_vsol("192.168.200.2", "admin", "x", pon="0/1", onu_id=2)
    if r.get("macs") != []:
        erros.append(f"onu_signal_vsol: ONU offline nao deveria ter macs, veio {r.get('macs')}")
    if any("mac-address-table" in c for c in canal.comandos):
        erros.append(f"onu_signal_vsol: consultou mac-address-table de ONU offline, comandos={canal.comandos}")

    return erros


def main() -> int:
    erros = falhas()
    for e in erros:
        print("FALHOU:", e)
    if not erros:
        print("OK: sightops_olt_vsol_opm_diag_test")
    return 1 if erros else 0


if __name__ == "__main__":
    raise SystemExit(main())
