from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services.recorder_media_service import (
    EXPORT_DIR,
    RecorderAuthError,
    RecordingNotFoundError,
    clean_host,
    download_dav,
    parse_dt,
    run_ffmpeg,
    safe_stem,
)

router = APIRouter(prefix="/api/playback", tags=["playback"])

SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]+$")


class PlaybackClipRequest(BaseModel):
    host: str = Field(min_length=3, max_length=128)
    user: str = Field(default="admin", min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=160)
    channel: int = Field(ge=0, le=256)
    start: str
    end: str
    format: Literal["mp4", "dav"] = "mp4"
    timeout_sec: int = Field(default=180, ge=10, le=900)
    connector_id: str = Field(default="", max_length=80)


class PlaybackSnapshotRequest(BaseModel):
    host: str = Field(min_length=3, max_length=128)
    user: str = Field(default="admin", min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=160)
    channel: int = Field(ge=0, le=256)
    timestamp: str
    timeout_sec: int = Field(default=45, ge=10, le=180)
    connector_id: str = Field(default="", max_length=80)


class PlaybackFramesRequest(PlaybackClipRequest):
    interval_seconds: int = Field(default=60, ge=1, le=3600)


def _parse_dt_or_422(value: str) -> datetime:
    try:
        return parse_dt(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Data/hora inválida.") from exc


def _clean_host_or_422(host: str) -> str:
    try:
        return clean_host(host)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="DVR inválido.") from exc


def _reach_playback_host(host: str, connector_id: str = "") -> str:
    """Gravador de conector isolado -> IP virtual (vnat) pra baixar a gravacao
    pelo tunel. O download e HTTP (loadfile.cgi/RPC), entao TCP virtual passa.
    O conector vem do request ou, se vazio, do inventario de gravadores (mesma
    logica do scan em nvr.py). Sem mapa vnat, virtual_ip_for devolve o IP real."""
    real = str(host or "").strip()
    if not real:
        return host
    try:
        from app.services import connector_routing_vnat as _vnat
        cid = str(connector_id or "").strip()
        if not cid:
            try:
                from app.api.endpoints.nvr import _recorder_connector_for_host
                cid = _recorder_connector_for_host(real)
            except Exception:
                cid = ""
        return _vnat.virtual_ip_for(cid, real) or real
    except Exception:
        return real


def _download_dav_or_http(payload: PlaybackClipRequest, start: datetime, end: datetime, out_path: Path) -> None:
    try:
        download_dav(
            host=_reach_playback_host(payload.host, getattr(payload, "connector_id", "")),
            user=payload.user,
            password=payload.password,
            channel=payload.channel,
            start=start,
            end=end,
            out_path=out_path,
            timeout_sec=payload.timeout_sec,
        )
    except RecorderAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RecordingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _run_ffmpeg_or_http(args: list[str], timeout_sec: int) -> None:
    try:
        run_ffmpeg(args, timeout_sec)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except RuntimeError as exc:
        status = 503 if "não está disponível" in str(exc) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _file_url(path: Path) -> str:
    return f"/api/playback/files/{quote(path.name)}"


def _validate_range(start: datetime, end: datetime) -> None:
    if end <= start:
        raise HTTPException(status_code=422, detail="O fim precisa ser maior que o início.")
    if end - start > timedelta(minutes=60):
        raise HTTPException(status_code=422, detail="Trecho limitado a 60 minutos por consulta.")


