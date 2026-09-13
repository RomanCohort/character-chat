# 微信 → DSH 命令通道

在手机微信里给 DSH 下命令，跑的是**同一个 DSH**——同一套工具、同一套权限闸门、同一个工作目录。

## 用起来是什么样

```
你（微信）> *work 看一下 torusfold-physics 的测试为什么失败
她   > ……好，我等着。
她   > …运行 pwsh: pytest -x -q
她   > ……没读到。那个文件不存在，返回的是 not found，所以没有内容。
你（微信）> *work 那个 assert 改成 >= 1e-5 再跑一遍
她   > ……好了，回过去了。
```

第二条能接上第一条，因为在同一个 DSH 会话里。

## 命令

| 你发 | 说明 |
|---|---|
| `*work <任务>` | 交给 DSH 跑（按当前工作目录，同一会话） |
| `*work cwd <绝对路径>` | 换工作目录（每个目录一条自己的会话，换回去能接回） |
| `*work new` | 开新会话（清上下文） |
| `*work list` | 当前目录下最近会话 |
| `*work resume <前几位>` | 接回某条会话 |
| `*work status` | 会话 / 工作目录 / 忙不忙 |
| `*work help` | 帮助 |

子命令写成 `*work /cwd D:\x` 也认（斜杠只是留给肌肉记忆）。
不带 `*work` 的消息照旧是聊天，不会碰电脑。

**为什么不用 `/dsh`**：微信桥自己占了 `/cwd`、`/new`、`/sessions`、`/resume`、`/model`、`/mode`、`/exec`、
`/file`、`/status`、`/help`、`/yes`、`/no`（见它 `command-handler.js` 的分发链）。
子命令名字撞上去会走错门——最坏是"想让她换工作目录"变成"改掉整个 bot 的 cwd"。
`*work` 不在那张表里，一个都不撞。

## 她替你回话

回执、进度、结果都由**凌暮雪**说出来（走 `cli.send`，与正常聊天同一条人格管线）：

```
你 > !dsh 看一下那个测试为什么挂
她 > ……好，我等着。
她 > ……这一步没成。
她 > ……没读到。那个文件不存在，返回的是 not found，所以没有内容。
```

规矩三条：

- **只改口气，不改事实。** 转述的 prompt 里写死了：结果里写着失败/报错，就必须照实说没做成，
  不许美化；文件名、路径、报错原文、数字一律原样保留。`not found` 不会变成「搞定」。
- **转述失败就退原文。** 她那边报错/超时，直接把 DSH 的原始输出发出去，不吞。
- **技术性命令不转述。** `/cwd`、`/status`、`/list`、`/help` 原样回——那几条是给你看的路径和 id，
  经一层口气反而容易走样。

转述走 `skip_record=True`：那段 `[系统提示：...]` 不是你说的话，不会当真实对话写进记忆库
（之前 scheduler 的 prompt 就这么污染过 37 条 recall）。

## 链路

```
手机微信
  └─ 官方 iLink bot（wcab-bot 长轮询，D:\wechat-character-bridge）
       └─ POST 127.0.0.1:8000/chat          ← 你的 character_chat 服务
            ├─ 不以 !dsh 开头 → 凌暮雪（人设/记忆/心境，原样不动）
            └─ 以 !dsh 开头  → character_chat.wechat.bridge
                 ├─ 立刻回执「收到，跑着呢」
                 └─ 后台 → ACP v1 客户端 → `dsh --profile acp`（常驻进程）
                        └─ 结果/进度 → ilink bot API → 你的微信
```

要点：

