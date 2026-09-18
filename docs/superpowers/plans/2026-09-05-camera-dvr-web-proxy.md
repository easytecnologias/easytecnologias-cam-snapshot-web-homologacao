# Proxy Web de Câmera/DVR/NVR — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer o botão "Web" (câmera, e agora também DVR/NVR) abrir a
interface administrativa real do equipamento de forma confiável — sem
depender do computador do operador ter VPN, com HTTPS funcionando, com login
automático pra câmera quando existe senha salva, e sem vazar proxy pra
equipamento de outro cliente.

**Architecture:** Um novo módulo `app/services/device_web_proxy.py`
concentra a parte testável sem FastAPI (tenta HTTPS depois HTTP, tenta Basic
depois Digest quando há credencial, filtra os headers de resposta que devem
voltar pro navegador). A rota já existente
`maintenance_camera_web_proxy` (`app/api/endpoints/maintenance.py`) passa a
chamá-lo, e ganha uma checagem de posse do IP no inventário do tenant atual
antes de proxiar (usando `_ip_in_inventory`/`_camera_row_for_ip`, já
existentes, mais um par de funções novas equivalentes pra DVR/NVR). No
frontend, `bootstrap.js` e `recorders.js` passam a abrir a mesma rota de
proxy em vez de `window.open('http://' + ip)` direto.

**Tech Stack:** Python (FastAPI, `requests`), JavaScript vanilla (sem
framework), scripts de teste standalone (`scripts/sightops_*_test.py`, sem
pytest).

## Global Constraints

- Sem pytest neste repositório — testes são scripts standalone em
  `scripts/sightops_*_test.py` que imprimem "ok" e saem com código 0, ou
  levantam `AssertionError`.
- `app.api.endpoints.maintenance` (e qualquer outro submódulo de
  `app.api.endpoints`) **não pode ser importado num script Python local** —
  `app/api/endpoints/__init__.py` importa `olt.py`, que importa
  `app.services.olt_service`, que falha com
  `ImportError: cannot import name 'ensure_connector_targets_allowed'`. Isso
  é um problema pré-existente e não relacionado a este trabalho — **não
  tentar corrigir**. Qualquer teste que precise importar código de
  `app/api/endpoints/*` diretamente não pode rodar localmente; nesses casos
  a tarefa diz explicitamente para validar contra o container real de
  produção (`docker exec sightops-prod-api python3 ...`), nunca criar um
  script `scripts/sightops_*_test.py` que vai falhar sempre por esse motivo.
- Todo código de rede pra equipamento usa `verify=False` (certificado
  autoassinado é normal em CFTV) — mesmo padrão já usado hoje no proxy.
- Nunca proxiar pra um IP/host que não esteja no inventário do tenant
  autenticado atual (`get_current_tenant_slug()`).
- Câmera ganha login automático (Basic, com retry em Digest) quando existe
  senha salva. DVR/NVR **não** ganha login automático nesta versão — não
  existe armazenamento de senha por gravador no sistema (decisão do usuário,
  2026-09-05, ver spec `docs/superpowers/specs/2026-09-05-camera-dvr-web-proxy-design.md`).
- Deploy em produção não faz parte deste plano — é um passo manual
  posterior, sob confirmação explícita do usuário.

---

## Task 1: `device_web_proxy.py` — fetch com fallback HTTPS→HTTP e login Basic/Digest

**Files:**
- Create: `app/services/device_web_proxy.py`
- Test: `scripts/sightops_device_web_proxy_test.py`

**Interfaces:**
- Produces: `fetch_device(host: str, path: str, query: str, method: str, headers: dict[str, str], body: bytes, username: str = "", password: str = "", *, timeout: tuple[float, float] = (4.0, 25.0)) -> requests.Response`
- Produces: `filter_response_headers(upstream_headers) -> dict[str, str]` (recebe qualquer objeto tipo-dict de headers de resposta do `requests`, ex. `response.headers`)
- Produces: `class DeviceUnreachable(Exception)`
- Produces: `RESPONSE_HEADER_ALLOWLIST: set[str]` (inclui `"www-authenticate"`, que hoje falta no proxy)

- [ ] **Step 1: Escrever o arquivo `app/services/device_web_proxy.py`**

