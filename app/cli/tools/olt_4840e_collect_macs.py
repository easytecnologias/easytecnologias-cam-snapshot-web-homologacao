#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
from typing import Any, Dict, List, Optional
import os
import select
import shutil
import socket
import subprocess
import re
import telnetlib
import time
import paramiko

_MAC_RE = re.compile(r"\b(?:[0-9a-f]{2}[:\-\.]){5}[0-9a-f]{2}\b", re.I)

# ---- Intelbras 4840E helpers (EPON) ----
_PON_LINE_RE = re.compile(r"^(?P<onu>\d+/\d+/\d+)\s+(?P<onu_mac>(?:[0-9a-f]{2}[:\-\.]){5}[0-9a-f]{2})\s+(?P<llid>\S+)\s+(?P<type>\S+)\s+(?P<cfg>\S+)\s+(?P<desc>.+)$", re.I)
_MAC_ONU_TABLE_RE = re.compile(
    r"^(?P<mac>(?:[0-9a-f]{2}[:\-\.]){5}[0-9a-f]{2})\s+(?P<vlan>\d+)\s+(?P<onu>\d+/\d+/\d+)\s+(?P<status>\S+)",
    re.I
)

def _split_onu(onu: str) -> Dict[str, str]:
    """'0/1/12' -> {'pon':'0/1', 'onu_id':'12'}"""
    s = (onu or "").strip()
    m = re.fullmatch(r"(\d+)/(\d+)/(\d+)", s)
    if not m:
        return {"pon": "", "onu_id": ""}
    return {"pon": f"{m.group(1)}/{m.group(2)}", "onu_id": m.group(3)}

def _parse_show_pon(output: str) -> List[Dict[str, Any]]:
    """Parse do 'show pon' da Intelbras 4840E."""
    out: List[Dict[str, Any]] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("onu", "total", "olt", "password", "login")):
            continue
        m = _PON_LINE_RE.match(line)
        if not m:
            continue
        onu = m.group("onu")
        sp = _split_onu(onu)
        out.append({
            "onu": onu,
            "pon": sp["pon"],
            "onu_id": sp["onu_id"],
            "onu_mac": _norm_mac(m.group("onu_mac")),
            "llid": m.group("llid"),
            "onu_type": m.group("type"),
            "config": m.group("cfg"),
            "description": (m.group("desc") or "").strip(),
        })
    return out

def _parse_mac_table_onu(output: str) -> List[Dict[str, Any]]:
    """Parse do 'show mac-address-table onu X/Y/Z'."""
    rows: List[Dict[str, Any]] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("mac", "address", "vlan", "total", "----")):
            continue
        m = _MAC_ONU_TABLE_RE.match(line)
        if not m:
            continue
        mac = _norm_mac(m.group("mac"))
        vlan = int(m.group("vlan"))
        onu = m.group("onu")
        status = m.group("status")
        sp = _split_onu(onu)
        rows.append({
            "pon": sp["pon"],
            "onu_id": sp["onu_id"],
            "onu": onu,
            "cpe_mac": mac,
            "vlan": vlan,
            # não existe "porta" nesse comando; mantemos o campo para compat com frontend
            "port": onu,
            "status": status,
        })
    return rows

def _norm_mac(v: str) -> str:
    s = (v or "").strip().lower()
    s = s.replace("-", ":").replace(".", ":")
    s = re.sub(r":+", ":", s)
    return s

def _read(chan, timeout: float = 10.0) -> str:
    """
    Lê do canal até:
      - prompt normal (>, #, (config)# etc)
      - OU prompts de login (Username/Password)
      - OU erro de credencial
    Também tenta avançar paginação se aparecer --More--.
    """
    t0 = time.time()
    buf = ""

    prompt_re = re.compile(r"(?:\r?\n)?[^\n]{0,120}(?:\(config[^\)]*\))?[>#]\s*$")
    user_re   = re.compile(r"(?:^|\n)\s*(?:login\s+as|username)\b.*:\s*$", re.I)
    pass_re   = re.compile(r"(?:^|\n)\s*password\b.*:\s*$", re.I)
    err_re    = re.compile(r"username\s+or\s+password\s+error", re.I)

    more_re   = re.compile(r"(--More--|More:|Press any key|Press any button|next page|continue)", re.I)

    while True:
        if chan.recv_ready():
            chunk = chan.recv(65535).decode("utf-8", errors="ignore")
            buf += chunk

            tail = buf[-300:]
            if more_re.search(tail):
                # avança paginação (se existir)
                try:
                    chan.send(" ")
                    time.sleep(0.05)
                except Exception:
                    pass

            if err_re.search(buf) or user_re.search(buf) or pass_re.search(buf) or prompt_re.search(buf):
                return buf

        if time.time() - t0 > timeout:
            return buf

        time.sleep(0.05)

