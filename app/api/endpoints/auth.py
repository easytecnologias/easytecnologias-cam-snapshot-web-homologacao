from __future__ import annotations

import os
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.security import AUTH_COOKIE_NAME
from app.services.auth_store import (
    act_as_tenant,
    auth_enabled,
    auth_status,
    authenticate_user,
    bootstrap_admin,
    create_access_token,
    create_tenant,
    create_user,
    delete_tenant,
    delete_user,
    get_user_by_token,
    init_auth_db,
    list_tenants,
    list_users,
    migrate_auth_storage,
    module_catalog,
    platform_audit_events,
    recent_audit_events,
    revoke_token,
    set_tenant_modules,
    update_tenant,
    update_user_profile,
    update_user_password,
    update_user_status,
    user_requires_initial_setup,
)
from app.services.zabbix_access_service import provision_zabbix_tenant_access

router = APIRouter(prefix="/api/auth", tags=["auth"])
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_WINDOW_SEC = 10 * 60
_LOGIN_LOCK_SEC = 15 * 60
_LOGIN_MAX_FAILS = 8


class BootstrapAdminRequest(BaseModel):
    username: str
    password: str
    tenant_slug: str = "default"
    tenant_name: str = "Default"
    full_name: str = ""
    email: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str
    label: str = "web"


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    full_name: str = ""
    email: str = ""
    active: bool = True


class TenantCreateRequest(BaseModel):
    slug: str = ""
    name: str
    owner_username: str = ""
    owner_password: str = ""
    owner_full_name: str = ""
    owner_email: str = ""


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    active: bool | None = None


class UserActiveRequest(BaseModel):
    active: bool


class UserPasswordResetRequest(BaseModel):
    new_password: str


class UserUpdateRequest(BaseModel):
    full_name: str = ""
    email: str = ""
    role: str = "viewer"


class AuthStorageMigrateRequest(BaseModel):
    source_backend: str = "sqlite"
    target_backend: str = "postgres"
    force: bool = False


class ActAsRequest(BaseModel):
    tenant_slug: str = ""


class TenantModulesRequest(BaseModel):
    modules: list[str] = []
    unrestricted: bool = False


class TenantZabbixAccessRequest(BaseModel):
    username: str
    password: str = ""


def _cookie_secure(request: Request) -> bool:
    raw = str(os.getenv("AUTH_COOKIE_SECURE", "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return str(request.url.scheme or "").lower() == "https"


def _cookie_samesite() -> str:
    raw = str(os.getenv("AUTH_COOKIE_SAMESITE", "lax")).strip().lower()
    return raw if raw in ("lax", "strict", "none") else "lax"


def _set_auth_cookie(response: Response, request: Request, token: str, ttl_hours: int) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=max(60, int(ttl_hours) * 3600),
        httponly=True,
        secure=_cookie_secure(request),
        samesite=_cookie_samesite(),
        path="/",
    )


def _clear_auth_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        secure=_cookie_secure(request),
        samesite=_cookie_samesite(),
        path="/",
    )


def _extract_bearer_token(request: Request, authorization: str = Header(default="")) -> str:
    raw = str(authorization or "").strip()
    if not raw:
        cookie_token = str(request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
        if cookie_token:
            return cookie_token
    if not raw:
        raise HTTPException(status_code=401, detail="authorization ausente")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="authorization invalida")
    return token.strip()


def current_user(token: str = Depends(_extract_bearer_token)) -> Dict[str, Any]:
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="token invalido ou expirado")
    return user


def _user_public(user: Dict[str, Any]) -> Dict[str, Any]:
    setup_required = user_requires_initial_setup(user)
    return {
        "id": user["id"],
        "username": user["username"],
        "full_name": user.get("full_name", ""),
        "email": user.get("email", ""),
        "role": user["role"],
        "is_platform_admin": bool(user.get("is_platform_admin")),
        "tenant_id": user.get("tenant_id"),
        "tenant_slug": user.get("tenant_slug"),
        "tenant_name": user.get("tenant_name"),
        "acting_as": bool(user.get("acting_as")),
        "effective_tenant_id": user.get("effective_tenant_id", user.get("tenant_id")),
        "effective_tenant_slug": user.get("effective_tenant_slug", user.get("tenant_slug")),
        "effective_tenant_name": user.get("effective_tenant_name", user.get("tenant_name")),
        "enabled_modules": user.get("effective_enabled_modules"),
        "setup_required": setup_required,
    }


def _login_key(request: Request, username: str) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = forwarded or (request.client.host if request.client else "") or "unknown"
    user = str(username or "").strip().lower()[:80]
    return f"{ip}|{user}"


