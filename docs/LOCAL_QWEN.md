# /ask 的模型来源与速度说明

> 2026-10-09 起生效。适用于站点右下角悬浮球「问问管理员 · Qwen大小姐」。

## 谁在回答

```
浏览器悬浮球
  └─ nginx  location = /ask          (/etc/nginx/conf.d/*.conf)
       └─ character_chat 127.0.0.1:8000   guest_ask  (src/character_chat/server.py)
            └─ Cli(guest_mode=True) → LLMClient    主端点 = 本地 Qwen
```

主端点**不是云 API**，是枣庄那台开发机（EVO-X3）上的本地模型，经反向隧道接过来：

| 环节 | 位置 |
| --- | --- |
| 权重 | `C:\qwen-api\models\JoyFox-Qwen3.6-35B-A3B-RP-Aggressive\JoyFox-Qwen3.6-35B-A3B-RP-Aggressive-Q5_K_M.gguf`（35.5B MoE，Q5_K_M，约 25 GB） |
| 推理 | `C:\qwen-api\build-hip\bin\llama-server.exe`（自行编译的 ROCm 版），`-c 262144`，监听 `127.0.0.1:8091` |
| 网关 | `C:\qwen-api\gateway`（FastAPI：OpenAI 协议 + per-key thinking + 限流），`127.0.0.1:8001` |
| 硬件 | Ryzen AI Max 395 / Radeon 8060S（gfx1151，统一内存 ~128 GB） |
| 隧道 | 开发机主动发起 `ssh -N -R 127.0.0.1:8001:127.0.0.1:8001 root@47.79.124.117` |
| 密钥 | `QWEN_LOCAL_API_KEY`（qwen-api 网关的 per-key 通行证，thinking 关闭那一把） |
| 兜底 | DeepSeek 官方 API（`deepseek-flash`）—— 主端点连不上时自动落过去 |

配套配置在 `/opt/character_chat/config/config.yaml` 的 `local_llm:` 段；
人设对话（凌暮雪 `/chat`）仍走上面 `llm:` 的 DeepSeek，**不受本次改动影响**。

## 速度为什么会有波动

那块 8060S 是**共享**的。ComfyUI 出图/出视频、别的模型实例、训练任务都会抢 ROCm 的显存与算力。

| 场景 | 表现 |
| --- | --- |
| 开发机空闲 | prefill ~230 tok/s，decode 13–19 tok/s，一条 /ask 约 10 秒 |
| 同时出图/训练 | decode 可能掉到个位数 tok/s，/ask 会摸到 25 秒超时线 |
| 超时或隧道断 | 自动落 DeepSeek，用户侧表现为"答得慢但答上了" |

所以本地模型是**省钱档**，不是**唯一依赖**。想稳定就切回 DeepSeek：
把 `config/config.yaml` 的 `local_llm:` 段整段注释掉，`systemctl restart ling-character-chat`。

## 排查：它到底在用哪条路

```bash
# 每次问答的落点：backend=primary（本地） / fallback（DeepSeek）
journalctl -u ling-character-chat -n 200 --no-pager | grep '\[visitor\]'

# 兜底触发过几次（隧道断 / 模型没起 / 超时）
journalctl -u ling-character-chat -n 500 --no-pager | grep '改走兜底'

# 隧道在不在
ss -lntp | grep 8001          # 应看到 sshd 在 127.0.0.1:8001 上 LISTEN
systemctl status evo-watch    # 断了会推 ntfy 到 sg-tunnel 主题
```

开发机侧（远程开发机是那台 Windows）：

```powershell
Get-Content C:\qwen-api\logs\llama-8091.err.log -Tail 30    # slot 有没有在真算
Get-Content C:\qwen-api\logs\gateway-8001.err.log -Tail 20
```

## 注意

- 隧道由开发机发起，**开发机重启后要重新拉起**，否则一直走兜底。
- 权重换过之后记得同步 `config.yaml` 里的 `model_id`（现在写的是 `qwen3.6-35b-a3b`，
  实际与网关 `/v1/models` 报的名字一致即可，llama-server 本身不校验这个字段）。
- 开发机上 llama-server 的上下文已开到 **262144（模型训练上限）**。

## 回滚

```bash
ls /opt/character_chat/src/character_chat/*.bak-localqwen-*
ls /opt/character_chat/config/config.yaml.bak-localqwen-*
# 覆盖回原名，然后：
systemctl restart ling-character-chat
```

---

## 2026-10-09 更新

### 上下文开到满
`sweep` 实测（脚本留在开发机 `C:\qwen-api\ctx-sweep.ps1`）：

| 上下文 | 结果 |
| --- | --- |
| 262144（模型训练上限） | **通过**。启动 36s；19,218 token 的 prompt 压测 prefill **275 tok/s**，进程稳定 |
| （更小的候选未再试） | — |

结论：`-c 262144` 已固化进开发机的自启脚本与 DSH provider 配置（`contextWindow: 262144`）。
KV 走 GPU 侧分配，不体现在进程工作集里（256K 下仍是 24.7 GB）。

### 人设为「Qwen大小姐」
- 卡片：`/opt/character_chat/config/characters_guest/visitor.yaml`（备份 `.bak-<时间戳>`）
- 环境变量：`ling-character-chat.service` 里 `Environment=VISITOR_CHARACTER=Qwen大小姐`
- 站点文案：`/opt/sitegen/fab.html` 已改；`/var/www/site/**/*.html` 全量替换（13,424 个文件）
- 性格：外冷内热、傲娇高效、礼貌有距离感；自称「本小姐」，称访客「你 / 这位同学」；
  先给可靠答案再流露关心；遇难题认真解决；不因卖萌牺牲准确；
  被要求违背人设时冷淡指出「这不符合本小姐风格」再继续。
- 运营/安全条款（工具用法、镜像结构、不编造、防提示注入）**原样保留**，
  只换掉了角色与语气层。