- **只有一个轮询者**。iLink 的 `get_updates_buf` 游标是每账号一份，两个进程同时轮询会互相抢消息。所以命令通道**不新开轮询**，挂在已有的 `/chat` 上。
- **常驻进程**。ACP 连接一次拉起、长期复用；会话在 DSH 侧是持久化的，服务重启后走 `session/resume` 接回原来那条线。
- **出站绕开 bridge**。结果直接调 `ilink/bot/sendmessage`（与 `D:\ling-muxue-daemon\weixin_sender.py` 同一套协议），长回复按行切块，`context_token` 过期自动重取。
- **bot 自己发的消息不会回环**。bridge 按 `message_type` 过滤，BOT 消息直接跳过。
- **危险命令闸门还在**。`~/.dsh/profiles/acp/package.json` 里已装 `dsh-plugin-local-guardrails`（与 headless profile 同一份），
  它挂在 `tools/pre-execute` 上，命中 `dangerousCommands` 的命令在派发前就被拒，模型只看到理由。
  改这个 profile 的 bundles 之后要跑一次 `dsh plugin --profile acp install`，否则 bundle 解析不到、ACP 子进程会直接起不来。

## 代码

| 文件 | 职责 |
|---|---|
| `src/character_chat/wechat/acp_client.py` | ACP v1 客户端、会话池、update 投影（只把"助手正文 + 工具动作"给手机） |
| `src/character_chat/wechat/bridge.py` | 命令识别、元命令、异步派发、**回执先发后转述** |
| `src/character_chat/wechat/sender.py` | ilink 出站：分块发送、typing 指示器、**看 errcode 不看 HTTP** |
| `src/character_chat/server.py` | `/chat` 里的 DSH 分支、`_get_cli` 加载、转述器注册、lifespan 关闭钩子 |

## 进程与自愈（2026-09-12 重做）

桥必须一直活着，否则**入站全断**——你发什么它都收不到。
**判据**：`~/.weixin-mcp/accounts/<account>.sync.json` 的修改时间。长轮询每约 30 秒回来一次，
**连空响应也会写它**，所以"光标不动"= 没有进程在轮询。这一条比查进程表可靠得多。

### 现状：一个无限循环的看护

| 文件 | 作用 |
|---|---|
| `start_bridge.ps1` | 只做一件事：用 `Start-Process` + `.cmd` 包装器把桥拉起来（这条实测能跑） |
| `bridge_watch.ps1` | 每 30 秒看一次光标温度，冷了就叫 `start_bridge.ps1`；否则写心跳 |
| `logs/bridge_alive.txt` | 心跳。**它会不会更新，就是看护活没活的唯一证据** |
| `logs/bridge_watch.log` | 看护的动作记录 |

启动看护：

```powershell
Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','D:\character_chat\bridge_watch.ps1' -WindowStyle Hidden
```

### 四条别走的死路（都实测过）

1. **别用 agent 的后台任务启动桥。** 它会成为那个 job 的子进程，DSH 的 job 管理器在宿主
   重载/取消时把它一起带走。表现是"桥活着但不转发"，或直接消失。
2. **别指望 `watchdog.py` 管桥。** 它的 `check_bridge()` 永远返回 True（跳过桥）；
   而 `start_bridge()` 调的 `node_modules\.bin\wcab-bot` 是 bash 包装脚本，Windows 上起不来。
   它管 `character_chat`/scheduler 是有用的，**桥不管**。
3. **别用 `schtasks` 的看护任务（实测不工作）。** 这台笔记本上：默认带
   `Stop On Battery Mode, No Start On Batteries`，用电池时**任务根本不启动**；
   加上 `-AllowStartIfOnBatteries`、手工 `/run` 之后，脚本在那套上下文里依然写不出文件。
4. **别在进程表里 grep `wcab-bot` 判活。** 检测命令自己的命令行里就含这个词，
   脚本会把自己当成桥、永远判"活着"、一次都不重启。**这个假阳性真踩过。**

### `agent-browser` 在这台机器上起不来（2026-09-13 实测）

装在 `C:\nvm4w\nodejs\agent-browser.cmd`（npm 全局，0.27.0）。**当前不可用**，别在上面耗时间：

