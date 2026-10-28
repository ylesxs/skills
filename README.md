# skills

通用 **Agent Skills** 集合：每个子目录是一个独立技能包，入口是 `SKILL.md`（YAML frontmatter + 操作说明），
同级资源文件按需加载。不绑定任何单一运行时——任何支持 `SKILL.md` 约定的 agent（DeepSeek Harness、
Claude Code、Codex CLI、Cursor、自研循环）都能直接使用，纯命令行、无网络依赖。

| 技能 | 说明 |
|---|---|
| [`stable-diffusion-cpp`](stable-diffusion-cpp/) | 用本机 stable-diffusion.cpp（`sd-cli.exe`）做图片生成 / 改图 / 扩图，以及 Wan2.2 文生视频、图生视频 |

## 安装到不同 agent

技能目录是自包含的，整个文件夹复制过去即可：

| 运行时 | 放置位置 | 生效方式 |
|---|---|---|
| DeepSeek Harness | `<project>/.dsh/skills/` 或 `~/.agents/skills/` | 下一次会话自动出现在技能目录 |
| Claude Code / Agent Skills | `<project>/.claude/skills/` 或 `~/.claude/skills/` | 自动发现 `SKILL.md` |
| Codex CLI / Cursor / 读 `AGENTS.md` 的工具 | 任意位置（例如仓库根） | 由仓库根的 [`AGENTS.md`](AGENTS.md) 指向技能 |
| 自研 agent / 其他框架 | 任意位置 | 把 `<skill>/SKILL.md` 注入系统提示或工具说明，允许其执行 shell |

```powershell
git clone https://github.com/ylesxs/skills.git
Copy-Item -Recurse skills\stable-diffusion-cpp <project>\.dsh\skills\   # 或 .claude\skills\
```

frontmatter 只依赖 `name` + `description` 两个字段；`whenToUse` 属于可选元数据，其他加载器会忽略。
技能本身只调用 Python 3.8+ 标准库（无第三方包、无网络、无常驻进程），因此任何能执行 shell 命令的
agent 都能驱动它。

---

# stable-diffusion-cpp

本地图片 + 视频生成技能。**全部通过 `sd-cli.exe` 一次进程完成**，不需要常驻 HTTP 服务、没有任务队列。

| 命令 | 模型 | 用途 |
|---|---|---|
| `gen` | Qwen-Image 2.1 UC (Q8_0) | 文生图、参考图改图、img2img、扩图（outpaint）、LoRA、批量 |
| `vid` | Wan2.2 TI2V-5B | 文生视频、图生视频 |
| `check` | — | 读文件内嵌元数据/边车，报告尺寸、帧数、seed、步数、是否可安全附赠 |
| `history` / `session` / `use` | — | 按会话隔离的改图链条、`latest` / `@N` 引用 |
| `info` | — | 模型栈、默认值、可用 LoRA、当前会话 |

## 依赖

技能**不含任何本机路径**。路径来自配置文件，其次环境变量（详见下文「配置」）：

1. **stable-diffusion.cpp**：`sd-cli.exe`（本仓库只调用，不分发），放在 `work_dir` 指向的目录。
2. **图片模型栈**（`qwen_dir`，默认取其中的固定文件名）：

   | 文件 | 作用 | 大小 |
   |---|---|---|
   | `qwen-image-2.1-UC-Q8_0.gguf` | 扩散模型（`qwen_image21`，非 Turbo 版） | 7.6 GB |
   | `qwen3vl_8b_int8_convrot.safetensors` | 文本编码器 | 9.4 GB |
   | `mmproj-qwen3vl_8b_heretic-f16.gguf` | 视觉塔 | 1.2 GB |
   | `qwen_image_2.1_vae_bf16.safetensors` | VAE | 0.7 GB |
   | `lora/*.safetensors` | 可选 LoRA（`lora_dir`，默认 `<qwen_dir>/lora`） | — |

