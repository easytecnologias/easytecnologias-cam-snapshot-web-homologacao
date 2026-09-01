"""Driver da OLT VSOL EPON (plataforma `epon olt platform`).

Homologado em 20/08/2026 contra a OLT 192.168.200.2 (Japaratinga/rads).

O que esta OLT tem de diferente das ja suportadas
-------------------------------------------------

**Tres autenticacoes em sequencia.** O SSH autentica, cai num CLI que pede
`Login:`/`Password:` de novo, e o `enable` pede senha outra vez. Nas OLTs
testadas as tres usam a mesma credencial, mas o driver trata cada etapa
separadamente -- se alguem separar as senhas depois, so uma etapa quebra.

**Os comandos de ONU vivem dentro da interface da PON.** Nao ha comando global:
e preciso `configure terminal` -> `interface epon 0/N` para so entao rodar
`show onu ...`. Cada PON e um contexto proprio.

**E EPON: a ONU e identificada por MAC, nao por serial GPON.** Isso ja tem
precedente aqui -- o driver Intelbras 4840e tambem e EPON, e o restante do
sistema lida com identificacao por MAC (a mensagem de alerta rotula "MAC" ou
"Serial" conforme o formato do valor).

**Distancia vem como RTT em TQ**, nao em metros. A conversao esta em
`_rtt_para_km`, com a aproximacao documentada la.

Formato real das saidas (capturado da OLT em producao):

    epon-olt(config-pon-0/1)# show onu basic-info
    ONU-ID      VendorID  Model     ID            hwVer     SwVer        Type   Interface Type
    EPON0/1:1   ITBS      R1v2      982A0AA02619  ONUR1_v2  1.3-220719   SFU    1GE

    epon-olt(config-pon-0/1)# show onu auth-info
    ONU-ID      LLID   Status    MAC  Address         RTT(TQ) Description  ...
    EPON0/1:1   2      online    98:2a:0a:a0:26:19    446     N/A          ...
    EPON0/1:7   -1     offline   98:2a:0a:a0:26:27    0       N/A          ...
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

# A sessao (SSH com KEX legado, sshpass de reserva, leitura de canal) e a mesma
# do 4840e -- OLT antiga, mesmos algoritmos obsoletos. Reaproveitar evita manter
# duas copias do mesmo trabalho delicado.
from app.cli.tools.olt_4840e_collect_macs import (  # noqa: F401
    _cli,
    _norm_mac,
    _read,
)
from app.cli.tools.olt_4840e_collect_macs import _open_shell as _open_shell_legado


def _abre_shell_vsol(host: str, user: str, password: str, port: int = 22, timeout: float = 15.0):
    """Abre a sessao interativa com a OLT VSOL.

    O `_open_shell` do 4840e decide de antemao que OLT legada nao serve para o
    paramiko e vai direto para `sshpass` -- que nao existe no container da API.
    Medido em 20/08/2026: o paramiko 5.0 conecta nesta VSOL sem ajuste nenhum.
    Entao tenta paramiko primeiro e so cai no caminho legado se ele recusar; assim
    o driver funciona no container e continua funcionando numa VSOL mais antiga
    que exija algoritmos obsoletos.
    """
    import paramiko

    try:
        cliente = paramiko.SSHClient()
        cliente.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cliente.connect(host, port=port, username=user, password=password,
                        look_for_keys=False, allow_agent=False, timeout=timeout)
        canal = cliente.invoke_shell(width=250, height=2000)
        canal.settimeout(timeout)
        return cliente, canal
    except Exception:
        return _open_shell_legado(host, user, password, port=port, timeout=timeout)

_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
# "EPON0/1:7" -> slot 0, porta 1, onu 7
_ONU_ID_RE = re.compile(r"EPON(\d+)/(\d+):(\d+)", re.I)


# --------------------------------------------------------------------- sessao

def _espera_prompt(chan, alvos, timeout: float = 20.0) -> str:
    """Le do canal ate aparecer um dos textos de `alvos`.

    Esta OLT derruba a sessao quando recebe comando fora de hora, e exibe banner
    mais tres prompts diferentes antes de liberar o CLI. Enviar as cegas com
    timeout fixo fecha o socket -- por isso cada etapa espera o seu prompt.
    """
    fim = time.time() + timeout
    buffer = ""
    while time.time() < fim:
        if chan.recv_ready():
            try:
                buffer += chan.recv(65535).decode("utf-8", "ignore")
            except Exception:
                break
            baixo = buffer.lower()
            for alvo in alvos:
                if alvo.lower() in baixo:
                    return buffer
        else:
            time.sleep(0.3)
    return buffer


def _manda(chan, texto: str, alvos, timeout: float = 20.0) -> str:
    """Envia uma linha e espera o proximo prompt esperado."""
    try:
        chan.send(texto + "\n")
    except Exception as e:
        raise RuntimeError("a OLT fechou a sessao ao receber %r: %s" % (texto, e))
    return _espera_prompt(chan, alvos, timeout=timeout)


def _login_vsol(chan, user: str, password: str, timeout: float = 20.0) -> None:
    """Passa pelos dois logins do CLI e entra em modo privilegiado.

    O SSH ja autenticou quando chegamos aqui, mas o CLI da OLT reapresenta
    `Login:`/`Password:`. Depois disso, `enable` pede senha mais uma vez.
    """
    inicio = _espera_prompt(chan, ["login:", "password:", ">", "#"], timeout=timeout)
    if "login:" in inicio.lower():
        _manda(chan, user, ["password:"], timeout=timeout)
        saida = _manda(chan, password, [">", "#", "failed", "retry"], timeout=timeout)
        if "failed" in saida.lower() or "retry" in saida.lower():
            raise RuntimeError("login recusado pela OLT VSOL")

    saida = _manda(chan, "enable", ["password:", "#"], timeout=timeout)
    if "password:" in saida.lower():
        saida = _manda(chan, password, ["#", "bad password", "retry"], timeout=timeout)
    if "bad password" in saida.lower():
        raise RuntimeError("senha de enable recusada pela OLT VSOL")
    if "#" not in saida:
        raise RuntimeError("nao consegui entrar em modo privilegiado na OLT VSOL")

    # sem isso a OLT pagina a saida com --More-- e o parser recebe lixo
    _manda(chan, "terminal length 0", ["#"], timeout=timeout)


def _volta_ao_topo(chan, timeout: float = 20.0) -> None:
    """Sai de qualquer contexto de configuracao, sem derrubar a sessao.

    Nesta OLT, `end` no prompt privilegiado (`epon-olt#`) ENCERRA a sessao,
    como um `exit` -- so e seguro quando ja se esta dentro de `(config...)`.
    Por isso o prompt atual e consultado antes de decidir.
    """
    try:
        chan.send(chr(10))
    except Exception as e:
        raise RuntimeError("sessao ja fechada: %s" % e)
    atual = _espera_prompt(chan, ["#", ">"], timeout=timeout)
    if "(config" in atual:
        _manda(chan, "end", ["#"], timeout=timeout)


def _rotulo_da_pon(valor: Any) -> str:
    """Aceita 1, "1", "0/1" ou "epon 0/1" e devolve sempre "0/1".

    As telas trabalham com o NUMERO da PON (o seletor "PON 1" chega aqui como
    1), e esta OLT so entende `interface epon 0/1`. Sem normalizar, o CLI ignora
    o comando em silencio, o prompt nao muda, e o driver fica esperando um
    `config-pon` que nunca vem -- foi o que pendurou a consulta de sinal.
    """
    texto = str(valor or "").strip().lower().replace("epon", " ").strip()
    m = re.search(r"(\d+)\s*/\s*(\d+)", texto)
    if m:
        return "%s/%s" % (m.group(1), m.group(2))
    if re.fullmatch(r"\d+", texto):
        return "0/%s" % texto
    return str(valor or "").strip()


def _entra_na_pon(chan, pon: str, timeout: float = 15.0) -> None:
    """Coloca a sessao no contexto `interface epon <pon>`.

    Sai de qualquer contexto anterior antes: navegar de uma PON para outra sem
    `end` deixa o CLI num nivel inesperado e os comandos seguintes falham em
    silencio.

    Confere o prompt em vez de confiar na ausencia de mensagem de erro: entrar
    numa PON inexistente NAO da erro nesta OLT. Sem essa checagem, todo comando
    seguinte espera o timeout inteiro antes de desistir -- minutos de tela
    parada em vez de um erro imediato.
    """
    alvo = _rotulo_da_pon(pon)
    _volta_ao_topo(chan, timeout=timeout)
    _manda(chan, "configure terminal", ["(config)#"], timeout=timeout)
    saida = _manda(chan, f"interface epon {alvo}", ["config-pon", "(config)#"], timeout=timeout)
    if "unknown command" in saida.lower() or "% invalid" in saida.lower():
        raise RuntimeError(f"PON invalida na OLT VSOL: {pon}")
    if _prompt_da_pon(saida) != alvo:
        raise RuntimeError(f"a OLT nao entrou na PON {alvo}: essa porta existe nesta OLT?")


# --------------------------------------------------------------------- parsers

def _partes_do_onu_id(valor: str) -> Dict[str, str]:
    """'EPON0/1:7' -> {'pon': '0/1', 'onu_id': '7'}"""
    m = _ONU_ID_RE.search(str(valor or ""))
    if not m:
        return {"pon": "", "onu_id": ""}
    return {"pon": f"{m.group(1)}/{m.group(2)}", "onu_id": m.group(3)}


def _rtt_para_km(rtt: Any) -> str:
    """Converte RTT em TQ para km aproximado.

    Um TQ (time quantum) equivale a 16 ns de ida e volta. A luz na fibra anda a
    ~2x10^8 m/s, entao cada TQ vale ~1,6 m de distancia (ida e volta ja
    descontada). E aproximacao: serve para ordenar de perto/longe e detectar
    valor absurdo, nao para medir lance de fibra.
    """
    try:
        n = float(str(rtt).strip())
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"{(n * 1.6) / 1000.0:.3f}"


def parse_onu_auth_info(saida: str) -> List[Dict[str, Any]]:
    """Le `show onu auth-info`: estado, MAC, LLID e RTT de cada ONU."""
    linhas: List[Dict[str, Any]] = []
    for bruta in (saida or "").splitlines():
        linha = bruta.strip()
        if not linha or linha.startswith("-") or linha.upper().startswith("ONU-ID"):
            continue
        m = _ONU_ID_RE.search(linha)
        if not m:
            continue
        macs = _MAC_RE.findall(linha)
        toks = re.split(r"\s+", linha)
        partes = _partes_do_onu_id(toks[0])

        llid = toks[1] if len(toks) > 1 else ""
        status = ""
        for t in toks:
            if t.lower() in ("online", "offline"):
                status = t.lower()
                break
        # RTT e o primeiro inteiro depois do MAC
        rtt = ""
        if macs:
            try:
                i = next(i for i, t in enumerate(toks) if _norm_mac(t) == _norm_mac(macs[0]))
                for t in toks[i + 1:]:
                    if re.fullmatch(r"-?\d+", t):
                        rtt = t
                        break
            except StopIteration:
                pass

        linhas.append({
            "pon": partes["pon"],
            "onu_id": partes["onu_id"],
            "llid": llid if re.fullmatch(r"-?\d+", llid or "") else "",
            "onu_mac": _norm_mac(macs[0]) if macs else "",
            "onu_serial": _norm_mac(macs[0]) if macs else "",
            "oper_status": "up" if status == "online" else ("down" if status == "offline" else ""),
            "status": status,
            "rtt": rtt,
            "distance_km": _rtt_para_km(rtt),
        })
    return linhas


def parse_onu_basic_info(saida: str) -> Dict[str, Dict[str, Any]]:
    """Le `show onu basic-info`: fabricante, modelo e versoes. Chave: 'pon:onu'."""
    fora: Dict[str, Dict[str, Any]] = {}
    for bruta in (saida or "").splitlines():
        linha = bruta.strip()
        if not linha or linha.startswith("-") or linha.upper().startswith("ONU-ID"):
            continue
        m = _ONU_ID_RE.search(linha)
        if not m:
            continue
        toks = re.split(r"\s+", linha)
        partes = _partes_do_onu_id(toks[0])
        chave = f"{partes['pon']}:{partes['onu_id']}"
        fora[chave] = {
            "vendor": toks[1] if len(toks) > 1 else "",
            "modelo": toks[2] if len(toks) > 2 else "",
            "onu_hw_id": toks[3] if len(toks) > 3 else "",
            "hw_version": toks[4] if len(toks) > 4 else "",
            "sw_version": toks[5] if len(toks) > 5 else "",
            "onu_type": toks[6] if len(toks) > 6 else "",
        }
    return fora


def parse_onu_discover(saida: str) -> List[Dict[str, Any]]:
    """Le `show onu discover`: ONUs vistas na fibra e ainda nao autorizadas."""
    achadas: List[Dict[str, Any]] = []
    for bruta in (saida or "").splitlines():
        linha = bruta.strip()
        if not linha or linha.startswith("-"):
            continue
        macs = _MAC_RE.findall(linha)
        if not macs:
            continue
        partes = _partes_do_onu_id(linha)
        achadas.append({
            "pon": partes["pon"],
            "onu_mac": _norm_mac(macs[0]),
            "onu_serial": _norm_mac(macs[0]),
            "autorizada": False,
        })
    return achadas


def parse_onu_mac_table(saida: str) -> List[Dict[str, Any]]:
    """Le `show onu <id> mac-address-table`: os MACs dos equipamentos do cliente.

    E o que faltava para o inventario servir: `show onu auth-info` da o MAC da
    ONU, e a ONU nao aparece na rede do cliente. Quem aparece no ARP do conector
    -- e portanto quem casa camera com ONU -- e o CPE atras dela.

    Formato capturado na OLT de Japaratinga em 20/08/2026:

        epon-olt(config-pon-0/1)# show onu 1 mac-address-table
         Mac Address Table
        ----------------------------------------------------------
        Index   VLAN   MAC  Address         PON       ONU    Aging(s)
        1       1000   54:6c:ac:25:e6:cf    EPON0/1   1      255

         Total Addresses Found in System :4

    Mesmo com o formato conhecido o parser nao conta colunas fixas: pega o MAC
    por expressao regular e deduz a VLAN pela posicao relativa a ele. A linha
    tem tres inteiros (Index, VLAN, Aging) e escolher pelo unico numero daria
    errado -- VLAN errada polui o inventario calada.
    """
    achados: List[Dict[str, Any]] = []
    vistos = set()
    for bruta in (saida or "").splitlines():
        linha = bruta.strip()
        if not linha or linha[0] in "-=+|":
            continue
        baixo = linha.lower()
        if "mac-address-table" in baixo:
            continue                      # eco do comando digitado
        if baixo.startswith(("mac", "vlan", "total", "no.", "index", "onu-id")):
            continue                      # cabecalho
        macs = _MAC_RE.findall(linha)
        if not macs:
            continue
        mac = _norm_mac(macs[0])
        if not mac or mac in vistos:
            continue
        vistos.add(mac)
        achados.append({"cpe_mac": mac, "vlan": _vlan_da_linha(linha, macs[0])})
    return achados


def _vlan_da_linha(linha: str, mac_bruto: str) -> str:
    """VLAN de uma linha da tabela de MAC.

    Nesta OLT a VLAN e o token imediatamente a esquerda do MAC (`Index VLAN MAC
    ...`). O rotulo explicito vem antes na ordem de tentativa porque outro
    firmware pode imprimir `VLAN: 1000` em vez de coluna. Se nenhuma das duas
    formas se aplicar, sobra o caso do unico inteiro plausivel na linha; nao
    havendo nem isso, devolve vazio em vez de chutar.
    """
    m = re.search(r"vlan[\s:=]+(\d{1,4})\b", linha, re.I)
    if m and 1 <= int(m.group(1)) <= 4094:
        return m.group(1)

    toks = re.split(r"\s+", linha.strip())
    try:
        i = next(i for i, t in enumerate(toks) if _norm_mac(t) == _norm_mac(mac_bruto))
    except StopIteration:
        i = -1
    if i > 0 and re.fullmatch(r"\d{1,4}", toks[i - 1]) and 1 <= int(toks[i - 1]) <= 4094:
        return toks[i - 1]

    numeros = [t for t in toks
               if re.fullmatch(r"\d{1,4}", t) and 1 <= int(t) <= 4094]
    return numeros[0] if len(numeros) == 1 else ""


def parse_onu_opm_diag(saida: str) -> Dict[str, Dict[str, Any]]:
    """Le `show onu opm-diag` -- potencia optica real de TODA a PON numa
    tabela so (mais rapido que uma consulta por ONU, e o comando certo:
    `show onu <id> ctc pon monitor_status`, usado antes, so informa se o
    monitoramento periodico esta ligado/desligado, nunca a leitura real --
    nesta OLT ele vem sempre 'disable', entao onu_rx nunca aparecia).

    Devolve um dict indexado por onu_id (string), cada valor com onu_rx,
    onu_tx, temperatura, voltagem, bias -- mesmos nomes de campo que a
    funcao antiga produzia, pra nao precisar mudar quem consome.
    """
    dados: Dict[str, Dict[str, Any]] = {}
    padrao = re.compile(
        r"^EPON\S+:(\d+)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+"
        r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
    )
    for bruta in (saida or "").splitlines():
        m = padrao.match(bruta.strip())
        if not m:
            continue
        onu_id, temp, volt, bias, tx, rx = m.groups()
        dados[onu_id] = {
            "temperatura": temp,
            "voltagem": volt,
            "bias": bias,
            "onu_tx": tx,
            "onu_rx": rx,
        }
    return dados


# ----------------------------------------------------------------- comandos

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
    -- tenta ler de volta ('show onu auth-info') ate 6 vezes, 5s entre
    tentativas (30s no total), antes de desistir de informar a posicao.
    Validado ao vivo (Japaratinga): o registro real da OLT levou entre 10 e
    30s pra aparecer, entao uma janela curta deixava 'pending' aparecer
    quase sempre. A autorizacao ja aconteceu de qualquer jeito nesse caso;
    so a posicao fica pendente."""
    alvo = _rotulo_da_pon(pon)
    mac_norm = _norm_mac(mac)
    if not mac_norm:
        return {"ok": False, "error": "mac e obrigatorio para autorizar ONU nesta OLT"}

    def tarefa(chan):
        _entra_na_pon(chan, alvo, timeout=timeout)
        saida = _manda(chan, f"onu mac-auth add {mac_norm}", ["config-pon"], timeout=timeout)
        if _comando_falhou(saida):
            return {"ok": False, "error": f"a OLT recusou autorizar o MAC {mac_norm}: {saida.strip()[:300]}"}

        onu_id = ""
        for _ in range(6):
            time.sleep(5)
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


