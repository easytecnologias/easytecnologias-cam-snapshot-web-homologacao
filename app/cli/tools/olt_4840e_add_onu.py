#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provisionamento de ONU na OLT Intelbras 4840E (EPON), via a mesma sessao
SSH interativa que olt_4840e_collect_macs.py ja usa (importa os helpers de
conexao de la -- _open_shell/_ensure_logged_in/_ensure_enable/_cli/_norm_mac
-- em vez de duplicar, esse arquivo NUNCA e tocado por este driver).

Sequencias de comando validadas contra a OLT real (cliente RADS, OLT
SANTANA) e contra o roteiro operacional do proprio tecnico do cliente + o
manual oficial Intelbras 4840E (secoes 9.1-9.11):

    interface pon 0/<pon>
    onu-authenticate mode mac-auth white-list      -> so se ainda nao estiver
    white-list add mac <mac>                       -> autoriza, OLT atribui onu-id
    show white-list                                -> le de volta o onu-id atribuido
    onu 0/<pon>/<onu>
    onu-description <texto>
    interface ethernet <porta>
    onu-vlan-mode tag vlan <vlan>
    onu-p2p                                        -> libera pra transmitir (camera)
    copy running-config startup-config             -> salva, senao perde no reboot

    no onu-binding onu 0/<pon>/<onu>                -> exclusao, passo 1
    white-list del mac <mac>                        -> exclusao, passo 2 (dentro da PON)

    onu-reboot                                      -> dentro do contexto onu, pede y/n

    show onu-status [mac <mac>]                     -> lista/consulta (MAC, Rtt, estado)
    show onu-opm-diagnosis                          -> dentro do contexto onu (RX/TX power)
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.cli.tools.olt_4840e_collect_macs import (
    _cli,
    _ensure_enable,
    _ensure_logged_in,
    _norm_mac,
    _open_shell,
    _parse_mac_table_onu,
)

_FAILURE_MARKERS = (
    "invalid parameter",
    "incomplete command",
    "unrecognized command",
)


def command_failed(output: str) -> bool:
    low = (output or "").strip().lower()
    return any(marker in low for marker in _FAILURE_MARKERS)


_MAC_SHAPE_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _validate_mac_shape(mac_norm: str) -> None:
    """`_norm_mac` (em olt_4840e_collect_macs.py, fora de escopo pra editar
    aqui) so normaliza separadores/caixa -- nao rejeita lixo. Sem essa
    checagem, um MAC invalido vira parte literal de um comando CLI
    (`white-list add mac <mac>` / `show onu-status mac <mac>`), incluindo a
    possibilidade de newline embutido virando um segundo comando na OLT."""
    if not _MAC_SHAPE_RE.match(mac_norm):
        raise ValueError(f"MAC invalido: {mac_norm!r}")


_CONFIRM_YN_RE = re.compile(r"\(y/n\)\??\s*\[n\]", re.IGNORECASE)
_GENERIC_PROMPT_RE = re.compile(r"(?:\r?\n)?[^\n]{0,120}(?:\([^\)]*\))?[>#]\s*$")


def _cli_confirm_save(chan, timeout: float = 25.0) -> bool:
    """Salva a config ('copy running-config startup-config'), respondendo
    a confirmacao y/n que esta OLT pede -- validado ao vivo (equipamento
    real, OLT BARRA DE SAO MIGUEL): sem responder essa confirmacao, o
    comando fica pendurado esperando resposta, a sessao SSH fecha antes
    dela chegar, a OLT assume o default '[n]' (nao salva), e a config
    NUNCA e persistida na flash -- mesmo que o driver reporte 'saved:
    True' (o texto de confirmacao nao bate com nenhum _FAILURE_MARKERS,
    entao 'command_failed()' nao pegava esse caso)."""
    while chan.recv_ready():
        chan.recv(65535)
    chan.send("copy running-config startup-config\n")
    buf = ""
    answered = False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if chan.recv_ready():
            buf += chan.recv(65535).decode("utf-8", errors="ignore")
            if not answered and _CONFIRM_YN_RE.search(buf):
                chan.send("y\n")
                answered = True
                buf = ""
                time.sleep(0.2)
                continue
            if answered and _GENERIC_PROMPT_RE.search(buf):
                return not command_failed(buf)
        time.sleep(0.05)
    return False


