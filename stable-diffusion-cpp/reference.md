---
title: stable-diffusion.cpp CLI reference — Qwen-Image 2.1 + Wan2.2 TI2V-5B
---

# sd.cpp CLI reference

Measured on this machine (AMD ROCm gfx1100, 24 GB, sd.cpp commit `e41fbcf`). The binary is
`D:\Ai\serves\lib\stable-diffusion\sd-cli.exe`; every model file lives under `D:\Ai\serves\model`.

## Two stacks

### Image — Qwen-Image 2.1 (UC, Q8_0)

GGUF architecture tag: `qwen_image21`. This is the plain 2.1 checkpoint, quantized Q8_0 by the "UC"
(uncensored) maintainers — **not** a Turbo/lightning build, despite a `…Turbo-Uncensored-F16.gguf`
vision-tower file sitting in the same folder (that is a different mmproj, not the diffusion model).

| File | Size | CLI flag |
|---|---|---|
| `qwen-image-2.1-UC-Q8_0.gguf` | 7.6 GB | `--diffusion-model` |
| `qwen3vl_8b_int8_convrot.safetensors` | 9.4 GB | `--llm` (text encoder) |
| `mmproj-qwen3vl_8b_heretic-f16.gguf` | 1.2 GB | `--llm_vision` (vision tower) |
| `qwen_image_2.1_vae_bf16.safetensors` | 0.7 GB | `--vae` |
| `lora/*.safetensors` | 0.4-1.2 GB | `--lora-model-dir` + prompt tag |

Driven at **cfg 1.0**, sampler `euler`, scheduler resolves to `flux` — exactly the `start.bat` settings.
`--vae-tiling` and `--diffusion-fa` come from the original `start.bat` and stay on. The metadata field
`distilled_guidance: 3.5` is sd.cpp's name for the built-in guidance vector the Qwen-Image pipeline
consumes; it is not a statement that this checkpoint is a distilled/Turbo model.

### Video — Wan2.2 TI2V-5B

| File | Size | CLI flag |
|---|---|---|
| `Wan2.2-TI2V-5B-Q8_0.gguf` | 5.0 GB | `--diffusion-model` |
| `umt5-xxl-encoder-Q8_0.gguf` | 5.6 GB | `--t5xxl` |
| `taew2_2.pth` | 22 MB | `--tae` (fast decode) |
| `Wan2.2_VAE.safetensors` | 1.4 GB | `--vae` (full quality decode) |

Loads 10.9 GB VRAM. cfg 5.0, scheduler resolves to `discrete`, sampler `euler`, flow-shift 3.0.

## `D:\Ai\serves\model\qwen\image\2.1\start.bat` → CLI mapping

The bat is a long-running launcher. `sdgen.py` keeps every model/quality choice from it and drops only
the listener parts that make no sense for a one-shot process:

| start.bat | sd-cli | kept? |
|---|---|---|
| `--diffusion-model …Q8_0.gguf` | same | yes |
| `--vae …vae_bf16.safetensors` | same | yes |
| `--llm …qwen3vl_8b_int8_convrot.safetensors` | same | yes |
| `--llm_vision …mmproj-qwen3vl_8b_heretic-f16.gguf` | same | yes |
| `--lora-model-dir %MODEL_DIR%\lora` | same | yes (this is what `--lora` resolves against) |
| `--params-backend disk` | same | yes, default (`--params-backend memory` to opt out) |
| `--cfg-scale 1.0` | `--cfg-scale 1.0` | yes |
| `--sampling-method euler` | same | yes |
| `--diffusion-fa`, `--vae-tiling` | same | yes |
| `--steps 25` | `--steps` (16 by default in the wrapper) | same flag; 16 is what the earlier setup produced, 25 is the bat launch value |
| `--listen-ip`, `--listen-port`, `-v` | — | dropped (listener only) |
| `%~1` port argument, llama-swap integration | — | not applicable to one-shot CLI |