# ------------------------------------------------------------------- sessao

def _prompt_da_pon(saida: str) -> str:
    """Extrai '0/2' de 'epon-olt(config-pon-0/2)#'.

    E o unico jeito confiavel de saber em que PON a sessao esta: entrar numa PON
    inexistente NAO da erro nesta OLT -- o CLI ignora e permanece no contexto
    anterior. Sem conferir o prompt, o driver leria a mesma PON varias vezes e
    duplicaria ONUs.
    """
    m = re.search(r"\(config-pon-(\d+/\d+)\)", saida or "")
    return m.group(1) if m else ""


def _com_sessao_vsol(olt_ip: str, user: str, password: str, port: int, timeout: float, tarefa):
    client, chan = _abre_shell_vsol(olt_ip, user, password, port=port, timeout=timeout)
    try:
        _login_vsol(chan, user=user, password=password, timeout=timeout)
        return tarefa(chan)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _pons_existentes(chan, pon: str = "all", maximo: int = 8, timeout: float = 15.0) -> List[str]:
    """Descobre quais PONs a OLT realmente tem.

    Medido na OLT de Japaratinga: existem 4 (0/1 a 0/4); da 0/5 em diante o
    comando e ignorado silenciosamente. Por isso a deteccao e pelo prompt.
    """
    pedido = str(pon or "all").strip().lower()
    if pedido and pedido not in ("all", "todas"):
        return [_rotulo_da_pon(pedido)]

    achadas: List[str] = []
    _volta_ao_topo(chan, timeout=timeout)
    _manda(chan, "configure terminal", ["(config)#"], timeout=timeout)
    for i in range(1, maximo + 1):
        alvo = "0/%d" % i
        saida = _manda(chan, "interface epon %s" % alvo, ["config-pon", "(config)#"], timeout=timeout)
        if _prompt_da_pon(saida) != alvo:
            break            # o CLI nao mudou de contexto: essa PON nao existe
        achadas.append(alvo)
    return achadas