def _cli(chan, cmd: str, timeout: float = 12.0) -> str:
    # limpa lixo pendente
    try:
        while chan.recv_ready():
            chan.recv(65535)
    except Exception:
        pass

    chan.send(cmd.rstrip() + "\n")
    time.sleep(0.08)
    return _read(chan, timeout=timeout)

def _open_shell(host: str, user: str, password: str, port: int = 22, timeout: float = 12.0):
    try:  # conector isolado -> IP virtual (vnat); real intacto se nao mapeado
        from app.services import connector_routing_vnat as _vnat
        host = _vnat.reach_olt_ip(host) or host
    except Exception:
        pass
    if port == 22 and not _paramiko_supports_legacy_4840e():
        try:
            return _sshpass_legacy_shell(host, user, password, port=port, timeout=timeout)
        except RuntimeError:
            return _telnet_shell(host, timeout=timeout)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
        )
    except Exception as exc:
        if "no acceptable kex algorithm" not in str(exc).lower():
            raise
        client.close()
        try:
            client = _legacy_kex_client(host, user, password, port=port, timeout=timeout)
        except Exception as legacy_exc:
            if "no acceptable kex algorithm" not in str(legacy_exc).lower():
                raise
            try:
                return _sshpass_legacy_shell(host, user, password, port=port, timeout=timeout)
            except RuntimeError:
                return _telnet_shell(host, timeout=timeout)
    chan = client.invoke_shell(width=220, height=80)
    time.sleep(0.25)
    return client, chan


def _paramiko_supports_legacy_4840e() -> bool:
    transport = paramiko.Transport(socket.socket())
    try:
        opts = transport.get_security_options()
        return (
            "diffie-hellman-group1-sha1" in tuple(opts.kex)
            and "ssh-rsa" in tuple(opts.key_types)
            and "3des-cbc" in tuple(opts.ciphers)
        )
    finally:
        transport.close()


class _ProcessSSHClient:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc

    def close(self) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class _ProcessChannel:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stdout is None or proc.stdin is None:
            raise RuntimeError("Falha ao abrir canal SSH legado.")
        self.proc = proc
        self.stdout = proc.stdout
        self.stdin = proc.stdin
        os.set_blocking(self.stdout.fileno(), False)

    def recv_ready(self) -> bool:
        if self.proc.poll() is not None:
            return True
        ready, _, _ = select.select([self.stdout], [], [], 0)
        return bool(ready)

    def recv(self, size: int) -> bytes:
        try:
            return os.read(self.stdout.fileno(), size)
        except BlockingIOError:
            return b""

    def send(self, data: str) -> int:
        raw = data.encode("utf-8", errors="ignore")
        self.stdin.write(raw)
        self.stdin.flush()
        return len(raw)


def _sshpass_legacy_shell(host: str, user: str, password: str, port: int = 22, timeout: float = 12.0):
    if not shutil.which("sshpass") or not shutil.which("ssh"):
        raise RuntimeError("OLT usa SSH legado, mas sshpass/ssh nao estao disponiveis no container.")
    env = dict(os.environ)
    env["SSHPASS"] = password or ""
    cmd = [
        "sshpass",
        "-e",
        "ssh",
        "-tt",
        "-p",
        str(int(port)),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1",
        "-o",
        "HostKeyAlgorithms=+ssh-rsa",
        "-o",
        "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o",
        "Ciphers=+3des-cbc,aes128-cbc,aes192-cbc,aes256-cbc",
        "-o",
        f"ConnectTimeout={max(3, int(timeout))}",
        f"{user}@{host}",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=0,
    )
    time.sleep(0.4)
    return _ProcessSSHClient(proc), _ProcessChannel(proc)


class _TelnetClient:
    def __init__(self, tn: telnetlib.Telnet) -> None:
        self.tn = tn

    def close(self) -> None:
        self.tn.close()


class _TelnetChannel:
    def __init__(self, tn: telnetlib.Telnet) -> None:
        self.tn = tn

    def recv_ready(self) -> bool:
        try:
            return bool(self.tn.sock_avail())
        except EOFError:
            return True

    def recv(self, size: int) -> bytes:
        try:
            return self.tn.read_eager()[:size]
        except EOFError:
            return b""

    def send(self, data: str) -> int:
        raw = data.encode("utf-8", errors="ignore")
        self.tn.write(raw)
        return len(raw)


