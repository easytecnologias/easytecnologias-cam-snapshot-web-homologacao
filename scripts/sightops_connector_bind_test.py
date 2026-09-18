"""Testes do bind por conector (app/services/connector_routing_bind).

Padrao local: autoexecutavel, asserts, sys.exit(1) em falha. Nao abre socket --
so confere a resolucao do IP de origem e o gating da sessao.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import connector_routing_bind as B


def _use_state(tmpdir, connectors):
    path = os.path.join(tmpdir, "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"connectors": connectors}, fh)
    B.STATE_PATH = path
    B._cache.update({"mtime": None, "at": 0.0, "map": {}})  # zera cache
    return path


def test_source_ip_lookup():
    with tempfile.TemporaryDirectory() as d:
        _use_state(d, {
            "mata-grande": {"server_ip": "10.201.0.4", "index": 2},
            "sem-ip": {"index": 9},
        })
        assert B.source_ip_for_connector("mata-grande") == "10.201.0.4"
        assert B.source_ip_for_connector("desconhecido") is None, "conector nao alocado -> None"
        assert B.source_ip_for_connector("sem-ip") is None, "sem server_ip -> None"
        assert B.source_ip_for_connector("") is None
        assert B.source_ip_for_connector(None) is None


def test_estado_ausente_nao_quebra():
    B.STATE_PATH = "/caminho/que/nao/existe/state.json"
    B._cache.update({"mtime": None, "at": 0.0, "map": {}})
    assert B.source_ip_for_connector("qualquer") is None, "sem arquivo -> None, sem excecao"


def test_bound_session_gated():
    with tempfile.TemporaryDirectory() as d:
        _use_state(d, {"mata-grande": {"server_ip": "10.201.0.4"}})
        # conector alocado -> sessao amarrada no IP de origem
        s = B.bound_session("mata-grande")
        ad = s.get_adapter("http://192.168.10.5/")
        assert isinstance(ad, B._SourceAddressAdapter), "deveria amarrar source"
        assert ad._source == ("10.201.0.4", 0)
        # mesma amarra em https
        assert isinstance(s.get_adapter("https://192.168.10.5/"), B._SourceAddressAdapter)

        # conector SEM alocacao -> sessao normal (comportamento de hoje)
        s2 = B.bound_session("nao-alocado")
        ad2 = s2.get_adapter("http://192.168.10.5/")
        assert not isinstance(ad2, B._SourceAddressAdapter), "sem alocacao nao pode amarrar"


def test_source_ip_via_kwarg():
    # passar source_ip direto amarra mesmo sem estado (util pra chamada explicita)
    B._cache.update({"mtime": None, "at": 0.0, "map": {}})
    s = B.bound_session(source_ip="1.2.3.4")
    ad = s.get_adapter("http://x/")
    assert isinstance(ad, B._SourceAddressAdapter) and ad._source == ("1.2.3.4", 0)


def main():
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in testes:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(testes)} testes passaram")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FALHOU: {e}")
        sys.exit(1)
