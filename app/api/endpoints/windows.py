from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.services.windows_inventory_service import (
    clear_windows_inventory,
    accept_windows_agent_report,
    build_windows_agent_script,
    build_windows_prepare_script,
    load_windows_inventory,
    save_windows_inventory,
    scan_windows_inventory,
    validate_windows_agent_token,
)
from app.services.windows_photo_enrichment import enrich_windows_rows_with_photos
from app.services.windows_pdf_report import build_windows_inventory_pdf

router = APIRouter(prefix="/api/windows", tags=["windows"])


@router.get("/inventory")
def api_windows_inventory() -> Dict[str, Any]:
    rows = load_windows_inventory()
    return {"ok": True, "count": len(rows), "inventory": rows}


def _windows_row_key(row: Dict[str, Any]) -> str:
    for key in ("ip", "hostname", "serial"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


@router.patch("/inventory/manual")
def api_windows_inventory_manual(payload: Dict[str, Any]) -> Dict[str, Any]:
    target = str(payload.get("key") or payload.get("ip") or "").strip()
    manual = payload.get("physical") if isinstance(payload.get("physical"), dict) else {}
    allowed = {
        "switch_name",
        "switch_port",
        "patch_panel",
        "patch_port",
        "outlet",
        "rack",
        "cable_id",
        "asset_tag",
        "notes",
    }
    cleaned = {k: str(manual.get(k) or "").strip() for k in allowed}
    rows = load_windows_inventory()
    updated = False
    for row in rows:
        if target and target in {_windows_row_key(row), str(row.get("ip") or "").strip(), str(row.get("hostname") or "").strip(), str(row.get("serial") or "").strip()}:
            current = row.get("physical") if isinstance(row.get("physical"), dict) else {}
            row["physical"] = {**current, **cleaned}
            updated = True
            break
    if not updated:
        raise HTTPException(status_code=404, detail="computador nao encontrado")
    save_windows_inventory(rows)
    return {"ok": True, "updated": True}


@router.post("/inventory/delete")
def api_windows_inventory_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
    keys = payload.get("keys")
    if not isinstance(keys, list):
        keys = []
    targets = {str(item or "").strip() for item in keys if str(item or "").strip()}
    if not targets:
        raise HTTPException(status_code=400, detail="nenhum computador selecionado")
    rows = load_windows_inventory()
    kept = [
        row for row in rows
        if _windows_row_key(row) not in targets
        and str(row.get("ip") or "").strip() not in targets
        and str(row.get("hostname") or "").strip() not in targets
        and str(row.get("serial") or "").strip() not in targets
    ]
    removed = len(rows) - len(kept)
    save_windows_inventory(kept)
    return {"ok": True, "removed": removed, "count": len(kept)}


@router.post("/scan")
def api_windows_scan(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return scan_windows_inventory(payload if isinstance(payload, dict) else {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/clear")
def api_windows_clear() -> Dict[str, Any]:
    return clear_windows_inventory()


@router.post("/enrich/photos")
def api_windows_enrich_photos() -> Dict[str, Any]:
    rows = load_windows_inventory()
    result = enrich_windows_rows_with_photos(rows)
    save_windows_inventory(result.get("rows") or rows)
    return {"ok": True, "count": len(result.get("rows") or []), "assets": result.get("assets", 0)}


@router.get("/report.pdf")
def api_windows_report_pdf(company_name: str = "") -> FileResponse:
    rows = load_windows_inventory()
    pdf_path = build_windows_inventory_pdf(rows, company_name=company_name)
    return FileResponse(path=pdf_path, media_type="application/pdf", filename=pdf_path.name, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@router.get("/prepare-script")
def api_windows_prepare_script(username: str = Query("sightops_inv")) -> Response:
    script = build_windows_prepare_script(username=username)
    return Response(
        content=script,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sightops-preparar-windows.ps1"'},
    )


@router.get("/agent-script")
def api_windows_agent_script(request: Request) -> Response:
    base_url = str(request.base_url).rstrip("/")
    script = build_windows_agent_script(base_url=base_url)
    return Response(
        content=script,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sightops-agente-windows.ps1"'},
    )


@router.post("/agent/report")
async def api_windows_agent_report(request: Request) -> Dict[str, Any]:
    token = str(request.headers.get("x-sightops-agent-token") or "").strip()
    tenant_slug = validate_windows_agent_token(token)
    if not tenant_slug:
        raise HTTPException(status_code=401, detail="token do agente invalido")
    try:
        payload = await request.json()
        remote_ip = request.client.host if request.client else ""
        return accept_windows_agent_report(
            payload if isinstance(payload, dict) else {}, remote_ip=remote_ip, tenant_slug=tenant_slug
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