```python
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
# dupla em toda sub-requisicao (JS/CSS/imagem) da mesma pagina.
_scheme_cache: Dict[str, str] = {}


class DeviceUnreachable(Exception):
    pass


def build_target_url(scheme: str, host: str, path: str, query: str) -> str:
    clean_path = "/" + str(path or "").lstrip("/")
    return urlunsplit((scheme, host, clean_path, str(query or ""), ""))


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
    timeout: Tuple[float, float] = (4.0, 25.0),
) -> requests.Response:
    """Fala com o equipamento, tentando HTTPS e HTTP (o que ja funcionou da
    ultima vez primeiro), e credencial Basic/Digest quando ha senha salva.

    So cai pro proximo esquema quando a CONEXAO falha (equipamento nao
    escuta naquela porta/protocolo) -- erro HTTP normal do proprio
    equipamento (404, 500, o proprio 401 de login) conta como resposta
    valida e nao dispara fallback nenhum.
    """
    auth = HTTPBasicAuth(username, password) if (username and password) else None
    ultimo_erro: Optional[Exception] = None
    resposta: Optional[requests.Response] = None
    scheme_usado = ""

    for scheme in _tentar_schemes(host):
        url = build_target_url(scheme, host, path, query)
        try:
            resposta = requests.request(
                method, url, headers=headers,
                data=body if body else None,
                timeout=timeout, allow_redirects=False, verify=False, auth=auth,
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
        url = build_target_url(scheme_usado, host, path, query)
        resposta = requests.request(
            method, url, headers=headers,
            data=body if body else None,
            timeout=timeout, allow_redirects=False, verify=False,
            auth=HTTPDigestAuth(username, password),
        )

    return resposta


def filter_response_headers(upstream_headers) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in upstream_headers.items():
        if key.lower() in RESPONSE_HEADER_ALLOWLIST:
            out[key] = value
    return out
```

- [ ] **Step 2: Escrever o teste**, mockando `requests.request` (sem servidor
  HTTP de verdade — mais simples e mais rápido, mesmo padrão já usado nesta
  sessão em `scripts/sightops_access_control_whatsapp_log_test.py` pra
  mockar `access_control_notifications.requests`):

```python
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
```

- [ ] **Step 3: Rodar o teste**

Run: `python scripts/sightops_device_web_proxy_test.py`
Expected: `device_web_proxy fetch com fallback e login automatico ok`, saída
sem traceback.

- [ ] **Step 4: Commit** (só se o usuário pediu commit nesta sessão — ver
  nota no fim do plano; caso contrário, deixar como alteração local)

```bash
git add app/services/device_web_proxy.py scripts/sightops_device_web_proxy_test.py
git commit -m "feat(maintenance): fetch de proxy web com fallback https/http e login basic/digest automatico"
```

---

## Task 2: Inventário de DVR/NVR por host (equivalente de `_ip_in_inventory` pra gravador)

**Files:**
- Modify: `app/api/endpoints/maintenance.py`

**Interfaces:**
- Consumes: `_load_rows_for_source(source: str, site: str = "", mode: str = "olt") -> list[dict]` (já existe em `maintenance.py`, linha ~616 — aceita `source in ("dvr", "nvr")`)
- Produces: `_recorder_row_for_host(host: str) -> dict | None`
- Produces: `_host_in_recorder_inventory(host: str) -> bool`

Este código mora em `app/api/endpoints/maintenance.py`, que **não pode ser
importado num script local** (ver Global Constraints). Não criar um
`scripts/sightops_*_test.py` pra isso — ele vai falhar sempre por um motivo
que não tem relação com o código escrito aqui. A validação é feita na Task 3,
contra o container de produção.

- [ ] **Step 1: Adicionar as duas funções em `app/api/endpoints/maintenance.py`**,
  logo antes de `maintenance_camera_web_proxy` (linha ~1181):

```python
def _recorder_row_for_host(host: str) -> Optional[Dict[str, Any]]:
    """Acha a linha do inventario de DVR/NVR (qualquer canal) para este
    host, no tenant atual -- mesmo padrao de _camera_row_for_ip em
    cameras.py, so que para gravador."""
    alvo = str(host or "").strip()
    if not alvo:
        return None
    for fonte in ("dvr", "nvr"):
        for linha in _load_rows_for_source(fonte):
            if isinstance(linha, dict) and str(linha.get("host") or "").strip() == alvo:
                return linha
    return None


def _host_in_recorder_inventory(host: str) -> bool:
    return _recorder_row_for_host(host) is not None
```

(Checar se `Optional`/`Dict`/`Any` já estão importados de `typing` no topo
do arquivo — muito provável que sim, dado o resto do arquivo já usa esses
tipos; se não estiverem, adicionar ao import existente de `typing`.)