def _login_blocked(key: str) -> int:
    now = time.time()
    items = [ts for ts in _LOGIN_FAILS.get(key, []) if now - ts <= _LOGIN_LOCK_SEC]
    _LOGIN_FAILS[key] = items
    if len(items) < _LOGIN_MAX_FAILS:
        return 0
    oldest = min(items)
    return max(1, int(_LOGIN_LOCK_SEC - (now - oldest)))


def _login_record_failure(key: str) -> None:
    now = time.time()
    items = [ts for ts in _LOGIN_FAILS.get(key, []) if now - ts <= _LOGIN_WINDOW_SEC]
    items.append(now)
    _LOGIN_FAILS[key] = items[-_LOGIN_MAX_FAILS:]


def _login_record_success(key: str) -> None:
    _LOGIN_FAILS.pop(key, None)


def _optional_current_user(request: Request) -> Dict[str, Any] | None:
    """Versao de current_user que nao derruba a requisicao se nao tiver
    sessao -- /status precisa continuar publico (a tela de login chama antes
    de qualquer login existir), mas so quem e admin de PLATAFORMA pode ver
    quantos tenants/usuarios o SaaS tem no total."""
    raw = str(request.headers.get("authorization") or "").strip()
    token = ""
    if raw:
        scheme, _, tok = raw.partition(" ")
        if scheme.lower() == "bearer":
            token = tok.strip()
    if not token:
        token = str(request.cookies.get(AUTH_COOKIE_NAME) or "").strip()
    if not token:
        return None
    return get_user_by_token(token)


@router.get("/status")
def api_auth_status(request: Request) -> Dict[str, Any]:
    init_auth_db()
    storage = auth_status()
    actor = _optional_current_user(request)
    is_platform_admin = bool(actor and actor.get("is_platform_admin"))
    # Cliente comum (ou visitante sem login) nunca deve saber quantos outros
    # clientes/usuarios existem no SaaS inteiro -- so o backend/tipo de banco,
    # que nao e informacao sensivel de negocio.
    visible_storage = storage if is_platform_admin else {"backend": storage.get("backend")}
    return {
        "ok": True,
        "enabled": auth_enabled(),
        "auth_required": str(os.getenv("AUTH_REQUIRED", "0")).strip().lower() in ("1", "true", "yes", "on"),
        "legacy_open": str(os.getenv("AUTH_LEGACY_OPEN", "1")).strip().lower() in ("1", "true", "yes", "on"),
        "bootstrap_allowed": int(storage.get("users", 0) or 0) == 0,
        "setup_required": False,
        "storage": visible_storage,
    }


@router.post("/bootstrap-admin")
def api_auth_bootstrap_admin(req: BootstrapAdminRequest) -> Dict[str, Any]:
    try:
        out = bootstrap_admin(
            username=req.username,
            password=req.password,
            tenant_slug=req.tenant_slug,
            tenant_name=req.tenant_name,
            full_name=req.full_name,
            email=req.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return out


@router.post("/login")
def api_auth_login(req: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
    login_key = _login_key(request, req.username)
    retry_after = _login_blocked(login_key)
    if retry_after > 0:
        raise HTTPException(
            status_code=429,
            detail=f"muitas tentativas de login; tente novamente em {retry_after}s",
        )
    user = authenticate_user(req.username, req.password)
    if not user:
        _login_record_failure(login_key)
        raise HTTPException(status_code=401, detail="usuario ou senha invalidos")
    _login_record_success(login_key)
    ttl_hours = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "24"))
    token = create_access_token(user, label=str(req.label or "web")[:40], ttl_hours=ttl_hours)
    _set_auth_cookie(response, request, str(token.get("access_token") or token.get("token") or ""), ttl_hours)
    setup_required = user_requires_initial_setup(user)
    return {
        "ok": True,
        **token,
        "setup_required": setup_required,
        "user": _user_public(user),
    }


@router.post("/logout")
def api_auth_logout(
    request: Request,
    response: Response,
    user: Dict[str, Any] = Depends(current_user),
    token: str = Depends(_extract_bearer_token),
) -> Dict[str, Any]:
    out = revoke_token(token, actor=user)
    _clear_auth_cookie(response, request)
    return out


