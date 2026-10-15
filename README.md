# nanovoice

自托管的语音转写服务 + 命令行客户端。

- `stt-server`：常驻 HTTP 服务。接收音频，ffmpeg 统一转码并去静音，调用 ASR 后端，可选做替换规则和 LLM 清理，返回文本。
- `stt`：瘦客户端，把本地音频上传给 server 并打印结果。

ASR 和 LLM 都走 **OpenAI 兼容接口**（OpenAI、Groq 或任何兼容服务）。`local` 后端只预留了接口，尚未实现。

```
iPhone 快捷指令 / stt CLI / OpenWhispr
        │  multipart 上传 (m4a/wav/mp3/ogg/flac/webm)
        ▼
stt-server ── ffmpeg → 16 kHz 单声道 FLAC + 去静音(VAD)
        ├── ASR:  POST {asr.remote.base_url}/audio/transcriptions   (词典 terms 作为 prompt)
        ├── 词典纠错（corrections）→ 替换规则（字符串 / 正则）
        ├── LLM:  POST {llm.base_url}/chat/completions   (失败自动退回原文)
        └── 历史记录（可选）→ 自动学习词典
```

## 安装与启动

前置条件：Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、ffmpeg（`brew install ffmpeg` / `apt install ffmpeg`）。

```bash
git clone <this repo> nanovoice && cd nanovoice
uv tool install .            # 安装 stt-server 和 stt 到 ~/.local/bin
# 开发时也可以：uv sync && uv run stt-server

mkdir -p ~/.config/stt
cp config.example.toml ~/.config/stt/config.toml      # 按需修改
cp deploy/env.example ~/.config/stt/env && chmod 600 ~/.config/stt/env
$EDITOR ~/.config/stt/env    # 填 STT_TOKEN / STT_ASR_API_KEY / STT_LLM_API_KEY

set -a; . ~/.config/stt/env; set +a
stt-server                   # 或 stt-server --config path/to/config.toml --port 8765
```

启动时会检查 ffmpeg，打印所有可访问地址；如果检测到 Tailscale，还会打印 MagicDNS 名称：

```
stt-server is reachable at:
  http://127.0.0.1:8765
  http://192.168.1.20:8765
  http://100.101.102.103:8765
  http://mybox.tail1234.ts.net:8765
  (Tailscale MagicDNS: mybox.tail1234.ts.net; with `tailscale serve`: https://mybox.tail1234.ts.net)
```

密钥只从环境变量读取：

| 环境变量 | 用途 |
|---|---|
| `STT_TOKEN` | 服务端 bearer token；CLI 也用它 |
| `STT_ASR_API_KEY` | ASR 上游的 API key（变量名可通过 `asr.remote.api_key_env` 修改） |
| `STT_LLM_API_KEY` | LLM 上游的 API key（变量名可通过 `llm.api_key_env` 修改） |
| `STT_CONFIG` | 可选，配置文件路径 |
| `STT_SERVER` | 可选，CLI 使用的服务器地址 |

### 配置云端 API

在 `~/.config/stt/config.toml` 中：

```toml
[asr.remote]
base_url = "https://api.groq.com/openai/v1"     # 或 https://api.openai.com/v1
model = "whisper-large-v3-turbo"                 # OpenAI: whisper-1 / gpt-4o-transcribe

[llm]
enabled = true
base_url = "https://api.openai.com/v1"
model = "gpt-4o-mini"
```

完整的配置项见 [`config.example.toml`](config.example.toml)，每一项都有注释。

### 开机自启

二选一：

- **Linux（systemd user service）**：[`deploy/stt-server.service`](deploy/stt-server.service)
  ```bash
  cp deploy/stt-server.service ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now stt-server
  loginctl enable-linger "$USER"
  journalctl --user -u stt-server -f
  ```
- **macOS（launchd）**：[`deploy/com.nanovoice.stt-server.plist`](deploy/com.nanovoice.stt-server.plist)
  ```bash
  cp deploy/com.nanovoice.stt-server.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nanovoice.stt-server.plist
  tail -f /tmp/stt-server.log
  ```

两者都从 `~/.config/stt/env` 读取密钥。

## CLI

