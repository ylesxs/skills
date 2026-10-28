# AGENTS.md

This repository ships **agent skills** — self-contained folders whose `SKILL.md` describes how to do one
job with local tools. They are plain Markdown + Python (stdlib only), so any agent that can run a shell
command can use them.

## Available skills

| Skill | Use it for |
|---|---|
| `stable-diffusion-cpp/` | local image generation / editing / outpainting (Qwen-Image 2.1) and text-to-video / image-to-video (Wan2.2 TI2V-5B) through `sd-cli.exe` |

## How to use one

1. Read `stable-diffusion-cpp/SKILL.md` before acting on an image or video request; it is the routing
   description and the operating manual (setup, prompting, containers, pitfalls).
2. Run `python stable-diffusion-cpp/scripts/sdgen.py info` first. If it reports `UNSET`, copy
   `stable-diffusion-cpp/config.example.json` to `sdgen.config.json` in the working directory and fill
   in `work_dir` / `qwen_dir` / `wan_dir`.
3. Deeper material (flag mapping, model stack, history schema, measured costs) is in
   `stable-diffusion-cpp/reference.md`.

## Hard rules

- **Never attach a generated image/video to the model context.** Report the file path instead; use
  `sdgen.py check <file>` for size/seed/steps/`attach_ok`. Attaching is what produces
  `400 invalid_request_error: Failed to load image or audio file` on many gateways.
- Do not bake machine paths into anything you write; this repo is public and path-free. Configuration
  goes through `sdgen.config.json` (git-ignored) or environment variables.
- Video finishes as `.mp4`, drafts as `.avi` (`--draft`); sd.cpp itself cannot write mp4 — ffmpeg does.
- Image sizes land on a 16 px grid (1920x1080 → 1920x1088). Do not promise exact 1920x1080.