def _le_pon(chan, pon: str, timeout: float = 15.0) -> List[Dict[str, Any]]:
    """Le uma PON e devolve uma linha por ONU, ja com fabricante e modelo."""
    _entra_na_pon(chan, pon, timeout=timeout)
    auth = _manda(chan, "show onu auth-info", ["config-pon"], timeout=max(30.0, timeout * 2))
    basic = _manda(chan, "show onu basic-info", ["config-pon"], timeout=max(30.0, timeout * 2))
    detalhes = parse_onu_basic_info(basic)
    linhas = parse_onu_auth_info(auth)
    for linha in linhas:
        extra = detalhes.get("%s:%s" % (linha["pon"], linha["onu_id"])) or {}
        linha.update({k: v for k, v in extra.items() if v})
    return linhas


def _le_macs_da_onu(chan, onu_id, timeout: float = 15.0) -> List[Dict[str, Any]]:
    """MACs aprendidos atras de uma ONU. Exige estar no contexto da PON dela."""
    bruto = _manda(chan, "show onu %s mac-address-table" % onu_id,
                   ["config-pon"], timeout=max(20.0, timeout * 2))
    baixo = bruto.lower()
    if "invalid" in baixo or "unknown command" in baixo:
        raise RuntimeError("a OLT nao aceitou 'show onu %s mac-address-table'" % onu_id)
    return parse_onu_mac_table(bruto)


