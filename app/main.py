from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.observability import RequestContextMiddleware, configure_logging
from app.core.paths import SAIDA_DIR, DATA_DIR, ensure_dirs
from app.core.security import AUTH_COOKIE_NAME, ApiAuthMiddleware
from app.core.settings import get_settings
from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug, tenant_snapshot_dir
from app.services.auth_store import get_user_by_token
from app.services.auth_store import init_auth_db
from app.services.db_store import init_db
from app.services.maintenance_ping_service import maintenance_ping_hub
from app.services.monitoring_service import list_monitoring_tenants, refresh_from_inventory
from app.services.zabbix_monitoring_service import sync_monitoring_to_zabbix
from app.services.telegram_notification_service import process_monitoring_notifications
from app.services.access_control_sync import (
    list_pending_provisions_for_retry,
    poll_device_events,
    retry_one_pending_provision,
)
from app.services.access_control_store import list_devices as list_access_devices
from app.api.endpoints.maintenance import scripts_zabbix_status_sync
from app.api.endpoints.olt import api_olt_registry_telemetry
from app.services.olt_registry import list_olts
from app.api.endpoints import (
    auth_router,
    cameras_router,
    system_router,
    scan_router,
    olt_router,
    tools_router,
    maintenance_router,
    switch_router,
    ws_router,
    dvr_router,
    nvr_router,
    ia_router,
    database_router,
    dashboard_router,
    windows_router,
    playback_router,
    connectors_router,
    network_tools_router,
    deployments_router,
    monitoring_router,
    planning_router,
    access_control_router,
)

ensure_dirs()
settings = get_settings()
configure_logging(settings)
logger = logging.getLogger("app.zabbix_status_sync")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if str(request.url.scheme or "").lower() == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=15552000; includeSubDomains")
        return response

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(ApiAuthMiddleware, settings=settings)

allowed_origins = [item.strip() for item in settings.allowed_origins.split(",") if item.strip()]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials="*" not in allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Routers (V7)
app.include_router(auth_router)
app.include_router(cameras_router)
app.include_router(system_router)
app.include_router(scan_router)
app.include_router(olt_router)
app.include_router(tools_router)
app.include_router(maintenance_router)
app.include_router(switch_router)
app.include_router(ws_router)
app.include_router(dvr_router)
app.include_router(nvr_router)
app.include_router(ia_router)
app.include_router(database_router)
app.include_router(dashboard_router)
app.include_router(windows_router)
app.include_router(playback_router)
app.include_router(connectors_router)
app.include_router(network_tools_router)
app.include_router(deployments_router)
app.include_router(monitoring_router)
app.include_router(planning_router)
app.include_router(access_control_router)

# Estado compartilhado (ex.: credencial do ultimo SCAN)
app.state.last_scan_auth = {"user": None, "pass": None}
app.state.settings = settings
app.state.zabbix_status_task = None
app.state.zabbix_status_last = {}
app.state.monitoring_refresh_task = None
app.state.monitoring_refresh_last = {}
app.state.olt_telemetry_task = None
app.state.olt_telemetry_last = {}
app.state.access_control_sync_task = None
app.state.access_control_sync_last = {}


def _zabbix_status_interval_s() -> int:
    raw = os.getenv("SIGHTOPS_ZABBIX_STATUS_SYNC_INTERVAL", "60")
    try:
        value = int(raw)
    except Exception:
        value = 60
    return max(30, min(value, 3600))


def _zabbix_status_tenant_slugs() -> list[str]:
    configured = os.getenv("SIGHTOPS_ZABBIX_STATUS_TENANT", "").strip().lower()
    if configured:
        return [configured]
    try:
        return list_monitoring_tenants()
    except Exception:
        return ["default"]


def _run_zabbix_status_sync_for_mode(mode: str, tenant_slug: str) -> dict:
    token = set_current_tenant_slug(tenant_slug)
    try:
        return scripts_zabbix_status_sync({"source": "ip", "mode": mode, "site": ""})
    finally:
        reset_current_tenant_slug(token)


