#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ferramenta auxiliar: coleta MACs por ONU na OLT Intelbras 8820i (GPON)
usando sessÃ£o interativa (invoke_shell) e gera saida/olt-cpe-macs.json.

Uso tÃ­pico (PowerShell):

    # PON especÃ­fica
    python .\tools\olt_8820i_collect_macs.py `
      --olt-ip 10.80.80.5 `
      --user admin `
      --password admin `
      --pon 1 `
      --out saida\olt-cpe-macs.json

    # Todas as PONs (1-8)
    python .\tools\olt_8820i_collect_macs.py `
      --olt-ip 10.80.80.5 `
      --user admin `
      --password admin `
      --pon all `
      --out saida\olt-cpe-macs.json
"""

import argparse
import csv
import os
import re
import sys
import time
from typing import Dict, List, Tuple

import paramiko  # type: ignore[import]


PROMPT = "intelbras-olt>"  # ajuste se o prompt for diferente
DEBUG_OLT_COLLECT = str(os.getenv("OLT_DEBUG_COLLECT", "0")).strip().lower() in ("1", "true", "yes", "on")


def normalize_mac(s: str) -> str:
    """Normaliza MAC para o formato aa:bb:cc:dd:ee:ff (minÃºsculo)."""
    if not s:
        return ""
    s = s.strip()
    s = re.sub(r"[^0-9A-Fa-f]", "", s)  # remove tudo que nÃ£o Ã© hexa
    s = s.lower()
    if len(s) == 12:
        return ":".join(s[i : i + 2] for i in range(0, 12, 2))
    return s


def open_shell(client: paramiko.SSHClient):
    """Abre uma sessÃ£o interativa na OLT e sincroniza com o prompt."""
    chan = client.invoke_shell()
    time.sleep(0.25)
    _ = read_until_prompt(chan)
    return chan


def read_until_prompt(chan, timeout: float = 10.0) -> str:
    """LÃª dados da sessÃ£o atÃ© encontrar o PROMPT ou estourar timeout."""
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
    """
    Envia um comando para a OLT via shell interativo e lÃª atÃ© o prompt retornar.
    Remove eco do comando e o prompt final, deixando sÃ³ o "miolo" da saÃ­da.
    """
    time.sleep(0.05)
    while chan.recv_ready():
        _ = chan.recv(4096)

    cmd_str = cmd.strip()
    chan.send(cmd_str + "\n")

    output = read_until_prompt(chan, timeout=timeout)

    output_lines = output.splitlines()
    cleaned_lines: List[str] = []
    for line in output_lines:
        if line.strip() == cmd_str:
            continue
        if PROMPT in line:
            continue
        cleaned_lines.append(line)

    final = "\n".join(cleaned_lines)
    if DEBUG_OLT_COLLECT:
        sys.stderr.write(f"[DBG] Saida de '{cmd_str}':\n{final}\n")
    return final


def parse_onu_status(output: str, pon: int | None = None) -> List[Dict]:
    """Parseia saida de 'onu status gpon <pon>' da Intelbras 8820i."""
    onus: List[Dict] = []
    pon_id = pon if pon is not None else 1
    # Formato observado no legado:
    # 1  8B3E3755  Active  OK  -20.52 dBm  -18.83 dBm  0.758  7:22:52:22
    full_re = re.compile(
        r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(-?\d+(?:\.\d+)?)?\s*dBm\s+(-?\d+(?:\.\d+)?)?\s*dBm\s+([\d\.]+)\s+([\d:]+)",
        re.IGNORECASE,
    )

    lines = output.splitlines()
    in_table = False

    for line in lines:
        s = line.strip()
        if not s:
            continue

        if s.startswith("ONU") and "OperStatus" in s:
            in_table = True
            continue

        if s.startswith("Configured ONUs"):
            break

        m = full_re.search(s)
        if m:
            onu_id = int(m.group(1))
            serial = m.group(2).strip()
            rx_olt = m.group(5) or ""
            rx_onu = m.group(6) or ""
            onus.append(
                {
                    "pon": pon_id,
                    "onu_id": onu_id,
                    "enabled": "Yes",
                    "serial": serial,
                    "model": "",
                    "profile": "",
                    "name": f"gpon {pon_id} onu {onu_id}",
                    "oper_status": m.group(3).strip(),
                    "omci_status": m.group(4).strip(),
                    "rx_olt": rx_olt,
                    "rx_onu": rx_onu,
                    "rx_power": rx_onu,
                    "onu_rx_power": rx_onu,
                    "signal": rx_onu,
                    "distance_km": m.group(7) or "",
                    "uptime": m.group(8) or "",
                }
            )
            continue

        if not in_table:
            continue

        parts = s.split()
        if len(parts) < 2:
            continue

        try:
            onu_id = int(parts[0])
        except ValueError:
            continue

        serial = parts[1]
        onus.append(
            {
                "pon": pon_id,
                "onu_id": onu_id,
                "enabled": "Yes",
                "serial": serial,
                "model": "",
                "profile": "",
                "name": f"gpon {pon_id} onu {onu_id}",
                "oper_status": parts[2] if len(parts) > 2 else "",
                "omci_status": parts[3] if len(parts) > 3 else "",
                "rx_olt": "",
                "rx_onu": "",
                "rx_power": "",
                "onu_rx_power": "",
                "signal": "",
                "distance_km": "",
                "uptime": "",
            }
        )

    return onus


def collect_onu_telemetry_8820i(
    olt_ip: str,
    user: str,
    password: str,
    pon: str = "all",
    timeout: float = 12.0,
) -> List[Dict]:
    """Coleta status e sinal de todas as ONUs sem executar a coleta pesada de MACs."""
    try:  # conector isolado -> IP virtual (vnat) so na conexao; olt_ip fica real
        from app.services import connector_routing_vnat as _vnat
        _conn_host = _vnat.reach_olt_ip(olt_ip) or olt_ip
    except Exception:
        _conn_host = olt_ip
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
    try:
        chan = open_shell(client)
        if str(pon).strip().lower() == "all":
            pons = range(1, 9)
        else:
            pons = [int(pon)]

        rows: List[Dict] = []
        for pon_id in pons:
            output = cli_run(chan, f"onu status gpon {pon_id}", timeout=10.0)
            rows.extend(parse_onu_status(output, pon_id))
        return rows
    finally:
        client.close()

def _first_number_after(labels: List[str], output: str) -> str:
    for label in labels:
        # Aceita variacoes como "RxONU: -18.8 dBm", "Rx ONU Power -18.8" ou "ONU Rx Power = -18.8".
        pattern = label + r"[^\r\n\-\d]{0,40}(-?\d+(?:[\.,]\d+)?)\s*(?:dBm)?"
        m = re.search(pattern, output, re.IGNORECASE)
        if m:
            return m.group(1).replace(",", ".")
    return ""


def parse_single_onu_status(output: str, pon: int, onu_id: int) -> Dict:
    """Parseia detalhes do comando 'onu status gpon <pon> onu <onu_id>'."""
    rows = parse_onu_status(output, pon)
    for row in rows:
        if str(row.get("onu_id") or "") == str(onu_id):
            return row

    compact = "\n".join(line.strip() for line in (output or "").splitlines() if line.strip())
    rx_olt = _first_number_after([r"rx\s*olt", r"olt\s*rx", r"receive\s*power.*olt"], compact)
    rx_onu = _first_number_after([r"rx\s*onu", r"onu\s*rx", r"receive\s*power.*onu", r"rx\s*power", r"signal"], compact)

    oper = ""
    omci = ""
    m_oper = re.search(r"oper\s*status\s*[:=]?\s*(\S+)", compact, re.IGNORECASE)
    if m_oper:
        oper = m_oper.group(1)
    m_omci = re.search(r"omci\s*status\s*[:=]?\s*(\S+)", compact, re.IGNORECASE)
    if m_omci:
        omci = m_omci.group(1)

    return {
        "pon": pon,
        "onu_id": onu_id,
        "oper_status": oper,
        "omci_status": omci,
        "rx_olt": rx_olt,
        "rx_onu": rx_onu,
        "rx_power": rx_onu,
        "onu_rx_power": rx_onu,
        "signal": rx_onu,
        "distance_km": _first_number_after([r"distance", r"distancia"], compact),
        "uptime": "",
    }


def enrich_onu_signal(chan, onu: Dict, pon: int, onu_id: int) -> Dict:
    if onu.get("rx_onu") or onu.get("rx_olt"):
        return onu
    cmd = f"onu status gpon {pon} onu {onu_id}"
    sys.stderr.write(f"[INFO] Lendo sinal de gpon {pon} onu {onu_id}...\n")
    out = cli_run(chan, cmd, timeout=12.0)
    detail = parse_single_onu_status(out, pon, onu_id)
    enriched = dict(onu)
    for key in ("oper_status", "omci_status", "rx_olt", "rx_onu", "rx_power", "onu_rx_power", "signal", "distance_km", "uptime"):
        if detail.get(key):
            enriched[key] = detail.get(key)
    return enriched


def parse_macs_from_output(output: str) -> List[Dict]:
    """Parseia saÃ­da de:

        bridge show mac gpon <pon> onu <onu>
    """
    macs: List[Dict] = []

    mac_pattern = re.compile(
        r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}|[0-9A-Fa-f]{12})", re.IGNORECASE
    )
    vlan_pattern = re.compile(r"vlan\s+(\d+)", re.IGNORECASE)

    for line in output.splitlines():
        line = line.strip()
        if not line or "MAC Address" in line or "Interface" in line or line.startswith("="):
            continue

        m_mac = mac_pattern.search(line)
        if not m_mac:
            continue

        m_vlan = vlan_pattern.search(line)
        vlan = m_vlan.group(1) if m_vlan else ""

        raw_mac = m_mac.group(1)
        mac_norm = normalize_mac(raw_mac)
        if not mac_norm:
            continue

        macs.append({"cpe_mac": mac_norm, "vlan": vlan})

    return macs


def get_macs_for_onu(chan, pon: int, onu_id: int) -> List[Dict]:
    """Executa o comando:

        bridge show mac gpon <pon> onu <onu_id>
    """
    cmd = f"bridge show mac gpon {pon} onu {onu_id}"
    out = cli_run(chan, cmd, timeout=15.0)
    macs = parse_macs_from_output(out)
    if macs:
        sys.stderr.write(f"[INFO] MACs encontrados para gpon {pon} onu {onu_id}.\n")
    else:
        sys.stderr.write(
            f"[WARN] Nenhum MAC parseado para gpon {pon} onu {onu_id}. Veja a saÃ­da acima.\n"
        )
    return macs


def _open_connection(olt_ip: str, user: str, password: str, timeout: float) -> paramiko.SSHClient:
    try:  # conector isolado -> IP virtual (vnat) so na conexao
        from app.services import connector_routing_vnat as _vnat
        _conn_host = _vnat.reach_olt_ip(olt_ip) or olt_ip
    except Exception:
        _conn_host = olt_ip
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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


_BULK_MAC_RE = re.compile(
    r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+gpon\s+(\d+)\s+onu\s+(\d+)\b.*?vlan\s+(\S+)",
    re.IGNORECASE,
)


def parse_bulk_macs_all(output: str) -> Dict[Tuple[int, int], List[Dict]]:
    """Parseia a saida de 'bridge show mac all' -- UM comando so que traz
    todos os MACs aprendidos pela OLT inteira (todas as PONs, todas as
    ONUs), em vez de um comando 'bridge show mac gpon <pon> onu <onu>' por
    ONU. So os registros com interface 'gpon <pon> onu <onu>' interessam
    aqui; os de 'eth <n>' sao MACs do lado de uplink, fora do escopo desta
    coleta (o comando antigo, por ONU, nunca os incluia).

    Retorna {(pon, onu_id): [{"cpe_mac": ..., "vlan": ...}, ...]}.
    """
    by_onu: Dict[Tuple[int, int], List[Dict]] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _BULK_MAC_RE.match(line)
        if not m:
            continue
        mac = normalize_mac(m.group(1))
        if not mac:
            continue
        pon_id, onu_id, vlan = int(m.group(2)), int(m.group(3)), m.group(4)
        by_onu.setdefault((pon_id, onu_id), []).append({"cpe_mac": mac, "vlan": vlan})
    return by_onu


# ============
# FUNÃ‡ÃƒO P/ BACKEND (FastAPI)
# ============

def collect_macs_8820i(
    olt_ip: str,
    user: str,
    password: str,
    pon: str = "all",
    olt_name: str = "OLT-8820I",
    timeout: float = 12.0,
) -> List[Dict]:
    """
    Coleta MACs por ONU na OLT Intelbras 8820i e retorna lista de dicts.
    Pode receber PON especÃ­fica (ex: "1") ou "all".

    Antes, os MACs de cada ONU eram lidos com um comando por ONU
    ('bridge show mac gpon <pon> onu <onu>' repetido em serie para cada ONU
    autorizada) -- numa OLT com ~230 ONUs isso da ~230 idas-e-voltas SSH
    sequenciais. Tentativa de paralelizar essas chamadas (varios channels ou
    varias conexoes SSH simultaneas) foi testada contra uma OLT 8820i real e
    NAO funciona: essa OLT so sustenta uma sessao SSH por vez -- varias
    conexoes ao mesmo tempo travam ou derrubam a sessao.

    A coleta agora usa 'bridge show mac all', que traz a tabela de MACs da
    OLT INTEIRA (todas as PONs, todas as ONUs) num unico comando -- testado
    contra OLT real: coleta completa de ~230 ONUs caiu de ~134s para ~15s,
    tudo numa sessao so (compativel com a limitacao acima). Se o comando nao
    existir ou nao devolver nada reconhecivel (firmware diferente), a coleta
    cai de volta no modo antigo, um comando por ONU, para nao perder
    compatibilidade com outras versoes de firmware da 8820i.
    """
    sys.stderr.write(f"[INFO] Conectando em {olt_ip}...\n")
    client = _open_connection(olt_ip, user, password, timeout)

    try:
        chan = open_shell(client)
        onu_status_cache: Dict[int, str] = {}

        # Descobre quais PONs vamos varrer
        if pon.lower() == "all":
            sys.stderr.write("[INFO] Varredura automÃ¡tica de PONs usando 'onu status gpon <pon>'...\n")
            pons: List[int] = []
            any_output = False
            for p in range(1, 9):
                out_status = cli_run(chan, f"onu status gpon {p}", timeout=10.0)
                if out_status.strip():
                    any_output = True
                if "Configured ONUs" in out_status:
                    sys.stderr.write(f"[INFO] PON {p} encontrada (Configured ONUs).\n")
                    pons.append(p)
                    onu_status_cache[p] = out_status
            if not pons:
                sys.stderr.write(
                    "[WARN] Nenhuma PON encontrada entre 1 e 8 com 'onu status gpon <pon>'.\n"
                )
                # As 8 consultas devolveram vazio/em branco -- diferente de
                # "a OLT respondeu e nao tem PON configurada", isso indica
                # sessao/prompt quebrado (CLI mudou, comando nao reconhecido,
                # conexao instavel). Sem essa distincao, esse caso virava uma
                # lista vazia silenciosa igual a uma OLT genuinamente sem
                # PON -- e uma coleta "vazia" apagava inventario existente.
                if not any_output:
                    raise RuntimeError(
                        "Nenhuma resposta reconhecivel da OLT em nenhuma das 8 consultas de "
                        "PON (sessao/prompt pode estar quebrado, ou o comando 'onu status gpon' "
                        "nao e reconhecido neste firmware) -- diferente de uma OLT que respondeu "
                        "e genuinamente nao tem PON configurada."
                    )
        else:
            try:
                pons = [int(pon)]
            except ValueError:
                sys.stderr.write(
                    "[ERRO] Valor invÃ¡lido para pon. Use um nÃºmero (ex: 1) ou 'all'.\n"
                )
                return []

        if not pons:
            sys.stderr.write("[ERRO] Nenhuma PON para processar.\n")
            return []

        onus_by_pon: Dict[int, List[Dict]] = {}
        for p in pons:
            sys.stderr.write(f"[INFO] Lendo ONUs da gpon {p} com 'onu status gpon {p}'...\n")
            out_onu = onu_status_cache.get(p) or cli_run(chan, f"onu status gpon {p}", timeout=15.0)
            onus = parse_onu_status(out_onu, p)
            if not onus:
                sys.stderr.write(
                    f"[WARN] Nenhuma ONU encontrada em gpon {p}. "
                    "Verifique se o comando e o parser estÃ£o corretos.\n"
                )
                continue
            onus_by_pon[p] = onus

        rows: List[Dict] = []
        if not onus_by_pon:
            return rows

        total_onus = sum(len(v) for v in onus_by_pon.values())
        sys.stderr.write(
            f"[INFO] Coletando MACs de {total_onus} ONU(s) via 'bridge show mac all'...\n"
        )
        bulk_macs: Dict[Tuple[int, int], List[Dict]] = {}
        try:
            bulk_out = cli_run(chan, "bridge show mac all", timeout=max(timeout, 30.0))
            bulk_macs = parse_bulk_macs_all(bulk_out)
        except Exception as exc:
            sys.stderr.write(f"[WARN] 'bridge show mac all' falhou ({exc}), usando modo antigo.\n")
        if not bulk_macs:
            sys.stderr.write(
                "[WARN] 'bridge show mac all' nao trouxe MACs reconheciveis -- "
                "caindo no modo antigo (um comando por ONU).\n"
            )

        for p, onus in onus_by_pon.items():
            for onu in onus:
                onu_id = int(onu["onu_id"])
                onu = enrich_onu_signal(chan, onu, p, onu_id)
                if bulk_macs:
                    macs = bulk_macs.get((p, onu_id), [])
                else:
                    sys.stderr.write(f"[INFO] Coletando MACs de gpon {p} onu {onu_id}...\n")
                    macs = get_macs_for_onu(chan, p, onu_id)
                for m in macs:
                    rows.append(
                        {
                            "cpe_mac": m["cpe_mac"],
                            "pon": p,
                            "onu_id": onu_id,
                            "onu_name": onu["name"],
                            "onu_serial": onu["serial"],
                            "onu_model": onu["model"],
                            "oper_status": onu.get("oper_status", ""),
                            "omci_status": onu.get("omci_status", ""),
                            "rx_olt": onu.get("rx_olt", ""),
                            "rx_onu": onu.get("rx_onu", ""),
                            "rx_power": onu.get("rx_power", ""),
                            "onu_rx_power": onu.get("onu_rx_power", ""),
                            "signal": onu.get("signal", ""),
                            "distance_km": onu.get("distance_km", ""),
                            "uptime": onu.get("uptime", ""),
                            "olt_ip": olt_ip,
                            "olt_name": olt_name,
                            "vlan": m.get("vlan", ""),
                        }
                    )

        return rows
    finally:
        client.close()


# ============
# CLI TRADICIONAL
# ============

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Coleta MACs por ONU na OLT Intelbras 8820i e gera saida/olt-cpe-macs.json. "
            "Use --pon N para PON especÃ­fica ou --pon all para todas (1-8)."
        )
    )
    parser.add_argument("--olt-ip", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--pon",
        required=True,
        help="PON desejada (ex: 1) ou 'all' para todas as PONs detectadas (1-8).",
    )
    parser.add_argument("--olt-name", default="OLT-8820I")
    parser.add_argument("--out", default="saida/olt-cpe-macs.json")
    args = parser.parse_args()

    rows = collect_macs_8820i(
        olt_ip=args.olt_ip,
        user=args.user,
        password=args.password,
        pon=args.pon,
        olt_name=args.olt_name,
    )

    if not rows:
        sys.stderr.write(
            "[WARN] Nenhum MAC coletado. O CSV serÃ¡ criado, mas estarÃ¡ vazio.\n"
        )

    fieldnames = [
        "cpe_mac",
        "pon",
        "onu_id",
        "onu_name",
        "onu_serial",
        "onu_model",
        "olt_ip",
        "olt_name",
        "vlan",
    ]

    import os
    import json
    from datetime import datetime

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    payload = {
        "meta": {
            "kind": "olt_cpe_macs",
            "olt_ip": args.olt_ip,
            "olt_name": getattr(args, "olt_name", ""),
            "scan_time": datetime.now().astimezone().isoformat(),
            "total_cpes": len(rows),
        },
        "cpes": rows,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    sys.stderr.write(f"[INFO] JSON gerado em: {args.out}\n")


if __name__ == "__main__":
    main()