@router.get("/me")
def api_auth_me(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {
        "ok": True,
        "setup_required": user_requires_initial_setup(user),
        "user": _user_public(user),
    }


@router.get("/agent-token")
def api_auth_agent_token(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    """Emite um token bearer curto pro SightOps Agent autenticar a ponte
    /ws/web-tunnel. O cookie de sessao (httponly) prova quem e; o agente e um
    processo separado no PC (sem cookie), entao recebe este token na query e
    conecta o WebSocket. Vida curta e escopo do proprio usuario."""
    tok = create_access_token(user, label="agent-web", ttl_hours=8)
    return {"ok": True, "token": str(tok.get("access_token") or tok.get("token") or "")}


@router.post("/act-as")
def api_auth_act_as(
    req: ActAsRequest,
    user: Dict[str, Any] = Depends(current_user),
    token: str = Depends(_extract_bearer_token),
) -> Dict[str, Any]:
    """Admin de plataforma passa a operar como outro cliente nesta sessao
    (sem saber a senha dele). tenant_slug vazio volta pro proprio tenant."""
    try:
        act_as_tenant(user, token, req.tenant_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    refreshed = get_user_by_token(token)
    if not refreshed:
        raise HTTPException(status_code=401, detail="sessao invalida")
    return {"ok": True, "user": _user_public(refreshed)}


@router.get("/modules/catalog")
def api_auth_modules_catalog(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"ok": True, "modules": module_catalog()}


@router.put("/tenants/{tenant_id}/modules")
def api_auth_set_tenant_modules(
    tenant_id: int, req: TenantModulesRequest, user: Dict[str, Any] = Depends(current_user)
) -> Dict[str, Any]:
    try:
        result = set_tenant_modules(user, tenant_id, req.modules, unrestricted=req.unrestricted)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


@router.post("/tenants/{tenant_id}/zabbix-access")
def api_auth_provision_tenant_zabbix_access(
    tenant_id: int,
    req: TenantZabbixAccessRequest,
    user: Dict[str, Any] = Depends(current_user),
) -> Dict[str, Any]:
    if not bool(user.get("is_platform_admin")):
        raise HTTPException(status_code=403, detail="somente admin da plataforma pode provisionar Zabbix")
    tenant = next((row for row in list_tenants(user) if int(row.get("id") or 0) == int(tenant_id)), None)
    if not tenant:
        raise HTTPException(status_code=404, detail="cliente nao encontrado")
    try:
        result = provision_zabbix_tenant_access(
            tenant_slug=str(tenant.get("slug") or ""),
            tenant_name=str(tenant.get("name") or ""),
            username=req.username,
            password=req.password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"falha ao provisionar Zabbix: {e}")
    return {"ok": True, **result}


@router.get("/users")
def api_auth_users(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"ok": True, "users": list_users(user)}


@router.get("/tenants")
def api_auth_tenants(user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"ok": True, "tenants": list_tenants(user)}


@router.post("/tenants")
def api_auth_create_tenant(req: TenantCreateRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        created = create_tenant(
            user,
            slug=req.slug,
            name=req.name,
            owner_username=req.owner_username,
            owner_password=req.owner_password,
            owner_full_name=req.owner_full_name,
            owner_email=req.owner_email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **created}


@router.patch("/tenants/{tenant_id}")
def api_auth_update_tenant(
    tenant_id: int, req: TenantUpdateRequest, user: Dict[str, Any] = Depends(current_user)
) -> Dict[str, Any]:
    try:
        updated = update_tenant(user, tenant_id, name=req.name, active=req.active)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **updated}


@router.delete("/tenants/{tenant_id}")
def api_auth_delete_tenant(tenant_id: int, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        return delete_tenant(user, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/users")
def api_auth_create_user(req: UserCreateRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        created = create_user(
            user,
            username=req.username,
            password=req.password,
            role=req.role,
            full_name=req.full_name,
            email=req.email,
            active=req.active,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    setup_completed = bool(created.pop("_setup_completed", False)) if isinstance(created, dict) else False
    return {"ok": True, "user": created, "setup_completed": setup_completed}


@router.post("/users/{user_id}/active")
def api_auth_set_user_active(user_id: int, req: UserActiveRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        updated = update_user_status(user, target_user_id=user_id, active=req.active)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "user": updated}


@router.post("/users/{user_id}/password")
def api_auth_reset_user_password(user_id: int, req: UserPasswordResetRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        updated = update_user_password(user, target_user_id=user_id, new_password=req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "user": updated}


@router.delete("/users/{user_id}")
def api_auth_delete_user(user_id: int, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        result = delete_user(user, target_user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.post("/users/{user_id}")
def api_auth_update_user(user_id: int, req: UserUpdateRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        updated = update_user_profile(
            user,
            target_user_id=user_id,
            full_name=req.full_name,
            email=req.email,
            role=req.role,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "user": updated}


@router.get("/audit")
def api_auth_audit(limit: int = 50, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    return {"ok": True, "events": recent_audit_events(user, limit=limit)}


@router.get("/audit/platform")
def api_auth_audit_platform(limit: int = 50, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    try:
        events = platform_audit_events(user, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"ok": True, "events": events}


@router.post("/storage/migrate")
def api_auth_storage_migrate(req: AuthStorageMigrateRequest, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    if str(user.get("role") or "").strip().lower() not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="permissao insuficiente")
    try:
        result = migrate_auth_storage(
            source_backend=req.source_backend,
            target_backend=req.target_backend,
            force=req.force,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="falha ao migrar armazenamento de autenticacao")
    return result