```bash
stt transcribe meeting.m4a                       # 只把最终文本打印到 stdout
stt transcribe note.wav --json                   # 完整 JSON
stt transcribe note.wav --no-clean --language zh
stt transcribe note.wav --prompt "Kubernetes, gRPC"
ffmpeg -i in.mp4 -f wav - | stt transcribe -     # 从 stdin 读
stt health
stt --server https://mybox.tail1234.ts.net/stt health

# 词典 / 历史 / 学习（见下文“词典、历史与自动学习”）
stt dict show                                    # 查看词典
stt dict add Tailscale "Claude Code"             # 加专有名词
stt dict fix "tail scale" Tailscale              # 加纠错规则（同时把 Tailscale 加为专有名词）
stt dict rm MagicDNS                             # 删除专有名词（并且以后不会再被自动学回来）
stt dict unfix "tail scale"                      # 删除纠错规则
stt dict edit                                    # 用 $EDITOR 编辑整个词典
stt history -n 20                                # 最近的转写（带 id）
stt correct <id> "改正后的完整文本"                # 根据你的改正自动生成纠错规则
stt learn                                        # 立即从最近的历史学习一次
```

服务器地址的优先级：`--server` > `$STT_SERVER` > 配置中的 `[client].server`。token 从 `$STT_TOKEN` 读取。日志和错误信息打印到 stderr；出错时退出码为 1（服务端返回错误）或 2（连不上服务器）。

## HTTP API

**完整的接口文档由服务自己提供**：浏览器打开 `http://<host>:8765/docs`，是 Swagger UI 交互文档，可以直接试调用，点右上角 Authorize 填 token；机器可读的 OpenAPI 3.1 规范在 `/openapi.json`，可以导入 Postman、Insomnia 或生成客户端。这两个地址都不需要鉴权。下面是摘要。

所有错误都返回 `{"error": {"code": "...", "message": "..."}}`，每个响应都带 `X-Request-ID` 头。

### `POST /transcribe`

`multipart/form-data`，下面的参数也可以放在 query string 里：

| 字段 | 说明 |
|---|---|
| `file` | 必填，音频文件 |
| `language` | 可选，`zh` / `en` / … ；默认 `auto`（自动检测，适合中英混杂） |
| `prompt` | 可选，追加在配置词典之后，作为 ASR prompt |
| `clean` | 可选，`true`/`false`，是否做 LLM 清理；默认取 `transcribe.clean` |
| `backend` | 可选，覆盖后端，例如 `remote` |
| `format` | `text`（`text/plain`，只返回最终文本）或 `json`；默认取 `transcribe.default_format`（`text`） |

`format=json` 的返回：

```json
{
  "text": "我们明天用 Claude Code 开会吧。",
  "raw_text": "嗯 那个 我们明天用 cloud code 开会吧 um 就是明天",
  "language": null,
  "duration_ms": 2500,
  "backend": "remote",
  "model": "whisper-large-v3-turbo",
  "cleaned": true,
  "timings": {"decode_ms": 76, "queue_ms": 0, "asr_ms": 820, "clean_ms": 640, "total_ms": 1560}
}
```

`language` 是请求时指定的语言，自动检测时为 `null`。`duration_ms` 是上传音频的时长。

### `POST /v1/audio/transcriptions`

兼容 OpenAI 的接口，字段为 `file`、`model`（接收但忽略，始终用配置的后端和模型）、`language`、`prompt`、`response_format`（`json` | `text` | `verbose_json`）。默认不做 LLM 清理，可以通过 `openai_compat.clean` 打开。鉴权方式相同：OpenAI 客户端里的 “API key” 就填你的 `STT_TOKEN`。

### `GET /health`

不需要鉴权。返回状态、默认后端和模型、LLM、ffmpeg 版本、鉴权模式、排队情况、词典条目数，以及历史记录和学习的状态（包括最近一次学习的结果）。

### 词典与历史

| 接口 | 说明 |
|---|---|
| `GET /dictionary` | 返回 `{terms, corrections, ignore}` |
| `PATCH /dictionary` | 按条修改：`{"add_terms": [...], "remove_terms": [...], "add_corrections": [{"wrong", "right"}], "remove_corrections": ["wrong", ...], "ignore_removed": true}` |
| `PUT /dictionary` | 整份替换（`stt dict edit` 用的就是这个接口） |
| `GET /history?limit=20` | 最近的转写，最新的在前（需要 `history.enabled`） |
| `POST /history/{id}/correct` | `{"text": "改正后的文本"}`，自动推出纠错规则并写进词典 |
| `POST /dictionary/learn` | 立即从最近的历史学习一次 |

