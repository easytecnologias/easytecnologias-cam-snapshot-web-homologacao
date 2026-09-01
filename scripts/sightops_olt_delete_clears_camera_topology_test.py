"""Excluir uma ONU (VSOL/4840E/8820i) tem que tirar a topologia (PON/ONU/
sinal) das cameras que ainda apontavam pra ela na tela de Cameras IP.

Achado ao vivo (Japaratinga, 2026-09-01): o usuario excluiu a ONU 0/2/7 e as
4 cameras atras dela continuaram mostrando PON/ONU/"ONU online" antigos.
Causa: o merge do frontend (cameras.js) so ATUALIZA um campo quando acha o
MAC de novo do lado da OLT -- nunca limpa sozinho quando o MAC some porque a
ONU foi excluida. `_clear_deleted_onu_from_camera_inventory` corrige isso
comparando por posicao (olt_ip + pon + onu_id), normalizando PON no formato
EPON ("0/2") pro mesmo texto que o GPON usa ("2") -- sem isso a comparacao
nunca bate pra VSOL/4840E.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import olt_service


def main() -> None:
    # _pon_num normaliza "0/2" (EPON), "2" (GPON) e 2 (int) pro mesmo texto
    assert olt_service._pon_num("0/2") == "2", olt_service._pon_num("0/2")
    assert olt_service._pon_num("2") == "2"
    assert olt_service._pon_num(2) == "2"
    assert olt_service._pon_num("0/12") == "12"

    cameras = [
        {  # camera atras da ONU excluida (PON formatado como EPON, "0/2")
            "ip": "100.66.11.5", "mac": "54:6c:ac:03:89:91", "titulo": "5 - TREVO 1",
            "pon": "0/2", "onu_id": "7", "onu_name": "epon 0/2 onu 7",
            "onu_serial": "98E55B4011E0", "onu_model": "R1v2",
            "onu_oper_status": "up", "onu_omci_status": "OK",
            "onu_rx": "-11.52", "olt_rx": "", "onu_telemetry_updated_at": "2026-09-01T12:00:00Z",
            "olt_ip": "192.168.200.2", "olt_name": "OLT-VSOL-JAPARATINGA", "vlan": "2000",
        },
        {  # outra camera, mesma OLT, ONU DIFERENTE (0/2 onu 3) -- nao pode ser tocada
            "ip": "100.66.11.9", "mac": "54:6c:ac:25:e7:a4", "titulo": "9 - CORUMBA 2",
            "pon": "0/2", "onu_id": "3", "onu_name": "epon 0/2 onu 3",
            "onu_serial": "80854..", "olt_ip": "192.168.200.2", "vlan": "2000",
        },
        {  # camera de outra OLT inteira -- nao pode ser tocada
            "ip": "10.10.11.47", "mac": "30:e1:f1:1a:9b:a3", "titulo": "28 - FRONTAL",
            "pon": "7", "onu_id": "7", "olt_ip": "10.80.80.5", "vlan": "3000",
        },
    ]
    saved: list[dict] = []
    old_load = olt_service.load_inventory_json
    old_save = olt_service.save_inventory_json
    try:
        olt_service.load_inventory_json = lambda mode="olt": [dict(row) for row in cameras]
        olt_service.save_inventory_json = lambda rows, mode="olt": saved.extend(dict(row) for row in rows)

        req = olt_service.OltDeleteOnuRequest(
            olt_ip="192.168.200.2", user="admin", password="x",
            olt_vendor="vsol_epon", olt_model="vsol_epon",
            pon=2, onu=7, serial="98:e5:5b:40:11:e0",
        )
        result = olt_service._clear_deleted_onu_from_camera_inventory(req)
    finally:
        olt_service.load_inventory_json = old_load
        olt_service.save_inventory_json = old_save

    assert result["cleared"] == 1, result
    assert len(saved) == 3, saved

    alvo = next(r for r in saved if r["mac"] == "54:6c:ac:03:89:91")
    for field in olt_service._ONU_TOPOLOGY_FIELDS:
        assert alvo.get(field, "") == "", (field, alvo)
    assert alvo["titulo"] == "5 - TREVO 1", alvo
    assert alvo["ip"] == "100.66.11.5", alvo

    outra_onu = next(r for r in saved if r["mac"] == "54:6c:ac:25:e7:a4")
    assert outra_onu["onu_id"] == "3", outra_onu
    assert outra_onu["pon"] == "0/2", outra_onu

    outra_olt = next(r for r in saved if r["mac"] == "30:e1:f1:1a:9b:a3")
    assert outra_olt["onu_id"] == "7", outra_olt

    print("OK: excluir ONU EPON limpa so a topologia das cameras dela na tela de Cameras IP")


if __name__ == "__main__":
    main()
