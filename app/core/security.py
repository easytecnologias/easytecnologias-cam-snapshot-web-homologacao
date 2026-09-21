from __future__ import annotations

from typing import Iterable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.settings import AppSettings
from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
from app.services.auth_store import get_user_by_token

AUTH_COOKIE_NAME = "sightops_session"

ROLE_RANK = {
    "viewer": 10,
    "operator": 20,
    "admin": 30,
    "owner": 40,
}


class ApiAuthMiddleware(BaseHTTPMiddleware):
    _WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

    def __init__(self, app, settings: AppSettings) -> None:
        super().__init__(app)
        self.settings = settings
        self._public_paths = {
            "/api/auth/status",
            "/api/auth/login",
            "/api/auth/bootstrap-admin",
            "/api/system/health/live",
            "/api/system/health/ready",
            "/api/windows/agent/report",
        }
        self._query_token_paths = (
            "/api/connectors/",
            "/api/backup/export",
            "/api/inventory/export",
            "/api/inventory/report",
            "/api/dvr/report",
            "/api/nvr/report",
            "/api/kmz/",
            "/api/playback/files/",
        )
        self._role_rules = [
            (("GET",), "/api/auth/users", "admin"),
            (("GET",), "/api/auth/audit", "admin"),
            (("GET",), "/api/auth/tenants", "admin"),
            (("POST",), "/api/auth/users", "admin"),
            (("DELETE",), "/api/auth/users", "admin"),
            (("POST",), "/api/auth/tenants", "admin"),
            (("POST",), "/api/auth/tenants/", "admin"),
            (("PUT",), "/api/auth/tenants/", "admin"),
            (("POST",), "/api/auth/act-as", "admin"),
            (("POST",), "/api/system/product", "admin"),
            (("POST",), "/api/db/init", "admin"),
            (("POST",), "/api/db/migrate", "admin"),
            (("POST",), "/api/db/storage/migrate", "admin"),
            (("POST",), "/api/db/sites", "admin"),
            (("POST",), "/api/db/assign-site", "admin"),
            (("POST",), "/api/settings/imgbb", "admin"),
            (("POST",), "/api/settings/imgbb/test", "admin"),
            (("POST",), "/api/scripts/", "admin"),
            (("POST",), "/api/backup/import", "admin"),
            (("POST",), "/api/inventory/report/settings", "admin"),
            (("POST",), "/api/inventory/report/logo", "admin"),
            (("POST",), "/api/dvr/report/settings", "admin"),
            (("POST",), "/api/dvr/report/logo", "admin"),
            (("POST",), "/api/nvr/report/settings", "admin"),
            (("POST",), "/api/nvr/report/logo", "admin"),
            (("POST",), "/api/inventory/import", "operator"),
            (("POST",), "/api/inventory/clear", "operator"),
            (("POST",), "/api/inventory/imgbb/upload", "operator"),
            (("POST",), "/api/kmz/", "operator"),
            (("POST",), "/api/scan", "operator"),
            (("POST",), "/api/inventory/delete", "operator"),
            (("POST",), "/api/rescan-single-ip", "operator"),
            (("POST",), "/api/olt/", "operator"),
            (("GET",), "/api/olt/", "viewer"),
            (("POST",), "/api/switch/", "operator"),
            (("POST",), "/api/windows/", "operator"),
            (("GET",), "/api/connectors", "operator"),
            (("POST",), "/api/connectors", "operator"),
            (("DELETE",), "/api/connectors", "operator"),
            (("GET", "POST"), "/api/deployments", "operator"),
            (("POST",), "/api/tools/scan-ip", "operator"),
            (("POST",), "/api/discovery/run", "operator"),
            (("POST",), "/api/portscan/apply", "operator"),
            (("POST",), "/api/cameras/save", "operator"),
            (("POST",), "/api/cameras/ping_many", "operator"),
            (("POST",), "/api/snapshot/save", "operator"),
            (("POST",), "/api/cameras/ptz_move", "operator"),
            (("POST",), "/api/cameras/reboot", "operator"),
            (("POST",), "/api/cameras/rename", "operator"),
            # O proxy web repassa o metodo do dispositivo (PUT/PATCH/DELETE
            # inclusive), nao so POST -- com o default virando NEGAR, cobrir
            # so POST quebraria o acesso web a camera/gravador.
            (("POST", "PUT", "PATCH", "DELETE"), "/api/maintenance/", "operator"),
            (("POST",), "/api/dvr/", "operator"),
            (("POST",), "/api/nvr/", "operator"),
            (("POST",), "/api/ia/", "operator"),
            (("POST", "DELETE"), "/api/alert/members", "operator"),
            (("POST",), "/api/alert/incidents", "operator"),

            # --- Fase 1 da correcao de autorizacao (2026-09-20) ---------------
            # Estas rotas de ESCRITA caiam no default "basta estar logado", que
            # dava a um viewer poder de abrir porta, cadastrar pessoa com
            # acesso, extrair gravacao e trocar o destino dos alertas.
            # A ORDEM IMPORTA: _match_role_rule devolve a PRIMEIRA regra que
            # casa, entao o que exige admin vem antes do prefixo generico.

            # -- consequencia alta: exige admin --
            (("DELETE",), "/api/access-control/devices", "admin"),
            (("POST", "PUT", "DELETE"), "/api/access-control/whatsapp", "admin"),
            (("POST",), "/api/system/bootstrap", "admin"),
            (("DELETE", "PATCH"), "/api/auth/tenants/", "admin"),
            (("POST",), "/api/auth/storage/migrate", "admin"),
            (("PUT",), "/api/monitoring/telegram", "admin"),
            (("POST",), "/api/monitoring/telegram/test", "admin"),

            # -- operacao do dia a dia: exige operator --
            # Controle de acesso: abrir porta, pessoas, grupos, regras, sync.
            (("POST", "PUT", "PATCH", "DELETE"), "/api/access-control/", "operator"),
            # Extrair gravacao e imagem e questao de privacidade.
            (("POST",), "/api/playback/", "operator"),
            (("POST", "PUT", "PATCH", "DELETE"), "/api/planning/", "operator"),
            (("POST",), "/api/monitoring/", "operator"),
            (("POST",), "/api/network/tools/", "operator"),
            (("POST",), "/api/telegram/", "operator"),
            (("POST",), "/api/cameras/", "operator"),
            (("POST",), "/api/inventory/report/job", "operator"),
            (("PATCH", "DELETE"), "/api/kmz/", "operator"),
            (("PATCH",), "/api/windows/", "operator"),
            (("DELETE",), "/api/olt/", "operator"),

            # Sair e a unica escrita que qualquer logado faz. Declarada aqui
            # de proposito: com o default virando NEGAR (Fase 2), rota sem
            # regra passa a ser recusada, entao esta precisa existir.
            (("POST",), "/api/auth/logout", "viewer"),
        ]

    def _is_public_path(self, path: str) -> bool:
        if path in self._public_paths:
            return True
        if path.startswith("/api/connectors/agent/"):
            return True
        # App do botao de panico: autentica pelo token do aparelho dentro do
        # proprio endpoint (app/api/endpoints/alert.py), nao por usuario.
        if path.startswith("/api/alert/app/"):
            return True
        if path.startswith("/api/access-control/whatsapp/inbound/") and path != "/api/access-control/whatsapp/inbound/simulate":
            return True
        # Webhook da Cloud API: a Meta chama sem credencial nossa. A protecao e o
        # token de verificacao conferido dentro do proprio endpoint.
        if path.startswith("/api/access-control/whatsapp/meta/"):
            return True
        return False

    def _match_role_rule(self, path: str, method: str) -> str:
        for methods, prefix, min_role in self._role_rules:
            if method not in methods:
                continue
            if path == prefix or path.startswith(prefix):
                return min_role
        return ""

    def _query_token_allowed(self, path: str, method: str) -> bool:
        if method.upper() not in ("GET", "HEAD"):
            return False
        return any(path == prefix or path.startswith(prefix) for prefix in self._query_token_paths)

    def _require_auth(self, path: str, method: str) -> bool:
        if not self.settings.auth_enabled:
            return False
        if not path.startswith("/api/"):
            return False
        if self._is_public_path(path):
            return False
        if method.upper() == "OPTIONS":
            return False
        if self._match_role_rule(path, method):
            return True
        if self.settings.auth_required:
            return True
        if not self.settings.auth_legacy_open and method.upper() not in ("GET", "HEAD"):
            return True
        return False

    @staticmethod
    def _role_allows(user_role: str, required_role: str) -> bool:
        have = ROLE_RANK.get(str(user_role or "").strip().lower(), 0)
        need = ROLE_RANK.get(str(required_role or "").strip().lower(), 10**9)
        return have >= need

    def _extract_bearer_token(self, headers: Iterable[tuple[str, str]] | dict | Request) -> str:
        if isinstance(headers, Request):
            raw = str(headers.headers.get("authorization") or "").strip()
            if not raw:
                cookie_token = str(headers.cookies.get(AUTH_COOKIE_NAME) or "").strip()
                if cookie_token:
                    return cookie_token
            if not raw and self._query_token_allowed(headers.url.path, headers.method):
                query_token = str(
                    headers.query_params.get("auth_token")
                    or headers.query_params.get("access_token")
                    or headers.query_params.get("token")
                    or ""
                ).strip()
                if query_token:
                    return query_token
        elif isinstance(headers, dict):
            raw = str(headers.get("authorization") or "").strip()
        else:
            raw = ""
            for key, value in headers:
                if str(key).lower() == "authorization":
                    raw = str(value or "").strip()
                    break
        scheme, _, token = raw.partition(" ")
        if scheme.lower() != "bearer":
            return ""
        return token.strip()

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method.upper()
        ctx_token = set_current_tenant_slug("")
        if not self._require_auth(path, method):
            try:
                return await call_next(request)
            finally:
                reset_current_tenant_slug(ctx_token)

        token = self._extract_bearer_token(request)
        if not token:
            try:
                return JSONResponse(status_code=401, content={"detail": "autenticacao obrigatoria"})
            finally:
                reset_current_tenant_slug(ctx_token)

        user = get_user_by_token(token)
        if not user:
            try:
                return JSONResponse(status_code=401, content={"detail": "token invalido ou expirado"})
            finally:
                reset_current_tenant_slug(ctx_token)

        required_role = self._match_role_rule(path, method)
        # FASE 2 -- default DENY para escrita. Antes, rota de escrita fora da
        # lista de prefixos caia em "basta estar logado": nascia no nivel mais
        # permissivo e ninguem percebia (eram 62 assim em 20/09/2026, entre
        # elas abrir porta). Agora o silencio custa 403, nao acesso.
        # Leitura segue no comportamento antigo de proposito: negar GET nao
        # declarado quebraria tela sem ganho de seguranca equivalente.
        if not required_role and method in self._WRITE_METHODS:
            try:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "rota sem perfil declarado: acesso negado por padrao"},
                )
            finally:
                reset_current_tenant_slug(ctx_token)
        if required_role and not self._role_allows(str(user.get("role") or ""), required_role):
            try:
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"permissao insuficiente: requer perfil {required_role}"},
                )
            finally:
                reset_current_tenant_slug(ctx_token)

        request.state.current_user = user
        # Usa o tenant EFETIVO (considera "operar como" outro cliente via
        # act-as), nao o tenant_slug fixo do usuario -- ver get_user_by_token
        # em app/services/auth_store.py.
        request.state.current_tenant_slug = str(user.get("effective_tenant_slug") or user.get("tenant_slug") or "").strip().lower()
        reset_current_tenant_slug(ctx_token)
        ctx_token = set_current_tenant_slug(request.state.current_tenant_slug)
        try:
            return await call_next(request)
        finally:
            reset_current_tenant_slug(ctx_token)