class OnuAddError(Exception):
    """Erro ao autorizar/excluir/reiniciar ONU -- carrega o que ja foi
    aplicado. `onu` fica preenchido quando o onu-id ja foi lido de volta
    (whitelist confirmada), mesmo que um passo seguinte (VLAN, p2p) falhe --
    sinal de que a ONU ja esta autorizada, so a config adicional que nao
    completou."""

    def __init__(self, message: str, failed_command: str, commands_run: List[str], onu: Optional[int] = None) -> None:
        super().__init__(message)
        self.failed_command = failed_command
        self.commands_run = commands_run
        self.onu = onu


_ONU_STATUS_LINE_RE = re.compile(
    r"^(?P<slot>\d+)/(?P<pon>\d+)/(?P<onu>\d+)\s+"
    r"(?P<mac>(?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s+"
    r"(?P<dist>-|\d+)\s+"
    r"(?P<register>-|\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<type>\S+)\s+(?P<software>\S+)\s+(?P<state>Up|Down)\s*$",
    re.IGNORECASE,
)


def _parse_onu_status(output: str) -> List[Dict[str, Any]]:
    """Parseia 'show onu-status' (lista, uma PON ou tudo) e 'show onu-status
    mac <mac>' (uma linha so). Formato real (validado ao vivo):

        0/1/1  30:e1:f1:3e:a0:3f 2555   26/08/28 05:45:20 other 1.3-220719 Up
        0/1/13 80:85:44:5f:32:f8 -      -                 other -          Down
    """
    rows: List[Dict[str, Any]] = []
    for raw in (output or "").splitlines():
        m = _ONU_STATUS_LINE_RE.match(raw.strip())
        if not m:
            continue
        rows.append({
            "pon": int(m.group("pon")),
            "onu": int(m.group("onu")),
            "mac": _norm_mac(m.group("mac")),
            "distance_m": None if m.group("dist") == "-" else int(m.group("dist")),
            "register_time": "" if m.group("register") == "-" else re.sub(r"\s+", " ", m.group("register")),
            "type": m.group("type"),
            "software": m.group("software"),
            "state": m.group("state").capitalize(),
        })
    return rows


_WHITE_LIST_LINE_RE = re.compile(
    r"^pon-(?P<pon>\d+)/(?P<pon2>\d+)\s+(?P<index>\d+)\s+"
    r"(?P<mac>(?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s*$",
    re.IGNORECASE,
)


def _parse_white_list(output: str) -> List[Dict[str, Any]]:
    """Parseia 'show white-list'. Formato real (validado no manual):

        WHITE LIST:
        Port Index Mac Address
        pon-0/1 1 00:0a:5a:00:01:01
        Total white-list entries: 1 .

    A coluna 'Port' vem como 'pon-<slot>/<pon>', nao so '<pon>'.
    """
    rows: List[Dict[str, Any]] = []
    for raw in (output or "").splitlines():
        m = _WHITE_LIST_LINE_RE.match(raw.strip())
        if not m:
            continue
        rows.append({
            "pon": int(m.group("pon2")),
            "index": int(m.group("index")),
            "mac": _norm_mac(m.group("mac")),
        })
    return rows


def _parse_opm_diagnosis(output: str) -> Dict[str, Any]:
    """Parseia 'show onu-opm-diagnosis' (dentro do contexto 'onu <endereco>').
    Formato real (validado no manual):

        ONU: 0/4/1
        Optical Transceiver Diagnosis :
        Work Temperature : 38.25 C
        Supply Voltage(Vcc) : 3.29 V
        TX Bias Current : 16.99 mA
        TX Power(Output) : 1.445 mW (3.00 dBm)
        RX Power(Input) : 0.573 mW (-2.40 dBm)
    """
    result: Dict[str, Any] = {}
    for raw in (output or "").splitlines():
        line = raw.strip()
        m = re.search(r"Work Temperature\s*:\s*([\-\d.]+)\s*C", line, re.IGNORECASE)
        if m:
            result["temperature_c"] = float(m.group(1))
            continue
        m = re.search(r"Supply Voltage.*:\s*([\-\d.]+)\s*V", line, re.IGNORECASE)
        if m:
            result["voltage_v"] = float(m.group(1))
            continue
        m = re.search(r"TX Bias Current\s*:\s*([\-\d.]+)\s*mA", line, re.IGNORECASE)
        if m:
            result["tx_bias_ma"] = float(m.group(1))
            continue
        m = re.search(r"TX Power.*\(([\-\d.]+)\s*dBm\)", line, re.IGNORECASE)
        if m:
            result["tx_power_dbm"] = float(m.group(1))
            continue
        m = re.search(r"RX Power.*\(([\-\d.]+)\s*dBm\)", line, re.IGNORECASE)
        if m:
            result["rx_power_dbm"] = float(m.group(1))
            continue
    return result


