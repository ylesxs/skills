#!/usr/bin/env python3
"""sdgen.py - local image + video generation through stable-diffusion.cpp (sd-cli.exe).

Agent-agnostic: plain Python 3.8+, stdlib only, no network, no server. Any agent that
can run a shell command can drive it.

Two model stacks, one entry point:

  gen  Qwen-Image 2.1 UC (Q8, not a Turbo build)  photos, art, reference-guided edits, img2img
  vid  Wan2.2 TI2V-5B                             text-to-video and image-to-video

Everything runs as a one-shot CLI process. Each run writes a log, a sidecar and a
per-session manifest entry (so `history`, `latest` and `@N` keep a refinement chain
that survives turns, isolated per agent session).

Configuration comes from a JSON file or environment variables - nothing machine
specific is baked in (see `info` and reference.md#configuration).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- config
#
# Nothing machine-specific is baked in. Values come from (highest priority first):
#   1. environment variables (WORK_DIR, SD_CLI, QWEN_MODEL, ...)
#   2. a JSON config file: $SDGEN_CONFIG, ./sdgen.config.json, <skill>/config.json
#   3. built-in file NAMES only (model filenames are stable; directories are not)
# Anything still unresolved is reported as "unset" and generation refuses to start.

CONFIG_KEYS = (
    "work_dir", "sd_cli", "rocm_path",
    "qwen_dir", "qwen_model", "qwen_vae", "qwen_llm", "qwen_vision", "lora_dir",
    "wan_dir", "wan_model", "wan_t5xxl", "wan_tae", "wan_vae",
    "out_dir", "ffmpeg",
)
ENV_KEYS = {
    "work_dir": "WORK_DIR", "sd_cli": "SD_CLI", "rocm_path": "ROCM_PATH",
    "qwen_dir": "QWEN_DIR", "qwen_model": "QWEN_MODEL", "qwen_vae": "QWEN_VAE",
    "qwen_llm": "QWEN_LLM", "qwen_vision": "QWEN_VISION", "lora_dir": "LORA_DIR",
    "wan_dir": "WAN_DIR", "wan_model": "MODEL_PATH", "wan_t5xxl": "T5XXL_PATH",
    "wan_tae": "TAE_PATH", "wan_vae": "VAE_PATH",
    "out_dir": "SDGEN_OUT", "ffmpeg": "SDGEN_FFMPEG",
}
SKILL_ROOT = Path(__file__).resolve().parent.parent


def config_candidates() -> list[Path]:
    found = []
    explicit = os.environ.get("SDGEN_CONFIG")
    if explicit:
        found.append(Path(explicit))
    found.append(Path.cwd() / "sdgen.config.json")
    found.append(SKILL_ROOT / "config.json")
    return found


def load_config() -> tuple[dict, Path | None]:
    values: dict = {}
    source = None
    for path in config_candidates():
        data = None
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            values.update({k: str(v) for k, v in data.items()
                           if k in CONFIG_KEYS and v not in (None, "")})
            source = path
            break
    for key, env in ENV_KEYS.items():
        if os.environ.get(env):
            values[key] = os.environ[env]
    return values, source


CFG, CONFIG_PATH = load_config()


def cfg(key: str) -> str | None:
    return CFG.get(key) or None


def p(key: str) -> Path | None:
    value = cfg(key)
    return Path(value).expanduser() if value else None


SD_DIR = p("work_dir")
SD_CLI = cfg("sd_cli") or "sd-cli.exe"
ROCM_PATH = p("rocm_path")

QWEN_DIR = p("qwen_dir")
QWEN_MODEL = p("qwen_model") or (QWEN_DIR / "qwen-image-2.1-UC-Q8_0.gguf" if QWEN_DIR else None)
QWEN_VAE = p("qwen_vae") or (QWEN_DIR / "qwen_image_2.1_vae_bf16.safetensors" if QWEN_DIR else None)
QWEN_LLM = p("qwen_llm") or (QWEN_DIR / "qwen3vl_8b_int8_convrot.safetensors" if QWEN_DIR else None)
QWEN_VISION = p("qwen_vision") or (QWEN_DIR / "mmproj-qwen3vl_8b_heretic-f16.gguf" if QWEN_DIR else None)
LORA_DIR = p("lora_dir") or (QWEN_DIR / "lora" if QWEN_DIR else None)

WAN_DIR = p("wan_dir")
WAN_MODEL = p("wan_model") or (WAN_DIR / "Wan2.2-TI2V-5B-Q8_0.gguf" if WAN_DIR else None)
WAN_T5 = p("wan_t5xxl") or (WAN_DIR / "umt5-xxl-encoder-Q8_0.gguf" if WAN_DIR else None)
WAN_TAE = p("wan_tae") or (WAN_DIR / "taew2_2.pth" if WAN_DIR else None)
WAN_VAE = p("wan_vae") or (WAN_DIR / "Wan2.2_VAE.safetensors" if WAN_DIR else None)

OUT_ROOT = p("out_dir") or Path("sdgen-output")
IMG_DIR = OUT_ROOT / "img"
VID_DIR = OUT_ROOT / "vid"
LOG_DIR = OUT_ROOT / "logs"
SESSIONS_DIR = OUT_ROOT / ".sessions"
LEGACY_MANIFEST = OUT_ROOT / ".sdgen-history.json"

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


# --------------------------------------------------------------------------- helpers


def die(msg: str):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def missing_config(keys: list[str]) -> list[str]:
    return [k for k in keys if not p(k)]


def require_config(keys: list[str], purpose: str) -> None:
    missing = missing_config(keys)
    if not missing:
        return
    where = CONFIG_PATH or "(no config file found)"
    die(f"{purpose} needs {', '.join(missing)}.\n"
        f"  config file: {where}\n"
        f"  set them in sdgen.config.json (copy config.example.json) or via env "
        f"({', '.join(ENV_KEYS[k] for k in missing)}).")


def cli_exe() -> str:
    """Windows CreateProcess does not search the child's cwd - resolve sd-cli.exe ourselves."""
    if os.path.dirname(SD_CLI):
        return SD_CLI
    if SD_DIR is None:
        die("sd.cpp install is not configured: set work_dir / $WORK_DIR to the folder "
            "holding sd-cli.exe")
    candidate = SD_DIR / SD_CLI
    if candidate.is_file():
        return str(candidate)
    die(f"cannot find {SD_CLI} in {SD_DIR} - check work_dir / $WORK_DIR")