# --------------------------------------------------------------- inventario

def _linhas_por_cpe(chan, onu: Dict[str, Any], timeout: float = 15.0) -> List[Dict[str, Any]]:
    """Explode uma ONU em uma linha por CPE, como fazem os outros drivers.

    Sem CPE a coleta e inutil no SightOps: a trava de `olt_service` cruza os
    MACs coletados com o ARP do conector e aborta quando nenhum casa (era este
    o erro "os MACs coletados nao batem com o conector selecionado").

    ONU offline nao e consultada -- nao ha o que aprender e a consulta so gasta
    o timeout. Quando nao ha CPE (offline, ou ligada sem ninguem na porta) a ONU
    sai mesmo assim, com o proprio MAC como chave: ficar de fora do relatorio e
    pior do que aparecer sem trafego.
    """
    onu_mac = _norm_mac(onu.get("onu_mac"))
    up = onu.get("oper_status") == "up"
    cpes: List[Dict[str, Any]] = []
    if up:
        try:
            cpes = _le_macs_da_onu(chan, onu["onu_id"], timeout=timeout)
        except Exception:
            cpes = []              # uma ONU que nao responde nao derruba a coleta
    sem_a_propria = [c for c in cpes if _norm_mac(c.get("cpe_mac")) != onu_mac]
    cpes = sem_a_propria or cpes

    base = dict(onu)
    base.update(_campos_de_topologia(onu, up))

    if not cpes:
        linha = dict(base)
        linha.update({"cpe_mac": onu_mac, "vlan": "", "cpe_source": "onu-sem-trafego"})
        return [linha]

    saida: List[Dict[str, Any]] = []
    for cpe in cpes:
        linha = dict(base)
        linha.update({
            "cpe_mac": cpe.get("cpe_mac", ""),
            "vlan": cpe.get("vlan", ""),
            "cpe_source": "mac-address-table",
        })
        saida.append(linha)
    return saida


