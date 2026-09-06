from __future__ import annotations

import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

from app.core.paths import DATA_DIR

EXPORT_DIR = DATA_DIR / "playback_exports"

_DAV_MAGIC = b"DHAV"

# Familia de fabricantes que expoe o download rapido via loadfile.cgi
# (mesma checagem usada por query_recording_segments em nvr_ai_service.py).
DAHUA_VENDOR_ALIASES = {"", "auto", "dahua", "intelbras", "intelbras/dahua", "dvr"}


class RecordingNotFoundError(Exception):
    """Levantado quando o DVR nao tem gravacao valida na janela pedida."""


class UnsupportedVendorError(Exception):
    """Levantado quando o fabricante do gravador nao suporta o download rapido (loadfile.cgi)."""


class RecorderAuthError(Exception):
    """Levantado quando o DVR recusa as credenciais."""


def is_vendor_supported(vendor: str) -> bool:
    return (vendor or "").strip().lower() in DAHUA_VENDOR_ALIASES


def parse_dt(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(text)


def clean_host(host: str) -> str:
    cleaned = host.strip()
    cleaned = re.sub(r"^https?://", "", cleaned, flags=re.I)
    cleaned = cleaned.split("/", 1)[0].strip()
    if not cleaned or any(ch.isspace() for ch in cleaned) or "\\" in cleaned:
        raise ValueError("DVR invalido.")
    return cleaned


def safe_stem(host: str, channel: int, start: datetime, end: datetime) -> str:
    host_part = re.sub(r"[^A-Za-z0-9_.-]+", "_", host)
    return f"{host_part}_ch{channel}_{start:%Y%m%d_%H%M%S}_{end:%H%M%S}_{int(time.time())}"


def _rpc_loadfile_fallback(
    *,
    host: str,
    http_port: int,
    user: str,
    password: str,
    channel: int,
    start: datetime,
    end: datetime,
    out_path: Path,
    timeout_sec: int,
) -> None:
    """Caminho de reserva quando loadfile.cgi devolve algo sem cabecalho DAV
    valido -- bug real confirmado em pelo menos um DVR Intelbras em producao
    (10.10.9.151), ja documentado antes para outros DVRs da mesma familia.

    Busca o(s) arquivo(s) reais pelo indice do proprio DVR (mediaFileFind.cgi,
    a mesma API que fabricante nenhum documenta como "rapida" mas que e
    exatamente isso), baixa cada um via RPC_Loadfile (download de arquivo
    inteiro, nao playback), concatena se a janela cruzar mais de um arquivo,
    e corta com ffmpeg (stream copy, sem recodificar) pro intervalo exato
    pedido -- loadfile.cgi teria devolvido so o intervalo pedido, entao o
    contrato desta funcao (out_path = exatamente [start, end]) se mantem
    igual pra quem chama, nao importa qual dos dois caminhos foi usado.
    """
    from app.services.nvr_ai_service import dahua_media_find_segments

    achado = dahua_media_find_segments(
        host=host, http_port=http_port, user=user, password=password,
        channel=channel, start_dt=start, end_dt=end,
    )
    segmentos = sorted(
        (s for s in (achado.get("segments") or []) if s.get("file_path")),
        key=lambda s: s.get("start_time") or "",
    )
    if not segmentos:
        raise RecordingNotFoundError(
            "Nenhum segmento encontrado no indice do DVR (mediaFileFind) para essa janela."
        )

    clean = clean_host(host)
    port_part = f":{int(http_port)}" if http_port and int(http_port) != 80 else ""
    partes: list[Path] = []
    try:
        for idx, seg in enumerate(segmentos):
            url = f"http://{clean}{port_part}/cgi-bin/RPC_Loadfile{seg['file_path']}"
            parte = out_path.parent / f"{out_path.stem}.parte{idx}.dav"
            parte.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(url, auth=HTTPDigestAuth(user, password), stream=True, timeout=(8, timeout_sec)) as res:
                if res.status_code in (401, 403):
                    raise RecorderAuthError("Credencial recusada pelo DVR (RPC_Loadfile).")
                if res.status_code >= 400:
                    raise RuntimeError(f"DVR respondeu HTTP {res.status_code} em RPC_Loadfile.")
                total = 0
                checked_magic = False
                with parte.open("wb") as fh:
                    for chunk in res.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if not checked_magic:
                            checked_magic = True
                            if not chunk.startswith(_DAV_MAGIC):
                                raise RecordingNotFoundError("RPC_Loadfile nao retornou um DAV valido.")
                        total += len(chunk)
                        fh.write(chunk)
                if total < 1024:
                    raise RecordingNotFoundError("RPC_Loadfile retornou arquivo vazio.")
            partes.append(parte)

        if len(partes) == 1:
            bruto = partes[0]
        else:
            lista_path = out_path.parent / f"{out_path.stem}.concat.txt"
            lista_path.write_text("".join(f"file '{p.name}'\n" for p in partes), encoding="utf-8")
            bruto = out_path.parent / f"{out_path.stem}.bruto.dav"
            run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lista_path), "-c", "copy", str(bruto)], timeout_sec)
            lista_path.unlink(missing_ok=True)
            partes.append(bruto)

        primeiro_inicio = parse_dt(segmentos[0]["start_time"])
        offset = max(0.0, (start - primeiro_inicio).total_seconds())
        duracao = max(0.1, (end - start).total_seconds())
        run_ffmpeg(
            ["-ss", f"{offset:.2f}", "-i", str(bruto), "-t", f"{duracao:.2f}", "-c", "copy", "-f", "mp4", str(out_path)],
            timeout_sec,
        )
    finally:
        for p in partes:
            p.unlink(missing_ok=True)