def env_for_cli() -> dict:
    env = dict(os.environ)
    extra = [str(SD_DIR)] if SD_DIR else []
    if ROCM_PATH and ROCM_PATH.is_dir():
        extra += [str(ROCM_PATH / "bin"), str(ROCM_PATH / "lib" / "llvm" / "bin")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def snap_frames(n: int) -> int:
    """Wan's VAE compresses time by 4; valid lengths are 4k+1."""
    return max(5, 1 + 4 * round((n - 1) / 4))


def on_grid(value: int, multiple: int = 16) -> int:
    """Round UP to the model's pixel grid (Qwen-Image: 16 px, Wan: 8 px)."""
    return max(multiple, math.ceil(value / multiple) * multiple)


def tail(path: Path, n: int = 25) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join([ln for ln in lines if ln.strip()][-n:])


def run_cli(argv: list[str], timeout: float, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(
                [cli_exe(), *argv], cwd=str(SD_DIR), env=env_for_cli(),
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout,
            )
            return proc.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\nERROR: timed out after {timeout:.0f}s\n")
            return 124
        except FileNotFoundError:
            die(f"cannot execute {SD_CLI!r} - is {SD_DIR} the sd.cpp install?")


def _extract_sdcpp(text: str) -> dict:
    """Pull the `SDCPP: {...}` blob stable-diffusion.cpp writes into image metadata."""
    marker = text.find("SDCPP: ")
    if marker < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, marker + len("SDCPP: "))
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def metadata(path: Path) -> dict:
    """Container + embedded generation record; never decodes pixels."""
    try:
        exe = cli_exe()
    except SystemExit:
        return {}          # sd.cpp not configured: header/sidecar info only
    try:
        proc = subprocess.run(
            [exe, "-M", "metadata", "--image", str(path), "--metadata-format", "json"],
            cwd=str(SD_DIR), env=env_for_cli(), capture_output=True, text=True,
            timeout=180, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    info: dict = {"format": raw.get("format")}
    for entry in raw.get("entries", []):
        name = entry.get("name")
        data = entry.get("data") or {}
        if name == "ICMT" and data.get("text"):            # AVI / video container
            try:
                info.update(json.loads(data["text"]))
            except (json.JSONDecodeError, TypeError):
                pass
        elif name in ("tEXt", "iTXt"):                     # PNG parameters chunk
            text = data.get("text") or ""
            info.update(_extract_sdcpp(text))
            if text:
                info.setdefault("parameters", text)
        elif name == "XMP":                                # WebP / JPEG XMP packet
            xml = html.unescape(data.get("xml") or "")
            info.update(_extract_sdcpp(xml))
    return info


def image_dims(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
                return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                f.seek(12)
                while True:
                    chunk = f.read(8)
                    if len(chunk) < 8:
                        return None
                    fourcc, size = chunk[:4], int.from_bytes(chunk[4:], "little")
                    if fourcc == b"VP8X":
                        data = f.read(10)
                        return (int.from_bytes(data[4:7], "little") + 1,
                                int.from_bytes(data[7:10], "little") + 1)
                    if fourcc == b"VP8 ":
                        data = f.read(10)
                        return (int.from_bytes(data[6:8], "little") & 0x3FFF,
                                int.from_bytes(data[8:10], "little") & 0x3FFF)
                    f.seek(size + (size & 1), 1)
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    marker = f.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(3)
                        return (int.from_bytes(f.read(2), "big"),
                                int.from_bytes(f.read(2), "big"))
                    seg = int.from_bytes(f.read(2), "big")
                    f.seek(seg - 2, 1)
    except OSError:
        return None
    return None


def build_outpaint(src: Path, target_w: int, target_h: int, workdir: Path) -> dict:
    """Canvas + mask for outpainting: the picture is fitted, the new margins are white in the mask."""
    dims = image_dims(src)
    if not dims:
        die(f"cannot read the dimensions of {src}")
    src_w, src_h = dims
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        die("outpainting needs ffmpeg (set SDGEN_FFMPEG or keep the bundled copy in scripts/vendor)")

    scale = min(target_w / src_w, target_h / src_h)
    inner_w = min(target_w, max(16, int(round(src_w * scale / 2)) * 2))
    inner_h = min(target_h, max(16, int(round(src_h * scale / 2)) * 2))
    x = max(0, ((target_w - inner_w) // 2) // 2 * 2)
    y = max(0, ((target_h - inner_h) // 2) // 2 * 2)
    if x == 0 and y == 0:
        die("the source already fills the target size - nothing to outpaint")

    workdir.mkdir(parents=True, exist_ok=True)
    canvas = workdir / "canvas.png"
    mask = workdir / "mask.png"

    def run(argv: list[str]) -> None:
        proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", *argv],
                              capture_output=True, text=True, timeout=600,
                              stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            die(f"ffmpeg failed: {(proc.stderr or '').strip().splitlines()[-1][:300]}")

    run(["-i", str(src), "-vf",
         f"scale={inner_w}:{inner_h},pad={target_w}:{target_h}:{x}:{y}:color=0x808080",
         "-frames:v", "1", str(canvas)])

    boxes = []
    right_w = target_w - x - inner_w
    bottom_h = target_h - y - inner_h
    if x > 0:
        boxes.append(f"drawbox=x=0:y=0:w={x}:h={target_h}:color=white:t=fill")
        boxes.append(f"drawbox=x={x + inner_w}:y=0:w={right_w}:h={target_h}:color=white:t=fill")
    if y > 0:
        boxes.append(f"drawbox=x=0:y=0:w={target_w}:h={y}:color=white:t=fill")
        boxes.append(f"drawbox=x=0:y={y + inner_h}:w={target_w}:h={bottom_h}:color=white:t=fill")
    run(["-f", "lavfi", "-i", f"color=c=black:s={target_w}x{target_h}:d=1",
         "-vf", ",".join(boxes), "-frames:v", "1", str(mask)])

    return {"canvas": canvas, "mask": mask, "inner": (inner_w, inner_h),
            "offset": (x, y), "target": (target_w, target_h)}


def container_of(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "AVI"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "WEBM"
    if head[4:8] == b"ftyp":
        return "MP4"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:2] == b"\xff\xd8":
        return "JPEG"
    return None


def ffmpeg_exe() -> str | None:
    """Locate ffmpeg: $SDGEN_FFMPEG / config, then PATH, then the bundled imageio-ffmpeg."""
    explicit = cfg("ffmpeg")
    if explicit and Path(explicit).is_file():
        return explicit
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    vendor = Path(__file__).resolve().parent / "vendor"
    if vendor.is_dir():
        # imageio-ffmpeg ships ffmpeg-win-x86_64-*.exe / ffmpeg-linux-* / ffmpeg-macos-*
        found = sorted(p for p in vendor.rglob("ffmpeg*")
                       if p.is_file() and not p.name.endswith((".py", ".txt", ".md")))
        if found:
            return str(found[-1])
    return None


def encode_mp4(source: Path, target: Path, metadata: dict | None = None) -> str | None:
    """H.264 mp4 from the sd.cpp AVI (or webm). None on success, else the reason."""
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        return "ffmpeg not found (put it on PATH, set SDGEN_FFMPEG, or install the bundled one)"
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
           # MJPEG/VP9 sources are usually full-range; normalise so every player is happy
           "-vf", "scale=in_range=full:out_range=tv",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-color_range", "tv",
           "-movflags", "+faststart"]
    for key, value in (metadata or {}).items():
        if value is not None and str(value) != "":
            cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(target))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ffmpeg could not run: {exc}"
    if proc.returncode != 0 or not target.is_file():
        return (proc.stderr or "ffmpeg failed").strip().splitlines()[-1][:300]
    return None


def find_outputs(folder: Path, stem: str, started: float) -> list[Path]:
    found = sorted([p for p in folder.glob(f"{stem}*")
                    if p.is_file() and p.suffix.lower() not in (".log", ".json")])
    fresh = [p for p in found if p.stat().st_mtime >= started - 2]
    return fresh or found


# --------------------------------------------------------------------------- manifest
#
# One manifest per agent session, so parallel conversations never see each other's
# `latest` / `@N`. Nothing here is harness-specific: --session wins, then SDGEN_SESSION,
# then whatever session id the surrounding agent exports, then a key derived from the
# working directory (so separate checkouts never share a chain).

_SESSION = "default"
SESSION_ENV_VARS = (
    "SDGEN_SESSION",
    "DSH_SESSION_ID",          # DeepSeek Harness
    "CLAUDE_SESSION_ID",       # Claude Code / Agent SDK
    "CODEX_SESSION_ID",        # Codex CLI
    "GEMINI_SESSION_ID",       # Gemini CLI
    "AGENT_SESSION_ID",        # generic
    "TERM_SESSION_ID",         # plain terminals
)


def session_source() -> str:
    for name in SESSION_ENV_VARS:
        if os.environ.get(name):
            return f"${name}"
    return "cwd-derived (set SDGEN_SESSION to pin one)"


def session_key(explicit: str | None = None) -> str:
    raw = explicit
    if not raw:
        for name in SESSION_ENV_VARS:
            if os.environ.get(name):
                raw = os.environ[name]
                break
    if not raw:
        digest = hashlib.sha1(str(Path.cwd().resolve()).encode("utf-8")).hexdigest()[:10]
        raw = f"cwd-{digest}"
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")[:80]
    return key or "default"


def set_session(explicit: str | None = None) -> str:
    global _SESSION
    _SESSION = session_key(explicit)
    return _SESSION


def manifest_path(session: str | None = None) -> Path:
    return SESSIONS_DIR / f"{session or _SESSION}.json"


def load_manifest(session: str | None = None) -> dict:
    path = manifest_path(session)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    elif LEGACY_MANIFEST.is_file():
        # one-time migration of the pre-session global manifest into this session
        try:
            legacy = json.loads(LEGACY_MANIFEST.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy = None
        if isinstance(legacy, dict) and isinstance(legacy.get("entries"), list):
            save_manifest(legacy, session)
            try:
                LEGACY_MANIFEST.rename(LEGACY_MANIFEST.with_suffix(".legacy.json"))
            except OSError:
                pass
            return legacy
    return {"index": 0, "entries": []}


def save_manifest(data: dict, session: str | None = None) -> None:
    path = manifest_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["entries"] = data["entries"][-500:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def record(entry: dict) -> dict:
    data = load_manifest()
    data["index"] = int(data.get("index", 0)) + 1
    entry["index"] = data["index"]
    entry["session"] = _SESSION
    entry["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["entries"].append(entry)
    save_manifest(data)
    return entry


def entry_files(entry: dict) -> list[Path]:
    return [Path(f) for f in entry.get("files", []) if Path(f).is_file()]


def newest_image() -> Path | None:
    """Newest image of *this* session only - sessions must not bleed into each other."""
    data = load_manifest()
    for entry in reversed(data["entries"]):
        if entry.get("kind") != "image":
            continue
        files = entry_files(entry)
        if files:
            return files[-1]
    return None


def resolve_source(token: str | None, what: str) -> Path | None:
    if not token:
        return None
    if token in ("latest", "last", "@last", "prev"):
        found = newest_image()
        if not found:
            die(f"{what} latest: nothing in this session's history yet (session {_SESSION}) - "
                f"pass an explicit path, or continue another session with --session NAME")
        return found
    if re.fullmatch(r"@\d+", token):
        wanted = int(token[1:])
        data = load_manifest()
        for entry in data["entries"]:
            if entry.get("index") == wanted:
                files = entry_files(entry)
                if not files:
                    die(f"{what} {token}: entry {wanted} has no file on disk any more")
                if entry.get("kind") != "image":
                    die(f"{what} {token}: entry {wanted} is a {entry.get('kind')}, not an image")
                return files[-1]
        die(f"{what} {token}: no history entry {wanted} (see `sdgen.py history`)")
    p = Path(token).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        die(f"{what}: input image not found: {p}")
    return p


# --------------------------------------------------------------------------- lora


def lora_files() -> list[Path]:
    if LORA_DIR is None or not LORA_DIR.is_dir():
        return []
    return sorted(LORA_DIR.glob("*.safetensors"))


def build_lora_tags(specs: list[str], prompt: str) -> tuple[str, list[dict]]:
    applied: list[dict] = []
    available = {p.name for p in lora_files()}
    stems = {p.stem: p.name for p in lora_files()}
    for spec in specs:
        name, _, strength = spec.partition(":")
        name = name.strip()
        weight = float(strength) if strength else 1.0
        if name in available:
            filename = name
        elif name in stems:
            filename = stems[name]
        elif f"{name}.safetensors" in available:
            filename = f"{name}.safetensors"
        else:
            die(f"unknown LoRA {name!r}; available: {', '.join(sorted(stems)) or '(none)'}")
        prompt = f"{prompt} <lora:{Path(filename).stem}:{weight:g}>"
        applied.append({"name": Path(filename).stem, "file": filename, "weight": weight})
    return prompt, applied


# --------------------------------------------------------------------------- image


def cmd_gen(args) -> int:
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        die("an empty prompt renders nothing useful - pass a prompt")
    require_config(["work_dir", "qwen_dir"], "image generation")

    prompt, loras = build_lora_tags(args.lora, prompt)

    width, height = args.width, args.height
    if args.size:
        try:
            width, height = (int(v) for v in args.size.lower().split("x"))
        except ValueError:
            die(f"--size wants WIDTHxHEIGHT, got {args.size!r}")
    if args.outpaint:
        try:
            width, height = (int(v) for v in args.outpaint.lower().split("x"))
        except ValueError:
            die(f"--outpaint wants WIDTHxHEIGHT, got {args.outpaint!r}")
    requested = (width, height)
    # Qwen-Image's DiT patch + VAE put the grid on 16 px; sd.cpp silently rounds otherwise.
    width, height = on_grid(width, 16), on_grid(height, 16)
    if (width, height) != requested and not args.json:
        print(f"note: size snapped {requested[0]}x{requested[1]} -> {width}x{height} "
              f"(Qwen-Image aligns to 16 px)", flush=True)

    refs = [resolve_source(t, "--ref") for t in args.ref]
    init = resolve_source(args.init, "--init")
    mask = resolve_source(args.mask, "--mask")
    control = resolve_source(args.control, "--control")
    ip_image = resolve_source(args.ip_adapter, "--ip-adapter")

    seed = args.seed if args.seed is not None and args.seed >= 0 else random.randint(1, 2**31 - 1)
    ext = "jpeg" if args.format.lower() in ("jpg", "jpeg") else args.format.lower()
    stem = f"{args.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    target = (IMG_DIR / f"{stem}.{ext}").resolve()

    outpaint_info = None
    source_image = init
    if args.outpaint:
        if not init:
            die("--outpaint needs --init IMAGE (the picture whose frame gets extended)")
        outpaint_info = build_outpaint(init, width, height, OUT_ROOT / ".work" / stem)
        init = outpaint_info["canvas"]
        mask = outpaint_info["mask"]

    strength = args.strength if args.strength is not None else (1.0 if outpaint_info else 0.75)

    argv = [
        "-M", "img_gen",
        "--diffusion-model", str(Path(args.model) if args.model else QWEN_MODEL),
        "--vae", str(QWEN_VAE),
        "--llm", str(QWEN_LLM),
        "--llm_vision", str(QWEN_VISION),
        "--lora-model-dir", str(LORA_DIR),
        "--steps", str(args.steps),
        "--cfg-scale", str(args.cfg),
        "--sampling-method", args.sampler,
        "--diffusion-fa",
        "--vae-tiling",
        "-W", str(width), "-H", str(height),
        "-b", str(args.batch),
        "-s", str(seed),
        "-p", prompt,
        "-o", str(target),
    ]
    if args.params_backend and args.params_backend.lower() not in ("memory", "none", "default", ""):
        argv += ["--params-backend", args.params_backend]
    if args.scheduler:
        argv += ["--scheduler", args.scheduler]
    if args.flow_shift is not None:
        argv += ["--flow-shift", str(args.flow_shift)]
    if args.negative:
        argv += ["-n", args.negative]
    if args.quality and ext in ("jpeg", "webp"):
        argv += ["--compression-quality", str(args.quality)]
    for ref in refs:
        argv += ["-r", str(ref.resolve())]
    if init:
        argv += ["-i", str(init.resolve()), "--strength", str(strength)]
    if mask:
        argv += ["--mask", str(mask.resolve())]
    if control:
        argv += ["--control-image", str(control.resolve())]
    if ip_image:
        argv += ["--ip-adapter-image", str(ip_image.resolve())]
    if args.low_vram:
        argv += ["--offload-to-cpu"]
    if args.extra:
        argv += args.extra.split()
    argv += list(args.extra_args or [])

    log_path = LOG_DIR / f"{stem}.log"
    started = time.time()
    mode = "outpaint" if outpaint_info else ("ref-edit" if refs else
                                             ("img2img" if init else "txt2img"))
    if not args.json:
        print(f"generating {mode} {width}x{height} batch {args.batch}, {args.steps} steps, "
              f"cfg {args.cfg}, seed {seed} -> {target.name}", flush=True)
        if outpaint_info:
            print(f"outpaint: source {source_image} fitted to {outpaint_info['inner'][0]}x"
                  f"{outpaint_info['inner'][1]} at offset {outpaint_info['offset']}, "
                  f"strength {strength}", flush=True)
        for label, paths in (("ref", refs), ("init", [source_image] if source_image else [])):
            for path in paths:
                print(f"{label} image: {path}", flush=True)
        print(f"sd-cli log: {log_path}", flush=True)

    rc = run_cli(argv, args.timeout, log_path)
    elapsed = time.time() - started
    files = find_outputs(IMG_DIR, stem, started)
    ok = rc == 0 and bool(files)

    actual = image_dims(files[0]) if files else None
    result = {
        "ok": ok,
        "kind": "image",
        "exit_code": rc,
        "elapsed_seconds": round(elapsed, 1),
        "files": [str(p) for p in files],
        "file": str(files[0]) if files else None,
        "requested_size": f"{requested[0]}x{requested[1]}",
        "width": actual[0] if actual else width,
        "height": actual[1] if actual else height,
        "batch": args.batch,
        "steps": args.steps, "cfg": args.cfg, "seed": seed,
        "sampler": args.sampler, "scheduler": args.scheduler,
        "flow_shift": args.flow_shift,
        "strength": strength if (init or outpaint_info) else None,
        "mode": mode,
        "prompt": prompt, "negative": args.negative,
        "loras": loras,
        "parents": [str(p) for p in refs] + ([str(source_image)] if source_image else []),
        "container": container_of(files[0]) if files else None,
        "log": str(log_path.resolve()),
    }
    if actual and actual != (width, height):
        result["size_note"] = f"the model produced {actual[0]}x{actual[1]} (grid rounding)"
    if outpaint_info:
        result["outpaint"] = {"source": str(source_image), "inner": outpaint_info["inner"],
                              "offset": outpaint_info["offset"],
                              "target": outpaint_info["target"]}

    if ok:
        if outpaint_info:
            shutil.rmtree(OUT_ROOT / ".work" / stem, ignore_errors=True)
        record({k: result[k] for k in ("kind", "mode", "files", "width", "height", "steps",
                                       "seed", "prompt", "negative", "loras", "parents",
                                       "flow_shift", "elapsed_seconds")})
        try:
            files[0].with_suffix(".json").write_text(json.dumps(result, indent=2),
                                                     encoding="utf-8")
        except OSError:
            pass
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"done in {elapsed:.0f}s -> {result['file']}")
            for extra in result["files"][1:]:
                print(f"           + {extra}")
            if result.get("size_note"):
                print(result["size_note"])
        return 0

    if args.json:
        result["log_tail"] = tail(log_path)
        print(json.dumps(result, indent=2))
    else:
        print(f"generation failed (exit {rc}) after {elapsed:.0f}s", file=sys.stderr)
        print(tail(log_path), file=sys.stderr)
        print(f"full log: {log_path}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- video


def cmd_vid(args) -> int:
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        die("an empty prompt generates noise - pass a prompt")
    require_config(["work_dir", "wan_dir"], "video generation")
    if args.lora:
        prompt, _ = build_lora_tags(args.lora, prompt)

    width, height = args.width, args.height
    if args.size:
        try:
            width, height = (int(v) for v in args.size.lower().split("x"))
        except ValueError:
            die(f"--size wants WIDTHxHEIGHT, got {args.size!r}")
    width, height = on_grid(width), on_grid(height)

    frames = snap_frames(args.frames)
    seed = args.seed if args.seed is not None and args.seed >= 0 else random.randint(1, 2**31 - 1)
    init = resolve_source(args.image, "--image")
    end = resolve_source(args.end_image, "--end-image")

    # sd.cpp can only write webm / avi / animated webp. A "final" mp4 is produced by
    # letting it write a fast AVI and remuxing/encoding once with ffmpeg.
    requested = (args.format.lower().lstrip(".") if args.format
                 else ("avi" if args.draft else "mp4"))
    if requested not in ("mp4", "avi", "webm", "webp"):
        die(f"unsupported video format {requested!r}; use mp4, avi, webm or webp")
    native_ext = "avi" if requested == "mp4" else requested
    stem = f"{args.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    VID_DIR.mkdir(parents=True, exist_ok=True)
    target = (VID_DIR / f"{stem}.{native_ext}").resolve()

    decoder = ["--tae", str(WAN_TAE)] if args.decoder == "tae" else ["--vae", str(WAN_VAE)]
    argv = [
        "-M", "vid_gen",
        "--diffusion-model", str(Path(args.model) if args.model else WAN_MODEL),
        "--t5xxl", str(WAN_T5),
        *decoder,
        "--cfg-scale", str(args.cfg),
        "--steps", str(args.steps),
        "-W", str(width), "-H", str(height),
        "--video-frames", str(frames),
        "--fps", str(args.fps),
        "--flow-shift", str(args.flow_shift),
        "-s", str(seed),
        "--diffusion-fa",
        "--vae-conv-direct",
        "-p", prompt,
        "-o", str(target),
    ]
    if args.sampler:
        argv += ["--sampling-method", args.sampler]
    if args.scheduler:
        argv += ["--scheduler", args.scheduler]
    if args.negative:
        argv += ["-n", args.negative]
    if init:
        argv += ["-i", str(init.resolve())]
    if end:
        argv += ["--end-img", str(end.resolve())]
    if args.low_vram:
        argv += ["--offload-to-cpu", "--vae-tiling", "--temporal-tiling"]
    if args.extra:
        argv += args.extra.split()
    argv += list(args.extra_args or [])

    log_path = LOG_DIR / f"{stem}.log"
    started = time.time()
    mode = "i2v" if init else "t2v"
    if not args.json:
        print(f"generating {mode} {width}x{height} {frames}f @{args.fps}fps, {args.steps} steps, "
              f"cfg {args.cfg}, seed {seed} -> {target.name}"
              f"{'  (then mp4)' if requested == 'mp4' else ''}", flush=True)
        if frames != args.frames:
            print(f"note: frames snapped {args.frames} -> {frames} (Wan needs 4k+1)", flush=True)
        if init:
            print(f"init image: {init}", flush=True)
        print(f"sd-cli log: {log_path}", flush=True)

    rc = run_cli(argv, args.timeout, log_path)
    elapsed = time.time() - started
    files = find_outputs(VID_DIR, stem, started)
    ok = rc == 0 and bool(files)
    found = files[0] if files else None
    container_note = None

    if ok and requested == "mp4" and found is not None:
        mp4 = (VID_DIR / f"{stem}.mp4").resolve()
        note = (f"stable-diffusion.cpp | prompt: {prompt} | seed: {seed} | steps: {args.steps} | "
                f"cfg: {args.cfg} | flow-shift: {args.flow_shift} | "
                f"{width}x{height} {frames}f @{args.fps}fps")
        error = encode_mp4(found, mp4, {"title": prompt[:200], "comment": note})
        if error is None:
            try:
                found.unlink()
            except OSError:
                pass
            files = [mp4] + [p for p in files if p != found]
            found = mp4
            elapsed = time.time() - started
        else:
            container_note = (f"mp4 encode unavailable ({error}); kept the native {found.suffix[1:].upper()}. "
                              f"Set SDGEN_FFMPEG to an ffmpeg.exe to get mp4.")

    result = {
        "ok": ok,
        "kind": "video",
        "exit_code": rc,
        "elapsed_seconds": round(elapsed, 1),
        "files": [str(p) for p in files],
        "file": str(found) if found else None,
        "width": width, "height": height, "frames": frames, "fps": args.fps,
        "seconds": round(frames / args.fps, 2),
        "steps": args.steps, "cfg": args.cfg, "seed": seed,
        "sampler": args.sampler or "euler", "scheduler": args.scheduler,
        "flow_shift": args.flow_shift,
        "mode": mode,
        "requested_format": requested,
        "draft": bool(args.draft) and not args.format,
        "prompt": prompt, "negative": args.negative,
        "parents": [str(init)] if init else [],
        "container": container_of(found) if found else None,
        "log": str(log_path.resolve()),
    }
    if container_note:
        result["container_note"] = container_note

    if ok:
        record({k: result[k] for k in ("kind", "mode", "files", "width", "height", "frames",
                                       "fps", "steps", "seed", "prompt", "negative", "parents",
                                       "flow_shift", "elapsed_seconds")})
        try:
            found.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"done in {elapsed:.0f}s -> {result['file']} "
                  f"({frames}f @{args.fps}fps = {result['seconds']}s)")
            if result.get("container_note"):
                print(result["container_note"], file=sys.stderr)
        return 0

    if args.json:
        result["log_tail"] = tail(log_path)
        print(json.dumps(result, indent=2))
    else:
        print(f"generation failed (exit {rc}) after {elapsed:.0f}s", file=sys.stderr)
        print(tail(log_path), file=sys.stderr)
        print(f"full log: {log_path}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- inspect


def cmd_check(args) -> int:
    failed = 0
    report = []
    for token in args.files:
        p = Path(token)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            print(f"missing: {p}", file=sys.stderr)
            failed += 1
            report.append({"file": str(p), "exists": False})
            continue

        info = metadata(p)
        if not info.get("format"):
            info["format"] = container_of(p)
        source = "embedded metadata" if info.get("width") else None

        sidecar = p.with_suffix(".json")
        if not info.get("width") and sidecar.is_file():
            try:
                side = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                side = {}
            if side:
                source = "sidecar"
                info.update({k: side.get(k) for k in
                             ("width", "height", "frames", "fps", "seed", "prompt", "steps",
                              "cfg", "sampler", "scheduler", "flow_shift", "container")})
        if not info.get("width") and p.suffix.lower() in IMAGE_EXT:
            dims = image_dims(p)
            if dims:
                info["width"], info["height"] = dims
                source = source or "file header"

        frames = info.get("video_frames") or info.get("frames")
        fps = info.get("fps")
        sample = info.get("sample_params") or {}
        kind = "video" if (frames and frames > 1) or p.suffix.lower() in \
            (".webm", ".avi", ".mp4", ".mov", ".webp") else "image"
        loras = [lo.get("path") for lo in (info.get("loras") or [])]
        entry = {
            "file": str(p.resolve()),
            "exists": True,
            "kind": kind,
            "bytes": p.stat().st_size,
            "format": info.get("format"),
            "width": info.get("width"),
            "height": info.get("height"),
            "frames": frames if kind == "video" else None,
            "fps": fps if kind == "video" else None,
            "seconds": round(frames / fps, 2) if kind == "video" and frames and fps else None,
            "seed": info.get("seed"),
            "prompt": info.get("prompt"),
            "steps": sample.get("sample_steps") or info.get("steps"),
            "sampler": sample.get("sample_method") or info.get("sampler"),
            "scheduler": sample.get("scheduler") or info.get("scheduler"),
            "flow_shift": (sample.get("flow_shift") if sample.get("flow_shift") is not None
                           else info.get("flow_shift")),
            "loras": loras,
            "source": source,
        }
        # Attaching a generated file to the conversation is what kills turns with
        # 400 "Failed to load image or audio file". Record the budget so the caller can see it.
        size = entry["bytes"]
        entry["attach_ok"] = bool(entry["width"] and entry["height"]
                                  and entry["width"] <= 2048 and entry["height"] <= 2048
                                  and size <= 4 * 1024 * 1024)
        report.append(entry)

        if not args.json:
            dims = f"{entry['width']}x{entry['height']}" if entry["width"] else "size ?"
            detail = (f"{entry['frames']}f @{entry['fps']}fps (~{entry['seconds']}s)"
                      if entry["kind"] == "video" else "still")
            print(f"{entry['file']}\n  {entry['format'] or '?'} {dims} {detail} "
                  f"{entry['bytes'] / 1e6:.2f}MB"
                  f"{'' if source else '  [container only; no metadata or sidecar]'}")
            if entry["prompt"]:
                print(f"  prompt {entry['prompt']!r}")
            extra = f" loras={','.join(filter(None, loras))}" if loras else ""
            shift = (f" flow-shift={entry['flow_shift']}"
                     if entry.get("flow_shift") is not None else "")
            print(f"  seed={entry['seed']} {entry['sampler'] or '?'}/"
                  f"{entry['scheduler'] or 'default'} {entry['steps'] or '?'} steps{shift}{extra}")
            print("  do NOT read_image this file - report the path instead"
                  + ("" if entry["attach_ok"] else
                     f" (and it is over the attachment budget: >4 MB or >2048 px, so an "
                     f"attachment would be re-encoded and rejected with HTTP 400)"))
    if args.json:
        print(json.dumps(report if len(report) != 1 else report[0], indent=2))
    return 1 if failed else 0


def cmd_history(args) -> int:
    if getattr(args, "all", False):
        rows = []
        for path in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for entry in data.get("entries", []):
                entry.setdefault("session", path.stem)
                rows.append(entry)
        rows.sort(key=lambda e: e.get("time") or "")
        entries = rows[-args.limit:][::-1]
        label = "all sessions"
    else:
        entries = load_manifest()[ "entries"][-args.limit:][::-1]
        label = _SESSION

    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return 0
    if not entries:
        print(f"no generations recorded yet (session {label})")
        return 0
    for entry in entries:
        files = entry.get("files") or []
        missing = [f for f in files if not Path(f).is_file()]
        size = f"{entry.get('width')}x{entry.get('height')}"
        if entry.get("frames"):
            size += f" {entry['frames']}f"
        prompt = (entry.get("prompt") or "").replace("\n", " ")
        if len(prompt) > 70:
            prompt = prompt[:67] + "..."
        session = entry.get("session")
        tag = f"  [{session}]" if getattr(args, "all", False) else ""
        print(f"@{entry.get('index')}  {entry.get('time')}  {entry.get('kind')}/"
              f"{entry.get('mode')}  {size}  seed={entry.get('seed')}"
              f"{'  [file missing]' if missing else ''}{tag}")
        print(f"     {prompt}")
        for f in files:
            print(f"     {f}")
    return 0


def cmd_session(args) -> int:
    data = load_manifest()
    name = _SESSION
    if args.json:
        print(json.dumps({
            "session": name,
            "manifest": str(manifest_path().resolve()),
            "entries": len(data.get("entries", [])),
            "index": data.get("index", 0),
            "source": "--session" if args.session else session_source(),
            "other_sessions": {p.stem: len(json.loads(p.read_text(encoding="utf-8"))
                                          .get("entries", []))
                               for p in sorted(SESSIONS_DIR.glob("*.json"))
                               if p.stem != name} if SESSIONS_DIR.is_dir() else {},
        }, indent=2))
        return 0
    print(f"session     {name}   (from {session_source()})")
    print(f"manifest    {manifest_path().resolve()}  ({len(data.get('entries', []))} entries)")
    if SESSIONS_DIR.is_dir():
        others = [(p.stem, len(json.loads(p.read_text(encoding="utf-8")).get("entries", [])))
                  for p in sorted(SESSIONS_DIR.glob("*.json")) if p.stem != name]
        if others:
            print("other sessions:")
            for stem, count in others:
                print(f"  {stem}  ({count} entries)")
    print("\n`latest` / `@N` resolve inside this session only. "
          "Use --session NAME on any command to look at another one, "
          "or set SDGEN_SESSION to pin the bucket explicitly.")
    return 0


def cmd_use(args) -> int:
    p = Path(args.file)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        die(f"file not found: {p}")
    dims = image_dims(p) or (None, None)
    entry = record({
        "kind": "image", "mode": "import", "files": [str(p.resolve())],
        "width": dims[0], "height": dims[1], "steps": None, "seed": None,
        "prompt": args.note or "", "negative": "", "loras": [], "parents": [],
        "elapsed_seconds": None,
    })
    if args.json:
        print(json.dumps(entry, indent=2))
    else:
        print(f"registered @{entry['index']}: {p.resolve()}")
    return 0


def cmd_info(args) -> int:
    exe = Path(SD_CLI) if os.path.dirname(SD_CLI) else (SD_DIR / SD_CLI if SD_DIR else None)
    rows = [
        ("sd-cli", exe),
        ("qwen model", QWEN_MODEL), ("qwen vae", QWEN_VAE),
        ("qwen llm", QWEN_LLM), ("qwen vision", QWEN_VISION), ("lora dir", LORA_DIR),
        ("wan model", WAN_MODEL), ("wan t5xxl", WAN_T5),
        ("wan tae", WAN_TAE), ("wan vae", WAN_VAE),
    ]
    ok = True
    for label, path in rows:
        if path is None:
            ok = False
            print(f"{label:<12} UNSET   (set it in the config file or env)")
            continue
        exists = path.is_file() or (label.endswith("dir") and path.is_dir())
        ok = ok and exists
        print(f"{label:<12} {'ok ' if exists else 'MISSING'} {path}")
    if ROCM_PATH is None:
        print(f"{'rocm':<12} unset   (optional; only needed for a shared ROCm install)")
    else:
        print(f"{'rocm':<12} {'ok ' if ROCM_PATH.is_dir() else 'n/a (self-contained install)'} "
              f"{ROCM_PATH}")
    print(f"{'config':<12} {CONFIG_PATH or '(none found - using env only)'}")
    print(f"{'output':<12} {OUT_ROOT.resolve()}")
    print(f"{'session':<12} {_SESSION}  ({len(load_manifest().get('entries', []))} entries, "
          f"{manifest_path().resolve()})")
    loras = [path.stem for path in lora_files()]
    print(f"{'loras':<12} {', '.join(loras) if loras else '(none)'}")
    print("\nimage defaults: 1024x1024, 16 steps, cfg 1.0, sampler euler, scheduler flux, "
          "flow-shift auto, params-backend disk, vae-tiling, png\n"
          "video defaults: 832x480, 20 steps, cfg 5.0, flow-shift 3.0, 72->73 frames @24fps, "
          "tae decoder, mp4 final / avi draft")
    if CONFIG_PATH is None:
        print("\nno config file found: copy config.example.json to sdgen.config.json "
              "(cwd) or <skill>/config.json and fill in your paths")
    return 0 if ok else 1


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sdgen.py",
        description="Local image (Qwen-Image 2.1) and video (Wan2.2 TI2V-5B) generation "
                    "through stable-diffusion.cpp sd-cli.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common_out(sp):
        sp.add_argument("--name", default="sd", help="filename stem prefix (default sd)")
        sp.add_argument("--json", action="store_true", help="machine-readable result")
        sp.add_argument("--session", help="history bucket to use "
                                          "(default: --session > $SDGEN_SESSION > the agent's own "
                                          "session env var > cwd-derived key)")

    def common_run(sp):
        sp.add_argument("--timeout", type=float, default=3600, help="seconds before giving up")
        sp.add_argument("--extra", help="extra sd-cli flags as one string")
        sp.add_argument("--extra-args", nargs=argparse.REMAINDER,
                        help="extra sd-cli flags, verbatim")

    g = sub.add_parser("gen", help="generate or edit an image (Qwen-Image 2.1)")
    g.add_argument("prompt", help="what to draw / what to change")
    g.add_argument("--prompt-file", help="read the prompt from a UTF-8 file")
    g.add_argument("--ref", action="append", default=[],
                   help="reference image for an edit (repeatable; path, 'latest' or @N)")
    g.add_argument("--init", help="img2img starting image (path, 'latest' or @N)")
    g.add_argument("--outpaint", metavar="WxH",
                   help="extend the --init picture to a new frame (canvas + mask, e.g. 1920x1088)")
    g.add_argument("--strength", type=float, default=None,
                   help="img2img denoise strength (default 0.75; outpainting defaults to 1.0)")
    g.add_argument("--mask", help="inpaint mask")
    g.add_argument("--control", help="ControlNet conditioning image")
    g.add_argument("--ip-adapter", help="IP-Adapter identity/style image")
    g.add_argument("--size", help="WIDTHxHEIGHT, e.g. 1024x1024, 1328x1328, 512x512")
    g.add_argument("--width", type=int, default=1024)
    g.add_argument("--height", type=int, default=1024)
    g.add_argument("--batch", type=int, default=1)
    g.add_argument("--steps", type=int, default=16,
                   help="default 16 (= what the earlier setup produced; --steps 25 = start.bat "
                        "launch value, 8 = fast draft)")
    g.add_argument("--cfg", type=float, default=1.0)
    g.add_argument("--sampler", default="euler")
    g.add_argument("--scheduler", help="model default is 'flux' for this checkpoint")
    g.add_argument("--flow-shift", type=float, default=None,
                   help="flow-matching shift for Qwen-Image (default: model 'auto'; try 1.0-3.0)")
    g.add_argument("--seed", type=int, default=-1, help="-1 picks a random seed and reports it")
    g.add_argument("--negative", default="")
    g.add_argument("--lora", action="append", default=[], help="NAME[:WEIGHT], repeatable")
    g.add_argument("--format", default="png", help="png|webp|jpeg (default png)")
    g.add_argument("--quality", type=int, default=95, help="jpeg/webp compression quality")
    g.add_argument("--params-backend", default="disk",
                   help="where weights live: disk (start.bat default) | memory | cpu")
    g.add_argument("--model", help="override the diffusion model path")
    g.add_argument("--low-vram", action="store_true", help="offload weights to CPU")
    common_out(g)
    common_run(g)
    g.set_defaults(func=cmd_gen)

    v = sub.add_parser("vid", help="generate a video (Wan2.2 TI2V-5B)")
    v.add_argument("prompt", help="English description of the shot")
    v.add_argument("--prompt-file", help="read the prompt from a UTF-8 file")
    v.add_argument("--image", help="first-frame image for i2v (path, 'latest' or @N)")
    v.add_argument("--end-image", help="last frame image")
    v.add_argument("--size", help="WIDTHxHEIGHT, e.g. 832x480 or 1280x704")
    v.add_argument("--width", type=int, default=832)
    v.add_argument("--height", type=int, default=480)
    v.add_argument("--frames", type=int, default=72, help="length in frames, snapped to 4k+1")
    v.add_argument("--fps", type=int, default=24)
    v.add_argument("--steps", type=int, default=20)
    v.add_argument("--cfg", type=float, default=5.0)
    v.add_argument("--flow-shift", type=float, default=3.0)
    v.add_argument("--sampler", help="default: euler (model default for Wan)")
    v.add_argument("--scheduler", help="default: model default ('discrete'); never 'lcm'")
    v.add_argument("--seed", type=int, default=-1)
    v.add_argument("--negative", default="")
    v.add_argument("--lora", action="append", default=[], help="NAME[:WEIGHT], repeatable")
    v.add_argument("--decoder", choices=("tae", "vae"), default="tae")
    v.add_argument("--model", help="override the diffusion model path")
    v.add_argument("--format", default=None,
                   help="mp4 (default, final), avi (default with --draft), webm, webp")
    v.add_argument("--draft", action="store_true",
                   help="quick preview: writes .avi natively instead of encoding an mp4")
    v.add_argument("--low-vram", action="store_true", help="offload + tile the VAE")
    common_out(v)
    common_run(v)
    v.set_defaults(func=cmd_vid)

    c = sub.add_parser("check", help="inspect images and videos without attaching them")
    c.add_argument("files", nargs="+")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    h = sub.add_parser("history", help="recent generations of this session "
                                       "(latest / @N as edit sources)")
    h.add_argument("--limit", type=int, default=10)
    h.add_argument("--all", action="store_true", help="merge every session, newest last")
    h.add_argument("--session", help="read another session's history")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_history)

    s = sub.add_parser("session", help="which history bucket this conversation writes to")
    s.add_argument("--session", help="inspect another session id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_session)

    u = sub.add_parser("use", help="register an existing image as an edit source")
    u.add_argument("file")
    u.add_argument("--note", help="what it is")
    u.add_argument("--session", help="register it into another session's history")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_use)

    i = sub.add_parser("info", help="model stacks, defaults, available LoRAs")
    i.add_argument("--session", help="inspect another session id")
    i.set_defaults(func=cmd_info)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_session(getattr(args, "session", None))
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
