"""Fetch HTTP pro proxy web de camera/DVR/NVR.

Isolado de app/api/endpoints/* de proposito: esse pacote tem um import
quebrado pre-existente (app.services.olt_service, nao relacionado a este
trabalho) que impede importar qualquer coisa de la num script local. Este
modulo fica em app/services/ especificamente pra continuar testavel.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple
from urllib.parse import urlunsplit

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

RESPONSE_HEADER_ALLOWLIST = {
    "content-type", "cache-control", "pragma", "expires", "www-authenticate",
}

# scheme que funcionou da ultima vez, por host -- evita pagar a tentativa
# dupla em toda sub-requisicao (JS/CSS/imagem) da mesma pagina. So mora em
# memoria: zera a cada restart/deploy (o chamador pode semear de volta um
# valor persistido via seed_scheme).
_scheme_cache: Dict[str, str] = {}

# timeout de conexao curto para a tentativa "as cegas" de esquema (quando
# ainda nao sabemos qual funciona) -- a maioria das cameras/DVRs de CFTV nao
# escuta em 443, e via WireGuard a rejeicao costuma nao ser instantanea, so
# expira no timeout normal (4s). A tentativa que realmente tem chance de dar
# certo (a ultima da lista, ou a que ja esta em cache) usa o timeout normal.
_PROBE_CONNECT_TIMEOUT = 1.5


class DeviceUnreachable(Exception):
    pass


def get_cached_scheme(host: str) -> str:
    """Esquema (http/https) que funcionou por ultimo para este host, se este
    processo ja descobriu -- vazio se ainda nao sabe."""
    return _scheme_cache.get(host, "")


def seed_scheme(host: str, scheme: str) -> None:
    """Preenche o cache em memoria com um esquema conhecido de fora (ex.:
    persistido em disco de uma execucao anterior), sem sobrescrever um valor
    que este processo ja descobriu sozinho."""
    if scheme in ("http", "https"):
        _scheme_cache.setdefault(host, scheme)


def build_target_url(scheme: str, host: str, path: str, query: str, http_port: int = 80) -> str:
    clean_path = "/" + str(path or "").lstrip("/")
    netloc = host if (scheme == "https" or not http_port or http_port == 80) else f"{host}:{http_port}"
    return urlunsplit((scheme, netloc, clean_path, str(query or ""), ""))


def _tentar_schemes(host: str) -> Tuple[str, ...]:
    lembrado = _scheme_cache.get(host)
    if lembrado == "http":
        return ("http", "https")
    return ("https", "http")


def fetch_device(
    host: str,
    path: str,
    query: str,
    method: str,
    headers: Dict[str, str],
    body: bytes,
    username: str = "",
    password: str = "",
    *,
    http_port: int = 80,
    timeout: Tuple[float, float] = (4.0, 25.0),
) -> requests.Response:
    """Fala com o equipamento, tentando HTTPS e HTTP (o que ja funcionou da
    ultima vez primeiro), e credencial Basic/Digest quando ha senha salva.

    So cai pro proximo esquema quando a CONEXAO falha (equipamento nao
    escuta naquela porta/protocolo) -- erro HTTP normal do proprio
    equipamento (404, 500, o proprio 401 de login) conta como resposta
    valida e nao dispara fallback nenhum. Outros erros de rede (timeout,
    SSL, encoding quebrado) nao significam "esquema errado" -- nao disparam
    fallback, mas tambem nao podem escapar crus: viram DeviceUnreachable.
    """
    auth = HTTPBasicAuth(username, password) if (username and password) else None
    ultimo_erro: Optional[Exception] = None
    resposta: Optional[requests.Response] = None
    scheme_usado = ""

    try:
        schemes = _tentar_schemes(host)
        for indice, scheme in enumerate(schemes):
            url = build_target_url(scheme, host, path, query, http_port)
            # so e "as cegas" se ainda nao ha certeza nenhuma sobre este
            # scheme (nem em cache) e ainda sobra outro esquema pra tentar
            # depois -- a ultima tentativa da lista sempre usa o timeout
            # normal, e a que ja bateu com o cache tambem.
            eh_as_cegas = scheme != _scheme_cache.get(host) and indice < len(schemes) - 1
            tentativa_timeout = (_PROBE_CONNECT_TIMEOUT, timeout[1]) if eh_as_cegas else timeout
            try:
                resposta = requests.request(
                    method, url, headers=headers,
                    data=body if body else None,
                    timeout=tentativa_timeout, allow_redirects=False, verify=False, auth=auth,
                )
                scheme_usado = scheme
                break
            except requests.exceptions.ConnectionError as exc:
                ultimo_erro = exc
                continue

        if resposta is None:
            raise DeviceUnreachable(f"{host} nao respondeu em https nem http: {ultimo_erro}")

        _scheme_cache[host] = scheme_usado

        if (
            resposta.status_code == 401
            and username and password
            and "digest" in (resposta.headers.get("WWW-Authenticate") or "").lower()
        ):
            url = build_target_url(scheme_usado, host, path, query, http_port)
            resposta = requests.request(
                method, url, headers=headers,
                data=body if body else None,
                timeout=timeout, allow_redirects=False, verify=False,
                auth=HTTPDigestAuth(username, password),
            )
    except DeviceUnreachable:
        raise
    except requests.exceptions.RequestException as exc:
        raise DeviceUnreachable(f"{host} deu erro de rede: {exc}") from exc

    return resposta


def filter_response_headers(upstream_headers) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in upstream_headers.items():
        if key.lower() in RESPONSE_HEADER_ALLOWLIST:
            out[key] = value
    return out