def _validate_frame_count(start: datetime, end: datetime, interval_seconds: int) -> int:
    total_seconds = int((end - start).total_seconds())
    count = (total_seconds // interval_seconds) + 1
    if count > 720:
        raise HTTPException(status_code=422, detail="Sequência limitada a 720 frames. Aumente o intervalo.")
    return count


@router.post("/clip")
def create_clip(payload: PlaybackClipRequest) -> dict:
    start = _parse_dt_or_422(payload.start)
    end = _parse_dt_or_422(payload.end)
    _validate_range(start, end)

    host = _clean_host_or_422(payload.host)
    stem = safe_stem(host, payload.channel, start, end)
    dav_path = EXPORT_DIR / f"{stem}.dav"
    _download_dav_or_http(payload, start, end, dav_path)

    if payload.format == "dav":
        return {
            "ok": True,
            "format": "dav",
            "url": _file_url(dav_path),
            "filename": dav_path.name,
            "size": dav_path.stat().st_size,
        }

    mp4_path = EXPORT_DIR / f"{stem}.mp4"
    try:
        _run_ffmpeg_or_http(
            ["-i", str(dav_path), "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart", "-an", str(mp4_path)],
            payload.timeout_sec,
        )
    except HTTPException as exc:
        return {
            "ok": True,
            "format": "dav",
            "url": _file_url(dav_path),
            "filename": dav_path.name,
            "size": dav_path.stat().st_size,
            "warning": exc.detail,
        }

    return {
        "ok": True,
        "format": "mp4",
        "url": _file_url(mp4_path),
        "filename": mp4_path.name,
        "size": mp4_path.stat().st_size,
        "source_url": _file_url(dav_path),
    }


@router.post("/snapshot")
def create_snapshot(payload: PlaybackSnapshotRequest) -> dict:
    ts = _parse_dt_or_422(payload.timestamp)
    start = ts - timedelta(seconds=2)
    end = ts + timedelta(seconds=3)
    clip_payload = PlaybackClipRequest(
        host=payload.host,
        user=payload.user,
        password=payload.password,
        channel=payload.channel,
        start=start.strftime("%Y-%m-%d %H:%M:%S"),
        end=end.strftime("%Y-%m-%d %H:%M:%S"),
        format="dav",
        timeout_sec=payload.timeout_sec,
    )

    host = _clean_host_or_422(payload.host)
    stem = safe_stem(host, payload.channel, start, end)
    dav_path = EXPORT_DIR / f"{stem}.dav"
    jpg_path = EXPORT_DIR / f"{stem}.jpg"
    _download_dav_or_http(clip_payload, start, end, dav_path)
    _run_ffmpeg_or_http(["-ss", "00:00:02", "-i", str(dav_path), "-frames:v", "1", "-q:v", "3", str(jpg_path)], payload.timeout_sec)

    return {
        "ok": True,
        "format": "jpg",
        "url": _file_url(jpg_path),
        "filename": jpg_path.name,
        "size": jpg_path.stat().st_size,
        "source_url": _file_url(dav_path),
    }


@router.post("/frames")
def create_frames(payload: PlaybackFramesRequest) -> dict:
    start = _parse_dt_or_422(payload.start)
    end = _parse_dt_or_422(payload.end)
    _validate_range(start, end)
    expected = _validate_frame_count(start, end, payload.interval_seconds)

    host = _clean_host_or_422(payload.host)
    stem = safe_stem(host, payload.channel, start, end)
    dav_path = EXPORT_DIR / f"{stem}.dav"
    _download_dav_or_http(payload, start, end, dav_path)

    pattern = EXPORT_DIR / f"{stem}_frame_%04d.jpg"
    _run_ffmpeg_or_http(
        [
            "-i", str(dav_path),
            "-vf", f"fps=1/{payload.interval_seconds},scale=1280:-2",
            "-q:v", "3",
            str(pattern),
        ],
        payload.timeout_sec,
    )

    files = sorted(EXPORT_DIR.glob(f"{stem}_frame_*.jpg"))
    frames = []
    for idx, path in enumerate(files):
        frames.append(
            {
                "index": idx + 1,
                "timestamp": (start + timedelta(seconds=idx * payload.interval_seconds)).strftime("%Y-%m-%d %H:%M:%S"),
                "url": _file_url(path),
                "filename": path.name,
                "size": path.stat().st_size,
            }
        )

    if not frames:
        raise HTTPException(status_code=404, detail="Nenhum frame extraído.")

    return {
        "ok": True,
        "format": "frames",
        "interval_seconds": payload.interval_seconds,
        "expected": expected,
        "count": len(frames),
        "frames": frames,
        "source_url": _file_url(dav_path),
    }


@router.get("/files/{filename}")
def get_playback_file(filename: str) -> FileResponse:
    if not SAFE_FILE.match(filename):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    path = EXPORT_DIR / filename
    try:
        path.resolve().relative_to(EXPORT_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.") from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

    media = "application/octet-stream"
    if path.suffix.lower() == ".mp4":
        media = "video/mp4"
    elif path.suffix.lower() in (".jpg", ".jpeg"):
        media = "image/jpeg"
    return FileResponse(path, media_type=media, filename=path.name, headers={"Cache-Control": "no-cache"})