3. **视频模型栈**（`wan_dir`）：`Wan2.2-TI2V-5B-Q8_0.gguf`（5.0 GB）、
   `umt5-xxl-encoder-Q8_0.gguf`（5.6 GB）、`taew2_2.pth`（22 MB）、`Wan2.2_VAE.safetensors`（1.4 GB）。
4. **ffmpeg**：mp4 输出与扩图的 canvas/mask 需要。`scripts/vendor/` 里的 87 MB 二进制不入库，克隆后执行一次：

   ```powershell
   python scripts\fetch_vendor.py
   ```

   也可直接 `SDGEN_FFMPEG=<path to ffmpeg.exe>`，或让 `ffmpeg` 出现在 `PATH` 中。

## 配置

优先级：环境变量 > 配置文件 > 内置文件名（模型文件名固定，目录不内置）。配置文件按顺序查找：
`$SDGEN_CONFIG` → `./sdgen.config.json`（当前工作目录）→ `<skill>/config.json`。

```powershell
Copy-Item .dsh\skills\stable-diffusion-cpp\config.example.json sdgen.config.json
# 填入 work_dir / qwen_dir / wan_dir（rocm_path 可选）
python .dsh\skills\stable-diffusion-cpp\scripts\sdgen.py info
```

`info` 会把缺失项标成 `UNSET`，`gen` / `vid` 在缺项时直接拒绝执行并列出缺哪些键。

| 配置键 | 对应环境变量 | 含义 |
|---|---|---|
| `work_dir` | `WORK_DIR` | 存放 `sd-cli.exe` 的目录 |
| `sd_cli` | `SD_CLI` | 可执行文件名或绝对路径（默认 `sd-cli.exe`） |
| `rocm_path` | `ROCM_PATH` | 可选，存在时才加入 `PATH` |
| `qwen_dir` | `QWEN_DIR` | Qwen-Image 模型目录 |
| `qwen_model` `qwen_vae` `qwen_llm` `qwen_vision` | `QWEN_MODEL` `QWEN_VAE` `QWEN_LLM` `QWEN_VISION` | 单独覆盖某个文件 |
| `lora_dir` | `LORA_DIR` | 默认 `<qwen_dir>/lora` |
| `wan_dir` | `WAN_DIR` | Wan2.2 模型目录 |
| `wan_model` `wan_t5xxl` `wan_tae` `wan_vae` | `MODEL_PATH` `T5XXL_PATH` `TAE_PATH` `VAE_PATH` | 单独覆盖某个文件 |
| `out_dir` | `SDGEN_OUT` | 默认 `sdgen-output` |
| `ffmpeg` | `SDGEN_FFMPEG` | 缺省时依次找 `PATH`、`scripts/vendor` |

另有三个纯环境变量：`SDGEN_CONFIG`（指定配置文件）、`SDGEN_SESSION`（历史分桶，默认取 `$DSH_SESSION_ID`）、
`SDGEN_FFMPEG`。

## 快速开始

配置好之后，在项目工作目录下执行（`$S` 按技能实际安装位置调整）：

```powershell
$S = ".dsh\skills\stable-diffusion-cpp\scripts\sdgen.py"

# 文生图（1024x1024、16 步）
python $S gen "a red apple on a wooden table, window light"

# 改图：只描述要改的地方，从上一张继续
python $S gen "make the apple bright blue, keep table and light" --ref latest

# 扩图：保留整幅原图，把画幅扩到 16:9（1536x864 恰为 16:9 且符合 16px 网格）
python $S gen "the same room continued to the sides, matching light" --init photo.png --outpaint 1536x864

# 文生视频：成品默认 mp4，草稿用 avi
python $S vid "a red fox trotting through fresh snow, slow camera push-in"
python $S vid "<shot>" --frames 21 --steps 8 --draft

# 图生视频：图片作首帧，提示词只写动作与运镜
python $S vid "she slowly turns her head and smiles" --image latest

# 核对成品（不打开图片）
python $S check sdgen-output\img\sd-20261028-121821.png
```