def _campos_de_topologia(onu: Dict[str, Any], up: bool) -> Dict[str, Any]:
    """Renomeia os campos da ONU para os nomes que o cruzamento do sistema le.

    `_sync_camera_inventory_from_olt_rows` (olt_service) copia a topologia da
    linha da OLT para a camera de mesmo MAC, e procura por nomes fixos:
    `onu_model`, `oper_status`, `omci_status`. Este driver produzia `modelo`
    (do `show onu basic-info`) e `up`/`down` -- nomes proprios, que o cruzamento
    ignorava em silencio: a camera casava mas ficava sem modelo e sem estado.

    O vocabulario de estado e o do 4840e (`Active`/`Offline`, `OK`/`LOS`), que e
    o que a tela ja sabe exibir.
    """
    return {
        "onu_model": onu.get("modelo") or onu.get("onu_model") or "",
        "oper_status": "Active" if up else "Offline",
        "omci_status": "OK" if up else "LOS",
    }


def collect_macs_vsol(
    olt_ip: str,
    user: str,
    password: str,
    pon: str = "all",
    olt_name: Optional[str] = None,
    port: int = 22,
    timeout: float = 12.0,
) -> List[Dict[str, Any]]:
    """Inventario de ONUs da OLT VSOL, no formato usado pelos outros drivers."""
    olt_ip = (olt_ip or "").strip()
    if not olt_ip or not (user or "").strip():
        raise ValueError("olt_ip e user sao obrigatorios")

    def tarefa(chan):
        linhas: List[Dict[str, Any]] = []
        for alvo in _pons_existentes(chan, pon, timeout=timeout):
            for linha in _le_pon(chan, alvo, timeout=timeout):
                linha.update({
                    "olt_ip": olt_ip,
                    "olt_name": olt_name or "OLT-VSOL",
                    "olt_model": "vsol_epon",
                    "onu_name": linha.get("onu_name") or "epon %s onu %s" % (linha["pon"], linha["onu_id"]),
                    "source": "olt-vsol",
                })
                linhas.extend(_linhas_por_cpe(chan, linha, timeout=timeout))
        return linhas

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)