def _telnet_shell(host: str, timeout: float = 12.0):
    tn = telnetlib.Telnet(host, 23, timeout=timeout)
    time.sleep(0.3)
    return _TelnetClient(tn), _TelnetChannel(tn)


def _prepend_unique(values: tuple[str, ...], current: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for item in (*values, *current):
        if item and item not in out:
            out.append(item)
    return tuple(out)


def _prepend_supported(values: tuple[str, ...], current: tuple[str, ...]) -> tuple[str, ...]:
    supported = set(current)
    return _prepend_unique(tuple(item for item in values if item in supported), current)


def _legacy_kex_client(host: str, user: str, password: str, port: int = 22, timeout: float = 12.0) -> paramiko.SSHClient:
    """Fallback para OLTs Intelbras antigas que so negociam SSH legado."""
    sock = socket.create_connection((host, port), timeout=timeout)
    transport = paramiko.Transport(sock)
    transport.banner_timeout = timeout
    transport.auth_timeout = timeout
    opts = transport.get_security_options()
    opts.kex = _prepend_supported(
        (
            "diffie-hellman-group14-sha1",
            "diffie-hellman-group1-sha1",
            "diffie-hellman-group-exchange-sha1",
        ),
        tuple(opts.kex),
    )
    opts.ciphers = _prepend_supported(
        ("aes128-cbc", "aes192-cbc", "aes256-cbc", "3des-cbc"),
        tuple(opts.ciphers),
    )
    opts.key_types = _prepend_supported(("ssh-rsa", "ssh-dss"), tuple(opts.key_types))
    try:
        transport.connect(username=user, password=password)
    except Exception:
        transport.close()
        sock.close()
        raise
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client._transport = transport
    return client

def _ensure_logged_in(chan, user: str, password: str, timeout: float = 12.0) -> None:
    out = _read(chan, timeout=timeout)
    low = (out or "").lower()

    if "username or password error" in low:
        raise RuntimeError("OLT respondeu: Username or password error (credenciais inválidas).")

    # Se pediu username/login
    if "login as" in low or "username" in low:
        chan.send((user or "").strip() + "\n")
        time.sleep(0.15)
        out = _read(chan, timeout=timeout)
        low = (out or "").lower()

    # Se pediu password
    if "password" in low:
        chan.send((password or "") + "\n")
        time.sleep(0.2)
        out = _read(chan, timeout=timeout)
        low = (out or "").lower()

    if "username or password error" in low:
        raise RuntimeError("OLT respondeu: Username or password error (credenciais inválidas).")

    # garante que chegamos num prompt normal
    if not re.search(r"(?:\(config[^\)]*\))?[>#]\s*$", out or ""):
        # tenta mais um pouco
        out2 = _read(chan, timeout=timeout)
        if not re.search(r"(?:\(config[^\)]*\))?[>#]\s*$", out2 or ""):
            # não travar “silencioso”
            raise RuntimeError("Não consegui detectar prompt da OLT após login (timeout/prompt inesperado).")

def _ensure_enable(chan, password: str, timeout: float = 12.0) -> None:
    out = _cli(chan, "en", timeout=timeout)
    if "password" in (out or "").lower():
        chan.send((password or "") + "\n")
        time.sleep(0.2)
        _read(chan, timeout=timeout)


def _pon_list_from_input(pon: str) -> List[str]:
    """Aceita '0/1', '1'..'64' ou 'all' (usa 0/1 só pra entrar no contexto)."""
    p = (pon or "").strip().lower()
    if not p or p == "all":
        return ["0/1"]

    p = p.replace("pon", "").strip()

    # formato 0/1
    if re.fullmatch(r"\d+/\d+", p):
        return [p]

    # somente número: 1..64 -> 0/<n>
    if re.fullmatch(r"\d+", p):
        n = int(p)
        if n <= 0:
            raise ValueError("Valor inválido para pon. Use 0/1..0/64, 1..64, ou 'all'.")
        return [f"0/{n}"]

    raise ValueError("Valor inválido para pon. Use 0/1..0/64, 1..64, ou 'all'.")

def _derive_pon_from_port(port: str) -> str:
    m = re.fullmatch(r"p(\d+)/(\d+)", (port or "").strip().lower())
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return ""

def _parse_mac_table(output: str, fallback_pon: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("mac", "address", "vlan", "total", "----")):
            continue

        macs = _MAC_RE.findall(line)
        if not macs:
            continue

        toks = re.split(r"\s+", line)
        mac = _norm_mac(macs[0])

        vlan = None
        port = ""
        status = ""

        # acha mac
        try:
            mi = next(i for i, t in enumerate(toks) if _norm_mac(t) == mac)
        except StopIteration:
            mi = 0

        for j in range(mi + 1, min(mi + 6, len(toks))):
            if toks[j].isdigit():
                vv = int(toks[j])
                if 1 <= vv <= 4094:
                    vlan = vv
                    if j + 1 < len(toks): port = toks[j + 1]
                    if j + 2 < len(toks): status = toks[j + 2]
                    break

        pon = _derive_pon_from_port(port) or (fallback_pon or "")
        rows.append({
            "pon": pon,
            "onu_id": "",
            "cpe_mac": mac,
            "vlan": vlan,
            "port": port,
            "status": status,
        })
    return rows

def collect_macs_4840e(
    olt_ip: str,
    user: str,
    password: str,
    pon: str = "all",
    olt_name: Optional[str] = None,
    port: int = 22,
    timeout: float = 12.0,
) -> List[Dict[str, Any]]:
    olt_ip = (olt_ip or "").strip()
    user = (user or "").strip()
    if not olt_ip or not user:
        raise ValueError("olt_ip e user são obrigatórios")

    pon_ports = _pon_list_from_input(pon)
    ctx_pon = pon_ports[0]

    client, chan = _open_shell(olt_ip, user, password, port=port, timeout=timeout)
    try:
        _ensure_logged_in(chan, user=user, password=password, timeout=timeout)

        # enable + config
        _ensure_enable(chan, password=password, timeout=timeout)
        _cli(chan, "conf t", timeout=timeout)
        # Entra em um contexto PON válido (mesmo se pon='all'), para ficar igual ao terminal.
        _cli(chan, f"interface pon {ctx_pon}", timeout=timeout)

        # coleta por ONU (resolve vínculo CPE -> ONU)
        # 1) lista ONUs (show pon)
        pon_out = _cli(chan, "show pon", timeout=max(30.0, timeout * 3))
        onu_entries = _parse_show_pon(pon_out)

        # define quais PONs entram na consulta
        pon_in = (pon or "").strip().lower()
        want_all = (not pon_in) or (pon_in == "all")
        if want_all:
            selected_pons = sorted({e.get("pon", "") for e in onu_entries if e.get("pon")})
        else:
            selected_pons = _pon_list_from_input(pon)

        # 2) para cada ONU das PONs selecionadas, consulta MACs por ONU
        out_rows: List[Dict[str, Any]] = []
        if onu_entries:
            onu_by_key = {e.get("onu"): e for e in onu_entries if e.get("onu")}
            for e in onu_entries:
                if e.get("pon") not in selected_pons:
                    continue
                onu = e.get("onu")
                if not onu:
                    continue

                mac_out = _cli(chan, f"show mac-address-table onu {onu}", timeout=max(60.0, timeout * 6))
                rows = _parse_mac_table_onu(mac_out)

                # enriquece com metadados da ONU (se quiser usar no frontend depois)
                for r in rows:
                    rr = dict(r)
                    rr["olt"] = olt_name or "Intelbras 4840E"
                    rr["onu_mac"] = e.get("onu_mac", "")
                    # Na 4840E (EPON) usamos o MAC da ONU como "serial".
                    rr["onu_serial"] = rr.get("onu_mac") or rr.get("mac") or ""
                    rr["onu_name"] = e.get("description", "")
                    rr["oper_status"] = "Active"
                    rr["omci_status"] = "OK"
                    rr["llid"] = e.get("llid", "")
                    out_rows.append(rr)
        else:
            # fallback: tabela global (sem ONU) — mantém compatibilidade
            mac_out = _cli(chan, "show mac-address-table", timeout=max(120.0, timeout * 8))
            rows = _parse_mac_table(mac_out, fallback_pon=ctx_pon)
            for r in rows:
                rr = dict(r)
                rr["olt"] = olt_name or "Intelbras 4840E"
                rr["onu_serial"] = rr.get("onu_mac") or rr.get("mac") or ""
                out_rows.append(rr)

        # aplica filtro "PON" no fallback global (evita e0/x e cpu)
        if (not want_all) and out_rows and ("onu_id" in out_rows[0] and out_rows[0].get("onu_id") == ""):
            allowed = set(selected_pons)
            out_rows = [r for r in out_rows if (r.get("pon") in allowed)]

        rows = out_rows


        out: List[Dict[str, Any]] = []
        for r in rows:
            rr = dict(r)
            rr["olt"] = olt_name or "Intelbras 4840E"
            out.append(rr)
        return out
    finally:
        try: chan.close()
        except Exception: pass
        try: client.close()
        except Exception: pass