- [ ] **Step 2: Validar por leitura** (sem rodar teste local, por causa do
  import quebrado): confirmar que `_load_rows_for_source("dvr")` e
  `_load_rows_for_source("nvr")` devolvem linhas com a chave `"host"`
  preenchida — já confirmado nesta sessão lendo `legacy_rows_from_db` em
  `app/services/db_store.py:1291-1316` (a query SQL seleciona `r.host`
  explicitamente e injeta no dict). Nenhuma ação necessária, só documentar
  que a suposição foi conferida.

- [ ] **Step 3: Commit**

```bash
git add app/api/endpoints/maintenance.py
git commit -m "feat(maintenance): funcao pra achar linha de DVR/NVR por host no inventario do tenant"
```

---

## Task 3: Corrigir `maintenance_camera_web_proxy` (HTTPS, headers, posse, login automático de câmera)

**⚠️ Achado importante antes desta task:** uma checagem de posse do IP
(`_ip_belongs_to_current_tenant`) **já foi corrigida e publicada em
produção antes**, num hotfix de auditoria de segurança anterior — ver
`docs/HANDOFF_AGENTES.md`, seção "Não reverter": *"A checagem de posse de
IP em `_camera_web_target_url`/`_ip_belongs_to_current_tenant` — sem ela,
um cliente volta a acessar câmera/serviço HTTP privado de outro."* Também
existe um teste pronto pra ela, `scripts/sightops_camera_web_proxy_test.py`
(já no repo, ainda não commitado), que monkeypatcha
`maintenance._ip_belongs_to_current_tenant` e espera exatamente esse nome.

**Confirmado ao vivo nesta sessão (2026-09-05) que essa proteção
REGREDIU**: extraí `app/api/endpoints/maintenance.py` do container real de
produção (`docker cp sightops-prod-api:...`) e `_camera_web_target_url` lá
é idêntica à versão local, sem `_ip_belongs_to_current_tenant` nenhuma —
o hotfix se perdeu num deploy posterior que partiu de uma tag de imagem
anterior ao hotfix (o mesmo tipo de drift já documentado várias vezes neste
projeto). Esta task portanto também **restaura uma proteção de segurança
que já existiu e sumiu**, não é feature nova do zero.

Por isso as funções abaixo usam o nome `_ip_belongs_to_current_tenant`
(não um nome novo) — pra bater com o teste que já existe e com a
convenção já registrada no handoff.

**Files:**
- Modify: `app/api/endpoints/maintenance.py:1181-1230` (rota
  `maintenance_camera_web_proxy`, e as funções auxiliares
  `_camera_web_target_url`/`_is_proxy_allowed_host` que ela usa hoje)
- Modify: `scripts/sightops_camera_web_proxy_test.py` (já existe — adicionar
  cobertura do caso de DVR/NVR, sem remover o que já está lá)

**Interfaces:**
- Consumes: `fetch_device(...)`, `filter_response_headers(...)`,
  `DeviceUnreachable` de `app.services.device_web_proxy` (Task 1)
- Consumes: `_host_in_recorder_inventory(host)` (Task 2)
- Consumes: `_ip_in_inventory(ip)`, `resolve_camera_password(ip, user, password)`
  de `app.api.endpoints.cameras` (já existem, já importados/importáveis
  neste arquivo — `maintenance.py` já importa de `cameras.py` hoje em outros
  pontos do arquivo)
- Produces: `_ip_belongs_to_current_tenant(ip: str) -> bool`

Este arquivo não pode ser importado num script local (ver Global
Constraints) — a validação desta task é **obrigatoriamente ao vivo**, contra
o container real, seguindo o mesmo padrão já usado nesta sessão (extrair o
arquivo do container, aplicar o patch, validar dentro de um container de
teste com `docker run`, só depois trocar o container de produção).

- [ ] **Step 1: Reescrever `_camera_web_target_url` (vira também o gate de posse) e a rota**

`_camera_web_target_url` continua existindo com a MESMA assinatura e o
MESMO retorno (uma URL `"http://..."`, pro teste já existente continuar
válido) — só ganha a checagem de posse no meio. A construção de URL com
fallback de esquema (Correção 1) fica em `device_web_proxy.build_target_url`,
usada só dentro do fetch de verdade, não aqui:

```python
from app.services.device_web_proxy import DeviceUnreachable, fetch_device, filter_response_headers


def _ip_belongs_to_current_tenant(ip: str) -> bool:
    """Confere se este IP pertence a uma camera OU um DVR/NVR cadastrado no
    tenant atual (nao IP arbitrario). Sem isso, um usuario logado em
    QUALQUER cliente consegue abrir a interface web de um IP privado de
    OUTRO cliente so sabendo o IP -- faixas privadas se repetem entre
    tenants neste sistema (mesmo raciocinio de _ip_in_inventory em
    cameras.py, agora cobrindo tambem o inventario de gravador)."""
    return _ip_in_inventory(ip) or _host_in_recorder_inventory(ip)


def _camera_web_target_url(ip: str, path: str = "", query: str = "") -> str:
    host = _as_str(ip)
    if not _is_proxy_allowed_host(host):
        raise HTTPException(status_code=400, detail="proxy web permitido apenas para IP privado/CGNAT")
    if not _ip_belongs_to_current_tenant(host):
        raise HTTPException(status_code=403, detail=f"{host} nao pertence a nenhum equipamento deste cliente")
    clean_path = "/" + str(path or "").lstrip("/")
    if ".." in clean_path.split("/"):
        raise HTTPException(status_code=400, detail="caminho invalido")
    return urlunsplit(("http", host, clean_path, str(query or ""), ""))


@router.api_route("/maintenance/web/{ip}/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/maintenance/web/{ip}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def maintenance_camera_web_proxy(ip: str, request: Request, path: str = ""):
    """Proxy HTTP da interface web da camera/DVR/NVR via servidor/WireGuard."""
    # so o efeito colateral de validar importa aqui (400 IP publico, 403
    # fora do inventario do tenant) -- a URL de fato usada no fetch vem de
    # fetch_device, que tenta https e http.
    _camera_web_target_url(ip, path, str(request.url.query or ""))

    username, password = resolve_camera_password(ip, "", "")

    headers: dict[str, str] = {
        "User-Agent": request.headers.get("user-agent") or "SightOps device web proxy",
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Language": request.headers.get("accept-language") or "pt-BR,pt;q=0.9,en;q=0.8",
    }
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    cookie = _camera_cookie_header(request)
    if cookie:
        headers["Cookie"] = cookie
    body = await request.body()

    try:
        upstream = fetch_device(
            ip, path, str(request.url.query or ""), request.method, headers, body,
            username=username, password=password,
        )
    except DeviceUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    resp_headers: dict[str, str] = {
        "Cache-Control": "no-store",
        "X-Frame-Options": "SAMEORIGIN",
        **filter_response_headers(upstream.headers),
    }
    location = upstream.headers.get("location")
    if location:
        resp_headers["Location"] = _proxy_location_header(location, ip=ip)
    set_cookie = upstream.headers.get("set-cookie")
    if set_cookie:
        resp_headers["Set-Cookie"] = set_cookie

    media_type = upstream.headers.get("content-type") or "application/octet-stream"
    content = _rewrite_camera_web_content(upstream.content or b"", ip=ip, content_type=media_type)
    return Response(content=content, status_code=upstream.status_code, media_type=media_type, headers=resp_headers)
```

`resolve_camera_password(ip, "", "")` é seguro de chamar mesmo pra um IP de
DVR/NVR (não de câmera): `_camera_row_for_ip` não acha nada, `mac`/`site`
ficam vazios, e a função devolve `("admin", "")` — senha vazia, então
`fetch_device` simplesmente não manda credencial nenhuma, sem branch
especial precisar existir aqui.

(`_camera_cookie_header`, `_proxy_location_header`, `_rewrite_camera_web_content`,
`_is_proxy_allowed_host` continuam exatamente como estão hoje — só a rota e
`_camera_web_target_url` mudam. Adicionar o import de `_ip_in_inventory` e
`resolve_camera_password` de `app.api.endpoints.cameras` no topo do arquivo,
junto dos imports que já existem desse módulo.)

- [ ] **Step 1b: Estender `scripts/sightops_camera_web_proxy_test.py` com o caso de DVR/NVR**

Adicionar, sem remover nada do que já está no arquivo, logo antes do
`print("ok")`:

```python
    # --- host de DVR/NVR (sem estar no inventario de camera) tambem e
    #     aceito pela checagem de posse, e host de nenhum dos dois inventarios
    #     continua bloqueado
    maintenance._ip_in_inventory = lambda ip: False
    maintenance._host_in_recorder_inventory = lambda ip: True
    assert_true(maintenance._ip_belongs_to_current_tenant("10.50.11.2"), "host de DVR/NVR deveria ser aceito")

    maintenance._host_in_recorder_inventory = lambda ip: False
    assert_true(not maintenance._ip_belongs_to_current_tenant("10.50.11.2"), "host fora dos dois inventarios deveria ser recusado")
```

(Isso substitui a checagem que o teste já faz via
`maintenance._ip_belongs_to_current_tenant = lambda ip: ...` diretamente —
manter as duas formas: os monkeypatches diretos de
`_ip_belongs_to_current_tenant` que já existem no arquivo continuam
cobrindo `_camera_web_target_url`; o bloco novo cobre a função
`_ip_belongs_to_current_tenant` em si, testando que ela realmente consulta
os dois inventários.)

