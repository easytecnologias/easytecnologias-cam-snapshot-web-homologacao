from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from app.services.gemini_video_search import search_clip_for_query
from app.services.kmz_ops import _extract_leading_num
from app.services.nvr_ai_service import list_nvr_targets, query_recording_segments
from app.services.recorder_media_service import (
    EXPORT_DIR,
    RecorderAuthError,
    RecordingNotFoundError,
    download_clip_mp4,
)

# Janela padrao por salto quando o usuario da um horario pontual em vez de intervalo.
NVR_AI_SEARCH_WINDOW_MIN = int(os.getenv("NVR_AI_SEARCH_WINDOW_MIN", "5"))
# Limite pra janela INICIAL pedida pelo usuario (nao os saltos, que ja sao
# curtos por padrao). Medido ao vivo: um site com link fraco (1.6 MB/s)
# levou ~5 minutos so pra baixar um segmento de 30 minutos de gravacao, e a
# conversao de 30 minutos de video pra IA estourou os 180s de timeout do
# ffmpeg -- sem esse limite, o operador pede uma janela grande (comum, "foi
# por volta desse horario") e a busca trava por muito tempo sem avisar nada.
NVR_AI_SEARCH_MAX_WINDOW_MIN = int(os.getenv("NVR_AI_SEARCH_MAX_WINDOW_MIN", "10"))
NVR_AI_SEARCH_MAX_HOPS_DEFAULT = int(os.getenv("NVR_AI_SEARCH_MAX_HOPS", "3"))
NVR_AI_SEARCH_MAX_HOPS_CAP = 6
NVR_AI_SEARCH_MAX_TOTAL_SEC = int(os.getenv("NVR_AI_SEARCH_MAX_TOTAL_SEC", "180"))
# Buffer pra tras cobre deriva de relogio entre gravadores/sites.
NVR_AI_SEARCH_BACK_BUFFER_MIN = 1


@dataclass
class SearchHit:
    host: str
    http_port: int
    channel: int
    title: str
    site: str
    number: Optional[int]
    timestamp: datetime
    description: str
    confidence: float
    clip_url: Optional[str] = None


@dataclass
class SearchMiss:
    host: str
    channel: int
    title: str
    reason: str


@dataclass
class ChainedSearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    misses: list[SearchMiss] = field(default_factory=list)
    truncated: bool = False


def _camera_number(target: dict) -> Optional[int]:
    num = _extract_leading_num(target.get("title") or target.get("local") or "")
    if not num:
        return None
    try:
        return int(num)
    except ValueError:
        return None


def _same_site_targets(targets: list[dict], site: str) -> list[dict]:
    site_l = (site or "").strip().lower()
    if not site_l:
        return []
    return [t for t in targets if (t.get("site") or t.get("local") or "").strip().lower() == site_l]


def _neighbors(targets_same_site: list[dict], current_number: int, visited: set[tuple[str, int]]) -> list[dict]:
    numbered: list[tuple[int, dict]] = []
    for t in targets_same_site:
        n = _camera_number(t)
        if n is None:
            continue
        key = (t.get("host"), int(t.get("channel") or 0))
        if key in visited:
            continue
        if n in (current_number - 1, current_number + 1):
            numbered.append((n, t))
    # mais proximo (N-1/N+1 empatam) primeiro; ordem estavel por numero
    numbered.sort(key=lambda pair: abs(pair[0] - current_number))
    return [t for _, t in numbered]


def _clip_url_for(path: Path) -> str:
    return f"/api/playback/files/{quote(path.name)}"


def _search_one_camera(
    *, target: dict, user: str, password: str, start_dt: datetime, end_dt: datetime, query: str,
) -> tuple[Optional[SearchHit], Optional[str]]:
    """Roda a pre-checagem + download + busca Gemini numa unica camera/janela.

    Retorna (hit, None) em caso de achado, ou (None, motivo) caso contrario.
    Deixa propagar GeminiNotConfiguredError/GeminiRateLimitedError -- essas nao sao
    "miss" de uma camera, sao motivo pra abortar a busca inteira.
    """
    host = target["host"]
    channel = int(target["channel"])
    http_port = int(target.get("http_port") or 80)

    try:
        segs = query_recording_segments(
            {
                "host": host,
                "user": user,
                "password": password,
                "channel": channel,
                "http_port": http_port,
                "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "vendor": "auto",
            }
        )
        if not segs.get("record_segments"):
            return None, "sem gravação nessa janela"
    except Exception:
        pass  # pre-checagem e best-effort; se falhar, tenta o download mesmo assim

    try:
        clip_path = download_clip_mp4(
            host=host,
            http_port=http_port,
            user=user,
            password=password,
            channel=channel,
            start_dt=start_dt,
            end_dt=end_dt,
            out_dir=EXPORT_DIR,
            timeout_sec=180,
        )
    except RecordingNotFoundError:
        return None, "sem gravação nessa janela"
    except RecorderAuthError:
        return None, "credencial recusada pelo gravador"
    except Exception as exc:
        return None, f"falha ao baixar: {exc}"

    result = search_clip_for_query(clip_path, query, clip_start_dt=start_dt, clip_end_dt=end_dt)

    if not result.found or result.hit_at is None:
        return None, "não encontrado"

    return (
        SearchHit(
            host=host,
            http_port=http_port,
            channel=channel,
            title=target.get("title") or f"Canal {channel}",
            site=target.get("site") or "",
            number=_camera_number(target),
            timestamp=result.hit_at,
            description=result.description,
            confidence=result.confidence,
            clip_url=_clip_url_for(clip_path),
        ),
        None,
    )