async def _zabbix_status_sync_loop() -> None:
    interval = _zabbix_status_interval_s()
    await asyncio.sleep(10)
    while True:
        try:
            totals: list[str] = []
            last: dict[str, object] = {"ok": True, "interval_s": interval, "tenants": {}}
            for tenant_slug in await asyncio.to_thread(_zabbix_status_tenant_slugs):
                tenant_last: dict[str, object] = {"ok": True, "modes": {}}
                for mode in ("basic", "olt", "switch"):
                    result = await asyncio.to_thread(
                        _run_zabbix_status_sync_for_mode,
                        mode,
                        tenant_slug,
                    )
                    tenant_last["modes"][mode] = result
                    if result.get("ok"):
                        totals.append(
                            f"{tenant_slug}/{mode}: {result.get('online', 0)}/{result.get('total', 0)} online, "
                            f"{result.get('offline', 0)} offline"
                        )
                    elif result.get("error"):
                        tenant_last["ok"] = False
                        logger.debug("zabbix status sync skipped for %s: %s", tenant_slug, result.get("error"))
                        break
                last["tenants"][tenant_slug] = tenant_last
                if not tenant_last.get("ok"):
                    last["ok"] = False
            app.state.zabbix_status_last = last
            if totals:
                logger.info("zabbix status sync updated: %s", "; ".join(totals))
        except asyncio.CancelledError:
            raise
        except Exception:
            app.state.zabbix_status_last = {"ok": False, "interval_s": interval, "error": "loop failed"}
            logger.exception("zabbix status sync loop failed")
        await asyncio.sleep(interval)


def _monitoring_refresh_interval_s() -> int:
    try:
        return max(60, min(int(os.getenv("SIGHTOPS_MONITORING_REFRESH_INTERVAL", "120")), 3600))
    except Exception:
        return 120


def _refresh_monitoring_tenant(tenant_slug: str) -> dict:
    token = set_current_tenant_slug(tenant_slug)
    try:
        result = refresh_from_inventory()
        result["zabbix"] = sync_monitoring_to_zabbix()
        result["telegram"] = process_monitoring_notifications()
        return result
    finally:
        reset_current_tenant_slug(token)


async def _monitoring_refresh_loop() -> None:
    interval = _monitoring_refresh_interval_s()
    await asyncio.sleep(20)
    while True:
        results: dict[str, object] = {}
        try:
            for tenant_slug in await asyncio.to_thread(list_monitoring_tenants):
                results[tenant_slug] = await asyncio.to_thread(_refresh_monitoring_tenant, tenant_slug)
            app.state.monitoring_refresh_last = {"ok": True, "interval_s": interval, "tenants": results}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.monitoring_refresh_last = {"ok": False, "interval_s": interval, "error": str(exc)}
            logger.exception("monitoring inventory refresh failed")
        await asyncio.sleep(interval)


async def _olt_telemetry_loop() -> None:
    try:
        interval = max(300, min(int(os.getenv("SIGHTOPS_OLT_TELEMETRY_INTERVAL", "600")), 3600))
    except Exception:
        interval = 600
    await asyncio.sleep(60)
    while True:
        results: dict[str, object] = {}
        try:
            for tenant_slug in await asyncio.to_thread(list_monitoring_tenants):
                token = set_current_tenant_slug(tenant_slug)
                try:
                    olts = await asyncio.to_thread(list_olts, False)
                    tenant_results = []
                    for olt in olts:
                        try:
                            # api_olt_registry_telemetry virou async (cria um job em
                            # background e devolve na hora, sem bloquear) -- rodar via
                            # to_thread so criava a corrotina sem nunca executar ela, o
                            # loop automatico nunca coletava nada de verdade.
                            tenant_results.append(await api_olt_registry_telemetry(int(olt["id"])))
                        except Exception as exc:
                            tenant_results.append({"ok": False, "olt_id": olt.get("id"), "error": str(exc)})
                    results[tenant_slug] = tenant_results
                finally:
                    reset_current_tenant_slug(token)
            app.state.olt_telemetry_last = {"ok": True, "interval_s": interval, "tenants": results}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.olt_telemetry_last = {"ok": False, "interval_s": interval, "error": str(exc)}
            logger.exception("OLT telemetry loop failed")
        await asyncio.sleep(interval)


# Quantos dispositivos/retries de controle de acesso rodam ao mesmo tempo por
# tenant. Antes disso era um de cada vez (await sequencial) -- um unico
# dispositivo sem resposta (10-75s de timeout) travava a fila inteira atras
# dele, e com muitos tenants de teste isso segurava o pool de threads do
# servidor por tempo suficiente pra ate a checagem de saude (que tambem usa
# esse pool) ficar sem vaga e o container virar "unhealthy" sem estar
# quebrado de verdade. Limitado (nao ilimitado) pra nao disparar uma rajada
# de conexoes simultaneas em cima da rede/dos proprios dispositivos.
_ACCESS_CONTROL_CONCURRENCY = 6


async def _run_bounded(coros, limit: int) -> list:
    """Roda uma lista de corrotinas com no maximo `limit` ao mesmo tempo, em
    vez de aguardar uma por vez. Exceções voltam na lista (return_exceptions)
    em vez de interromper as outras -- um dispositivo que falha não deve
    derrubar a rodada dos demais."""
    sem = asyncio.Semaphore(limit)

    async def _run_one(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*[_run_one(c) for c in coros], return_exceptions=True)


