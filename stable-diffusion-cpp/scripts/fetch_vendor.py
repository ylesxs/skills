#!/usr/bin/env python3
"""fetch_vendor.py - install the ffmpeg binary this skill uses for mp4 output.

The wheel `imageio-ffmpeg` bundles a static ffmpeg; we only need the executable.
It is not committed to the repository (87 MB), so a fresh clone runs this once:

    python scripts/fetch_vendor.py

Lookup order inside sdgen.py: $SDGEN_FFMPEG -> ffmpeg on PATH -> this vendor copy.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple/imageio-ffmpeg/",
    "https://pypi.org/simple/imageio-ffmpeg/",
]


def platform_tag() -> str:
    if sys.platform.startswith("win"):
        return "win_amd64"
    if sys.platform == "darwin":
        return "macosx"
    return "manylinux"


def find_wheel_url(tag: str) -> str | None:
    pattern = re.compile(r'href="([^"#]+' + tag + r'[^"#]*\.whl)[^"]*"')
    for mirror in MIRRORS:
        try:
            page = urllib.request.urlopen(mirror, timeout=30).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            print(f"  index failed: {mirror} ({exc})", file=sys.stderr)
            continue
        links = pattern.findall(page)
        if links:
            print(f"  index: {mirror}", file=sys.stderr)
            return urllib.parse.urljoin(mirror, links[-1])
    return None


def main() -> int:
    tag = platform_tag()
    print(f"looking for an imageio-ffmpeg wheel ({tag}) ...")
    url = find_wheel_url(tag)
    if not url:
        print("no wheel found; install ffmpeg yourself and set SDGEN_FFMPEG", file=sys.stderr)
        return 1
    name = url.split("/")[-1]
    tmp = VENDOR.with_suffix(".download")
    tmp.mkdir(parents=True, exist_ok=True)
    blob = tmp / name
    print(f"downloading {name} ...", flush=True)
    with urllib.request.urlopen(url, timeout=300) as response, blob.open("wb") as handle:
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
            total += len(chunk)
            print(f"  {total / 1e6:.1f} MB", end="\r", flush=True)
    print(f"\nextracting into {VENDOR} ...")
    VENDOR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(blob) as archive:
        archive.extractall(VENDOR)
    blob.unlink(missing_ok=True)
    try:
        tmp.rmdir()
    except OSError:
        pass
    binaries = list(VENDOR.rglob("ffmpeg*.exe")) + list(VENDOR.rglob("ffmpeg-*"))
    for binary in binaries:
        print(f"ok: {binary} ({binary.stat().st_size / 1e6:.0f} MB)")
    if not binaries:
        print("extracted, but no ffmpeg executable was found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