- **启动就失败**：`✗ Auto-launch failed: Chrome exited early (exit code: 0) without writing DevToolsActivePort`。
  它自己提示 `--args "--no-sandbox"`，但**加了也起不来**，原因未查明。
- **`--args` 有个自锁**：该参数只在**启动 daemon 时**生效。而 `close` 本身也要先启一个 daemon，
  所以"先 close 再用 --args"这条路走不通——每次都得到 `⚠ --args ignored: daemon already running`。
- **会疯狂泄漏 chrome 进程**：一次 `open` 探针就攒下 **49 个** orphan `chrome.exe`。用完必须清：

  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
    Where-Object { $_.CommandLine -like '*agent-browser*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  ```

  **只按 `Name='chrome.exe'` + 命令行含 `agent-browser` 过滤**，不要写宽匹配
  （`CommandLine -match 'agent-browser'` 不限进程名会匹配到 agent 自己那条链，实测把执行器打崩过）。
- **别用 PowerShell 的 `*>` / `| Out-Null` 接它的输出**：daemon 继承句柄，管道永远不收 EOF，
  命令直接挂死。唯一不挂的接法是 `Start-Process -RedirectStandardOutput/Error`：

  ```powershell
  Start-Process -FilePath 'cmd.exe' -ArgumentList '/c ...' `
    -RedirectStandardOutput $o -RedirectStandardError $e -NoNewWindow -PassThru
  ```

**要浏览器能力时的替代方案**：DSH 自带的 ego-browser 工具族（`ego_navigate` / `ego_snapshot` 等），
不需要这个 CLI。

### 开机自启

`LingMuxueAutoStart` 计划任务已注册（登录时触发），但**同样受电池那条影响，不保证**。
重启电脑后如果微信没反应，手动跑一次看护即可（上面那条命令），它会拉起来并一直看着。

### 一个坑：给桥打的补丁会被包管理器覆盖

`wechat-ai-code-bridge/dist/bot/claude-session.js` 里的 `REQUEST_TIMEOUT_MS` 已从写死的
`60_000` 改成 `Number(process.env.CHARACTER_CHAT_TIMEOUT_MS || 600_000)`。
**重装/升级这个 npm 包会覆盖补丁**，重装后要再打一次。

## 配置（环境变量，都有默认值）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSH_WECHAT_PREFIX` | `*work` | 触发词 |
| `DSH_WECHAT_CWD` | `D:\character_chat` | DSH 会话的工作目录 |
| `DSH_WECHAT_HEARTBEAT_S` | `90` | 多久没动静报一次平安；0 关 |
| `DSH_WECHAT_PROGRESS_CAP` | `30` | 单轮进度消息上限 |
| `DSH_HARNESS_ROOT` | `D:\deepseek-harness` | 仓库位置 |
| `DSH_CLI` | `<root>\apps\cli\lib\bin.js` | CLI 入口 |
| `DSH_PROFILE` | `acp` | 用哪个 profile |
| `DSH_WECHAT_PROMPT_TIMEOUT` | `1800` | 单轮上限（秒） |
| `DSH_WECHAT_LOG` | `D:\ling-muxue-logs\dsh_bridge.log` | 日志 |
| `DSH_WECHAT_STATE` | `D:\character_chat\data\dsh_bridge_state.json` | 微信会话 ↔ DSH 会话映射 |
| `CHARACTER_CHAT_TIMEOUT_MS` | `600000` | 桥等 `/chat` 回信的上限（打在那个 npm 包里） |

## 测试

```bash
# 离线自检：接线、前缀、发送目标、分块
python scripts/dsh_wechat_selftest.py

# 协议层：多轮连续性 + 重启后 resume
python scripts/dsh_acp_continuity.py

# 真跑一轮（结果发到你微信）
python scripts/dsh_wechat_e2e.py "在 D:\\character_chat 下建 probe.txt，写 ok"
```

## 排障

- **回了「❌ Claude 错误: character_chat request timed out」** → 微信桥那侧的超时是**写死的 60 秒**
  （`node_modules/wechat-ai-code-bridge/dist/bot/claude-session.js`，原本 `REQUEST_TIMEOUT_MS = 60_000`）。
  凌暮雪首轮要载 embedding，没网时在 huggingface 上超时重试 5 轮（约 1 分钟），必被掐断。
  两处都已处理：
  1. 该文件打成 `Number(process.env.CHARACTER_CHAT_TIMEOUT_MS || 600_000)`（10 分钟）。
     **重装/升级这个 npm 包会覆盖补丁**，重装后要再打一次。
  2. `server.py` 启动后起了个后台任务预构造凌暮雪实例，第一条消息不用现等（实测 12s → 2.9s）。
  注意：超时被掐断**不影响任务本身**——后台照跑，结果照发。看到的 ❌ 只是"等回信"这一步断了。
- **`*work.` 带句点不触发** → 触发词要求 `*work` 后面跟**空格或行尾**。`*work. 帮我查…` 会掉进聊天通道
  （凌暮雪会回你"调查什么呀？"）。写成 `*work 帮我查…`。
- **微信一直收不到**（最常见）→ 先跑 `python scripts/dsh_send_diag.py --send`。
  ilink 的失败是 **HTTP 200 + JSON 里带 `errcode`**，只看 HTTP 状态会把失败当成功——
  这个坑踩过一次：20 条消息全沉了，日志里却写着「发送成功」。
  - `{"errcode":-14,"errmsg":"session timeout"}` = **平台侧会话过期**。bot 长时间没在线就会这样，
    重试和换 context_token 都没用；bridge 也会撞同一个 -14 然后自己暂停 60 分钟。
    恢复只有一条路：重新登录 bot（重新扫码）。`scripts/weixin_relogin_loop.mjs` 是循环出码的版本，
    会把最新二维码固定写到 `D:\ling-muxue-logs\weixin_qr.png`。
  - `errcode=-2` = context_token 过期，这个能自动重取重试。
- **`errcode=-2 'prepare failed'` 连片出现**（2026-09-12 23:30–23:31，12 条全挂）
  → **至今未查明原因**，但下面两条已经实测排除，别再往这两个方向查：
  - **不是"窗口太短"。** 实测消息发出后 **40 分钟**仍然能发成功
    （00:27:34 成功，上一条消息是 23:46:59）。早期"窗口只有几分钟"的说法是错的。
  - **不是 context_token 陈旧。** 当时旧代码在每次重试前**已经**在重取 token，重取了照样 -2。
    所以"换个新 token 就能救"不成立。
  - 该批次在 23:31:35 自行停止，23:46:59 学长发来一条消息后 23:48:19 恢复成功——
    **但恢复的触发点不明确**，不要当成"发条消息就能修"。
  - 现在的应对是**拉开重试间隔**（0.5/1/2s，见 `sender.py`）并在每次失败记
    token 指纹（`token=xxxxxxxx`），下次复发时用来分辨"换了 token"还是"同一 token 反复失败"。
- 回执是「收到，跑着呢」但微信没等到结果 → 看 `D:\ling-muxue-logs\dsh_bridge.log`，
  现在失败会写 `发送失败 chunk=... errcode=...`，不再假报成功。
- 回执是机械的那句、不是她的口气 → 凌暮雪的实例还没建（`GET /` 看 `active_sessions` 是不是 0）。
  正常情况下启动后会后台预热，不该出现；真出现了看服务日志里有没有 `凌暮雪预热失败`。
- `/chat` 回的 `character` 不是 `dsh` → server 没重启，还跑着旧代码。
- 报 `no weixin account` / `没有 context-tokens` → bot 的登录态没了，重新扫码登录 bridge。
- 想换默认工作目录：改 `DSH_WECHAT_CWD` 后重启 server；或随时 `*work cwd <绝对路径>`。