# --------------------------------------------------------------- telemetria

def collect_onu_telemetry_vsol(
    olt_ip: str,
    user: str,
    password: str,
    pon: str = "all",
    port: int = 22,
    timeout: float = 12.0,
) -> List[Dict[str, Any]]:
    """Telemetria por ONU: estado, distancia e potencia optica.

    O sinal vem de `show onu opm-diag`, uma consulta so por PON (traz todas
    as ONUs online de uma vez) -- mais rapido que consultar ONU por ONU.
    """
    def tarefa(chan):
        saida: List[Dict[str, Any]] = []
        for alvo in _pons_existentes(chan, pon, timeout=timeout):
            try:
                bruto_pon = _manda(chan, "show onu opm-diag", ["config-pon"], timeout=max(20.0, timeout * 2))
                opticos_pon = parse_onu_opm_diag(bruto_pon)
            except Exception:
                opticos_pon = {}     # PON que nao responde nao derruba a coleta
            for linha in _le_pon(chan, alvo, timeout=timeout):
                up = linha.get("oper_status") == "up"
                optico = opticos_pon.get(str(linha.get("onu_id") or ""), {}) if up else {}
                try:
                    pon_num = int(str(linha["pon"]).split("/")[-1])
                except (ValueError, IndexError):
                    pon_num = 0
                saida.append({
                    "pon": pon_num,
                    "pon_label": linha["pon"],
                    "onu_id": int(linha["onu_id"] or 0),
                    "serial": linha.get("onu_mac", ""),
                    "name": linha.get("onu_name", ""),
                    "oper_status": "Active" if up else "Offline",
                    "omci_status": "OK" if up else "LOS",
                    "rx_onu": optico.get("onu_rx", ""),
                    "rx_olt": "",     # esta OLT nao informa a potencia recebida pela OLT
                    "distance_km": linha.get("distance_km", ""),
                })
        saida.sort(key=lambda r: (r["pon"], r["onu_id"]))
        return saida

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)


