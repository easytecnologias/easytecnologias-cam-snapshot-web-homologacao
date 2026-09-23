from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.services import planning_service
from app.services.planning_pdf_report import generate_project_network_pdf


router = APIRouter(prefix="/api/planning", tags=["planning"])


def _handle_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(404, str(exc))
    return HTTPException(400, str(exc))


@router.get("/projects")
def projects_list() -> Dict[str, Any]:
    items = planning_service.list_projects()
    return {"ok": True, "count": len(items), "items": items}


@router.get("/catalog")
def equipment_catalog() -> Dict[str, Any]:
    items = planning_service.list_equipment_catalog()
    return {"ok": True, "count": len(items), "items": items}


@router.post("/projects")
def projects_create(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"ok": True, "item": planning_service.save_project(payload)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.get("/projects/{project_id}")
def projects_get(project_id: int) -> Dict[str, Any]:
    item = planning_service.get_project(project_id)
    if not item:
        raise HTTPException(404, "Projeto nao encontrado")
    return {"ok": True, "item": item}


@router.get("/projects/{project_id}/export-kmz")
def projects_export_kmz(project_id: int) -> Response:
    try:
        filename, content = planning_service.export_project_kmz(project_id)
        return Response(
            content=content,
            media_type="application/vnd.google-earth.kmz",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                # Sem no-store o Cloudflare cacheia o download (a URL termina em
                # .pdf/.kmz) e o usuario baixa de novo o arquivo ANTIGO.
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            },
        )
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.get("/projects/{project_id}/network-document.pdf")
def projects_network_document(
    project_id: int,
    route_margin_pct: float | None = None,
    slack_meters: float | None = None,
) -> Response:
    project = planning_service.get_project(project_id)
    if not project:
        raise HTTPException(404, "Projeto nao encontrado")
    try:
        kwargs: Dict[str, Any] = {}
        if route_margin_pct is not None:
            kwargs["route_margin_pct"] = route_margin_pct
        if slack_meters is not None:
            kwargs["slack_meters"] = slack_meters
        content = generate_project_network_pdf(project, **kwargs)
        safe_name = "".join(
            char if char.isascii() and (char.isalnum() or char in "-_") else "-"
            for char in str(project.get("name") or "projeto-cftv")
        ).strip("-")
        filename = f"{safe_name or 'projeto-cftv'}-documento-rede.pdf"
        return Response(
            content=content,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                # Sem no-store o Cloudflare cacheia o download (a URL termina em
                # .pdf/.kmz) e o usuario baixa de novo o arquivo ANTIGO.
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            },
        )
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.put("/projects/{project_id}")
def projects_update(project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"ok": True, "item": planning_service.save_project(payload, project_id)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.delete("/projects/{project_id}")
def projects_delete(project_id: int) -> Dict[str, Any]:
    if not planning_service.delete_project(project_id):
        raise HTTPException(404, "Projeto nao encontrado")
    return {"ok": True, "removed": True}


@router.post("/projects/{project_id}/sites")
def projects_site_save(project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"ok": True, "item": planning_service.save_site(project_id, payload)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.put("/projects/{project_id}/sites/{site_id}")
def projects_site_update(project_id: int, site_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"ok": True, "item": planning_service.update_site(project_id, site_id, payload)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.delete("/projects/{project_id}/sites/{site_id}")
def projects_site_delete(project_id: int, site_id: int) -> Dict[str, Any]:
    if not planning_service.delete_site(project_id, site_id):
        raise HTTPException(404, "Site nao encontrado")
    return {"ok": True, "removed": True}


@router.post("/projects/{project_id}/devices")
def projects_device_create(project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"ok": True, "item": planning_service.save_device(project_id, payload)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.post("/projects/{project_id}/devices/bulk")
def projects_devices_bulk(project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "Informe os equipamentos planejados")
    if len(items) > 1000:
        raise HTTPException(400, "O limite por importacao e 1000 equipamentos")
    saved = []
    try:
        for item in items:
            saved.append(planning_service.save_device(project_id, dict(item or {})))
        return {"ok": True, "count": len(saved), "items": saved}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.put("/projects/{project_id}/devices/{device_id}")
def projects_device_update(project_id: int, device_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {"ok": True, "item": planning_service.save_device(project_id, payload, device_id)}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.delete("/projects/{project_id}/devices/{device_id}")
def projects_device_delete(project_id: int, device_id: int) -> Dict[str, Any]:
    if not planning_service.delete_device(project_id, device_id):
        raise HTTPException(404, "Equipamento planejado nao encontrado")
    return {"ok": True, "removed": True}


@router.post("/projects/{project_id}/generate")
def projects_generate(project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        items = planning_service.generate_devices(project_id, payload)
        return {"ok": True, "count": len(items), "items": items}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.post("/projects/{project_id}/assemble-gpon-box")
def projects_assemble_gpon_box(project_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        result = planning_service.assemble_gpon_box(project_id, payload)
        return {"ok": True, "count": len(result["items"]), **result}
    except Exception as exc:
        raise _handle_error(exc) from exc


@router.post("/projects/{project_id}/import-csv")
async def projects_import_csv(
    project_id: int,
    file: UploadFile = File(...),
    defaults_json: str = Form("{}"),
) -> Dict[str, Any]:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Arquivo CSV vazio")
    try:
        defaults = json.loads(defaults_json or "{}")
        return planning_service.import_csv(project_id, raw, defaults)
    except Exception as exc:
        raise _handle_error(exc) from exc