async def _access_control_sync_loop() -> None:
    try:
        interval = max(5, min(int(os.getenv("SIGHTOPS_ACCESS_CONTROL_SYNC_INTERVAL", "15")), 900))
    except Exception:
        interval = 15
    await asyncio.sleep(5)
    while True:
        results: dict[str, object] = {}
        try:
            for tenant_slug in await asyncio.to_thread(list_monitoring_tenants):
                token = set_current_tenant_slug(tenant_slug)
                try:
                    pending, people_by_id = await asyncio.to_thread(list_pending_provisions_for_retry)
                    if pending:
                        retry_outcomes = await _run_bounded(
                            [asyncio.to_thread(retry_one_pending_provision, item, people_by_id) for item in pending],
                            _ACCESS_CONTROL_CONCURRENCY,
                        )
                        retried = sum(1 for r in retry_outcomes if r is True)
                    else:
                        retried = 0

                    devices = [d for d in await asyncio.to_thread(list_access_devices) if d.get("active")]
                    events_count = 0
                    device_errors: list[dict[str, object]] = []
                    poll_outcomes = await _run_bounded(
                        [asyncio.to_thread(poll_device_events, d["id"]) for d in devices],
                        _ACCESS_CONTROL_CONCURRENCY,
                    )
                    for device, outcome in zip(devices, poll_outcomes):
                        if isinstance(outcome, BaseException):
                            device_errors.append({"ok": False, "device_id": device.get("id"), "error": str(outcome)})
                            logger.error(
                                "access control event poll failed for device %s", device.get("id"),
                                exc_info=outcome,
                            )
                        else:
                            events_count += outcome
                    results[tenant_slug] = {
                        "retried": retried,
                        "events": events_count,
                        "device_errors": device_errors,
                    }
                finally:
                    reset_current_tenant_slug(token)
            app.state.access_control_sync_last = {"ok": True, "interval_s": interval, "tenants": results}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.access_control_sync_last = {"ok": False, "interval_s": interval, "error": str(exc)}
            logger.exception("access control sync loop failed")
        await asyncio.sleep(interval)