def search_recordings(
    *,
    host: str,
    http_port: int,
    channel: int,
    user: str,
    password: str,
    site: str,
    start_dt: datetime,
    end_dt: datetime,
    query: str,
    max_hops: int = NVR_AI_SEARCH_MAX_HOPS_DEFAULT,
    window_min: int = NVR_AI_SEARCH_WINDOW_MIN,
) -> ChainedSearchResult:
    """Busca a camera/janela pedida; se achar, encadeia pras cameras vizinhas
    (numero sequencial do titulo, mesmo site) numa janela curta apos o horario achado.

    Chamadas ficam limitadas por construcao: no maximo 1 (semente) + 2 vizinhos por
    salto x max_hops saltos, com corte adicional por orcamento de tempo total.

    Levanta ValueError se a janela pedida for maior que NVR_AI_SEARCH_MAX_WINDOW_MIN --
    medido ao vivo: um site com link fraco levou ~5 min so pra baixar 30 min de
    gravacao, e a conversao pra IA estourou o timeout do ffmpeg. Sem esse limite,
    um pedido comum ("foi por volta desse horario", janela larga) trava a busca
    por muito tempo sem avisar nada antes.
    """
    janela_min = (end_dt - start_dt).total_seconds() / 60
    if janela_min > NVR_AI_SEARCH_MAX_WINDOW_MIN:
        raise ValueError(
            f"Janela de {janela_min:.0f} min maior que o limite de "
            f"{NVR_AI_SEARCH_MAX_WINDOW_MIN} min para busca por IA -- "
            f"reduza o intervalo entre Inicio e Fim."
        )
    max_hops = max(0, min(int(max_hops), NVR_AI_SEARCH_MAX_HOPS_CAP))
    deadline = time.time() + NVR_AI_SEARCH_MAX_TOTAL_SEC
    result = ChainedSearchResult()

    targets_resp = list_nvr_targets()
    all_targets = targets_resp.get("targets") or []
    site_targets = _same_site_targets(all_targets, site) if site else []

    seed_target = {"host": host, "http_port": http_port, "channel": channel, "title": "", "site": site}
    for t in all_targets:
        if t.get("host") == host and int(t.get("channel") or 0) == channel:
            seed_target = t
            break

    hit, miss_reason = _search_one_camera(
        target=seed_target, user=user, password=password, start_dt=start_dt, end_dt=end_dt, query=query
    )
    if not hit:
        result.misses.append(
            SearchMiss(
                host=host,
                channel=channel,
                title=seed_target.get("title") or "",
                reason=miss_reason or "não encontrado",
            )
        )
        return result

    result.hits.append(hit)
    visited = {(host, channel)}
    current = hit
    hops_done = 0

    while hops_done < max_hops and site_targets:
        if time.time() > deadline:
            result.truncated = True
            break
        if current.number is None:
            break

        candidates = _neighbors(site_targets, current.number, visited)
        if not candidates:
            break

        hop_start = current.timestamp - timedelta(minutes=NVR_AI_SEARCH_BACK_BUFFER_MIN)
        hop_end = current.timestamp + timedelta(minutes=window_min)

        next_hit = None
        for cand in candidates:
            visited.add((cand.get("host"), int(cand.get("channel") or 0)))
            cand_hit, cand_miss = _search_one_camera(
                target=cand, user=user, password=password, start_dt=hop_start, end_dt=hop_end, query=query
            )
            if cand_hit:
                next_hit = cand_hit
                result.hits.append(cand_hit)
                break
            result.misses.append(
                SearchMiss(
                    host=cand.get("host"),
                    channel=int(cand.get("channel") or 0),
                    title=cand.get("title") or "",
                    reason=cand_miss or "não encontrado",
                )
            )

        hops_done += 1
        if not next_hit:
            break
        current = next_hit

    return result
