---
name: stable-diffusion-cpp
description: Generate or edit images and videos with the local stable-diffusion.cpp CLI (Qwen-Image 2.1 + Wan2.2 TI2V-5B). Use when asked to 画图、生成图片、出图、文生图、改图/编辑图片、生成视频、文生视频、图生视频、让这张图动起来, or to "generate / edit an image", "make a video / clip", "animate this picture" locally, or to inspect a produced file (size, frames, seed, prompt).
whenToUse: The user wants a picture or a short clip produced locally, wants an existing picture edited or animated, or wants a finished file inspected. One script drives both models through sd-cli.exe. Works with any agent runtime (DSH, Claude Code, Codex, Cursor, custom loops).
---

# Local image + video generation (sd.cpp CLI)

One entry point, two model stacks:

| Command | Model | What it does |
|---|---|---|
| `gen` | Qwen-Image 2.1 UC (Q8_0) | 文生图, reference-guided edits, img2img, LoRA |
| `vid` | Wan2.2 TI2V-5B | 文生视频 / 图生视频 |

Both run **`sd-cli.exe` one shot per request** and write timestamped files under `sdgen-output/`.
The two `start.bat` files stay as the reference for model paths and flags; the CLI wrapper mirrors them.

**Install (any agent):** copy this folder into your skills directory — `.dsh/skills/`,
`~/.agents/skills/`, `.claude/skills/`, or simply keep it in the repo and point the agent at
`<skill>/SKILL.md`. Requires Python 3.8+ and a local stable-diffusion.cpp build; the repository
README covers dependencies and per-agent wiring.


## Setup (once per machine)

No path is hardcoded. Values come from `sdgen.config.json` in the working directory (or
`<skill>/config.json`, or `$SDGEN_CONFIG`), with environment variables overriding them:

```powershell
Copy-Item <skill>/config.example.json sdgen.config.json
# edit work_dir (folder with sd-cli.exe), qwen_dir, wan_dir; rocm_path is optional
python <skill>/scripts/sdgen.py info
```