- [ ] **Step 2: Validar dentro de um container de teste, contra o arquivo real de produção**

```bash
# no servidor (10.10.12.7), depois de aplicar o patch num arquivo extraido
# do container real (docker cp), NAO direto no container rodando:
docker run --rm -e DATABASE_BACKEND=sqlite -e DATA_DIR=/tmp/webproxyval \
  -e SIGHTOPS_SECRET_KEY=val sightops-prod-api:<tag-de-teste> \
  python3 -c "import app.api.endpoints.maintenance; print('import OK dentro do container')"
```

Depois, com um servidor HTTP de mentira rodando dentro do mesmo container
(ex.: `python3 -m http.server` numa porta local) e uma pessoa cadastrada no
inventário do tenant de teste apontando pra esse host, confirmar
manualmente (via `curl` de dentro do container) que:
- IP fora do inventário devolve `403`.
- IP de câmera cadastrada com senha salva devolve `200` sem precisar de
  header de autenticação (login automático funcionou).

Expected: import sem traceback, os dois casos acima batendo.

- [ ] **Step 3: Commit**

```bash
git add app/api/endpoints/maintenance.py
git commit -m "fix(maintenance): proxy web de camera/DVR/NVR tenta https, repassa WWW-Authenticate e confere posse no inventario"
```

---

## Task 4: Frontend — DVR/NVR e o segundo fluxo de câmera passam a usar o proxy

**Files:**
- Modify: `frontend/js/bootstrap.js:241`
- Modify: `frontend/js/recorders.js:755`
- Modify: `frontend/index.html` (bump de `?v=` dos dois arquivos acima)

**Interfaces:**
- Consumes: rota `GET /api/maintenance/web/{host}/` (Task 3), já usada hoje
  por `frontend/js/cameras.js:2489` sem mudança nenhuma nela.

- [ ] **Step 1: Corrigir `frontend/js/bootstrap.js:241`**

Antes:
```javascript
      if (channelAction === 'web' && item?.camera_ip) { window.open(`http://${item.camera_ip}`, '_blank', 'noopener'); return; }
```

Depois:
```javascript
      if (channelAction === 'web' && item?.camera_ip) { window.open(`${API_BASE}/api/maintenance/web/${encodeURIComponent(item.camera_ip)}/`, '_blank', 'noopener'); return; }
```

- [ ] **Step 2: Corrigir `frontend/js/recorders.js:755`**

Antes:
```javascript
  if (action === 'web') { window.open(`http://${_recActive.host}`, '_blank'); return; }
```

Depois:
```javascript
  if (action === 'web') { window.open(`${API_BASE}/api/maintenance/web/${encodeURIComponent(_recActive.host)}/`, '_blank'); return; }
```

- [ ] **Step 3: Confirmar que não sobrou nenhum `window.open('http://'` nos três arquivos**

Run: `grep -n "window.open(\`http://" frontend/js/bootstrap.js frontend/js/recorders.js frontend/js/cameras.js`
Expected: nenhuma linha encontrada (grep sem saída / exit code 1).

- [ ] **Step 4: Subir a versão dos dois arquivos em `frontend/index.html`**

Ler a versão atual de cada um (`grep -n "bootstrap.js?v=\|recorders.js?v=" frontend/index.html`)
e subir cada uma em +1 — nunca reusar um número já usado antes (cache do
navegador/Cloudflare serviria a versão antiga do arquivo com o HTML novo).

- [ ] **Step 5: Validação de sintaxe**

Run: `node --check frontend/js/bootstrap.js && node --check frontend/js/recorders.js`
Expected: sem saída (sintaxe ok nos dois).

- [ ] **Step 6: Commit**

```bash
git add frontend/js/bootstrap.js frontend/js/recorders.js frontend/index.html
git commit -m "fix(frontend): botao Web de DVR/NVR e do segundo fluxo de camera passam a usar o proxy do servidor"
```

---

## Nota sobre commits

Nesta sessão, nenhuma mudança anterior foi commitada — tudo ficou como
alteração local, e o deploy em produção sempre foi feito extraindo o
arquivo real do container, aplicando o patch, e publicando via
`deploy_api.py`/`deploy_api_v3.py`, sem passar pelo git. Os passos "Commit"
de cada task acima seguem o formato padrão desta skill, mas **só devem ser
executados se o usuário pedir commit explicitamente** nesta execução do
plano — daí a ressalva no Step de commit da Task 1. Se o usuário não pedir,
pule os steps de commit e deixe as mudanças como estão no working tree.
