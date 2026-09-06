from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.endpoints.cameras import resolve_camera_password
from app.services.gemini_video_search import GeminiNotConfiguredError, GeminiRateLimitedError
from app.services.nvr_ai_service import list_nvr_targets, query_recording_segments
from app.services.nvr_search_service import ChainedSearchResult, search_recordings
from app.services.recorder_media_service import parse_dt

router = APIRouter(prefix="/api/ia", tags=["ia"])
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_SEARCH_JOBS: Dict[str, Dict[str, Any]] = {}


class NvrRecordingsRequest(BaseModel):
    host: str
    channel: int = Field(default=1, ge=1, le=256)
    http_port: int = Field(default=80, ge=1, le=65535)
    user: str = "admin"
    password: str = ""
    start_time: str
    end_time: str
    vendor: str = "auto"


class NvrSearchRequest(BaseModel):
    host: str
    http_port: int = Field(default=80, ge=1, le=65535)
    channel: int = Field(ge=1, le=256)
    user: str = "admin"
    password: str = ""
    site: str = ""
    start_time: str
    end_time: str
    query: str = Field(min_length=1, max_length=400)
    max_hops: int = Field(default=3, ge=0, le=6)
    window_min: int = Field(default=5, ge=1, le=30)


def _result_to_dict(result: ChainedSearchResult) -> Dict[str, Any]:
    return {
        "hits": [
            {
                "host": h.host,
                "http_port": h.http_port,
                "channel": h.channel,
                "title": h.title,
                "site": h.site,
                "number": h.number,
                "timestamp": h.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "description": h.description,
                "confidence": h.confidence,
                "clip_url": h.clip_url,
            }
            for h in result.hits
        ],
        "misses": [
            {"host": m.host, "channel": m.channel, "title": m.title, "reason": m.reason}
            for m in result.misses
        ],
        "truncated": result.truncated,
    }


@router.get("/nvr/targets")
def api_ia_nvr_targets() -> Dict[str, Any]:
    return list_nvr_targets()


def _resolve_ia_password(host: str, user: str, password: str) -> tuple[str, str]:
    """Se o operador nao digitou senha, tenta a ja salva pra este DVR/camera
    (mesmo deposito usado pelo botao Web) -- so pede pra digitar quando
    nao existe nenhuma salva ainda."""
    return resolve_camera_password(host, user, password)


@router.post("/nvr/recordings")
def api_ia_nvr_recordings(req: NvrRecordingsRequest) -> Dict[str, Any]:
    user, password = _resolve_ia_password(req.host, req.user, req.password)
    if not password:
        raise HTTPException(status_code=400, detail="Sem senha salva para este gravador -- informe a senha.")
    payload = req.model_dump()
    payload["user"], payload["password"] = user, password
    try:
        return query_recording_segments(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


def _run_search_job(job_id: str, req: NvrSearchRequest) -> None:
    job = _SEARCH_JOBS.get(job_id)
    if not job:
        return
    job["status"] = "running"
    job["started_at"] = time.time()
    try:
        user, password = _resolve_ia_password(req.host, req.user, req.password)
        if not password:
            raise ValueError("Sem senha salva para este gravador -- informe a senha.")
        start_dt = parse_dt(req.start_time)
        end_dt = parse_dt(req.end_time)
        if end_dt <= start_dt:
            raise ValueError("data final deve ser maior que a inicial")

        result = search_recordings(
            host=req.host,
            http_port=req.http_port,
            channel=req.channel,
            user=user,
            password=password,
            site=req.site,
            start_dt=start_dt,
            end_dt=end_dt,
            query=req.query,
            max_hops=req.max_hops,
            window_min=req.window_min,
        )
        job["status"] = "done"
        job["result"] = _result_to_dict(result)
        job["finished_at"] = time.time()
    except GeminiNotConfiguredError as e:
        job["status"] = "error"
        job["error"] = f"IA nao configurada: {e}"
        job["finished_at"] = time.time()
    except GeminiRateLimitedError as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["finished_at"] = time.time()
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["finished_at"] = time.time()


@router.post("/nvr/search/jobs")
def api_ia_nvr_search_job(req: NvrSearchRequest) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    _SEARCH_JOBS[job_id] = {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "created_at": time.time(),
        "payload": {
            "host": req.host,
            "channel": req.channel,
            "start_time": req.start_time,
            "end_time": req.end_time,
            "query": req.query,
            "max_hops": req.max_hops,
            "window_min": req.window_min,
        },
    }
    _SEARCH_EXECUTOR.submit(_run_search_job, job_id, req)
    return {"ok": True, "job_id": job_id, "status": "queued"}


@router.get("/nvr/search/jobs/{job_id}")
def api_ia_nvr_search_job_status(job_id: str) -> Dict[str, Any]:
    job = _SEARCH_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="busca nao encontrada")
    elapsed = int(time.time() - float(job.get("created_at") or time.time()))
    return {
        "ok": True,
        "job_id": job_id,
        "status": job.get("status"),
        "elapsed_sec": elapsed,
        "payload": job.get("payload") or {},
        "result": job.get("result"),
        "error": job.get("error") or "",
    }