async def _live_stream_cleanup_loop() -> None:
    from app.services.live_stream_service import reap_idle_streams

    interval = 300  # 5 minutos
    await asyncio.sleep(30)
    while True:
        try:
            removed = await asyncio.to_thread(reap_idle_streams)
            app.state.live_stream_cleanup_last = {"ok": True, "interval_s": interval, "removed": removed}
            if removed:
                logger.info("live stream cleanup: removidos %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.live_stream_cleanup_last = {"ok": False, "interval_s": interval, "error": str(exc)}
            logger.exception("live stream cleanup loop failed")
        await asyncio.sleep(interval)


@app.get("/api/scripts/zabbix/status-sync/auto")
def zabbix_status_sync_auto_state() -> JSONResponse:
    task = getattr(app.state, "zabbix_status_task", None)
    return JSONResponse(
        content={
            "ok": True,
            "running": bool(task and not task.done()),
            "interval_s": _zabbix_status_interval_s(),
            "last": getattr(app.state, "zabbix_status_last", {}) or {},
        }
    )


@app.on_event("startup")
async def startup_events() -> None:
    init_db()
    init_auth_db()
    await maintenance_ping_hub.start()
    app.state.zabbix_status_task = asyncio.create_task(
        _zabbix_status_sync_loop(),
        name="zabbix-status-sync-loop",
    )
    app.state.monitoring_refresh_task = asyncio.create_task(
        _monitoring_refresh_loop(), name="monitoring-refresh-loop"
    )
    app.state.olt_telemetry_task = asyncio.create_task(_olt_telemetry_loop(), name="olt-telemetry-loop")
    app.state.access_control_sync_task = asyncio.create_task(
        _access_control_sync_loop(), name="access-control-sync-loop"
    )
    app.state.live_stream_cleanup_task = asyncio.create_task(
        _live_stream_cleanup_loop(), name="live-stream-cleanup-loop"
    )


@app.on_event("shutdown")
async def shutdown_events() -> None:
    for task_name in (
        "zabbix_status_task",
        "monitoring_refresh_task",
        "olt_telemetry_task",
        "access_control_sync_task",
        "live_stream_cleanup_task",
    ):
        task = getattr(app.state, task_name, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await maintenance_ping_hub.stop()


@app.get("/", include_in_schema=False)
def index_page() -> RedirectResponse:
    # Frontend legado (web/pages) aposentado -- /v2/ e a unica UI agora.
    return RedirectResponse(url="/v2/")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    # Evita 404 no browser (favicon opcional)
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
def chrome_devtools_manifest() -> JSONResponse:
    # Chrome DevTools faz essa requisicao em alguns cenarios; retornamos um JSON vazio.
    return JSONResponse(content={})


# Static
(DATA_DIR / "nvr_ai").mkdir(parents=True, exist_ok=True)


def _request_token(request: Request) -> str:
    raw = str(request.headers.get("authorization") or "").strip()
    scheme, _, token = raw.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return str(request.cookies.get(AUTH_COOKIE_NAME) or "").strip()


def _media_context(request: Request) -> tuple[bool, str]:
    """(autorizado, tenant_slug) do tenant efetivo da sessao.

    Rotas de midia ficam fora de /api/, entao o ApiAuthMiddleware nao roda e o
    contextvar de tenant nao esta populado -- o slug tem que sair do proprio
    usuario, nunca de env global. Admin de plataforma em act-as deve ver a
    midia do cliente em que esta atuando, nao do tenant real dele.
    """
    if not settings.auth_enabled:
        return True, ""
    token = _request_token(request)
    if not token:
        return False, ""
    user = get_user_by_token(token)
    if not user:
        return False, ""
    return True, str(user.get("effective_tenant_slug") or user.get("tenant_slug") or "").strip().lower()


def _allow_legacy_media_fallback(tenant_slug: str) -> bool:
    return (not settings.auth_enabled) or str(tenant_slug or "").strip().lower() in {"", "default"}


# /saida e /data/nvr_ai NAO sao servidos por HTTP de proposito.
#
# Eram dois mounts estaticos de pasta inteira, sem escopo de tenant: qualquer
# usuario logado de qualquer cliente conseguia ler arquivo de outro sabendo o
# nome. Nada do produto consumia essas rotas -- o frontend nao as chama, e os
# clipes de IA sao servidos por /api/playback/files/, que passa pelo middleware
# de auth. SAIDA_DIR ainda guarda artefato operacional (inventario, dump de MAC
# da OLT, CSV) e continua acessivel pelas APIs proprias de cada recurso.
#
# Se um dia for preciso servir frame/clipe da busca de IA, criar um endpoint
# /api/... que resolva o arquivo dentro do tenant do usuario -- nunca reabrir um
# mount de pasta.

# Snapshots: URL estavel /data/snapshot/<arquivo> com fallback entre pastas.
(DATA_DIR / "snapshot").mkdir(parents=True, exist_ok=True)
(SAIDA_DIR / "snapshot").mkdir(parents=True, exist_ok=True)


@app.get("/data/snapshot/{filename:path}", include_in_schema=False)
def data_snapshot_file(filename: str, request: Request) -> Response:
    from pathlib import Path

    authorized, tenant_slug = _media_context(request)
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "autenticacao obrigatoria"})
    name = Path(filename).name
    candidates = [
        tenant_snapshot_dir("ip", tenant_slug) / name,
    ]
    if _allow_legacy_media_fallback(tenant_slug):
        candidates.extend([
            DATA_DIR / "snapshot" / name,
            SAIDA_DIR / "snapshot" / name,
            SAIDA_DIR / "snapshot_manual" / name,
        ])
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return FileResponse(p, headers={"Cache-Control": "no-cache"})
        except Exception:
            continue
    return Response(status_code=404)


@app.get("/data/dvr_snapshot/{filename:path}", include_in_schema=False)
def data_dvr_snapshot_file(filename: str, request: Request) -> Response:
    from pathlib import Path

    authorized, tenant_slug = _media_context(request)
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "autenticacao obrigatoria"})
    name = Path(filename).name
    candidates = [
        tenant_snapshot_dir("dvr", tenant_slug) / name,
    ]
    if _allow_legacy_media_fallback(tenant_slug):
        candidates.append(DATA_DIR / "dvr_snapshot" / name)
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return FileResponse(p, headers={"Cache-Control": "no-cache"})
        except Exception:
            continue
    return Response(status_code=404)


@app.get("/data/nvr_snapshot/{filename:path}", include_in_schema=False)
def data_nvr_snapshot_file(filename: str, request: Request) -> Response:
    from pathlib import Path

    authorized, tenant_slug = _media_context(request)
    if not authorized:
        return JSONResponse(status_code=401, content={"detail": "autenticacao obrigatoria"})
    name = Path(filename).name
    candidates = [
        tenant_snapshot_dir("nvr", tenant_slug) / name,
    ]
    if _allow_legacy_media_fallback(tenant_slug):
        candidates.append(DATA_DIR / "nvr_snapshot" / name)
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return FileResponse(p, headers={"Cache-Control": "no-cache"})
        except Exception:
            continue
    return Response(status_code=404)