def download_dav(
    *,
    host: str,
    http_port: int = 80,
    user: str,
    password: str,
    channel: int,
    start: datetime,
    end: datetime,
    out_path: Path,
    timeout_sec: int = 180,
) -> None:
    """Baixa um trecho gravado via HTTP loadfile.cgi, com reserva automatica
    via busca de indice + RPC_Loadfile quando loadfile.cgi falha.

    Isso e uma transferencia de arquivo comum (limitada pela banda da rede), NAO um
    playback RTSP em tempo real -- e o que torna essa busca viavel (30 min de gravacao
    nao levam mais 30 min pra baixar).

    Levanta RecordingNotFoundError se o DVR nao tiver gravacao valida nessa janela
    (mesmo depois de tentar a reserva), RecorderAuthError se as credenciais forem
    recusadas.
    """
    clean = clean_host(host)
    port_part = f":{int(http_port)}" if http_port and int(http_port) != 80 else ""
    start_s = quote(start.strftime("%Y-%m-%d %H:%M:%S"))
    end_s = quote(end.strftime("%Y-%m-%d %H:%M:%S"))
    url = (
        f"http://{clean}{port_part}/cgi-bin/loadfile.cgi?action=startLoad"
        f"&channel={channel}&startTime={start_s}&endTime={end_s}"
    )
    try:
        with requests.get(
            url,
            auth=HTTPDigestAuth(user, password),
            stream=True,
            timeout=(8, timeout_sec),
        ) as res:
            if res.status_code in (401, 403):
                raise RecorderAuthError("Credencial recusada pelo DVR.")
            if res.status_code == 404:
                raise RecordingNotFoundError("Gravacao nao encontrada no DVR.")
            if res.status_code >= 400:
                # visto ao vivo num DVR real: loadfile.cgi as vezes devolve
                # 400 pra uma janela onde a gravacao existe de verdade (o
                # indice acha ela sem problema) -- nao trata como erro
                # definitivo, cai pro fallback como qualquer outra falha
                # deste endpoint especifico.
                raise RecordingNotFoundError(f"loadfile.cgi respondeu HTTP {res.status_code}.")

            out_path.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            checked_magic = False
            with out_path.open("wb") as fh:
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if not checked_magic:
                        checked_magic = True
                        if not chunk.startswith(_DAV_MAGIC):
                            raise RecordingNotFoundError(
                                "DVR nao retornou uma gravacao valida (sem cabecalho DAV) nessa janela."
                            )
                    total += len(chunk)
                    fh.write(chunk)
            if total < 1024:
                raise RecordingNotFoundError("DVR retornou arquivo vazio.")
    except RecordingNotFoundError:
        # loadfile.cgi e conhecido por devolver algo sem cabecalho DAV valido
        # (ou vazio) em pelo menos um firmware real, mesmo quando a gravacao
        # existe de verdade -- tenta o caminho de reserva antes de desistir.
        _rpc_loadfile_fallback(
            host=host, http_port=http_port, user=user, password=password,
            channel=channel, start=start, end=end, out_path=out_path, timeout_sec=timeout_sec,
        )
    except (RecorderAuthError, RuntimeError):
        raise
    except requests.RequestException as exc:
        raise RuntimeError(f"Falha ao consultar DVR: {exc}") from exc


def run_ffmpeg(args: list[str], timeout_sec: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg nao esta disponivel no servidor.")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        subprocess.run(cmd, check=True, timeout=timeout_sec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Conversao excedeu o tempo limite.") from exc
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(msg or "Falha ao converter gravacao.") from exc


def download_clip_mp4(
    *,
    host: str,
    http_port: int = 80,
    user: str,
    password: str,
    channel: int,
    start_dt: datetime,
    end_dt: datetime,
    out_dir: Path = EXPORT_DIR,
    timeout_sec: int = 180,
    scale: str = "scale=640:-2",
) -> Path:
    """Baixa + transcodifica um trecho gravado para um MP4 leve (h264, sem audio,
    resolucao reduzida) pronto pra subir numa API de video. Apaga o .dav intermediario.

    Levanta RecordingNotFoundError / RecorderAuthError / RuntimeError / TimeoutError.
    """
    clean = clean_host(host)
    stem = safe_stem(clean, channel, start_dt, end_dt)
    dav_path = out_dir / f"{stem}.dav"
    mp4_path = out_dir / f"{stem}.mp4"
    try:
        download_dav(
            host=host,
            http_port=http_port,
            user=user,
            password=password,
            channel=channel,
            start=start_dt,
            end=end_dt,
            out_path=dav_path,
            timeout_sec=timeout_sec,
        )
        run_ffmpeg(
            [
                "-i", str(dav_path),
                "-vf", scale,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-movflags", "+faststart", "-an",
                str(mp4_path),
            ],
            timeout_sec,
        )
        return mp4_path
    finally:
        dav_path.unlink(missing_ok=True)