`/transcribe` 返回的 JSON 和每个响应的 `X-Request-ID` 头里都有 `id`，也就是这条转写在历史里的 id。

例子：

```bash
H="Authorization: Bearer $STT_TOKEN"
curl -H "$H" -X PATCH http://host:8765/dictionary -H 'Content-Type: application/json' \
     -d '{"add_corrections": [{"wrong": "tail scale", "right": "Tailscale"}]}'
curl -H "$H" -X POST http://host:8765/history/1ed70d2417d2/correct -H 'Content-Type: application/json' \
     -d '{"text": "我们明天用 Claude Code 开会吧。"}'
# → {"added": {"terms": ["Claude Code"], "corrections": [{"wrong": "cloud code", "right": "Claude Code"}]},
#    "skipped": [...]}
```

### 状态码

| 状态码 | 含义 |
|---|---|
| 400 | 参数错误或缺少 `file` |
| 401 | token 缺失或错误；tailscale 模式下缺少 `Tailscale-User-Login` 头 |
| 403 | tailscale 用户不在白名单中 |
| 413 | 文件超过 `server.max_upload_mb`（chunked 上传也会被截断） |
| 415 | ffmpeg 无法解码 |
| 502 / 504 | ASR 上游报错 / 超时 |
| 503 | 排队超时（服务忙），或后端不可用 |
| 500 | 内部错误 |

### 行为说明

- **并发**：同一时间最多 `server.max_concurrency` 个 ASR 请求，多出的排队；排队超过 `queue_timeout_s` 返回 503。LLM 清理不占队列位置。
- **VAD**：用 ffmpeg `silenceremove` 去掉超过 `min_silence_s` 的静音，对所有后端生效。全是静音的录音直接返回空文本，不调用 ASR，也就避免了 Whisper 在静音上“幻听”出文字。
- **后处理顺序**：ASR（词典 terms 作为 prompt）→ 词典纠错 → 替换规则 → LLM 清理。
- **LLM 清理**：temperature 0；非空白字符少于 `min_chars` 时跳过。下列情况都会退回替换后的原文，请求照常成功：调用失败、超时、返回为空、输出长度异常（超过原文两倍再加 50 个字符，说明模型可能在“回答”而不是清理）。
- **隐私**：临时文件处理完立即删除，任何情况下都不保存音频。转写文本只有在打开 `history.enabled` 时才会写进历史文件（权限 600）；代码里的默认值是关闭，`config.example.toml` 里是打开的。日志里不记录转写内容（`server.log_transcripts = false`）。日志是 JSON 行格式，包含解码、排队、ASR、清理各阶段的耗时。

## 词典、历史与自动学习

### 词典

词典是一个单独的文件（默认 `~/.config/stt/dictionary.toml`，示例见 [`deploy/dictionary.example.toml`](deploy/dictionary.example.toml)），分三部分：

- `terms`：专有名词、人名、产品名、术语。拼接后作为 ASR 的 prompt，让模型倾向于输出这些写法。越靠前优先级越高；总长度超过 `dictionary.max_prompt_chars` 的部分不会发送（Whisper 只看 prompt 的最后约 224 个 token）。
- `corrections`：`wrong → right`。ASR 输出后做不区分大小写的字面替换，用来修正那些“提示了也还是会认错”的词。英文片段按整词匹配，`cloud` 不会误伤 `icloud`。
- `ignore`：不允许自动学习加入的词或纠错。

改词典有四种方式，效果一样，都不用重启：

1. **直接编辑文件**：服务会在下一个请求时发现文件变了并重新加载。如果文件写错了（比如 TOML 语法错误），服务会继续用上一个正确的版本，并在日志里报错，不会影响转写。注意：通过 API 或自动学习保存时会重写这个文件，文件里的注释不会保留。
2. **CLI**：`stt dict add / rm / fix / unfix / edit`（可以在任何一台能访问服务器的机器上用）。
3. **HTTP API**：`PATCH` / `PUT /dictionary`，见上文。
4. **改正一条历史转写**：`stt correct <id> "正确的文本"`，或 `POST /history/{id}/correct`。服务把你的版本和当时的结果做 diff，每处被替换的片段生成一条 `wrong → right`，`right` 同时加进 `terms`。为了不误伤，下面这些改动会被跳过并在返回里注明原因：只改了一个字（比如“在 → 再”，做成全局替换太危险）、只改了标点或空格、纯新增或删除的内容，以及 `wrong` 在原始 ASR 输出里根本没出现过的情况。