# ------------------------------------------------------- descoberta e busca

def discover_onus_vsol(
    olt_ip: str, user: str, password: str, pon: str = "all",
    port: int = 22, timeout: float = 12.0,
) -> Dict[str, Any]:
    """ONUs vistas na fibra e ainda nao autorizadas.

    `show onu discover` lista TODA ONU com o link OAM/MPCP completo,
    autorizada ou nao -- nao e uma lista de pendentes. Com `onu auth-mode
    disable` isso nunca aparecia (a ONU virava autorizada na hora, sem
    passar tempo nenhum "so descoberta"); com `mac-auth` ativo (a partir de
    2026-09-01, ver docs/HANDOFF_AGENTES.md) uma ONU ja autorizada continua
    aparecendo aqui pra sempre, porque o OAM/MPCP dela segue completo.
    Por isso cruza contra `show onu auth-info` (via `_le_pon`) e tira quem
    ja esta autorizado antes de devolver -- senao a tela mostra toda ONU ja
    em producao como "descoberta, clique pra autorizar".
    """
    def tarefa(chan):
        achadas: List[Dict[str, Any]] = []
        for alvo in _pons_existentes(chan, pon, timeout=timeout):
            _entra_na_pon(chan, alvo, timeout=timeout)
            ja_autorizados = {
                _norm_mac(l.get("onu_mac"))
                for l in _le_pon(chan, alvo, timeout=timeout)
                if _norm_mac(l.get("onu_mac"))
            }
            bruto = _manda(chan, "show onu discover", ["config-pon"], timeout=max(30.0, timeout * 2))
            for item in parse_onu_discover(bruto):
                if _norm_mac(item.get("onu_mac")) in ja_autorizados:
                    continue
                item["pon"] = item.get("pon") or alvo
                achadas.append(item)
        return {"ok": True, "onus": achadas, "total": len(achadas)}

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)


