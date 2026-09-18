#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ferramenta auxiliar: descobre ONUs nao autorizadas e autoriza uma ONU na
OLT Intelbras 8820i (GPON), via sessao SSH interativa (mesmo padrao de
olt_8820i_collect_macs.py -- essa OLT ja e acessada por SSH em producao
nesta base, prompt "intelbras-olt>").

Sequencia de comandos (validada contra a mesma OLT em C:\\PROJETOS\\telegram-olt-bot,
que fala com ela por Telnet e ja tem esse fluxo testado em campo):

    onu show                       -> descobre ONUs plugadas e nao autorizadas,
    onu show gpon <pon>               junto com as posicoes (slots) livres
    onu set gpon <pon> onu <slot> id <serno_id> meprof <profile>   -> autoriza
    onu description add gpon <pon> onu <slot> text <texto>        -> opcional
    bridge add gpon <pon> onu <slot> <service> vlan <vlan> \
        tagged|untagged eth 1                                     -> servico

Uso tipico (PowerShell):

    python .\\tools\\olt_8820i_add_onu.py `
      --olt-ip 10.80.80.5 --user admin --password admin `
      --discover --pon 1
"""

import argparse
import os
import re
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import paramiko  # type: ignore[import]


PROMPT = "intelbras-olt>"  # mesmo valor usado em olt_8820i_collect_macs.py
DEBUG_OLT_ADD_ONU = str(os.getenv("OLT_DEBUG_ADD_ONU", "0")).strip().lower() in ("1", "true", "yes", "on")

# Mapa de modelo de ONU -> perfil (meprof) da OLT. Mesmos valores usados em
# producao no bot de referencia (telegram-olt-bot/config.json).
DEFAULT_PROFILE = "intelbras-r1"
ONT_DEFAULT_PROFILE = "intelbras-120ac"
MODEL_PROFILES = {
    "110gi": "intelbras-r1",
    "110g": "intelbras-r1",
    "121ac": "intelbras-r1",
    "120ac": "intelbras-120ac",
    "142n": "intelbras-r1",
}

_FAILURE_MARKERS = (
    "failed",
    "invalid",
    "no such",
    "please set onu first",
    "entry not found",
    "error",
    "incomplete command",
)


class OnuAddError(Exception):
    """Erro ao autorizar ONU -- carrega quais comandos ja foram aplicados.

    `slot` fica preenchido quando o 'onu set' ja tinha sido concluido com
    sucesso e so o passo de bridge/servico falhou -- e o sinal de que da pra
    tentar recuperar so a bridge (ver `add_bridge_only`) em vez de refazer a
    autorizacao inteira.
    """

    def __init__(self, message: str, failed_command: str, commands_run: List[str], slot: Optional[int] = None) -> None:
        super().__init__(message)
        self.failed_command = failed_command
        self.commands_run = commands_run
        self.slot = slot


# A OLT so aceita UM tipo de bridge por VLAN -- quem chegou primeiro decide
# (validado contra OLT real: VLAN 3000 travada em 'tls', VLAN 300 travada em
# 'downlink', cada uma com varias ONUs diferentes usando o mesmo tipo). Se a
# OLT recusar por a VLAN ja ter o outro tipo, tenta uma vez com o oposto em
# vez de obrigar o tecnico a descobrir isso na mao.
_BRIDGE_TYPE_CONFLICT_RE = re.compile(r"cannot add .*bridge.*already has a.*bridge", re.IGNORECASE)
_OTHER_BRIDGE_TYPE = {"downlink": "tls", "tls": "downlink"}

# A OLT leva um instante depois do 'onu set' pra terminar de reconhecer a
# ONU (OMCI) -- se o 'bridge add' vier logo em seguida, ela recusa com essa
# mensagem (validado contra OLT real). Vale tentar de novo apos uma pausa.
_ONU_NOT_READY_RE = re.compile(r"please set onu first", re.IGNORECASE)


def _bridge_add_resilient(
    chan, pon: int, slot: int, service: str, vlan: int, tag: str, bridge_port: str, timeout: float,
    commands_run: List[str],
) -> Tuple[str, str]:
    """Aplica 'bridge add', absorvendo duas falhas conhecidas (validadas
    contra OLT real) antes de desistir:

    1. Tipo de bridge errado pra essa VLAN -- tenta uma vez com o tipo oposto.
    2. ONU ainda nao assentou na OLT ("Please set ONU first") -- espera um
       pouco e tenta de novo, ate 3 vezes no total.

    Devolve (ultimo_comando, ultima_saida); quem chama decide se a ultima
    saida ainda representa falha.
    """
    attempted_services = {service}
    cmd = out = ""
    for attempt in range(3):
        cmd = f"bridge add gpon {pon} onu {slot} {service} vlan {vlan} {tag} {bridge_port}"
        out = cli_run(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if not command_failed(out):
            return cmd, out
        if _ONU_NOT_READY_RE.search(out) and attempt < 2:
            time.sleep(2.0)
            continue
        alt = _OTHER_BRIDGE_TYPE.get(service)
        if _BRIDGE_TYPE_CONFLICT_RE.search(out) and alt and alt not in attempted_services:
            service = alt
            attempted_services.add(service)
            continue
        break
    return cmd, out


def profile_for_model(model: str, terminal: str = "onu") -> str:
    key = re.sub(r"[^a-z0-9]", "", (model or "").strip().lower())
    for needle, profile in MODEL_PROFILES.items():
        if needle in key:
            return profile
    return ONT_DEFAULT_PROFILE if str(terminal).strip().lower() == "ont" else DEFAULT_PROFILE


def command_failed(output: str) -> bool:
    low = (output or "").strip().lower()
    return any(marker in low for marker in _FAILURE_MARKERS)


def open_shell(client: paramiko.SSHClient):
    """Abre sessao interativa e sincroniza com o prompt (mesmo padrao de collect_macs_8820i)."""
    chan = client.invoke_shell()
    time.sleep(0.25)
    _ = read_until_prompt(chan)
    return chan


def read_until_prompt(chan, timeout: float = 10.0) -> str:
    buffer = ""
    start = time.time()
    while True:
        if chan.recv_ready():
            data = chan.recv(4096).decode(errors="ignore")
            buffer += data
            if PROMPT in buffer:
                break
        if time.time() - start > timeout:
            break
        time.sleep(0.05)
    return buffer


def cli_run(chan, cmd: str, timeout: float = 10.0) -> str:
    time.sleep(0.05)
    while chan.recv_ready():
        _ = chan.recv(4096)

    cmd_str = cmd.strip()
    chan.send(cmd_str + "\n")

    output = read_until_prompt(chan, timeout=timeout)

    cleaned_lines: List[str] = []
    for line in output.splitlines():
        if line.strip() == cmd_str:
            continue
        if PROMPT in line:
            continue
        cleaned_lines.append(line)

    final = "\n".join(cleaned_lines)
    if DEBUG_OLT_ADD_ONU:
        sys.stderr.write(f"[DBG] Saida de '{cmd_str}':\n{final}\n")
    return final


_CONFIRM_PROMPT_RE = re.compile(
    r"(\[yes\].*\[no\]|yes.*or.*no|do you want|request\?|configuration\?|:\s*$)",
    re.IGNORECASE,
)


def cli_run_with_confirmation(chan, cmd: str, answers: List[str], timeout: float = 20.0) -> str:
    """Roda um comando que a OLT responde com uma ou mais perguntas
    interativas (ex: 'onu delete', que pede confirmacao antes de executar).

    Poll no output; toda vez que reconhece um padrao de pergunta pendente,
    manda a proxima resposta da lista `answers` na ordem. Sequencia validada
    em producao (telegram-olt-bot) para 'onu delete': ["y", "n", "y"].
    """
    time.sleep(0.05)
    while chan.recv_ready():
        _ = chan.recv(4096)

    cmd_str = cmd.strip()
    chan.send(cmd_str + "\n")

    output = ""
    answer_idx = 0
    start = time.time()
    while time.time() - start < timeout:
        if chan.recv_ready():
            output += chan.recv(4096).decode(errors="ignore")
        if answer_idx < len(answers) and _CONFIRM_PROMPT_RE.search(output):
            chan.send(answers[answer_idx] + "\n")
            answer_idx += 1
            output = ""  # evita casar a mesma pergunta de novo no proximo poll
            time.sleep(0.2)
            continue
        if PROMPT in output:
            break
        time.sleep(0.05)

    if DEBUG_OLT_ADD_ONU:
        sys.stderr.write(f"[DBG] Saida de '{cmd_str}' (com confirmacao):\n{output}\n")
    return output


def _connect(olt_ip: str, user: str, password: str, timeout: float) -> paramiko.SSHClient:
    try:  # conector isolado -> IP virtual (vnat) so na conexao
        from app.services import connector_routing_vnat as _vnat
        _conn_host = _vnat.reach_olt_ip(olt_ip) or olt_ip
    except Exception:
        _conn_host = olt_ip
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            _conn_host,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
        return client
    except paramiko.ssh_exception.BadAuthenticationType as exc:
        client.close()
        allowed = set(getattr(exc, "allowed_types", []) or [])
        if "keyboard-interactive" not in allowed:
            raise

    sock = socket.create_connection((olt_ip, 22), timeout=timeout)
    transport: Optional[paramiko.Transport] = None
    try:
        transport = paramiko.Transport(sock)
        transport.banner_timeout = timeout
        transport.auth_timeout = timeout
        transport.start_client(timeout=timeout)

        def password_handler(_title: str, _instructions: str, prompts: list[tuple[str, bool]]) -> list[str]:
            return [password for _prompt, _echo in prompts]

        transport.auth_interactive(user, password_handler)
        if not transport.is_authenticated():
            raise paramiko.AuthenticationException("keyboard-interactive authentication failed")

        ki_client = paramiko.SSHClient()
        ki_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ki_client._transport = transport
        return ki_client
    except Exception:
        if transport is not None:
            transport.close()
        else:
            sock.close()
        raise


def parse_onu_show(output: str, fallback_pon: Optional[int] = None) -> Dict[str, Any]:
    """Parseia a saida de 'onu show' / 'onu show gpon <pon>'.

    Formato adaptado de telegram-olt-bot/olt_client.py:parse_onu_show --
    ja validado em campo contra a mesma OLT.
    """
    pon_match = re.search(r"Free slots in GPON Link\s+(\d+)", output, re.IGNORECASE)
    pon = int(pon_match.group(1)) if pon_match else fallback_pon

    free_slots: List[int] = []
    discovered: List[Dict[str, Any]] = []
    in_slots = False
    in_discovered = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if re.search(r"Free slots in GPON Link", line, re.IGNORECASE):
            in_slots = True
            in_discovered = False
            continue
        if line.startswith("Discovered serial numbers"):
            in_slots = False
            in_discovered = True
            continue
        if not line or set(line) <= {"="}:
            continue
        if in_slots:
            free_slots.extend(int(item) for item in re.findall(r"\b\d{1,3}\b", line))
            continue
        if in_discovered:
            if line.lower().startswith("sernoid"):
                continue
            m = re.match(r"^(\d+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(.+))?$", line)
            if m and pon:
                discovered.append(
                    {
                        "pon": pon,
                        "serno_id": int(m.group(1)),
                        "vendor": m.group(2),
                        "serial": m.group(3),
                        "model": m.group(4),
                        "time_discovered": (m.group(5) or "").strip(),
                    }
                )

    return {"pon": pon, "free_slots": sorted(set(free_slots)), "discovered": discovered}


def _pon_list(pon: str) -> List[int]:
    p = (pon or "all").strip().lower()
    if p == "all":
        return list(range(1, 9))
    return [int(p)]


def discover_unauthorized_onus(
    olt_ip: str,
    user: str,
    password: str,
    pon: str = "all",
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Descobre ONUs plugadas e nao autorizadas + posicoes livres, por PON.

    Retorna {"pons": {"<pon>": {"free_slots": [...], "discovered": [...]}}}.
    """
    client = _connect(olt_ip, user, password, timeout)
    try:
        chan = open_shell(client)
        pons_out: Dict[str, Any] = {}
        for p in _pon_list(pon):
            output = cli_run(chan, f"onu show gpon {p}", timeout=timeout)
            parsed = parse_onu_show(output, fallback_pon=p)
            pons_out[str(p)] = {"free_slots": parsed["free_slots"], "discovered": parsed["discovered"]}
        return {"ok": True, "pons": pons_out}
    finally:
        client.close()