`stt dict rm` 和 `unfix` 删掉的条目默认会进入 `ignore`，之后自动学习不会再把它加回来（加 `--no-ignore` 可以不这样做）。手动 `add` 或 `fix` 的条目会自动从 `ignore` 里移除。

### 历史记录

`history.enabled = true` 时，每次转写都会在 `~/.local/share/stt/history.jsonl` 追加一行，包括 `id`、时间、原始文本、最终文本、语言、时长和模型。全是静音的录音不会记录。用 `stt history` 或 `GET /history` 查看。文件只会不断追加，不会自动轮转；需要的话可以手动清理或归档。

### 自动学习

`learn.auto = true` 时，每新增 `learn.every` 条历史（默认 20），服务会在后台把最近 `learn.batch` 条（默认 50）转写和当前词典一起发给 LLM（使用 `[llm]` 的接口，模型可以用 `learn.model` 单独指定），让它找出：

- 值得加进词典的专有名词；
- 固定的识别错误（`wrong → right`）。

LLM 的建议在合并前会先过滤：纠错的 `wrong` 必须在最近的原始转写里逐字出现过，`right` 必须是词典里已有的词或这次新加的词，并且不允许把已有的正确词条改掉；`ignore` 里的条目一律跳过；每次最多新增 `max_new_terms` 个词和 `max_new_corrections` 条纠错。学习在后台进行，不影响转写请求；失败只记日志。也可以随时用 `stt learn` 手动触发并查看学到了什么。

学错了怎么办：`stt dict rm <词>` 或 `stt dict unfix <wrong>`，它们会进入 `ignore`，以后不会再被学回来。

## 网络与 Tailscale

### 方式 A：直接走 HTTP（最简单）

保持 `server.host = "0.0.0.0"`（或只绑定 tailnet IP，例如 `"100.101.102.103"`）、`auth.mode = "token"`。iPhone 上打开 Tailscale VPN 后，访问 `http://<magicdns-name>:8765/transcribe`。tailnet 内的流量已经由 WireGuard 加密。

### 方式 B：`tailscale serve` 提供 HTTPS

1. 在 Tailscale 管理后台 → DNS，开启 **MagicDNS** 和 **HTTPS Certificates**。
2. 让服务只监听本机：
   ```toml
   [server]
   host = "127.0.0.1"
   base_path = "/stt"            # 如果按路径挂载
   forwarded_allow_ips = "127.0.0.1"
   ```
3. 开启反代：
   ```bash
   # 挂在根路径：https://mybox.tail1234.ts.net/transcribe
   tailscale serve --bg 8765
   # 或者挂在 /stt 下：https://mybox.tail1234.ts.net/stt/transcribe
   tailscale serve --bg --set-path /stt http://127.0.0.1:8765
   tailscale serve status         # 查看；tailscale serve reset 可以清除
   ```
   设置了 `base_path` 后，`/stt/...` 和 `/...` 两种路径都能访问，所以不管 tailscale serve 转发时是否剥离前缀都可以正常工作。`X-Forwarded-For` / `X-Forwarded-Proto` 只信任来自 `forwarded_allow_ips` 的请求。
4. 鉴权二选一：
   - 继续用 token（推荐，同时适用于 CLI 和 OpenWhispr）。
   - 或者改为 `auth.mode = "tailscale"`：校验 tailscale serve 注入的 `Tailscale-User-Login` 头，可以用 `tailscale_users = ["you@example.com"]` 限定用户，快捷指令里就不需要带 token。这个模式要求 `server.host` 是回环地址，否则服务直接拒绝启动，因为别人可以直接连到端口伪造这个头。注意：这个头只对用户设备有效，打了 tag 的设备没有用户身份，不会带这个头。

### 超时建议

快捷指令的“获取 URL 内容”没有超时设置，等太久就会失败，所以整条链路最好控制在 30 秒以内：

