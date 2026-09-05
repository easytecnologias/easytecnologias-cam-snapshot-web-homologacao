"""Fetch do proxy web de camera/DVR/NVR: fallback de esquema e login
Basic/Digest automatico."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import requests


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.services import device_web_proxy as proxy

    proxy._scheme_cache.clear()

    class Resposta:
        def __init__(self, status_code=200, headers=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.content = b"ok"

    # --- HTTPS falha (equipamento so escuta HTTP) -- cai pro HTTP sozinho
    def fake_request_https_falha(method, url, **kw):
        if url.startswith("https://"):
            raise requests.exceptions.ConnectionError("recusado")
        return Resposta(200)

    with patch.object(proxy.requests, "request", side_effect=fake_request_https_falha):
        resp = proxy.fetch_device("10.0.0.5", "/index.html", "", "GET", {}, b"")
    assert resp.status_code == 200
    assert proxy._scheme_cache["10.0.0.5"] == "http", proxy._scheme_cache

    # --- host que ja tinha funcionado por HTTP tenta HTTP primeiro na proxima vez
    chamadas = []

    def fake_request_grava_ordem(method, url, **kw):
        chamadas.append(url.split("://")[0])
        return Resposta(200)

    with patch.object(proxy.requests, "request", side_effect=fake_request_grava_ordem):
        proxy.fetch_device("10.0.0.5", "/outra.html", "", "GET", {}, b"")
    assert chamadas == ["http"], chamadas  # nao tentou https de novo

    # --- os dois esquemas falham -> DeviceUnreachable
    def fake_request_sempre_falha(method, url, **kw):
        raise requests.exceptions.ConnectionError("recusado")

    with patch.object(proxy.requests, "request", side_effect=fake_request_sempre_falha):
        try:
            proxy.fetch_device("10.0.0.9", "/", "", "GET", {}, b"")
            raise AssertionError("deveria ter levantado DeviceUnreachable")
        except proxy.DeviceUnreachable:
            pass

    # --- credencial salva: tenta Basic primeiro
    proxy._scheme_cache.clear()
    autenticacoes = []

    def fake_request_basic_ok(method, url, auth=None, **kw):
        autenticacoes.append(type(auth).__name__ if auth else None)
        return Resposta(200)

    with patch.object(proxy.requests, "request", side_effect=fake_request_basic_ok):
        proxy.fetch_device("10.0.0.7", "/", "", "GET", {}, b"", username="admin", password="1234")
    assert autenticacoes == ["HTTPBasicAuth"], autenticacoes

    # --- equipamento pede Digest -> tenta de novo com Digest, sem o operador ver 401
    proxy._scheme_cache.clear()
    tentativas = []

    def fake_request_precisa_digest(method, url, auth=None, **kw):
        tipo = type(auth).__name__ if auth else None
        tentativas.append(tipo)
        if tipo == "HTTPBasicAuth":
            return Resposta(401, headers={"WWW-Authenticate": 'Digest realm="cam", nonce="abc"'})
        return Resposta(200)

    with patch.object(proxy.requests, "request", side_effect=fake_request_precisa_digest):
        resp = proxy.fetch_device("10.0.0.8", "/", "", "GET", {}, b"", username="admin", password="1234")
    assert tentativas == ["HTTPBasicAuth", "HTTPDigestAuth"], tentativas
    assert resp.status_code == 200

    # --- senha errada mesmo com Digest -> devolve o 401 real (com header),
    #     nao trava nem esconde do operador
    proxy._scheme_cache.clear()

    def fake_request_senha_errada(method, url, auth=None, **kw):
        return Resposta(401, headers={"WWW-Authenticate": 'Digest realm="cam", nonce="abc"'})

    with patch.object(proxy.requests, "request", side_effect=fake_request_senha_errada):
        resp = proxy.fetch_device("10.0.0.8", "/", "", "GET", {}, b"", username="admin", password="errada")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers

    # --- http_port customizado (equipamento fora da porta 80) aparece so no esquema http
    proxy._scheme_cache.clear()
    urls_chamadas = []

    def fake_request_porta_customizada(method, url, **kw):
        urls_chamadas.append(url)
        if url.startswith("https://"):
            raise requests.exceptions.ConnectionError("recusado")
        return Resposta(200)

    with patch.object(proxy.requests, "request", side_effect=fake_request_porta_customizada):
        proxy.fetch_device("10.0.0.6", "/", "", "GET", {}, b"", http_port=8080)
    assert urls_chamadas[0] == "https://10.0.0.6/", urls_chamadas  # https sempre porta padrao
    assert urls_chamadas[1] == "http://10.0.0.6:8080/", urls_chamadas  # http usa a porta customizada

    # --- Timeout (nao e ConnectionError) nao dispara fallback de esquema,
    #     mas tambem nao pode escapar cru -- vira DeviceUnreachable
    proxy._scheme_cache.clear()

    def fake_request_timeout(method, url, **kw):
        raise requests.exceptions.ReadTimeout("equipamento travou")

    with patch.object(proxy.requests, "request", side_effect=fake_request_timeout):
        try:
            proxy.fetch_device("10.0.0.10", "/", "", "GET", {}, b"")
            raise AssertionError("deveria ter levantado DeviceUnreachable")
        except proxy.DeviceUnreachable:
            pass

    # --- erro de rede durante o retry de Digest tambem vira DeviceUnreachable
    proxy._scheme_cache.clear()

    def fake_request_digest_falha_rede(method, url, auth=None, **kw):
        tipo = type(auth).__name__ if auth else None
        if tipo == "HTTPBasicAuth":
            return Resposta(401, headers={"WWW-Authenticate": 'Digest realm="cam", nonce="abc"'})
        raise requests.exceptions.SSLError("certificado ruim no retry")

    with patch.object(proxy.requests, "request", side_effect=fake_request_digest_falha_rede):
        try:
            proxy.fetch_device("10.0.0.11", "/", "", "GET", {}, b"", username="admin", password="1234")
            raise AssertionError("deveria ter levantado DeviceUnreachable")
        except proxy.DeviceUnreachable:
            pass

    # --- filter_response_headers deixa passar www-authenticate (faltava
    #     antes desta correcao) e descarta o que nao esta na lista
    filtrados = proxy.filter_response_headers({
        "Content-Type": "text/html",
        "WWW-Authenticate": 'Basic realm="cam"',
        "Server": "nao deve passar",
        "Content-Length": "123",
    })
    assert filtrados == {"Content-Type": "text/html", "WWW-Authenticate": 'Basic realm="cam"'}, filtrados

    print("device_web_proxy fetch com fallback e login automatico ok")


if __name__ == "__main__":
    main()