There is also a second vision tower in the model dir (`mmproj-Qwen-Image-2.1-Turbo-Uncensored-F16.gguf`);
the bat uses the `heretic` one and the wrapper follows the bat.

## Parameter parity with the earlier setup (verified)

That setup is gone, so parity was checked the hard way: the wrapper was asked to reproduce a picture it
had produced, with the identical request.

| Input | Earlier setup | New CLI (`sdgen.py gen`) |
|---|---|---|
| prompt | `majestic snow-capped mountains …` | same |
| seed | 298 | 298 |
| size | 1024x1024 | 1024x1024 |
| steps | 16 | 16 |
| sampler / scheduler | euler / flux | euler / flux (model defaults, not forced) |
| cfg / distilled_guidance (sd.cpp field) | 1.0 / 3.5 | 1.0 / 3.5 (sd-cli defaults) |
| vae tiling | on, tile 0 (auto), overlap 0.5 | same (`--vae-tiling`) |
| clip skip | 0 | 0 |
| models | same four files | same |

Result: **byte-identical PNG** — both files are 2,429,239 bytes with MD5
`93479ac4db24f49cb1d58ee5df88666c` (`epic-mountains-…png` vs `parity-check-…png`; MAE 0, PSNR ∞).
So the CLI path is not an approximation of the earlier setup: it is the same computation, down to the
encoder.

The quality regression came from **one** wrapper default: the merged CLI shipped with `--steps 8`
while the earlier setup produced 16 steps (and its `start.bat` launches at 25). This checkpoint is not a
Turbo or distilled build, so 8 steps simply does not converge - the image comes out soft and
low-detail. The default is now 16, so the wrapper reproduces the accepted baseline; `--steps 25` is the
bat launch value, `--steps 8 --size 512x512` is a 12-second draft.

## Formats and metadata

| Container | Generation record | Read back by |
|---|---|---|
| PNG | `tEXt` keyword `parameters` with an A1111 line + `SDCPP: {json}` | `sdgen.py check`, `-M metadata` |
| WebP / JPEG | `XMP` packet, `sdcpp:parameters` | same (html-unescaped first) |
| AVI (video) | `ICMT` chunk with the JSON | same |
| WebM (video) | **nothing** | the `.json` sidecar written next to the clip |
| MP4 (video) | `title` / `comment` tags written by the wrapper | the `.json` sidecar (`check` parses that) |

Consequences:
- Every run also writes `<stem>.json` (sidecar) and `sdgen-output/logs/<stem>.log` (full sd-cli log).
- `check` prefers embedded metadata, falls back to the sidecar, then to header dimensions.

### Attachment budget (why `read_image` is forbidden)

DSH passes an attachment through untouched only while it is **≤ 2048 px on both sides and ≤ 4 MB**;
outside that it re-encodes to WebP, and the model gateway answers
`400 invalid_request_error: Failed to load image or audio file`. Several gateways reject image tool
results even inside the budget, so the rule is absolute: never attach a generated file.

`check` computes `attach_ok` and says so explicitly. Real case: a 1920×1088 PNG at **4.24 MB** was
attached and killed the turn — the picture itself was fine and still on disk.

### Video containers

sd.cpp can only write **`.webm`, `.avi`, animated `.webp`** — `-o x.mp4` produces `x.mp4.avi`. The
wrapper therefore treats mp4 as a two-step job:

| `--format` | Native write | Post-step | Use for |
|---|---|---|---|
| *(none)* + `--draft` | `.avi` (MJPEG, fast) | none | drafts, quick previews, iterating |
| *(none)*, plain `vid` | `.avi` | ffmpeg → `.mp4` (H.264 High, `yuv420p` limited range, `+faststart`, CRF 20) | final deliverable |
| `avi` | `.avi` | none | drafts, or lossless-ish intermediates |
| `webm` | `.webm` (VP9) | none | small files that still play in a browser |
| `webp` | animated `.webp` | none | sticker-ish web use |

