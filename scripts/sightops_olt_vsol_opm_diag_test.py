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


OPM_DIAG_PON02 = (
    "show onu opm-diag\r\n\r\n"
    "ONU-ID      Temperature(C)    Supply Voltage(V)   TX Bias Current(mA)   TX Power(dBm)   RX Power(dBm)\r\n"
    "------      --------------    -----------------   -------------------   -------------   -------------\r\n"
    "EPON0/2:1   40.00             3.20                14.00                 5.00            -12.34\r\n"
    "epon-olt(config-pon-0/2)# "
)

AUTH_INFO_PON02 = """show onu auth-info
ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description
EPON0/2:1   1      online    11:22:33:44:55:66   500     N/A
epon-olt(config-pon-0/2)#"""


class CanalFalsoComContexto:
    """Rastreia a PON atual (como a OLT faria) pra responder 'show onu
    opm-diag' com a tabela certa -- pega o bug real de 2026-09-01: a
    primeira versao de collect_onu_telemetry_vsol consultava opm-diag ANTES
    de entrar na PON de cada iteracao, entao a 2a PON em diante lia a
    tabela da PON anterior (ou nenhuma)."""

    def __init__(self) -> None:
        self.pon_atual = ""
        self.comandos: list[str] = []
        self.tabelas = {"0/1": OPM_DIAG_PON01, "0/2": OPM_DIAG_PON02}
        self.auths = {"0/1": AUTH_INFO_PON01, "0/2": AUTH_INFO_PON02}

    def resposta(self, cmd: str) -> str:
        self.comandos.append(cmd)
        if cmd.startswith("interface epon"):
            self.pon_atual = cmd.split()[-1]
            return "epon-olt(config-pon-%s)#" % self.pon_atual
        if cmd == "show onu opm-diag":
            return self.tabelas.get(self.pon_atual, "epon-olt#")
        if cmd == "show onu auth-info":
            return self.auths.get(self.pon_atual, "epon-olt#")
        if cmd == "show onu basic-info":
            return "show onu basic-info\r\nepon-olt(config-pon-%s)#" % self.pon_atual
        return "epon-olt#"


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

    # 6) collect_onu_telemetry_vsol com 2 PONs: cada PON tem que ler a sua
    #    PROPRIA tabela de opm-diag, nao a da PON anterior (achado ao vivo
    #    em Japaratinga: sem entrar na PON antes do opm-diag, so 10 de 21
    #    ONUs vinham com sinal)
    canal = CanalFalsoComContexto()
    vsol._manda = lambda chan, texto, alvos, timeout=20.0: chan.resposta(texto)
    vsol._espera_prompt = lambda chan, alvos, timeout=20.0: "epon-olt#"
    vsol._volta_ao_topo = lambda chan, timeout=20.0: None
    vsol._com_sessao_vsol = lambda ip, u, p, port, timeout, tarefa: tarefa(canal)
    vsol._pons_existentes = lambda chan, pon, maximo=8, timeout=15.0: ["0/1", "0/2"]

    linhas = vsol.collect_onu_telemetry_vsol("192.168.200.2", "admin", "x", pon="all")
    por_posicao = {(l["pon"], l["onu_id"]): l for l in linhas}
    onu_p1 = por_posicao.get((1, 2), {})
    onu_p2 = por_posicao.get((2, 1), {})
    if onu_p1.get("rx_onu") != "-10.74":
        erros.append(f"collect_onu_telemetry_vsol: PON 0/1 ONU 2 esperava rx_onu '-10.74', veio {onu_p1.get('rx_onu')!r} -- sinal de PON errada")
    if onu_p2.get("rx_onu") != "-12.34":
        erros.append(f"collect_onu_telemetry_vsol: PON 0/2 ONU 1 esperava rx_onu '-12.34', veio {onu_p2.get('rx_onu')!r} -- sinal de PON errada (bug de contexto)")

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