| 配置项 | 建议值 | 说明 |
|---|---|---|
| `asr.remote.timeout_s` | 30 | 云端 Whisper 处理一分钟内的语音通常只需 1–5 秒 |
| `llm.timeout_s` | 10–15 | 超时直接退回原文，不会让请求失败 |
| `server.queue_timeout_s` | 20 | 单人使用基本不会排队 |
| `audio.decode_timeout_s` | 30 | |
| `client.timeout_s` | 120 | CLI 可以等更久（长音频） |

快捷指令适合几十秒到几分钟的短录音。很长的音频建议用 CLI。另外注意，很多云端接口单个文件的上限是 25 MB。

## iOS 快捷指令

新建快捷指令，依次添加：

1. **录制音频**：音频质量选“正常”即可；“开始录制”选“立即”，“结束录制”选“轻点时”。
2. **获取 URL 内容**：
   - URL：`http://mybox.tail1234.ts.net:8765/transcribe`（方式 A）或 `https://mybox.tail1234.ts.net/stt/transcribe`（方式 B）
   - 方法：`POST`
   - 头部：添加 `Authorization`，值为 `Bearer <你的 STT_TOKEN>`（`auth.mode = "tailscale"` 时不需要）
   - 请求体：**表单**
     - 添加字段，类型选 **文件**，键为 `file`，值选上一步的“录制的音频”
     - 可选：添加文本字段 `language` = `zh`，或 `clean` = `false`
3. **拷贝到剪贴板**：输入选“URL 的内容”。
4. 可选：**显示通知**，内容为“URL 的内容”，用来确认转写结果。

返回的是纯文本（默认 `format=text`），不需要解析 JSON。iPhone 上要先打开 Tailscale VPN。如果服务出错，返回的是 `{"error": ...}` 这段 JSON，会被原样拷到剪贴板，方便你看到出了什么问题。

**绑定 Action Button**（iPhone 15 Pro 及以后）：设置 → 操作按钮 → 滑到“快捷指令”→ 选择刚才建的快捷指令。按一下开始录音，轻点停止，结果就到了剪贴板。也可以加到锁屏、控制中心或“背面轻点”（设置 → 辅助功能 → 触控 → 轻点背面）。

## 用作 OpenWhispr iOS 的 Custom provider

OpenWhispr 的 Custom 转写 provider 要求的是一个 OpenAI 兼容的 `/v1/audio/transcriptions` 接口，正好对应 `POST /v1/audio/transcriptions`：

| OpenWhispr 设置 | 填写 |
|---|---|
| Base URL / Endpoint | `https://mybox.tail1234.ts.net/stt/v1`（方式 B）或 `http://mybox.tail1234.ts.net:8765/v1`（方式 A） |
| API Key | 你的 `STT_TOKEN` |
| Model | 任意，例如 `whisper-1`（服务端会忽略，使用配置里的模型） |

不同版本 App 里的字段名可能不一样：如果它要的是完整地址而不是 base URL，就填 `.../v1/audio/transcriptions`。这个接口默认不做 LLM 清理；需要的话把 `openai_compat.clean` 设为 `true`。

## 开发

```bash
uv sync
uv run pytest          # 测试需要 ffmpeg；ASR 和 LLM 用 fake 或 mock 实现，不访问网络
```

代码结构：

```
src/nanovoice/
  server.py        HTTP API、鉴权、排队、流水线、启动逻辑
  cli.py           stt 客户端
  config.py        TOML 配置（未知 key 会报错，避免拼写错误被悄悄忽略）
  audio.py         ffmpeg 转码 + VAD
  postprocess.py   词典 prompt、替换规则、LLM 清理
  dictionary.py    词典文件（热加载、校验、增删改）
  history.py       转写历史（JSONL）
  learn.py         自动学习 + 由用户改正推出纠错规则
  openapi.py       OpenAPI 规范 + /docs 页面
  backends/
    __init__.py    Backend 接口：transcribe(audio_path, language, prompt) -> str
    remote.py      OpenAI 兼容接口
    local.py       预留占位（未实现）
```

**添加新后端**（例如 FunASR SenseVoice）：在 `backends/` 下新建一个类，提供 `name`、`model` 和 `transcribe()`，然后在 `backends/__init__.py` 的 `create_backend` 里加一个分支，并把名字加进 `BACKEND_NAMES`。后端只会实例化一次并常驻内存，模型应该在 `__init__` 里加载。
