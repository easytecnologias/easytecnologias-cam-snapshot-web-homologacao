from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import time

from app.core.tenant_context import reset_current_tenant_slug, set_current_tenant_slug
from app.services.alert_store import (
    AppAuthError,
    activate_app,
    add_incident_note,
    app_status,
    cancel_incident,
    create_activation_code,
    delete_member,
    get_incident,
    list_incidents,
    list_members,
    live_summary,
    connector_cameras,
    nearby_cameras,
    nearest_camera,
    effective_radius_m,
    NEARBY_RADIUS_M,
    record_positions,
    resolve_app_session,
    revoke_member_devices,
    save_member,
    set_app_pins,
    transition_incident,
    trigger_incident,
)

router = APIRouter(prefix="/api/alert", tags=["alert"])

# Ativacao do aparelho e rota PUBLICA (sem login): sem freio, aceita tentativa
# em massa de codigo. O codigo tem 8 caracteres (36^8), entao forca bruta nao
# fecha, mas nada impedia varrer sem custo nem deixar rastro. Mesmo padrao do
# login em auth.py: memoria do processo, janela e bloqueio por origem.
_ATIVA_FALHAS: Dict[str, List[float]] = {}
_ATIVA_JANELA_SEG = 10 * 60
_ATIVA_BLOQUEIO_SEG = 15 * 60
_ATIVA_MAX_FALHAS = 10


def _ativa_origem(request: Request) -> str:
    encaminhado = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return encaminhado or (request.client.host if request.client else "") or "desconhecida"


def _ativa_bloqueada(chave: str) -> int:
    agora = time.time()
    itens = [t for t in _ATIVA_FALHAS.get(chave, []) if agora - t <= _ATIVA_BLOQUEIO_SEG]
    _ATIVA_FALHAS[chave] = itens
    if len(itens) < _ATIVA_MAX_FALHAS:
        return 0
    return max(1, int(_ATIVA_BLOQUEIO_SEG - (agora - min(itens))))


def _ativa_registra_falha(chave: str) -> None:
    agora = time.time()
    itens = [t for t in _ATIVA_FALHAS.get(chave, []) if agora - t <= _ATIVA_JANELA_SEG]
    itens.append(agora)
    _ATIVA_FALHAS[chave] = itens[-_ATIVA_MAX_FALHAS:]


def _operator(request: Request) -> str:
    user = getattr(request.state, "user", {}) or {}
    return str(user.get("username") or user.get("sub") or "operador")


# ---------------------------------------------------------------------------
# Central (usuario logado no SightOps; tenant vem do middleware)
# ---------------------------------------------------------------------------

class AlertMemberRequest(BaseModel):
    id: Optional[str] = ""
    full_name: str = Field(min_length=1, max_length=160)
    document_id: str = ""
    role_title: str = ""
    unit_name: str = ""
    unit_connector_id: str = ""
    unit_lat: Optional[Any] = None
    unit_lon: Optional[Any] = None
    phone: str = ""
    notes: str = ""
    active: bool = True


class AlertTransitionRequest(BaseModel):
    note: str = ""


@router.get("/members")
def api_alert_members(search: str = Query("")) -> Dict[str, Any]:
    members = list_members(search)
    return {"ok": True, "count": len(members), "members": members}


@router.post("/members")
def api_alert_save_member(req: AlertMemberRequest) -> Dict[str, Any]:
    try:
        return {"ok": True, "member": save_member(req.model_dump())}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/members/{member_id}")