`info` prints `UNSET` for anything missing and `gen`/`vid` refuse to start until it is set, naming the
exact keys. `config.example.json` documents every key. See
[reference.md](reference.md#configuration) for the full key → env-var table.

## ⚠️ Do not attach a generated file to the conversation

Reading a produced PNG/JPEG/WebP back into the model context is the most common way this skill loses
a turn. Many harnesses and gateways reject image tool results, and the failure arrives *after* the
file was written:

```
400 invalid_request_error: Failed to load image or audio file
```

Concrete case: DeepSeek Harness passes an attachment through untouched only while it is **≤ 4 MB and
≤ 2048 px** on both sides; outside that it re-encodes to WebP and the gateway refuses it. Other
clients simply do not accept image parts at all.

So: **report the path, do not look at the file.** `check` gives you every fact you need (size, seed,
steps, prompt, `attach_ok`). If a visual judgement is genuinely required, ask the user to look — a
doomed attachment costs the whole turn.

`check` prints `do NOT read_image this file` for every file, and adds the budget explanation when
`attach_ok` is false.

## Images

```powershell
# text to image
python <skill>/scripts/sdgen.py gen "a red apple on a wooden table, window light"

# edit: describe ONLY the change, starting from an existing image
python <skill>/scripts/sdgen.py gen "make the apple bright blue, keep table and light" --ref latest

# img2img when the structure must stay exact
python <skill>/scripts/sdgen.py gen "same scene, golden hour" --init latest --strength 0.4

# outpaint / 扩图: keep the whole picture and let the model fill a wider frame
python <skill>/scripts/sdgen.py gen "the same room continued to the sides, same light" --init "C:/path/photo.png" --outpaint 1536x864

# LoRA by name (repeatable), weight after the colon
python <skill>/scripts/sdgen.py gen "portrait of a woman" --lora qwen-image-anime-nsfw:0.7
```

- `--ref` is the good default for visible edits (Qwen-Image reference editing keeps pose, framing and
  background). `--init --strength 0.3-0.5` is for near-identical retouches.
- **`--ref`/`--init` do not widen a picture** — they crop it to the target aspect. To keep the whole
  photo and grow the frame, use `--outpaint WxH --init IMAGE` (canvas + mask are built for you, the
  original stays intact in the middle, only the new margins are generated; strength defaults to 1.0).
- **Sizes sit on a 16 px grid** (Qwen-Image's patch × VAE). `--size 1920x1080` becomes **1920x1088**;
  exact 16:9 sizes are **1024x576, 1536x864, 2048x1152**. The snapped value is printed up front and the
  real file dimensions are reported afterwards, so never promise 1920x1080.
- Defaults reproduce the earlier setup's parameters: **1024x1024, 16 steps, cfg 1.0, sampler euler,
  scheduler flux, vae-tiling, params-backend disk**. Verified: the same prompt + seed + size + steps
  through this CLI is **byte-identical** to the image that setup produced. `--steps 25` = the
  `start.bat` launch value; `--steps 8 --size 512x512` = ~12 s draft (do not judge quality from it).
- This checkpoint is the plain **Qwen-Image 2.1 (UC / uncensored) Q8**, *not* a Turbo build, and it is
  driven at **cfg 1.0** exactly as `start.bat` does — leave cfg alone. `--sampler`/`--scheduler`
  defaults already match the model (`euler` / `flux`); changing them diverges from the baseline.
- `--flow-shift N` — the flow-matching sigma shift. Omitted = the model's `auto`; passing a value
  changes the picture at the same seed (verified), so it is a deliberate look knob, not a free win.
  The value is stored in the file and shown by `check`.
- `--size` must be multiples of 8 (1024x1024, 1328x1328, 512x512, 768x1024 …). `--batch N` explores.
- `--format png|webp|jpeg` (+ `--quality`). PNG is the default and carries full metadata.

## Videos

```powershell
# final: mp4 (H.264, faststart) — sd.cpp writes a fast AVI, then one ffmpeg pass encodes it
python <skill>/scripts/sdgen.py vid "a red fox trotting through fresh snow, slow camera push-in, soft morning light"

# draft / quick preview: avi, written natively by sd.cpp (no encode step, ~1 s faster, bigger file)
python <skill>/scripts/sdgen.py vid "<shot>" --frames 21 --steps 8 --draft

# image to video: the picture is the first frame, the prompt describes ONLY the motion
python <skill>/scripts/sdgen.py vid "she slowly turns her head and smiles" --image latest
```

- **English** prompts. One line of content + one of motion/camera; present continuous verbs; one shot.
- **Container defaults**: `mp4` for a finished clip, `avi` when `--draft` is passed (that is the
  "intermediate / preview" mode — say nothing about mp4 when drafting). `--format avi|webm|webp|mp4`
  overrides. mp4 is encoded with the bundled ffmpeg (`scripts/vendor`, H.264 + `yuv420p` + faststart,
  title/comment carry prompt+seed+steps so the file survives being copied away from its sidecar).
  On a fresh clone run `python scripts/fetch_vendor.py` once to install it, or set `SDGEN_FFMPEG`.
- `--frames` is snapped to **4k+1** (Wan's temporal stride): 72 becomes 73. 121 ≈ 5 s is the ceiling.
- Cost is dominated by frames: 21 frames / 20 steps ≈ 3 min, 73 frames / 20 steps ≈ 16 min at 832x480,
  1280x704 ≈ 2.2× that. Always draft small first, and start the final as a **background job**.
- `--flow-shift` defaults to **3.0** here (Wan's tuned value; sd-cli's own `auto` also works). It
  reshapes the sigma schedule — changing it changes the clip at the same seed, so keep 3.0 unless the
  user wants a different motion/texture balance.
- `--decoder tae` (default, fast) vs `--decoder vae` (sharper, slower).

## Prompting

- **Images**: natural language works; Qwen-Image renders short text in images well. Name the subject,
  setting, light and style; add lens words for photos ("85mm, shallow depth of field").
- **Videos**: describe motion and camera, not a still. Wan likes explicit camera language
  (`slow push-in`, `lateral tracking`, `static wide shot`) and dislikes cuts, crowds, on-screen text.
  `--negative "blurry, flicker, distorted limbs, watermark"` helps a little.
- Reference edits: the prompt is the *delta* ("hair to silver, everything else unchanged"), never a
  full re-description.

## Iterating: never re-invent the scene

```powershell
$S = "<skill>/scripts/sdgen.py"       # <skill> = the folder you installed this skill in
python $S history                          # @N index, prompt, seed, file per generation
python $S gen "<one change>" --ref "@3"            # branch from an earlier take
python $S gen "<one change>" --ref latest          # continue from the newest image
python $S use "C:/path/upload.png" --note "user photo"   # register any existing picture
```

- `latest` = newest **image** of this conversation (a newer video does not shadow it),
  `@N` = history entry N of this conversation.
- **Quote `@N`** (`--ref "@3"`): PowerShell reads a bare `@` as splatting and swallows the value.
- Keep the same `--seed` and change one clause to iterate; drop the seed to explore.

### Each conversation gets its own refinement chain

History is keyed by a session id, so two conversations running side by side never fight over
`latest` or `@N`. Works with any agent:

- `--session NAME` wins if given.
- otherwise `$SDGEN_SESSION`, then whatever your agent exports (`DSH_SESSION_ID`,
  `CLAUDE_SESSION_ID`, `CODEX_SESSION_ID`, `GEMINI_SESSION_ID`, `AGENT_SESSION_ID`,
  `TERM_SESSION_ID`), then a key derived from the working directory.
- `sdgen-output/.sessions/<key>.json` — one manifest per conversation, up to 500 entries; it survives
  turns and restarts of the same conversation, and `--ref latest` keeps refining the previous result.
- A new conversation starts empty. Continue another chain deliberately with `--session <id>` on
  `gen`/`vid`/`history`, or register a file with `use`.
- Inspect: `python $S session` (which bucket, and where it came from), `python $S history --all`.

- `--name iter-fox` keeps the version trail readable (`iter-fox-<timestamp>.mp4`, or `.avi` drafts).
- Every run is recorded in `sdgen-output/.sdgen-history.json`; files are timestamped, never overwritten.

## Verify, don't re-read

```powershell
python sdgen.py check sdgen-output/img/sd-20261027-215433.png sdgen-output/vid/sd-20261027-215518.webm
```

Reports container, dimensions, frames/duration, seed, steps, sampler, prompt, LoRAs and `attach_ok` —
read from the file's own metadata (PNG tEXt / WebP XMP / AVI chunk) or the `.json` sidecar.
`--json` for machines. See the warning at the top: never attach the file itself.

## When it fails

| Symptom | Fix |
|---|---|
| `info` shows `UNSET` / `needs work_dir, qwen_dir` | no config yet: copy `config.example.json` to `sdgen.config.json`, or set the env vars it names. |
| `400 invalid_request_error: Failed to load image or audio file` | You attached a generated file. The generation is fine and the file is on disk — report its path, and stop calling `read_image`. |
| `cannot find sd-cli.exe` | `WORK_DIR` / `SD_CLI` env vars; `sdgen.py info` shows every resolved path. |
| Wanted 1920x1080, got 1920x1088 | the 16 px grid; use `--size` on a 16-grid, or record the real dimensions from the result. |
| `--init` cut the photo instead of extending it | that is img2img cropping; use `--outpaint WxH` to grow the frame instead. |
| GPU OOM / `ggml_backend_alloc` | `--low-vram`, smaller `--size`/`--frames`, or close any other sd.cpp process that still holds VRAM. |
| Video is colour noise | `--steps` below ~10 or `--scheduler lcm` (an LCM/accelerated-setup scheduler, not for this checkpoint). Drop the scheduler flag. |
| `.mp4` asked, `.avi` delivered | ffmpeg was missing or the encode failed; the JSON result has `container_note` with the reason (set `SDGEN_FFMPEG`, or check `scripts/vendor`). |
| LoRA rejected | `sdgen.py info` lists the exact names in the LoRA dir; the wrapper also validates and prints them. |
| i2v ignores the photo | The prompt re-described the scene; describe movement/camera instead. |
| Generation slow (image >2 min, video >5 min) | expected — see costs; use `--steps 4` and `--size 512x512` for drafts. |

Flag-by-flag mapping to `sd-cli`, model file sizes, metadata formats, the history schema and the
`start.bat` quirks: [reference.md](reference.md).