def resolve_current_serno_id(discovered: List[Dict[str, Any]], serial: str, fallback_serno_id: int) -> int:
    """Reresolve o serno_id atual pelo serial (estavel) numa lista de
    descoberta recem-lida ('onu show gpon <pon>').

    A OLT reenumera o serno_id toda vez que a tabela de descoberta e
    consultada (inclusive por uma consulta concorrente de outra sessao),
    entao um serno_id capturado na tela minutos antes pode ja ter expirado.
    Se `serial` bater com uma entrada da lista, usa o serno_id atual dela.
    Se nao houver `serial` para comparar, cai no `fallback_serno_id` como
    estava antes. Se nem o serial nem o fallback existirem mais na lista,
    levanta OnuAddError com uma mensagem acionavel em vez de deixar o erro
    cru da OLT ("No such ID in discovered ONUs") subir sem contexto.
    """
    if not serial:
        return fallback_serno_id
    match = next(
        (d for d in discovered if str(d.get("serial", "")).strip().upper() == serial.strip().upper()),
        None,
    )
    if match:
        return match["serno_id"]
    if any(d.get("serno_id") == fallback_serno_id for d in discovered):
        return fallback_serno_id
    raise OnuAddError(
        f"ONU com serial {serial} nao esta mais na lista de descoberta da OLT "
        "na PON informada (sinal caiu ou o ID expirou). Refaca a descoberta e tente novamente.",
        "onu show gpon",
        [],
    )