def _classify_auth_mode(output: str) -> str:
    """Classifica a saida de 'show onu-authenticate mode' (dentro do
    contexto da PON) em 'mac-auth', 'loid-auth', 'hybrid-auth' ou
    'disable'. So autoriza via whitelist quando ja for 'mac-auth' ou
    'disable' (ainda nao configurado) -- 'loid-auth'/'hybrid-auth' sao
    esquemas diferentes que este driver nao deve sobrescrever sozinho."""
    low = (output or "").lower()
    if "mac-auth" in low:
        return "mac-auth"
    if "loid-auth" in low:
        return "loid-auth"
    if "hybrid-auth" in low:
        return "hybrid-auth"
    return "disable"


def _connect_and_login(olt_ip: str, user: str, password: str, port: int, timeout: float):
    """Abre a sessao e loga/eleva privilegio. Se o login ou o 'enable'
    falharem DEPOIS do socket/shell abrir, fecha a conexao antes de propagar
    o erro -- senao ela vaza (nenhum chamador tem uma referencia pra fechar,
    porque a excecao acontece antes do 'return')."""
    client, chan = _open_shell(olt_ip, user, password, port=port, timeout=timeout)
    try:
        _ensure_logged_in(chan, user=user, password=password, timeout=timeout)
        _ensure_enable(chan, password=password, timeout=timeout)
    except Exception:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        raise
    return client, chan


def find_onu_4840e(
    olt_ip: str, user: str, password: str, mac: str, port: int = 22, timeout: float = 12.0,
) -> Optional[Dict[str, Any]]:
    """Localiza uma ONU ja autorizada pelo MAC ('show onu-status mac <mac>').
    Retorna None se nao achar."""
    mac_norm = _norm_mac(mac)
    _validate_mac_shape(mac_norm)
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    try:
        _cli(chan, "conf t", timeout=timeout)
        out = _cli(chan, f"show onu-status mac {mac_norm}", timeout=timeout)
        rows = _parse_onu_status(out)
        return rows[0] if rows else None
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def onu_signal_4840e(
    olt_ip: str, user: str, password: str, pon: int, onu: int, port: int = 22, timeout: float = 12.0,
) -> Dict[str, Any]:
    """Consulta status (Rtt/distancia/estado) + diagnostico optico
    (RX/TX power/temperatura/tensao) de uma ONU ja autorizada."""
    addr = f"0/{pon}/{onu}"
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    try:
        _cli(chan, "conf t", timeout=timeout)
        status_out = _cli(chan, "show onu-status", timeout=timeout)
        rows = _parse_onu_status(status_out)
        match = next((r for r in rows if r["pon"] == pon and r["onu"] == onu), None)
        if not match:
            return {"ok": False, "error": f"ONU {addr} nao encontrada em 'show onu-status'."}

        cmd = f"onu {addr}"
        out = _cli(chan, cmd, timeout=timeout)
        if command_failed(out):
            return {"ok": False, "error": f"Falha ao entrar no contexto {addr}: {out.strip()[:300]}"}
        diag_out = _cli(chan, "show onu-opm-diagnosis", timeout=timeout)
        diag = _parse_opm_diagnosis(diag_out)
        _cli(chan, "exit", timeout=timeout)

        # MACs das cameras/dispositivos atras da ONU (nao o MAC da propria
        # ONU, que ja veio de 'show onu-status' acima) -- mesmo comando
        # ja validado em collect_macs_4840e: precisa do contexto
        # 'interface pon' (nao o contexto 'onu' usado para o diagnostico
        # optico acima).
        macs: List[Dict[str, Any]] = []
        iface_out = _cli(chan, f"interface pon 0/{pon}", timeout=timeout)
        if not command_failed(iface_out):
            mac_out = _cli(chan, f"show mac-address-table onu {addr}", timeout=max(30.0, timeout * 2))
            for row in _parse_mac_table_onu(mac_out):
                macs.append({
                    "mac": row.get("cpe_mac", ""),
                    "interface": f"VLAN {row.get('vlan', '-')} ({row.get('status', '-')})",
                })
            _cli(chan, "exit", timeout=timeout)

        return {
            "ok": True,
            "pon": pon,
            "onu": onu,
            "mac": match["mac"],
            "distance_m": match["distance_m"],
            "register_time": match["register_time"],
            "state": match["state"],
            "software": match["software"],
            "macs": macs,
            **diag,
        }
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def _pon_range(pon: str) -> List[int]:
    p = (pon or "all").strip().lower()
    if p == "all":
        return [1, 2, 3, 4]
    return [int(p)]


