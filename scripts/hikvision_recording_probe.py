from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import requests
from requests.auth import HTTPDigestAuth


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


def _track_id(channel: int) -> int:
    return int(channel) * 100 + 1


def _search_xml(channel: int, start: datetime, end: datetime, position: int, max_results: int, include_metadata: bool) -> str:
    search_id = f"sightops-probe-{int(time.time() * 1000)}-{channel}-{position}"
    metadata = (
        """
  <metadataList>
    <metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>
  </metadataList>"""
        if include_metadata
        else ""
    )
    return f"""<CMSearchDescription>
  <searchID>{search_id}</searchID>
  <trackIDList>
    <trackID>{_track_id(channel)}</trackID>
  </trackIDList>
  <timeSpanList>
    <timeSpan>
      <startTime>{start:%Y-%m-%dT%H:%M:%SZ}</startTime>
      <endTime>{end:%Y-%m-%dT%H:%M:%SZ}</endTime>
    </timeSpan>
  </timeSpanList>
  <contentTypeList>
    <contentType>video</contentType>
  </contentTypeList>
  <maxResults>{int(max_results)}</maxResults>
  <searchResultPostion>{int(position)}</searchResultPostion>{metadata}
</CMSearchDescription>"""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in node.iter():
        if _local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def _parse_search_response(text: str, channel: int) -> tuple[int | None, list[dict[str, Any]], str]:
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None, [], "invalid XML response"

    total = None
    for node in root.iter():
        if _local_name(node.tag) in {"numofmatches", "totalmatches"} and node.text:
            try:
                total = int(float(node.text.strip()))
            except ValueError:
                pass

    items: list[dict[str, Any]] = []
    for node in root.iter():
        if _local_name(node.tag) != "searchmatchitem":
            continue
        start = _child_text(node, "startTime")
        end = _child_text(node, "endTime")
        uri = _child_text(node, "playbackURI")
        track = _child_text(node, "trackID")
        rec_type = _child_text(node, "metadataDescriptor", "recordType")
        items.append(
            {
                "channel": channel,
                "track_id": track or str(_track_id(channel)),
                "start_time": start,
                "end_time": end,
                "record_type": rec_type,
                "playback_uri": uri,
            }
        )

    status = _child_text(root, "responseStatusStrg", "statusString")
    return total, items, status


def _search_window(
    *,
    base_url: str,
    auth: HTTPDigestAuth,
    channel: int,
    start: datetime,
    end: datetime,
    timeout: float,
    max_results: int,
    include_metadata: bool,
) -> list[dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    position = 0
    while True:
        body = _search_xml(channel, start, end, position=position, max_results=max_results, include_metadata=include_metadata)
        res = requests.post(
            f"{base_url}/ISAPI/ContentMgmt/search",
            auth=auth,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=(4, timeout),
        )
        if res.status_code in (401, 403):
            raise RuntimeError("DVR refused username/password")
        if res.status_code >= 400:
            compact = re.sub(r"\s+", " ", res.text or "").strip()
            raise RuntimeError(f"DVR returned HTTP {res.status_code}: {compact[:500]}")

        total, items, status = _parse_search_response(res.text or "", channel)
        all_items.extend(items)
        if not items:
            break
        position += len(items)
        if total is not None and position >= total:
            break
        if len(items) < max_results:
            break
        if status and status.lower() in {"nomatch", "no match"}:
            break
    return all_items


def _with_rtsp_credentials(uri: str, user: str, password: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "rtsp" or "@" in parsed.netloc:
        return uri
    host = parsed.netloc
    userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
    return urlunparse(parsed._replace(netloc=f"{userinfo}@{host}"))


def _write_samples(items: list[dict[str, Any]], user: str, password: str, out_dir: Path, per_segment: int) -> list[dict[str, Any]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []

    samples: list[dict[str, Any]] = []
    for idx, item in enumerate(items[:per_segment], start=1):
        uri = str(item.get("playback_uri") or "")
        if not uri:
            continue
        sample_path = out_dir / f"sample_{idx:03d}_{re.sub(r'[^0-9A-Za-z_-]+', '_', item.get('start_time') or '')}.jpg"
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            _with_rtsp_credentials(uri, user, password),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(sample_path),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=45, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            samples.append({"start_time": item.get("start_time"), "file": str(sample_path), "size": sample_path.stat().st_size})
        except Exception as exc:
            samples.append({"start_time": item.get("start_time"), "error": str(exc)[:180]})
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Hikvision ISAPI recording index without storing credentials.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password-env", default="DVR_PASS")
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--date", help="YYYY-MM-DD; expands to the full day")
    parser.add_argument("--start", type=_parse_dt)
    parser.add_argument("--end", type=_parse_dt)
    parser.add_argument("--window-minutes", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--max-results", type=int, default=64)
    parser.add_argument("--sample-count", type=int, default=0)
    parser.add_argument("--include-metadata", action="store_true")
    parser.add_argument("--out-dir", default="data/playback_exports/hikvision_probe")
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
    if end <= start:
        print("end must be greater than start", file=sys.stderr)
        return 2

    host = re.sub(r"^https?://", "", args.host.strip(), flags=re.I).split("/", 1)[0]
    base_url = f"http://{host}"
    auth = HTTPDigestAuth(args.user, password)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(minutes=max(args.window_minutes, 1)), end)
        chunk_items = _search_window(
            base_url=base_url,
            auth=auth,
            channel=args.channel,
            start=cursor,
            end=chunk_end,
            timeout=args.timeout,
            max_results=args.max_results,
            include_metadata=args.include_metadata,
        )
        items.extend(chunk_items)
        print(f"{cursor:%H:%M}-{chunk_end:%H:%M}: {len(chunk_items)} segment(s)")
        cursor = chunk_end

    # De-duplicate repeated boundary results.
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("start_time")), str(item.get("end_time")), str(item.get("playback_uri")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    samples = _write_samples(unique, args.user, password, out_dir, args.sample_count) if args.sample_count > 0 else []
    result = {
        "ok": True,
        "host": host,
        "channel": args.channel,
        "track_id": _track_id(args.channel),
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(unique),
        "segments": unique,
        "samples": samples,
    }
    out_file = out_dir / f"{host.replace(':', '_')}_ch{args.channel}_{start:%Y%m%d}_segments.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON={out_file}")
    print(f"COUNT={len(unique)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