def serial_number_arg(vendor: str, serial: str) -> str:
    """Monta o argumento de 'onu set ... serial-number <X>' (formato colado
    VENDOR+SERIAL, ex: ITBS2C96E6A7 -- validado contra OLT real).

    Aceita vendor/serial separados (como vem de discover_onus/onu show) ou um
    serial ja concatenado (como onu_signal devolve, ex: 'ITBS2C96E6A7') --
    nesse segundo caso nao duplica o prefixo do fabricante.
    """
    v = (vendor or "").strip().upper()
    s = (serial or "").strip().upper()
    if v and s.startswith(v):
        return s
    return f"{v}{s}"


def add_onu(
    olt_ip: str,
    user: str,
    password: str,
    pon: int,
    serno_id: int,
    profile: str,
    slot: Optional[int] = None,
    description: str = "",
    service: str = "",
    vlan: Optional[int] = None,
    services: Optional[List[Dict[str, Any]]] = None,
    tag_mode: str = "tagged",
    terminal: str = "onu",
    serial: str = "",
    vendor: str = "",
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Autoriza a ONU descoberta numa posicao livre da PON.

    Reconfirma a posicao livre bem antes de executar (o free_slots pode ter
    mudado desde a descoberta se outro tecnico mexeu na mesma OLT nesse meio
    tempo) -- mesma protecao usada no bot de referencia.

    Autoriza por SERIAL ("onu set ... serial-number <VENDOR+SERIAL> meprof
    <perfil>") sempre que `serial` for informado -- validado contra OLT real
    (comando `onu set gpon <pon> onu <onu> serial-number <X> meprof <perfil>`).
    O serial nao muda; ja o `serno_id` e um ID de sessao de descoberta que a
    OLT REENUMERA toda vez que roda "onu show gpon <pon>" (inclusive por
    outra consulta concorrente) -- se o tecnico demorar entre descobrir e
    autorizar, o id capturado na tela pode ja ter expirado ("No such ID in
    discovered ONUs"). Autorizar pelo serial elimina essa corrida por
    completo. So cai de volta no `id <serno_id>` (reresolvido via
    `resolve_current_serno_id`) quando nenhum serial e informado.

    `services` permite mais de um par servico/VLAN (um "bridge add" por
    entrada) -- so faz sentido em modo ONT/roteador; em modo ONU/bridge
    so a primeira entrada e aplicada (mesma regra do bot de referencia).
    Se `services` nao for passado, cai no par unico `service`/`vlan`.

    Levanta OnuAddError se algum comando falhar, com o que ja foi aplicado.
    """
    client = _connect(olt_ip, user, password, timeout)
    commands_run: List[str] = []
    try:
        chan = open_shell(client)

        refreshed = parse_onu_show(cli_run(chan, f"onu show gpon {pon}", timeout=timeout), fallback_pon=pon)
        chosen_slot = slot
        if chosen_slot is None:
            if not refreshed["free_slots"]:
                raise OnuAddError(f"Nenhuma posicao livre na PON {pon}.", "onu show gpon", commands_run)
            chosen_slot = refreshed["free_slots"][0]

        if serial:
            cmd = f"onu set gpon {pon} onu {chosen_slot} serial-number {serial_number_arg(vendor, serial)} meprof {profile}"
        else:
            try:
                current_serno_id = resolve_current_serno_id(refreshed["discovered"], serial, serno_id)
            except OnuAddError as e:
                raise OnuAddError(str(e), e.failed_command, commands_run) from e
            cmd = f"onu set gpon {pon} onu {chosen_slot} id {current_serno_id} meprof {profile}"

        out = cli_run(chan, cmd, timeout=timeout)
        commands_run.append(cmd)
        if command_failed(out):
            raise OnuAddError(f"Falha ao autorizar ONU: {out.strip()[:300]}", cmd, commands_run)

        if description:
            cmd = f"onu description add gpon {pon} onu {chosen_slot} text {description}"
            out = cli_run(chan, cmd, timeout=timeout)
            commands_run.append(cmd)
            if command_failed(out):
                raise OnuAddError(f"ONU autorizada, mas falha ao gravar descricao: {out.strip()[:300]}", cmd, commands_run)

        entries = [e for e in (services or []) if e.get("service") and e.get("vlan")]
        if not entries and service and vlan:
            entries = [{"service": service, "vlan": vlan}]
        is_ont = str(terminal).strip().lower() == "ont"
        if not is_ont:
            entries = entries[:1]

        if entries:
            tag = "untagged" if str(tag_mode).strip().lower() == "untagged" else "tagged"
            bridge_port = "router" if is_ont else "eth 1"
            for entry in entries:
                cmd, out = _bridge_add_resilient(
                    chan, pon, chosen_slot, entry["service"], entry["vlan"], tag, bridge_port, timeout, commands_run
                )
                if command_failed(out):
                    raise OnuAddError(
                        f"ONU autorizada, mas falha ao aplicar servico/VLAN ({entry['service']} vlan {entry['vlan']}): {out.strip()[:300]}",
                        cmd,
                        commands_run,
                        slot=chosen_slot,
                    )

        return {"ok": True, "pon": pon, "slot": chosen_slot, "commands_run": commands_run}
    finally:
        client.close()


def add_bridge_only(
    olt_ip: str,
    user: str,
    password: str,
    pon: int,
    slot: int,
    service: str = "",
    vlan: Optional[int] = None,
    services: Optional[List[Dict[str, Any]]] = None,
    tag_mode: str = "tagged",
    terminal: str = "onu",
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Aplica SO o passo de bridge/servico/VLAN numa ONU JA autorizada (o
    'onu set' ja foi feito, com sucesso, em outra chamada). Recuperacao para
    quando `add_onu` autoriza a ONU mas o `bridge add` falha -- ate agora nao
    havia como retomar so essa parte pela tela, o tecnico tinha que entrar
    na OLT direto. Nao roda 'onu set' de novo -- so a bridge.
    """
    client = _connect(olt_ip, user, password, timeout)
    commands_run: List[str] = []
    try:
        chan = open_shell(client)

        entries = [e for e in (services or []) if e.get("service") and e.get("vlan")]
        if not entries and service and vlan:
            entries = [{"service": service, "vlan": vlan}]
        is_ont = str(terminal).strip().lower() == "ont"
        if not is_ont:
            entries = entries[:1]
        if not entries:
            raise OnuAddError("Nenhum servico/VLAN informado.", "", commands_run, slot=slot)

        tag = "untagged" if str(tag_mode).strip().lower() == "untagged" else "tagged"
        bridge_port = "router" if is_ont else "eth 1"
        for entry in entries:
            cmd, out = _bridge_add_resilient(
                chan, pon, slot, entry["service"], entry["vlan"], tag, bridge_port, timeout, commands_run
            )
            if command_failed(out):
                raise OnuAddError(
                    f"Falha ao aplicar servico/VLAN ({entry['service']} vlan {entry['vlan']}): {out.strip()[:300]}",
                    cmd,
                    commands_run,
                    slot=slot,
                )

        return {"ok": True, "pon": pon, "slot": slot, "commands_run": commands_run}
    finally:
        client.close()


_FIND_FSAN_RE = re.compile(r"gpon\s+(\d+)\s+onu\s+(\d+)\s+([A-Fa-f0-9]{8})\s+(\S+)\s+(\S+)")

_DELETE_SUCCESS_MARKERS = (
    "deleting onu",
    "deleting onu at",
    "clearing onu",
    "deleted",
    "successfully deleted",
)


def delete_succeeded(output: str) -> bool:
    low = (output or "").strip().lower()
    return any(marker in low for marker in _DELETE_SUCCESS_MARKERS)


def find_onu_by_serial(olt_ip: str, user: str, password: str, serial: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
    """Localiza uma ONU ja autorizada pelo serial (comando 'onu find fsan').

    Retorna {"pon", "onu", "serial", "vendor", "model"} ou None se nao achar.
    """
    client = _connect(olt_ip, user, password, timeout)
    try:
        chan = open_shell(client)
        output = cli_run(chan, f"onu find fsan {serial}", timeout=timeout)
        m = _FIND_FSAN_RE.search(output)
        if not m:
            return None
        return {
            "pon": int(m.group(1)),
            "onu": int(m.group(2)),
            "serial": m.group(3).upper(),
            "vendor": m.group(4),
            "model": m.group(5),
        }
    finally:
        client.close()


_ONU_DETAIL_RE = re.compile(r"gpon\s+(\d+)\s+onu\s+(\d+)\s+(Yes|No)\s+(\S+)\s+(\S+)\s+(\S+)", re.IGNORECASE)

_ONU_STATUS_RE = re.compile(
    r"^(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(-?\d+(?:\.\d+)?\s+dBm|-)\s+(-?\d+(?:\.\d+)?\s+dBm|-)\s+(\S+)"
)

_MAC_RE = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})\s+(.+)", re.IGNORECASE)


def parse_onu_show_detail(output: str) -> Dict[str, Any]:
    """Parseia 'onu show gpon <pon> onu <onu>' (uma unica ONU ja autorizada)."""
    m = _ONU_DETAIL_RE.search(output or "")
    if not m:
        return {}
    return {
        "pon": int(m.group(1)),
        "onu": int(m.group(2)),
        "enabled": m.group(3),
        "serial_full": m.group(4),
        "model": m.group(5),
        "profile": m.group(6),
    }


def parse_onu_status(output: str) -> Dict[str, Any]:
    """Parseia 'onu status gpon <pon> onu <onu>' (sinal/RX/distancia/status)."""
    for raw_line in (output or "").splitlines():
        m = _ONU_STATUS_RE.match(raw_line.strip())
        if m:
            return {
                "onu": m.group(1),
                "serial": m.group(2),
                "oper_status": m.group(3),
                "omci_status": m.group(4),
                "olt_rx": m.group(5),
                "onu_rx": m.group(6),
                "distance_km": m.group(7),
            }
    return {}


def parse_macs(output: str) -> List[Dict[str, str]]:
    """Parseia 'bridge show mac gpon <pon> onu <onu>' (MACs aprendidos atras da ONU)."""
    macs: List[Dict[str, str]] = []
    for raw_line in (output or "").splitlines():
        m = _MAC_RE.search(raw_line.strip())
        if m:
            macs.append({"mac": m.group(1).lower(), "interface": re.sub(r"\s+", " ", m.group(2)).strip()})
    return macs


def onu_signal(
    olt_ip: str,
    user: str,
    password: str,
    pon: Optional[int] = None,
    onu: Optional[int] = None,
    serial: str = "",
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Consulta sinal (RX/distancia/status) e MACs aprendidos de uma ONU ja
    autorizada. Aceita pon+onu diretos, ou resolve por serial via 'onu find fsan'.
    """
    client = _connect(olt_ip, user, password, timeout)
    try:
        chan = open_shell(client)

        if serial and not (pon and onu):
            found_out = cli_run(chan, f"onu find fsan {serial}", timeout=timeout)
            m = _FIND_FSAN_RE.search(found_out)
            if not m:
                return {"ok": False, "error": f"Nao encontrei ONU com serial {serial}."}
            pon, onu = int(m.group(1)), int(m.group(2))

        if not pon or not onu:
            return {"ok": False, "error": "Informe o serial ou a posicao (PON + numero da ONU)."}

        show_out = cli_run(chan, f"onu show gpon {pon} onu {onu}", timeout=timeout)
        detail = parse_onu_show_detail(show_out)
        if not detail:
            return {"ok": False, "error": f"ONU nao encontrada em gpon {pon} onu {onu}."}

        status_out = cli_run(chan, f"onu status gpon {pon} onu {onu}", timeout=timeout)
        macs_out = cli_run(chan, f"bridge show mac gpon {pon} onu {onu}", timeout=timeout)
        signal = parse_onu_status(status_out)
        macs = parse_macs(macs_out)

        return {
            "ok": True,
            "pon": pon,
            "onu": onu,
            "serial": detail.get("serial_full", ""),
            "model": detail.get("model", ""),
            "profile": detail.get("profile", ""),
            "enabled": detail.get("enabled", ""),
            "oper_status": signal.get("oper_status", ""),
            "omci_status": signal.get("omci_status", ""),
            "olt_rx": signal.get("olt_rx", ""),
            "onu_rx": signal.get("onu_rx", ""),
            "distance_km": signal.get("distance_km", ""),
            "macs": macs,
        }
    finally:
        client.close()


def delete_onu(
    olt_ip: str,
    user: str,
    password: str,
    pon: int,
    onu: int,
    timeout: float = 22.0,
) -> Dict[str, Any]:
    """Exclui uma ONU ja autorizada (posicao pon/onu). Equipamento vivo --
    remove o cadastro e as configuracoes de servico associadas.

    A OLT pede confirmacao interativa antes de excluir (nao e um comando
    seco como 'onu set'). Sequencia de respostas validada em producao no
    bot de referencia: ["y", "n", "y"].
    """
    client = _connect(olt_ip, user, password, timeout)
    try:
        chan = open_shell(client)
        cmd = f"onu delete gpon {pon} onu {onu}"
        output = cli_run_with_confirmation(chan, cmd, ["y", "n", "y"], timeout=timeout)
        ok = delete_succeeded(output)
        return {"ok": ok, "pon": pon, "onu": onu, "command": cmd, "raw_output": output.strip()[:500]}
    finally:
        client.close()


def reboot_onu(
    olt_ip: str,
    user: str,
    password: str,
    pon: int,
    onu: int,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """Reinicia uma ONU/ONT ja autorizada (posicao pon/onu). Equipamento vivo
    -- a ONU fica sem servico por um tempo ate voltar a subir sozinha.

    Comando informado pelo usuario (nao validado ainda contra OLT real nesta
    sessao, ao contrario de 'onu set'/'bridge add'): 'onu reboot gpon <pon>
    onu <onu>'. Usa `cli_run` simples (sem confirmacao interativa) -- se a
    OLT pedir confirmacao como faz em 'onu delete', ajustar para
    `cli_run_with_confirmation` depois de validar em campo.
    """
    client = _connect(olt_ip, user, password, timeout)
    try:
        chan = open_shell(client)
        cmd = f"onu reboot gpon {pon} onu {onu}"
        output = cli_run(chan, cmd, timeout=timeout)
        ok = not command_failed(output)
        return {"ok": ok, "pon": pon, "onu": onu, "command": cmd, "raw_output": output.strip()[:500]}
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--olt-ip", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--pon", default="all")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()

    if args.discover:
        result = discover_unauthorized_onus(args.olt_ip, args.user, args.password, pon=args.pon, timeout=args.timeout)
        for pon_str, data in result["pons"].items():
            print(f"PON {pon_str}: livres={data['free_slots']} descobertas={data['discovered']}")
    else:
        print("Use --discover para listar ONUs nao autorizadas.", file=sys.stderr)


if __name__ == "__main__":
    main()