def discover_onus_4840e(
    olt_ip: str, user: str, password: str, pon: str = "all", port: int = 22, timeout: float = 12.0,
) -> Dict[str, Any]:
    """Descobre MACs vistos fisicamente na PON mas ainda fora da whitelist
    (candidatas a autorizar). Cruza 'show onu-status' (tudo que ja foi
    visto, autorizado ou nao -- os nao-autorizados aparecem com State=Down
    e sem RTT) com 'show white-list' (o que ja esta autorizado)."""
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    try:
        pons_out: Dict[str, Any] = {}
        for p in _pon_range(pon):
            _cli(chan, "conf t", timeout=timeout)
            iface_out = _cli(chan, f"interface pon 0/{p}", timeout=timeout)
            if command_failed(iface_out):
                pons_out[str(p)] = {"discovered": [], "error": f"Falha ao entrar na PON {p}: {iface_out.strip()[:200]}"}
                try:
                    _cli(chan, "exit", timeout=timeout)
                except Exception:
                    pass
                continue
            status_rows = _parse_onu_status(_cli(chan, "show onu-status", timeout=timeout))
            white_rows = _parse_white_list(_cli(chan, "show white-list", timeout=timeout))
            _cli(chan, "exit", timeout=timeout)

            whitelisted_macs = {row["mac"] for row in white_rows if row["pon"] == p}
            discovered = [
                {"pon": row["pon"], "mac": row["mac"], "state": row["state"]}
                for row in status_rows
                if row["pon"] == p and row["state"] == "Down" and row["mac"] not in whitelisted_macs
            ]
            pons_out[str(p)] = {"discovered": discovered}
        return {"ok": True, "pons": pons_out}
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def add_onu_4840e(
    olt_ip: str,
    user: str,
    password: str,
    pon: int,
    mac: str,
    description: str = "",
    ports: Optional[List[Dict[str, Any]]] = None,
    port: int = 22,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Autoriza uma ONU pelo MAC (whitelist), aplica descricao e VLAN por
    porta ethernet, libera p2p (camera) e salva a config. Equipamento vivo.

    A OLT auto-atribui o onu-id ao dar 'white-list add mac' -- essa funcao
    le esse id de volta via 'show white-list' antes de continuar, nunca
    escolhe a posicao manualmente (fora de escopo desta entrega)."""
    mac_norm = _norm_mac(mac)
    _validate_mac_shape(mac_norm)
    description = re.sub(r"[\r\n]+", " ", description or "").strip()
    ports = ports or [{"port": 1, "vlan": None}]
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    commands_run: List[str] = []
    try:
        cmd = "conf t"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)

        cmd = f"interface pon 0/{pon}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            raise OnuAddError(f"Falha ao entrar na PON {pon}: {out.strip()[:300]}", cmd, commands_run)

        cmd = "show onu-authenticate mode"
        mode_out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        mode = _classify_auth_mode(mode_out)
        if mode in ("loid-auth", "hybrid-auth"):
            raise OnuAddError(
                f"PON {pon} esta configurada com autenticacao '{mode}', nao mac-auth/white-list. "
                "Ajuste manual necessario na OLT antes de autorizar por aqui.",
                cmd, commands_run,
            )
        if mode != "mac-auth":
            cmd = "onu-authenticate mode mac-auth white-list"
            out = _cli(chan, cmd, timeout=timeout)
            commands_run.append(cmd)
            if command_failed(out):
                raise OnuAddError(f"Falha ao configurar autenticacao MAC/whitelist na PON {pon}: {out.strip()[:300]}", cmd, commands_run)

        cmd = f"white-list add mac {mac_norm}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            raise OnuAddError(f"Falha ao adicionar {mac_norm} na whitelist da PON {pon}: {out.strip()[:300]}", cmd, commands_run)

        cmd = "show white-list"
        wl_out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        wl_match = next((e for e in _parse_white_list(wl_out) if e["mac"] == mac_norm and e["pon"] == pon), None)
        if not wl_match:
            raise OnuAddError(
                f"MAC {mac_norm} adicionado na whitelist mas nao apareceu em 'show white-list' pra confirmar a posicao.",
                cmd, commands_run,
            )
        onu_id = wl_match["index"]

        cmd = "exit"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)

        addr = f"0/{pon}/{onu_id}"
        cmd = f"onu {addr}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            raise OnuAddError(f"ONU autorizada na whitelist ({addr}) mas falha ao entrar no contexto: {out.strip()[:300]}", cmd, commands_run, onu=onu_id)

        if description:
            cmd = f"onu-description {description}"
            out = _cli(chan, cmd, timeout=timeout)
            commands_run.append(cmd)
            if command_failed(out):
                raise OnuAddError(f"ONU autorizada, mas falha ao gravar descricao: {out.strip()[:300]}", cmd, commands_run, onu=onu_id)

        for entry in ports:
            vlan = entry.get("vlan")
            if not vlan:
                continue
            eth_port = int(entry.get("port") or 1)
            cmd = f"interface ethernet 0/{eth_port}"
            out = _cli(chan, cmd, timeout=timeout)
            commands_run.append(cmd)
            if command_failed(out):
                raise OnuAddError(f"ONU autorizada, mas falha ao entrar na porta ethernet {eth_port}: {out.strip()[:300]}", cmd, commands_run, onu=onu_id)
            cmd = f"onu-vlan-mode tag vlan {vlan}"
            out = _cli(chan, cmd, timeout=timeout)
            commands_run.append(cmd)
            if command_failed(out):
                raise OnuAddError(f"ONU autorizada, mas falha ao aplicar VLAN {vlan} na porta {eth_port}: {out.strip()[:300]}", cmd, commands_run, onu=onu_id)
            cmd = "exit"
            _cli(chan, cmd, timeout=timeout)
            commands_run.append(cmd)

        cmd = "onu-p2p"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            raise OnuAddError(f"ONU autorizada, mas falha ao liberar p2p (camera pode nao transmitir): {out.strip()[:300]}", cmd, commands_run, onu=onu_id)

        cmd = "exit"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        cmd = "end"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)

        cmd = "copy running-config startup-config"
        saved = _cli_confirm_save(chan, timeout=max(timeout, 25.0))
        commands_run.append(cmd)
        if saved:
            commands_run.append("y")

        return {"ok": True, "pon": pon, "onu": onu_id, "mac": mac_norm, "commands_run": commands_run, "saved": saved}
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def delete_onu_4840e(
    olt_ip: str, user: str, password: str, pon: int, onu: int, mac: str, port: int = 22, timeout: float = 22.0,
) -> Dict[str, Any]:
    """Exclui uma ONU ja autorizada: desvincula a posicao (onu-binding) E
    tira o MAC da whitelist da PON. Os dois passos sao necessarios --
    fazer so um deixa a ONU voltando a se registrar sozinha (so tirou a
    whitelist) ou com posicao fantasma (so tirou o binding)."""
    mac_norm = _norm_mac(mac)
    _validate_mac_shape(mac_norm)
    addr = f"0/{pon}/{onu}"
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    commands_run: List[str] = []
    try:
        cmd = "conf t"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)

        cmd = f"no onu-binding onu {addr}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        binding_failed = command_failed(out)

        cmd = f"interface pon 0/{pon}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            return {"ok": False, "pon": pon, "onu": onu, "commands_run": commands_run, "saved": False,
                    "error": f"Falha ao entrar na PON {pon} pra tirar da whitelist: {out.strip()[:300]}"}

        cmd = f"white-list del mac {mac_norm}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        whitelist_failed = command_failed(out)

        cmd = "exit"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        cmd = "end"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)

        ok = not (binding_failed or whitelist_failed)
        saved = False
        if ok:
            cmd = "copy running-config startup-config"
            saved = _cli_confirm_save(chan, timeout=max(timeout, 25.0))
            commands_run.append(cmd)
            if saved:
                commands_run.append("y")

        return {"ok": ok, "pon": pon, "onu": onu, "commands_run": commands_run, "saved": saved}
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def _cli_confirm_reboot(chan, cmd: str, timeout: float) -> Tuple[str, bool]:
    """So usada por reboot_onu_4840e. Manda `cmd`, espera o prompt de
    confirmacao '(y/n)?[n]' aparecer e responde 'y' explicitamente -- nunca
    conta com o proximo comando da fila pra responder (ver restricao de
    seguranca no topo do plano: foi exatamente essa suposicao que quase
    causou um reboot real da OLT inteira durante a investigacao).

    Devolve (output, answered) -- `answered` so vira True depois de ver o
    texto real de confirmacao E mandar 'y'. So retorna no prompt generico
    DEPOIS de confirmar (nunca antes -- um retorno prematuro, por causa de
    algum texto solto no buffer que bata com o prompt generico antes da
    confirmacao real aparecer, deixaria a OLT esperando resposta enquanto o
    driver ja fechou a sessao). Se o comando falhar antes mesmo de pedir
    confirmacao (erro de sintaxe etc.), retorna cedo com answered=False em
    vez de esperar o timeout inteiro."""
    while chan.recv_ready():
        chan.recv(65535)
    time.sleep(0.08)  # mesmo tempo de assentamento que _cli usa
    chan.send(cmd.rstrip() + "\n")

    buf = ""
    answered = False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if chan.recv_ready():
            buf += chan.recv(65535).decode("utf-8", errors="ignore")
            if not answered and _CONFIRM_YN_RE.search(buf):
                chan.send("y\n")
                answered = True
                buf = ""
                time.sleep(0.1)
                continue
            if answered and _GENERIC_PROMPT_RE.search(buf):
                return buf, True
            if not answered and command_failed(buf):
                return buf, False
        time.sleep(0.05)
    return buf, answered


def reboot_onu_4840e(
    olt_ip: str, user: str, password: str, pon: int, onu: int, port: int = 22, timeout: float = 20.0,
) -> Dict[str, Any]:
    """Reinicia uma ONU ja autorizada (dentro do contexto 'onu <endereco>',
    comando 'onu-reboot'). NUNCA envia o comando 'reboot' (sem 'onu-') --
    esse reinicia a OLT inteira, nao a ONU."""
    addr = f"0/{pon}/{onu}"
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    commands_run: List[str] = []
    try:
        cmd = "conf t"
        _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)

        cmd = f"onu {addr}"
        out = _cli(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            return {"ok": False, "pon": pon, "onu": onu, "commands_run": commands_run,
                    "error": f"Falha ao entrar no contexto {addr}: {out.strip()[:300]}"}

        cmd = "onu-reboot"
        out, answered = _cli_confirm_reboot(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if answered:
            commands_run.append("y")

        ok = answered and not command_failed(out)
        result: Dict[str, Any] = {"ok": ok, "pon": pon, "onu": onu, "command": cmd, "raw_output": out.strip()[:500], "commands_run": commands_run}
        if not ok:
            result["error"] = (
                f"Comando de reinicio nao foi confirmado pela OLT (sem 'y' de confirmacao): {out.strip()[:300]}"
                if not answered else f"Falha ao reiniciar a ONU {addr}: {out.strip()[:300]}"
            )
        return result
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def collect_onu_telemetry_4840e(
    olt_ip: str, user: str, password: str, pon: str = "all", port: int = 22, timeout: float = 12.0,
) -> List[Dict[str, Any]]:
    """Telemetria leve por ONU ja autorizada: 'show onu-status' pra todas as
    ONUs da(s) PON(s), sem diagnostico optico individual (isso ficaria caro
    -- uma sessao por ONU -- e nao e o que a coleta periodica precisa;
    'onu_signal_4840e' continua sendo o jeito de pedir RX/TX power de UMA
    ONU especifica sob demanda)."""
    client, chan = _connect_and_login(olt_ip, user, password, port, timeout)
    out: List[Dict[str, Any]] = []
    try:
        for p in _pon_range(pon):
            _cli(chan, "conf t", timeout=timeout)
            iface_out = _cli(chan, f"interface pon 0/{p}", timeout=timeout)
            if command_failed(iface_out):
                # PON com problema (numero invalido etc.) -- pula pra
                # proxima em vez de perder a telemetria das outras.
                try:
                    _cli(chan, "exit", timeout=timeout)
                except Exception:
                    pass
                continue
            rows = _parse_onu_status(_cli(chan, "show onu-status", timeout=timeout))
            _cli(chan, "exit", timeout=timeout)
            for row in rows:
                if row["pon"] != p:
                    continue
                out.append({
                    "pon": row["pon"],
                    "onu_id": row["onu"],
                    "serial": row["mac"],
                    "oper_status": row["state"],
                    "omci_status": "" ,
                    "rx_olt": "",
                    "rx_onu": "",
                    "distance_km": (round(row["distance_m"] / 1000.0, 3) if row["distance_m"] else ""),
                })
        return out
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
