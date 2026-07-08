# Character Chat 终端版 AI 角色

![Project Status](https://img.shields.io/badge/Phase-1.5,%20时间%2B场景%20意识-green)
[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-orange)](https://platform.deepseek.com/)

终端版 AI 角色扮演应用（类似猫箱/星野/Glow），支持可定制性格/背景、叙事时间和自然场景转移，使用 DeepSeek API。

## ✅ Phase 1 完成 (骨架 + LLM + CLI)

- ✅ 项目结构（`/src /tests /config /scripts /docs`）
- ✅ LLM 客户端（DeepSeek 包装）
- ✅ 角色卡 schema（YAML 文件）
- ✅ 角色加载器
- ✅ 基础 CLI REPL（无情感/记忆）

## ✅ Phase 1.5 完成 (时间 + 场景意识)

基于你的关键反馈「现有产品对场景与时间的意识不足」，我们在 Phase 1.5 引入了**叙事时间轴**和**场景状态机**：

### 新增功能（Phase 1.5）

| 模块 | 功能 | 说明 |
|---|---|---|
| **WorldClock** | 叙事计时器 | 每轮对话自动推进虚拟时间（可自定义步长），跟踪日/分钟/时段（深夜/晨/午/黄昏/夜），注入到 prompt |
| **SceneManager** | 场景状态机 | 场景是时间相位对象，支持相位转移规则（黄昏→夜、夜→深夜），用 LLM 输出的标签驱动转移 |
| **EventBus** | 事件总线 | 同步 pub/sub 系统，时间推进/场景转移发出 `TIME_ADVANCE`/`SCENE_TRANSITION` 事件，支持优先级排序 |
| **SceneParser** | 标签解析器 | 解析 LLM 输出的 `<scene:新场景名,phase:新相位>` 标签，容错多种格式 |
| **Prompt 增强** | 时间/场景注入 | 系统提示词自动注入「当前叙事时间+场景」，并给出时段行为提示（黄昏柔和、深夜内向） |

### 交互示例（相位依赖）

```bash
$ python -m character_chat

[已加载角色：小夜]
🕌 第1天 17:00（黄昏）| 🎬 天台

小夜> 你又来了啊...哼，才不是在等你。

你> 我们走吧，天台风太大了
小夜> ...好吧。（站起来）  *裹紧外套*

[第1天 17:20（黄昏）| 🎬 天台]

你> 我们回家吧
小夜> 嗯，走吧。<scene:家,phase:夜>

[第1天 17:40（黄昏）| 🎬 家（夜）]
# 场景转移成功！黄昏的冷风变为夜晚的私密感
小夜> 终于到家了...今天真是辛苦你了。  （小声）

you> /time
# ... 用户可查看时间（虽然现在是叙事时间）
```

### 关键设计决策

**叙事时间 vs 真实时间**：
- 不依赖现实时钟。时间按对话轮次推进一小段（默认每轮 15-30 分钟），模拟「一直在聊，时间流逝了」的感觉。
- 这样 CLI 体验顺滑（你不用等 6 小时），又保留了「时间流逝」的实际影响。

**LLM 输出标签驱动场景转移**：
- Prompt 明确告知 LLM：「若场景自然变化，请在回复末尾附加场景标签 `<scene:新场景名,phase:新相位>`」
- CLI 解析回复中的标签，由阶段转移规则决定是否允许转移（黄昏不能直转晨，需经过下午）
- 容错：容忍空格、大小写、无相位的情况。

### 架构复用

- **EventBus**：lift CLF `core/event_bus.py`（轻量级同步 pub/sub + 优先级）
- **时间相位映射**：参考 CLF 海马体 θ 节律相位分离概念，但抽象为规则驱动
- **场景相位规则**：与 CLF 的角色行为受时段影响的思想一致（只是目前是 prompt 级，未来可接情感分类）

## 🎯 功能路线图（计划）

| Phase | 功能 | Time |
|---|---|---|
| 1 | 骨架 + LLM + CLI | ✅ 今天 |
| 1.5 | 时间 + 场景意识 | ✅ 今天 |
| 2 | 情感三层管线（规则 + 神经 + 标签） | 2-3 天 |
| 3 | 长时记忆 + 海马体巩固 | 2-3 天 |
| 4 | 人格演化 | 1-2 天 |
| 5 | 抛光/增强 | 1-2 天 |

## 📦 安装

### 1. 克隆/创建项目

```bash
cd D:\character_chat
```

### 2. 安装依赖

```bash
pip install -e .
```

依赖包括：
- `pydantic≥2.0` — 数据验证
- `openai≥1.0` — DeepSeek API
- `sentence-transformers≥2.2` — 情感分类器（Phase 2）
- `torch≥2.0` — 深度学习（Phase 2）
- `sqlite-vec≥0.1` — 向量存储（Phase 3 battlefield）
- `loguru≥0.7` — 日志
- `ruamel.yaml≥0.18` — YAML 解析

### 3. 配置 DeepSeek API Key

```bash
# 先复制示例配置
cp config/config.example.yaml config/config.yaml

# 编辑 config.yaml，填入你的 DeepSeek API Key
# llm.api_key: "sk-xxxxx..."
```

### 4. （可选）添加角色卡

在 `config/characters/` 放新的 `.yaml` 文件，参考 `小夜.yaml`。

## 🚀 运行

### 启动 CLI

```bash
# 使用第一个可用角色
python -m character_chat

# 指定角色
python -m character_chat -c 小夜
```

### 命令列表

| 命令 | 功能 | Phase |
|---|---|---|
| `/list` | 列出可用角色 | ✅ |
| `/switch <name>` | 切换角色 | ✅ |
| `/mood` | 查看情感状态 | 2+ |
| `/remember <text>` | 强制记忆 | 3+ |
| `/sleep` | 触发记忆巩固 | 3+ |
| `/quit` | 退出 | ✅ |

### 交互示例

```bash
$ python -m character_chat

[已加载角色：小夜 | 放学后的天台]
小夜> 你又来了啊...哼，才不是在等你。

你> 我带了你想看的漫画
小夜> ...哼，谁要看啊。（伸手）

你> /list
[可用角色] 小夜, α

你> /switch α
[切换到角色：α | 赛博朋克公寓]

你> 今天天气不错
α> 系统同步率：98.4%

你> /mood
[提示] 情感模块在 Phase 2 实现

你> /quit
[再见]
```

## 📁 项目结构

```
D:\character_chat\
├── src\
│   └── character_chat\
│       ├── __init__.py
│       ├── __main__.py          # python -m entry
│       ├── cli.py               # REPL 主循环
│       ├── config.py            # 配置加载
│       ├── llm\
│       │   ├── __init__.py
│       │   └── client.py        # DeepSeek API 客户端
│       └── character\
│           ├── __init__.py
│           ├── schema.py        # 角色卡 schema
│           └── loader.py        # YAML 加载器
├── config\
│   ├── config.example.yaml      # 配置模板
│   └── config.yaml              # 你的密钥（gitignored）
├── characters\
│   ├── 小夜.yaml                # 示例角色卡
│   └── ...
├── tests\
│   ├── test_character.py
│   ├── test_llm.py
│   └── test_cli.py
├── scripts\
│   └── init_db.py               # 初始化 sqlite-vec（Phase 3）
└── pyproject.toml
```

## 🔧 实现机制

### LLM 客户端

直接包装 OpenAI SDK 作为 DeepSeek API：

```python
- api_key: 从 config.yaml 读取
- base_url: https://api.deepseek.com/v1
- model_id: deepseek-chat
- temperature: 0.7（可调）
```

### 角色卡 Schema

基于 Pydantic，扩展自 IGEM-sama 的 `CharacterConfig`，添加：

```yaml
- background: 角色背景（情感相关）
- personality: trait 值（Phase 2 加入）
- relationships: 与用户的关系配置
- expression_tags: 情感 → 表情标签映射（Phase 2）
```

### CLI 实现

- ANSI 颜色渲染舞台指示（`*动作*` → 灰色，`（语气）` → 灰色）
- 命令解析：`/switch`, `/list`, `/help`, `/quit`
- 会话状态：`history` 列表保存最近 `max_history` 条对话
- 系统提示词组装（基础版 - Phase 4 增强人格/记忆/情感注入）

## 📚 复用来源

| 模块 | 来源 | 操作 |
|---|---|---|
| LLM 客户端 | IGEM-sama `llm_sync.py` | ✅ Lift 直接复制（简化版） |
| 角色卡 schema | IGEM-sama `character/config.py` | ✅ Lift + 扩展 |
| 角色加载器 | — | ✅ 新建 |
| CLI REPL | — | ✅ 新建 |
| 短期记忆压缩 | IGEM-sama `short_term.py` | Phase 3 Lift |
| 长期记忆 | IGEM-sama `long_term.py` | Phase 3 Lift |
| 情感追踪 | IGEM-sama `emotion/tracker.py` | Phase 2 Lift |
| 情感分类器 | CLF `train_real.py` + `limbic.py` | Phase 2 Port |

## 🔜 Phase 2：情感管线

三层组合：

1. **规则追踪器**：基于关键词 + EMA 融合
2. **神经分类器**：CLF 的 `all-MiniLM-L6-v2` + `AmygdalaNucleus`（5 维情感概率）
3. **冲突消歧器**：合并规则与神经信号
4. **标签系统**：情感 → `*动作*` `（语气）` 映射

## 📖 技术文档

- `docs/architecture.md` — 完整架构设计（待创建）

## 🙏 致谢

本项目复用了以下开源工作的优秀设计：

- **IGEM-sama (ZerolanLiveRobot)** 的 LLM pipeline、情感追踪、人格演化
- **Civis Lucri-Faber (CLF)** 的情感分类器和小海马体概念
- **cat-box / 星野 / Glow** 的 UI/UX 设计理念

本项目是自己实验性质的衍生品，遵循 "玩火" 原则（仅供学习和实验室测试）。