## 关键约定

### 1. 绝不把生成结果作为附件读回对话

`read_image` 一张生成图是本技能最容易毁掉整轮对话的操作：

```
400 invalid_request_error: Failed to load image or audio file
```

图片超过 **4 MB** 或任一边超过 **2048 px** 时，DSH 会先重编码成 WebP 再发送，网关会拒绝；
此外部分网关本来就拒绝图片 tool result。正确做法：**只报路径**，用 `check` 获取事实信息
（尺寸、帧数、seed、步数、prompt、`attach_ok`）。`check` 会显式打印
`do NOT read_image this file - report the path instead`。

### 2. 尺寸对齐 16 px（Qwen-Image）

DiT patch（2×2）× VAE（8×）= 16 px 网格，sd.cpp 会**静默取整**：请求 `1920x1080`，实际得到
`1920x1088`。脚本会先向上对齐并提示，结果里同时记录 `requested_size` 与实际 `width`/`height`。
精确 16:9 尺寸：**1024×576、1536×864、2048×1152、2560×1440**。

### 3. 扩图用 `--outpaint`，不要用 `--init`

`--init` 是 img2img：会把原图**居中裁切**到目标画幅，边缘信息丢失。要保留整幅图并扩展画幅，用
`--outpaint WxH --init IMG`：脚本用 ffmpeg 生成 canvas（原图缩放居中、边距填中性灰）与 mask
（原图区域黑、新增区域白），然后 `-i canvas --mask mask --strength 1.0`，只重绘新增边距。

### 4. 视频容器：成品 mp4，草稿 avi

sd.cpp 本身只能写 `.webm` / `.avi` / 动画 `.webp`。技能的做法：

| 命令 | 产物 | 说明 |
|---|---|---|
| `vid ...` | `.mp4` | sd.cpp 先快速写 AVI，再由 ffmpeg 一遍编码成 H.264（`yuv420p`、`+faststart`、CRF 20），并写入 title/comment 元数据 |
| `vid ... --draft` | `.avi` | 原生直写，无编码步骤，用于草稿/快速预览 |
| `--format avi\|webm\|webp\|mp4` | 对应容器 | 显式覆盖 |

### 5. 每个对话独立的改图链条

历史清单按 DSH 会话隔离：`sdgen-output/.sessions/<session-id>.json`。`latest` 与 `@N` 只在当前会话内
解析，多个对话并排跑不会互相干扰；每条记录带 `parents`，改图链条可回溯。跨会话接力用
`--session <id>`，或 `use <file>` 注册外部图片。

### 6. 已验证的默认参数

图像默认值是照旧 `:8993` 服务基线复刻的：**1024×1024、16 步、cfg 1.0、euler、flux、vae-tiling、
params-backend disk**。同一 prompt + seed + 尺寸 + 步数下，本 CLI 与旧服务输出**字节级一致**
（PNG 2,429,239 B，MD5 `93479ac4db24f49cb1d58ee5df88666c`）。`--steps 25` 对应 `start.bat` 启动值，
`--steps 8 --size 512x512` 是约 12 秒的草稿档。

## 参数速查