def api_alert_delete_member(member_id: str) -> Dict[str, Any]:
    try:
        delete_member(member_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.post("/members/{member_id}/activation-code")
def api_alert_activation_code(member_id: str) -> Dict[str, Any]:
    try:
        return {"ok": True, **create_activation_code(member_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/members/{member_id}/revoke-devices")
def api_alert_revoke_devices(member_id: str) -> Dict[str, Any]:
    return {"ok": True, "revoked": revoke_member_devices(member_id)}


@router.get("/incidents")
def api_alert_incidents(scope: str = Query("live"), limit: int = Query(200)) -> Dict[str, Any]:
    incidents = list_incidents(scope=scope, limit=limit)
    return {"ok": True, "count": len(incidents), "incidents": incidents}


@router.get("/incidents/{incident_id}")
def api_alert_incident(incident_id: str) -> Dict[str, Any]:
    try:
        return {"ok": True, "incident": get_incident(incident_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/incidents/{incident_id}/{action}")
def api_alert_incident_action(incident_id: str, action: str, req: AlertTransitionRequest, request: Request) -> Dict[str, Any]:
    try:
        if action == "note":
            incident = add_incident_note(incident_id, _operator(request), req.note)
        elif action in ("acknowledge", "dispatch", "close"):
            incident = transition_incident(incident_id, action, _operator(request), req.note)
        else:
            raise HTTPException(status_code=404, detail="acao desconhecida")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "incident": incident}


@router.get("/summary")
def api_alert_summary() -> Dict[str, Any]:
    return {"ok": True, **live_summary()}


@router.get("/nearby-cameras")
def api_alert_nearby_cameras(
    lat: float = Query(...),
    lon: float = Query(...),
    limit: int = Query(6),
    radius_m: float = Query(NEARBY_RADIUS_M, ge=1, le=5000),
    accuracy_m: Optional[float] = Query(None, ge=0, le=100000),
) -> Dict[str, Any]:
    """Cameras dentro do raio do ponto do alerta.

    Fora do raio nao entra na lista: camera de outro lugar nao ajuda quem
    atende e ainda confunde. Quando nao ha nenhuma, devolve `nearest` com a
    distancia da mais proxima -- "nao tem camera aqui, a mais perto esta a
    180 m" e uma informacao operacional; "nao tem" sozinho nao e."""
    if abs(lat) > 90 or abs(lon) > 180:
        raise HTTPException(status_code=400, detail="coordenada invalida")
    raio = effective_radius_m(radius_m, accuracy_m)
    cams = nearby_cameras(lat, lon, limit=limit, max_distance_m=radius_m, accuracy_m=accuracy_m)
    out: Dict[str, Any] = {
        "ok": True,
        "count": len(cams),
        "cameras": cams,
        "radius_m": raio,            # o que valeu de fato
        "radius_base_m": radius_m,   # o configurado
        "accuracy_m": accuracy_m,    # o que o aparelho informou
        "widened": raio > radius_m,  # raio ampliado por causa do GPS
    }
    if not cams:
        perto = nearest_camera(lat, lon)
        if perto:
            out["nearest"] = {
                "titulo": perto.get("titulo"),
                "ip": perto.get("ip"),
                "distance_m": perto.get("distance_m"),
            }
    return out


@router.get("/unit-cameras")
def api_alert_unit_cameras(
    connector_id: str = Query(...), lat: Optional[float] = Query(None), lon: Optional[float] = Query(None)
) -> Dict[str, Any]:
    cams = connector_cameras(connector_id, lat, lon)
    return {"ok": True, "count": len(cams), "cameras": cams}


# ---------------------------------------------------------------------------
# App do celular. Rota PUBLICA no ApiAuthMiddleware (/api/alert/app/): quem
# autentica e o token do aparelho, conferido aqui. O tenant sai da sessao do
# aparelho, nunca de algo que o app mande.
# ---------------------------------------------------------------------------

class AppActivateRequest(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    device_label: str = ""
    platform: str = ""


class AppPinsRequest(BaseModel):
    pin: str
    duress_pin: str
    current_pin: str = ""


class AppPosition(BaseModel):
    lat: float
    lon: float
    accuracy: Optional[float] = None
    battery: Optional[float] = None
    recorded_at: str = ""


class AppTriggerRequest(BaseModel):
    position: Optional[AppPosition] = None


class AppPositionsRequest(BaseModel):
    positions: List[AppPosition] = []


class AppCancelRequest(BaseModel):
    pin: str


def _app_token(request: Request) -> str:
    raw = str(request.headers.get("authorization") or "").strip()
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class _AppContext:
    """Resolve o aparelho e entra no tenant dele durante a chamada."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.ctx: Dict[str, Any] = {}
        self._token = None

    def __enter__(self) -> Dict[str, Any]:
        try:
            self.ctx = resolve_app_session(_app_token(self.request))
        except AppAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        self._token = set_current_tenant_slug(self.ctx["tenant_slug"])
        return self.ctx

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            reset_current_tenant_slug(self._token)


@router.post("/app/activate")
def api_alert_app_activate(req: AppActivateRequest, request: Request) -> Dict[str, Any]:
    chave = _ativa_origem(request)
    espera = _ativa_bloqueada(chave)
    if espera > 0:
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas de ativacao. Tente de novo em alguns minutos.",
            headers={"Retry-After": str(espera)},
        )
    try:
        saida = {"ok": True, **activate_app(req.code, req.device_label, req.platform)}
    except AppAuthError as exc:
        _ativa_registra_falha(chave)
        raise HTTPException(status_code=400, detail=str(exc))
    _ATIVA_FALHAS.pop(chave, None)  # ativou: zera o historico daquela origem
    return saida


@router.get("/app/me")
def api_alert_app_me(request: Request) -> Dict[str, Any]:
    with _AppContext(request) as ctx:
        return {"ok": True, **app_status(ctx)}


@router.post("/app/pins")
def api_alert_app_pins(req: AppPinsRequest, request: Request) -> Dict[str, Any]:
    with _AppContext(request) as ctx:
        try:
            set_app_pins(ctx, req.pin, req.duress_pin, req.current_pin)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.post("/app/trigger")
def api_alert_app_trigger(req: AppTriggerRequest, request: Request) -> Dict[str, Any]:
    with _AppContext(request) as ctx:
        pos = req.position.model_dump() if req.position else {}
        return {"ok": True, **trigger_incident(ctx, pos)}


@router.post("/app/incidents/{incident_id}/positions")
def api_alert_app_positions(incident_id: str, req: AppPositionsRequest, request: Request) -> Dict[str, Any]:
    with _AppContext(request) as ctx:
        try:
            return {"ok": True, **record_positions(ctx, incident_id, [p.model_dump() for p in req.positions])}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


@router.post("/app/incidents/{incident_id}/cancel")
def api_alert_app_cancel(incident_id: str, req: AppCancelRequest, request: Request) -> Dict[str, Any]:
    with _AppContext(request) as ctx:
        try:
            return cancel_incident(ctx, incident_id, req.pin)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