def find_onu_vsol(
    olt_ip: str, user: str, password: str, serial: str,
    port: int = 22, timeout: float = 12.0,
) -> Optional[Dict[str, Any]]:
    """Localiza uma ONU pelo MAC (nesta OLT o 'serial' e o MAC)."""
    alvo_mac = _norm_mac(serial)
    if not alvo_mac:
        return None

    def tarefa(chan):
        for pon in _pons_existentes(chan, "all", timeout=timeout):
            for linha in _le_pon(chan, pon, timeout=timeout):
                if _norm_mac(linha.get("onu_mac")) == alvo_mac:
                    return linha
        return None

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)


def onu_signal_vsol(
    olt_ip: str, user: str, password: str, pon: str, onu_id: Any,
    port: int = 22, timeout: float = 12.0,
) -> Dict[str, Any]:
    """Sinal optico e dados de uma ONU especifica."""
    alvo = _rotulo_da_pon(pon)

    def tarefa(chan):
        _entra_na_pon(chan, alvo, timeout=timeout)
        bruto = _manda(chan, "show onu opm-diag", ["config-pon"], timeout=max(20.0, timeout * 2))
        opticos = parse_onu_opm_diag(bruto)
        dados = dict(opticos.get(str(onu_id), {}))
        atual = next((l for l in _le_pon(chan, alvo, timeout=timeout)
                      if str(l.get("onu_id")) == str(onu_id)), {})
        dados.update({
            "pon": alvo, "onu_id": str(onu_id),
            "onu_mac": atual.get("onu_mac", ""),
            "oper_status": atual.get("oper_status", ""),
            "distance_km": atual.get("distance_km", ""),
        })
        return dados

    return _com_sessao_vsol(olt_ip, user, password, port, timeout, tarefa)
