from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import cv2


def _parse_dt(value: str) -> datetime:
    text = value.strip().replace("T", " ")
    if len(text) == 16:
        text += ":00"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"invalid datetime: {value!r}")


def _rtsp_url(host: str, user: str, password: str, track_id: int, start: datetime, end: datetime) -> str:
    userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return (
        f"rtsp://{userinfo}@{host}:554/Streaming/tracks/{track_id}/"
        f"?starttime={start:%Y%m%dT%H%M%SZ}&endtime={end:%Y%m%dT%H%M%SZ}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a short Hikvision RTSP playback clip with OpenCV.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password-env", default="DVR_PASS")
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--start", type=_parse_dt, required=True)
    parser.add_argument("--end", type=_parse_dt, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-frames", type=int, default=3000)
    args = parser.parse_args()

    password = os.getenv(args.password_env, "")
    if not password:
        print(f"Missing password in {args.password_env}", file=sys.stderr)
        return 2
    if args.end <= args.start:
        print("end must be greater than start", file=sys.stderr)
        return 2

    host = re.sub(r"^https?://", "", args.host.strip(), flags=re.I).split("/", 1)[0].split(":", 1)[0]
    track_id = int(args.channel) * 100 + 1
    cap = cv2.VideoCapture(_rtsp_url(host, args.user, password, track_id, args.start, args.end), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("open failed", file=sys.stderr)
        return 1

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0 or fps > 60:
        fps = 15.0
    writer = None
    written = 0
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        while written < args.max_frames:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if writer is None:
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
                if not writer.isOpened():
                    print("writer failed", file=sys.stderr)
                    return 1
            writer.write(frame)
            written += 1
    finally:
        cap.release()
        if writer is not None:
            writer.release()

    if written <= 0:
        print("no frames", file=sys.stderr)
        return 1
    print(f"OUT={out_path}")
    print(f"FRAMES={written}")
    print(f"SIZE={out_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
