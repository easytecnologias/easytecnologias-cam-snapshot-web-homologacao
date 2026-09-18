from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import cv2
from PIL import Image, ImageDraw


def _parse_dt(value: str) -> datetime:
    text = value.strip().replace("T", " ")
    if len(text) == 10:
        text += " 00:00:00"
    if len(text) == 16:
        text += ":00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"invalid datetime: {value!r}")


def _rtsp_url(host: str, user: str, password: str, track_id: int, start: datetime, seconds: int) -> str:
    end = start + timedelta(seconds=seconds)
    userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return (
        f"rtsp://{userinfo}@{host}:554/Streaming/tracks/{track_id}/"
        f"?starttime={start:%Y%m%dT%H%M%SZ}&endtime={end:%Y%m%dT%H%M%SZ}"
    )


def _capture_one(url: str, out_path: Path, warmup_frames: int) -> tuple[bool, str]:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        if not cap.isOpened():
            return False, "open failed"
        frame = None
        for _ in range(max(warmup_frames, 1)):
            ok, img = cap.read()
            if ok and img is not None:
                frame = img
                break
        if frame is None:
            return False, "no frame"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out_path), frame):
            return False, "write failed"
        return True, str(out_path)
    finally:
        cap.release()


def _make_contact_sheet(frames: list[tuple[datetime, Path]], out_path: Path, thumb_w: int = 320) -> None:
    if not frames:
        return
    thumbs: list[tuple[datetime, Image.Image]] = []
    for ts, path in frames:
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            continue
        ratio = thumb_w / max(img.width, 1)
        thumb_h = max(int(img.height * ratio), 1)
        img = img.resize((thumb_w, thumb_h))
        canvas = Image.new("RGB", (thumb_w, thumb_h + 24), "white")
        canvas.paste(img, (0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.text((6, thumb_h + 5), ts.strftime("%Y-%m-%d %H:%M:%S"), fill=(0, 0, 0))
        thumbs.append((ts, canvas))
    if not thumbs:
        return

    cols = 4
    cell_w = thumb_w
    cell_h = max(img.height for _, img in thumbs)
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
    for idx, (_, img) in enumerate(thumbs):
        x = (idx % cols) * cell_w
        y = (idx // cols) * cell_h
        sheet.paste(img, (x, y))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=90)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample Hikvision RTSP playback frames without storing credentials.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password-env", default="DVR_PASS")
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--date")
    parser.add_argument("--start", type=_parse_dt)
    parser.add_argument("--end", type=_parse_dt)
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--clip-seconds", type=int, default=60)
    parser.add_argument("--warmup-frames", type=int, default=12)
    parser.add_argument("--out-dir", default="data/playback_exports/hikvision_probe/rtsp_samples")
    args = parser.parse_args()

    password = os.getenv(args.password_env, "")
    if not password:
        print(f"Missing password in {args.password_env}", file=sys.stderr)
        return 2
    if args.date:
        start = _parse_dt(args.date)
        end = start + timedelta(days=1)
    elif args.start and args.end:
        start = args.start
        end = args.end
    else:
        print("Use --date or --start/--end", file=sys.stderr)
        return 2

    host = re.sub(r"^https?://", "", args.host.strip(), flags=re.I).split("/", 1)[0].split(":", 1)[0]
    track_id = int(args.channel) * 100 + 1
    out_dir = Path(args.out_dir)
    frames: list[tuple[datetime, Path]] = []

    cursor = start
    while cursor < end:
        name = f"{host}_ch{args.channel}_{cursor:%Y%m%d_%H%M%S}.jpg"
        out_path = out_dir / name
        ok, info = _capture_one(
            _rtsp_url(host, args.user, password, track_id, cursor, args.clip_seconds),
            out_path,
            args.warmup_frames,
        )
        print(f"{cursor:%Y-%m-%d %H:%M:%S}: {'OK' if ok else 'FAIL'} {info}")
        if ok:
            frames.append((cursor, out_path))
        cursor += timedelta(minutes=max(args.interval_minutes, 1))

    sheet = out_dir / f"{host}_ch{args.channel}_{start:%Y%m%d}_contact_sheet.jpg"
    _make_contact_sheet(frames, sheet)
    print(f"FRAMES={len(frames)}")
    if sheet.exists():
        print(f"SHEET={sheet}")
    return 0 if frames else 1


if __name__ == "__main__":
    raise SystemExit(main())