The intermediate AVI is deleted after a successful encode. If ffmpeg is missing or fails, the AVI is
kept and the JSON result carries `container_note`; nothing is lost silently.

ffmpeg is bundled by this skill at
`scripts/vendor/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe` (87 MB, imageio-ffmpeg 0.6.0).
Lookup order: `SDGEN_FFMPEG` env var → `ffmpeg` on `PATH` → the bundled binary. Encoding cost is
negligible next to sampling (a 21-frame 832×480 clip encoded in ~1 s; the 77 s run below includes it).

## Measured cost

Images (Qwen-Image, 832-1024 px, 24 GB card, warm cache):

| Run | Wall clock |
|---|---|
| 512x512, 4 steps, png | 12-18 s |
| 512x512, 4 steps, ref-edit (`-r`) | 22-25 s |
| 1024x1024, 8 steps | ~104 s |
| 1024x1024, 16 steps (**default**) | ~187 s |
| 1024x1024, 25 steps (start.bat's launch value) | ~296 s |

Reference edits add ~10-20 % over the same-step generate.

Videos (Wan2.2 TI2V-5B, 832x480, tae):

| Run | Wall clock |
|---|---|
| 21 frames, 4 steps (`lcm`) | ~24 s (colour noise — too few steps) |
| 21 frames, 20 steps | ~172 s (8.15 s/step) |
| 73 frames, 20 steps | **~966 s ≈ 16 min** (47-50 s/step) |
| i2v, 21 frames, 8 steps, `--draft` (avi) | ~77 s |
| i2v, 21 frames, 8 steps, mp4 (encode included) | ~78 s |

Video time grows faster than frame count (≈ frames^1.6): 3.48× frames cost 5.6× time. 1280×704 is
another ≈2.2×. Draft at 21 frames / 8 steps, render the chosen seed at the defaults, and run real
renders as background jobs.

## Image capabilities

- **Reference edit** — `--ref IMAGE` (repeatable). sd.cpp logs `Using 'qwen' preset for reference
  images`; the photo supplies composition and identity, so the prompt is the delta only.
- **img2img** — `--init IMAGE --strength 0.3-0.5` when the structure must survive nearly unchanged.
- **Inpaint / ControlNet / IP-Adapter** — `--mask`, `--control`, `--ip-adapter` (no extra models are
  installed for the latter two; the flags are wired but will complain about missing weights).
- **LoRA** — files in `D:\Ai\serves\model\qwen\image\2.1\lora`. The wrapper turns `--lora NAME:0.7`
  into the prompt tag `<lora:NAME:0.7>`; sd.cpp logs `apply lora at runtime` and the tensor count.
  Available now: `fusal-qwen-image-2.1-stack-exact-r80`, `qwen-image-anime-nsfw`,
  `qwen2_1-femaledVaginus-nsfw-v1_3-fine_L22-31`.
- **Batch** — `--batch N`; files come back as `<stem>` plus siblings, all collected into the result.
- **Outpaint / 扩图** — `gen "…" --init IMG --outpaint WxH`. The wrapper builds the two inputs sd.cpp
  needs, using the bundled ffmpeg:
  1. *canvas* — the source scaled to fit, centred on a `WxH` frame, margins filled with neutral grey;
  2. *mask* — black over the source rectangle, white over the new margins, so only the margins are
     regenerated.

  It then runs `img_gen` with `-i canvas --mask mask --strength 1.0` (override with `--strength`).
  Measured on a 1080×1428 portrait → 1920×1088: inner picture 822×1088 at offset (548, 0), mask 57 %
  white, centre region changed by 7.6/255 (VAE round-trip only — the original survives), margins by
  80.8/255 (freshly generated). Do **not** use `--init` alone for this: img2img centre-crops the photo
  to the target aspect and throws the rest away.
- **16 px grid** — Qwen-Image's DiT patch × VAE puts both sides on multiples of 16 and sd.cpp silently
  rounds (1920×1080 came back as 1920×1088). The wrapper rounds *up* before the run and prints the
  snap, and the result records both `requested_size` and the real `width`/`height` read from the file.
  Exact 16:9 sizes: 1024×576, 1536×864, 2048×1152, 2560×1440.
- **flow-shift** — `--flow-shift F` maps to sd-cli's flow-matching sigma shift. Omitted = `auto`
  (the model decides); an explicit value is written into the PNG/WebP record
  (`sample_params.flow_shift`) and shown by `check`. Proven to be effective, not cosmetic: the same
  prompt + seed=4242 at 512×512 / 4 steps produced `f921eed3…` with `auto` and `4266b0df…` with
  `--flow-shift 1.0` (different pixels). Treat it as a look knob.
- 1024×1024 and 1328×1328 are the training-size classes; 512×512 is a fast draft.

## Video capabilities

- **i2v** — `--image` (`-i`). The input is centre-cropped and resized to `-W/-H`, e.g. a 1024×1024
  photo at 832×480 is cropped to `crop=(0,217,1024,590)`. Match the target aspect to the photo, or add
  `--extra "--image-preprocess target=init,mode=fit-pad"` to letterbox instead.
- **Frame count** — Wan's VAE has a 4× temporal stride: use 4k+1 (21, 49, 73, 121). The wrapper snaps.
- **fps** is 24 natively; `--fps` is a container/metadata field.
- **Never `lcm`** on this checkpoint: `STEPS=3` + `SCHEDULER=lcm` from the old `start.bat` is what
  produces the classic colour-noise video. Let the scheduler default (`discrete`).
- `--flow-shift 3.0` for Wan (the tuned value); `auto` also works. The flag is passed to
  `sd-cli --flow-shift`, stored in the `.json` sidecar (and in the AVI `ICMT` chunk when that container
  is used), and reported by `check` as `flow-shift=`.
- **Draft vs final** — `--draft` writes `.avi` natively; a plain `vid` writes AVI then encodes `.mp4`.
  Both are the same pixels; only the container and encode step differ.

## History, sessions and the refinement chain

**One manifest per DSH session**, so parallel conversations cannot see or borrow each other's
`latest` / `@N`:

```
sdgen-output/
  .sessions/session-4a8d694e-ff44-45d3-b112-8d27bd38a199.json   <- this conversation
  .sessions/<other-session-id>.json
  img/ vid/ logs/
```

Session key resolution: `--session NAME` → `$SDGEN_SESSION` → `$DSH_SESSION_ID` (set by the harness) →
`default`. A pre-session `sdgen-output/.sdgen-history.json` is migrated into the first session that
loads it and renamed `*.legacy.json`.

Newest 500 entries per session, rewritten atomically:

```jsonc
{
  "index": 7,                          // the @N address space, per session
  "entries": [
    {
      "index": 7, "session": "session-4a8d…", "time": "2026-10-27T21:55:18",
      "kind": "video", "mode": "i2v", "files": ["D:\\...\\vid\\apple-spin-....webm"],
      "width": 832, "height": 480, "frames": 5, "fps": 24,
      "steps": 2, "seed": 1596816501, "prompt": "the apple rotates slowly, static shot",
      "negative": "", "parents": ["D:\\...\\img\\apple-blue-....png"],
      "elapsed_seconds": 16.1
    }
  ]
}
```

- `latest` / `@last` → newest **image** entry *of this session* whose file still exists (videos never
  shadow images).
- `@N` → that entry of this session, image only (an `@N` pointing at a video is refused clearly).
- `parents` records what each result was derived from, so a refinement chain stays auditable; calling
  `gen --ref latest` repeatedly is the intended "keep editing the last result" loop.
- `use FILE --note "..."` inserts an `import` entry into the current session so an
  uploaded/screenshot picture becomes an edit source. Files are referenced in place, never copied.
- `session`, `history --all`, `history --session ID`, and `--session ID` on `gen`/`vid` are the
  cross-session escape hatches.

## Raw CLI equivalents

```bat
:: image
cd /d D:\Ai\serves\lib\stable-diffusion
sd-cli.exe -M img_gen ^
  --diffusion-model D:\Ai\serves\model\qwen\image\2.1\qwen-image-2.1-UC-Q8_0.gguf ^
  --vae D:\Ai\serves\model\qwen\image\2.1\qwen_image_2.1_vae_bf16.safetensors ^
  --llm D:\Ai\serves\model\qwen\image\2.1\qwen3vl_8b_int8_convrot.safetensors ^
  --llm_vision D:\Ai\serves\model\qwen\image\2.1\mmproj-qwen3vl_8b_heretic-f16.gguf ^
  --lora-model-dir D:\Ai\serves\model\qwen\image\2.1\lora ^
  --params-backend disk --steps 16 --cfg-scale 1.0 --sampling-method euler ^
  --diffusion-fa --vae-tiling -W 1024 -H 1024 -s 42 ^
  -p "a red apple on a wooden table" -o D:\out\apple.png

:: video
sd-cli.exe -M vid_gen ^
  --diffusion-model D:\Ai\serves\model\wan\2.2\Wan2.2-TI2V-5B-Q8_0.gguf ^
  --t5xxl D:\Ai\serves\model\wan\2.2\umt5-xxl-encoder-Q8_0.gguf ^
  --tae D:\Ai\serves\model\wan\2.2\taew2_2.pth ^
  --cfg-scale 5 --steps 20 --flow-shift 3.0 --fps 24 ^
  -W 832 -H 480 --video-frames 73 -s 42 --diffusion-fa --vae-conv-direct ^
  -p "a lovely cat walking on the grass" -o D:\out\cat.webm
```

## The two `start.bat` files: what to keep, what to ignore

`model\qwen\image\2.1\start.bat` — a long-running launcher. Model/quality flags are mirrored above; only
its listener flags are meaningless for the one-shot CLI. Its `--steps 25` is a quality choice for a
non-Turbo checkpoint, not an accelerated-model default.

`model\wan\2.2\start.bat` — a double-click video generator that is **broken for automation**:

1. `-n "%OUTPUT_NAME%"` is sd.cpp's **negative prompt**, not the filename, and it never passes `-o`, so
   the clip lands in `D:\Ai\serves\lib\stable-diffusion` under the CLI default name — while the chosen
   name becomes a negative prompt that fights the prompt at cfg>1.
2. `STEPS=3` + `SCHEDULER=lcm` are LCM/accelerated-setup settings; this checkpoint needs ~20 steps and
   the default scheduler.
3. Its positional form takes only 6 values (prompt width height frames output image); the README's 7th
   and 8th (steps, cfg) are silently ignored.
4. `pause >nul` blocks non-interactive callers unless stdin is redirected (`<nul`).
5. `ROCM_PATH` points at a non-existent ROCm install — harmless: the sd.cpp folder ships its own DLLs
   (`amdhip64_7.dll`, `MIOpen.dll`, …), so `sdgen.py` only prepends that dir when it exists.

## Environment variables

| Variable | Default |
|---|---|
| `WORK_DIR` / `SD_CLI` | `D:\Ai\serves\lib\stable-diffusion` / `sd-cli.exe` |
| `ROCM_PATH` | `C:\Program Files\AMD\ROCm\7.1.1` (optional) |
| `QWEN_DIR` + `QWEN_MODEL`, `QWEN_VAE`, `QWEN_LLM`, `QWEN_VISION`, `LORA_DIR` | `D:\Ai\serves\model\qwen\image\2.1` |
| `WAN_DIR` + `MODEL_PATH`, `T5XXL_PATH`, `TAE_PATH`, `VAE_PATH` | `D:\Ai\serves\model\wan\2.2` |
| `SDGEN_OUT` | `sdgen-output` (relative to the workspace) |
| `SDGEN_SESSION` | `$DSH_SESSION_ID` (per-conversation history bucket) |
| `SDGEN_FFMPEG` | unset — ffmpeg comes from PATH or the bundled `scripts/vendor` binary |