| 参数 | 适用 | 默认 | 说明 |
|---|---|---|---|
| `--size WxH` | gen/vid | 1024x1024 / 832x480 | 图像对齐 16 px，视频对齐 8 px |
| `--steps` | gen/vid | 16 / 20 | 图像低于 10 步明显发软 |
| `--cfg` | gen/vid | 1.0 / 5.0 | 图像不要调高 |
| `--seed` | gen/vid | 随机并回显 | 复现与迭代用 |
| `--ref IMG` | gen | — | 参考图改图（`latest` / `@N`） |
| `--init IMG` | gen | — | img2img 起点（会裁切） |
| `--outpaint WxH` | gen | — | 扩图，需配合 `--init` |
| `--strength` | gen | 0.75（扩图 1.0） | img2img 去噪强度 |
| `--lora NAME[:W]` | gen | — | 可重复，名称见 `info` |
| `--flow-shift F` | gen/vid | auto / 3.0 | 改变 sigma 调度，同 seed 结果不同 |
| `--format` | gen/vid | png / mp4 | 视频 `--draft` 时默认 avi |
| `--frames` / `--fps` | vid | 72→73 / 24 | 帧数吸附到 4k+1 |
| `--decoder tae\|vae` | vid | tae | vae 更锐但更慢 |
| `--low-vram` | gen/vid | 关 | 权重卸载 CPU + VAE 分块 |

## 环境变量

配置键与变量的完整对应表见上文「配置」。没有配置文件对应项的只有三个：

| 变量 | 默认 |
|---|---|
| `SDGEN_CONFIG` | 未设置——显式指定配置文件 |
| `SDGEN_SESSION` | `$DSH_SESSION_ID`（按会话隔离历史） |
| `SDGEN_FFMPEG` | 未设置——依次找 `PATH`、`scripts/vendor` |

## 实测耗时（AMD ROCm gfx1100 / 24 GB）

| 任务 | 参数 | 耗时 |
|---|---|---|
| 图片 | 512×512 / 4 步 | 12–18 s |
| 图片 | 1024×1024 / 8 步 | ~104 s |
| 图片 | 1024×1024 / 16 步（默认） | ~187 s |
| 图片 | 1024×1024 / 25 步 | ~296 s |
| 扩图 | 1536×864 / 8 步 | ~48 s |
| 视频 | 21 帧 / 20 步（832×480） | ~172 s |
| 视频 | 73 帧 / 20 步（832×480） | ~966 s |
| 视频 | 21 帧 / 8 步 i2v（含 mp4 编码） | ~78 s |

视频耗时随帧数超线性增长（≈ 帧数^1.6），1280×704 再乘约 2.2。实拍渲染请放在后台作业里跑。

## 目录结构

```
stable-diffusion-cpp/
  SKILL.md            # 技能入口：路由描述 + 操作说明
  reference.md        # 参数映射、模型栈、容器、历史结构、排障
  scripts/
    sdgen.py          # 唯一的执行脚本（gen / vid / check / history / session / use / info）
    fetch_vendor.py   # 下载 ffmpeg 到 scripts/vendor（不入库）
    vendor/           # 运行时生成（fetch_vendor.py），已在 .gitignore 中忽略
  config.example.json # 配置模板：复制为 sdgen.config.json 后填入本机路径
```

## 排障

| 现象 | 处理 |
|---|---|
| `400 Failed to load image or audio file` | 把生成图当成了附件。图片已在磁盘上，报路径即可，不要再 `read_image` |
| `info` 显示 `UNSET` / `needs work_dir, qwen_dir` | 还没配置：复制 `config.example.json` 为 `sdgen.config.json` 并填写 |
| 请求 1920×1080 得到 1920×1088 | 16 px 网格，见上文 |
| `--init` 把图裁掉了 | 那是 img2img 的裁切行为；改用 `--outpaint` |
| 视频全是彩色噪点 | 步数过低或误用 `--scheduler lcm`（那是加速模型调度器） |
| 要 mp4 却得到 avi | 找不到 ffmpeg 或编码失败；结果 JSON 的 `container_note` 有原因 |
| 显存不足 | `--low-vram`、减小尺寸/帧数，或关闭其它占用显存的 sd.cpp 进程 |

## 许可

本仓库代码以 [MIT](LICENSE) 发布。`scripts/vendor/` 中的 ffmpeg 由 `imageio-ffmpeg` 分发，遵循其自身许可（GPL 构建），不随本仓库提交。
