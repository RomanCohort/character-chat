# DSH 接入层

让 `character_chat` 跑在 DSH 上的那层配置。DSH 本体不在这个仓库里（它有自己的
仓库和版本节奏），这里只放**配置、钩子、人格、preset**——几十 KB，能照着重装。

搬一台新机器要动的东西全在这一层。没有它，`src/character_chat/wechat/` 里的代码
不知道去哪找 DSH、用哪个人格、拿哪个 key。

## 目录对应关系

| 本目录 | 搬到哪 |
|---|---|
| `cordis.patch.yml` | `~/.dsh/cordis.patch.yml` |
| `settings.yaml` | `~/.dsh/settings.yaml` |
| `.credentials.example.yaml` | `~/.dsh/.credentials.yaml`（**填完再去掉 `.example`**） |
| `hooks.json` | `~/.dsh/hooks.json` |
| `hooks/persona-context.mjs` | `~/.dsh/hooks/persona-context.mjs` |
| `agent-presets/milivia/` | `~/.dsh/.agent-presets/milivia/` |
| `agent-presets/milivia-chat/` | `~/.dsh/.agent-presets/milivia-chat/` |
| `agent-presets/liangshen/` | `~/.dsh/.agent-presets/liangshen/` |
| `persona/milivia.md` | `~/.dsh/persona/milivia.md` |
| `persona/milivia-world.md` | `~/.dsh/persona/milivia-world.md` |
| `persona/active.txt` | `~/.dsh/persona/active.txt` |
| `profiles/web/cordis.patch.yml` | `~/.dsh/profiles/web/cordis.patch.yml` |
| `profiles/acp/cordis.patch.yml` | `~/.dsh/profiles/acp/cordis.patch.yml` |

## 人格是怎么进去的（容易看错的一步）

人设**不在** preset 里。preset 只留一句身份锚点兜底。

真正的注入链路：

```
DSH 每轮提交用户消息
  → UserPromptSubmit 钩子（~/.dsh/hooks.json）
  → hooks/persona-context.mjs
  → 读 ~/.dsh/persona/active.txt 拿文件名
  → 读 ~/.dsh/persona/<那个文件>
  → 作为 additionalContext 放在**上下文末尾**
```

为什么这么绕：放在上下文末尾，人设的优先级才压得住工具描述和 harness 的通用口吻。
一开始只在 system prompt 前缀里放一次，位置太靠前，长会话里会被冲淡。

换人格不用改代码：`echo liangshen > ~/.dsh/persona/active.txt`。
关掉注入：把环境变量 `DSH_PERSONA_FILE` 设成空串。

`persona-context.mjs` 永远 `exit 0`，任何异常都输出 `{}`——钩子挂掉不能把整轮对话带崩。

## 三个 preset

| preset | 用途 | 工具 |
|---|---|---|
| `milivia` | 干活（默认） | 全套 |
| `milivia-chat` | 纯角色扮演、沉浸聊天 | 一个都不挂 |
| `liangshen` | 「梁神模式」，整活 | 全套 + 自定义 bash 工具 |

`settings.yaml` 里 `agent-presets.default: milivia` 决定默认用哪个。

> `liangshen` 是第三方 preset，`NOTICE` 是它自己的声明，改之前先看那个文件。

## 权限

`settings.yaml` 里是 `danger-full-access`（沙箱不限制写、审批 `never`）。
这是为了让微信端下命令时不用人守在电脑前点确认。**代价是任何命令都会直接执行。**
介意的话改成 `workspace-write` + `approval: ask`。

## 要改的占位符

搬机器时这几个地方不能照抄：

1. `hooks.json` — `%USERPROFILE%` 换成你的 home，或保留（Windows 上能用）。
2. `cordis.patch.yml` — `<ling-muxue-daemon>` 换成 `memory_mcp_server.py` 所在目录。
   这个 MCP server 不在本仓库。
3. `profiles/web/cordis.patch.yml` — `configPath: ~/.dsh/hooks.json`，
   某些版本不认 `~`，那就写绝对路径。
4. `.credentials.example.yaml` — 填自己的 key。

## ⚠ 凭据

`.credentials.yaml` 里是**明文** API key。本仓库 `.gitignore` 排除了它，
只提交 `.credentials.example.yaml`。

- 别 `git add -f` 它。
- 别贴进聊天记录或 issue。
- 真泄露了：去平台吊销重发，别只删文件——进了 git 历史就删不干净。

## ⚠ 局域网暴露

`profiles/web/cordis.patch.yml` 把 GUI 绑到 `0.0.0.0:3080`，同一网络下**任何设备
都能打开**，而且当时是 `danger-full-access`。手机连着方便，代价是这个。
不需要就删掉那段，回到 `127.0.0.1`。

## 微信通道

装了这层之后，`*work` 那条链才有得跑：

```
微信 → wechat-ai-code-bridge → POST /chat → 8000 服务
     → dsh --profile acp（JSON-RPC over stdio）
     → 结果绕过 bridge 直发 ilink
```

细节、坑、四条实测死路都在 `docs/wechat-dsh-channel.md`。
