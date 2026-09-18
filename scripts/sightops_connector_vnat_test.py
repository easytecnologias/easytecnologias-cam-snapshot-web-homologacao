"""Testes do NAT 1:1 por conector (modelo A): alocador virtual + reader do app.

- ops/connector_routing/allocator: _virtual_slice / virtual_map_for
- app/services/connector_routing_vnat: virtual_ip_for (GATED)

Os valores batem com as regras aplicadas no host em 2026-09-15
(Porto Real 192.168.10.0/24 -> 10.208.0.0/24, Mata Grande -> 10.208.64.0/24).
"""
import importlib
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from ops.connector_routing import allocator as A


def test_virtual_slice_por_index():
    assert A.derive(1, "pr")["virtual_slice"] == "10.208.0.0/18"
    assert A.derive(2, "mg")["virtual_slice"] == "10.208.64.0/18"
    assert A.derive(3, "x")["virtual_slice"] == "10.208.128.0/18"


def test_virtual_map_empacota_alinhado():
    # index 1: primeira LAN cai na base do /18
    assert A.virtual_map_for(1, ["192.168.10.0/24"]) == [
        {"real_cidr": "192.168.10.0/24", "virtual_cidr": "10.208.0.0/24"}
    ]
    # index 2: /24 na base, /20 alinhado ao proprio tamanho (nao em 10.208.65.0)
    got = A.virtual_map_for(2, ["192.168.10.0/24", "172.16.16.0/20"])
    assert got == [
        {"real_cidr": "192.168.10.0/24", "virtual_cidr": "10.208.64.0/24"},
        {"real_cidr": "172.16.16.0/20", "virtual_cidr": "10.208.80.0/20"},
    ]


def test_virtual_ip_for_preserva_host_e_e_gated():
    m = {
        "PR": [{"real_cidr": "192.168.10.0/24", "virtual_cidr": "10.208.0.0/24"}],
        "MG": [{"real_cidr": "192.168.10.0/24", "virtual_cidr": "10.208.64.0/24"}],
    }
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(m, fh)
    fh.close()
    os.environ["CONNECTOR_VNAT_MAP"] = fh.name
    from app.services import connector_routing_vnat as V
    importlib.reload(V)
    try:
        assert V.virtual_ip_for("PR", "192.168.10.50") == "10.208.0.50"
        assert V.virtual_ip_for("MG", "192.168.10.50") == "10.208.64.50"
        # mesmo IP real, conectores diferentes -> IPs virtuais diferentes (no leak)
        assert V.virtual_ip_for("PR", "192.168.10.50") != V.virtual_ip_for("MG", "192.168.10.50")
        # GATED: conector sem mapa / IP fora da LAN -> real intacto
        assert V.virtual_ip_for("DESCONHECIDO", "192.168.10.50") == "192.168.10.50"
        assert V.virtual_ip_for("PR", "10.0.0.9") == "10.0.0.9"
        assert V.virtual_ip_for("PR", "") == ""
        assert V.virtual_ip_for("", "192.168.10.50") == "192.168.10.50"
    finally:
        os.unlink(fh.name)
        os.environ.pop("CONNECTOR_VNAT_MAP", None)


def test_inverso_e_alvo():
    m = {"PR": [{"real_cidr": "192.168.10.0/24", "virtual_cidr": "10.208.0.0/24"}]}
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(m, fh)
    fh.close()
    os.environ["CONNECTOR_VNAT_MAP"] = fh.name
    from app.services import connector_routing_vnat as V
    importlib.reload(V)
    try:
        # inverso: virtual -> real (des-virtualizar o inventario)
        assert V.real_ip_for("PR", "10.208.0.202") == "192.168.10.202"
        assert V.real_ip_for("PR", "8.8.8.8") == "8.8.8.8"  # fora do range -> intacto
        # ida e volta
        assert V.real_ip_for("PR", V.virtual_ip_for("PR", "192.168.10.7")) == "192.168.10.7"
        assert V.has_mapping("PR") and not V.has_mapping("MG")
        # tradutor de alvo: IP, CIDR, range completo, range so ultimo octeto, lista
        assert V.virtualize_target("PR", "192.168.10.202") == "10.208.0.202"
        assert V.virtualize_target("PR", "192.168.10.0/24") == "10.208.0.0/24"
        assert V.virtualize_target("PR", "192.168.10.1-192.168.10.50") == "10.208.0.1-10.208.0.50"
        assert V.virtualize_target("PR", "192.168.10.1-50") == "10.208.0.1-50"
        assert V.virtualize_target("PR", "192.168.10.5,192.168.10.9") == "10.208.0.5,10.208.0.9"
        # GATED: conector sem mapa -> alvo intacto
        assert V.virtualize_target("MG", "192.168.10.5") == "192.168.10.5"
    finally:
        os.unlink(fh.name)
        os.environ.pop("CONNECTOR_VNAT_MAP", None)


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("PASSOU")


if __name__ == "__main__":
    _run()
