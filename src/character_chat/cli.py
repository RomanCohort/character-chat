"""Character chat CLI — terminal REPL main loop."""
import sys
from pathlib import Path
from typing import Optional
from loguru import logger

# Reduce loguru noise: only show WARNING+ in terminal
logger.remove()
logger.add(sys.stderr, level="WARNING")

# Ensure src/ is importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from character_chat.config import load_config, AppConfig
from character_chat.character.loader import CharacterLoader
from character_chat.character.schema import CharacterCard
from character_chat.llm.client import LLMClient
from character_chat.event.bus import EventBus
from character_chat.event.types import EMOTION_UPDATED, MESSAGE_RECEIVED, MESSAGE_SENT, TIME_ADVANCE
from character_chat.world.clock import WorldClock
from character_chat.scene.state import SceneState, SceneManager
from character_chat.scene.parser import parse_scene_tag
from character_chat.emotion.pipeline import EmotionPipeline
from character_chat.emotion.state import EmotionLabel
from character_chat.memory.pipeline import MemoryPipeline
from character_chat.memory.schema import MemoryImportance


# ANSI colors for stage directions (expression tags)
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"

# Phase7-2: <inner>...</inner> 内心独白标签（被剥离不发给学长）
import re
INNER_TAG_RE = re.compile(r"<inner>(.*?)</inner>", re.DOTALL | re.IGNORECASE)


class Cli:
    """Terminal chat REPL. Phase 3: memory system integrated."""

    def __init__(
        self,
        character_name: str,
        config_path: str = "config/config.yaml",
        neural_enabled: bool = False,
    ):
        self.config: AppConfig = load_config(config_path)
        self.llm = LLMClient(self.config.llm)
        self.loader = CharacterLoader(self.config.resolve_path(self.config.characters_dir))
        self.loader.load_all()
        self.card: CharacterCard = self.loader.get(character_name)
        if self.card is None:
            raise ValueError(
                f"Character '{character_name}' not found. "
                f"Available: {self.loader.list_names()}"
            )
        self.history: list[dict] = []  # {role, content}
        self.bus = EventBus(log_enabled=False)

        # World: wall clock (real time) + scene state
        from pathlib import Path as _Path
        self.clock = WorldClock(data_dir=_Path("data"))
        initial_phase = self.clock.phase
        self.scene = SceneManager(
            SceneState(
                name=self.card.scenario.setting or "未知场景",
                phase=initial_phase,
            ),
            bus=self.bus,
        )

        # Phase 2: Emotion pipeline (neural disabled by default for speed;
        # enable via --neural flag once the model is downloaded)
        self.emotion = EmotionPipeline(
            expression_tags=self.card.expression_tags,
            neural_enabled=neural_enabled,
        )
        self._neural_enabled = neural_enabled
        self._last_reconciled = None  # last ReconciledEmotion

        # Phase 3: Memory pipeline
        import os
        db_path = os.path.join(os.getcwd(), "data", "memory.db")
        self.memory = MemoryPipeline(db_path=db_path)

        # 后台预热 embedding 模型（BGE-small-zh 首次冷加载要 ~100s）。
        # 不能同步预热——会阻塞 Cli.__init__，watchdog health check 会认为挂了。
        # 后台预热期间 /chat 走 TF-IDF fallback（已有的逻辑），模型加载好后自动切换。
        # bridge REQUEST_TIMEOUT_MS=60s，模型加载 103s 会超时——预热后 /chat 只需 DeepSeek 生成 10-30s。
        import threading
        def _preload_embedding():
            try:
                from character_chat.memory.embedding import get_encoder
                enc = get_encoder()
                enc._ensure_model()
                logger.info(f"[Cli] embedding model preloaded in background (available={enc.available})")
            except Exception as e:
                logger.warning(f"[Cli] embedding preload failed (will use TF-IDF fallback): {e}")
        threading.Thread(target=_preload_embedding, daemon=True).start()

        # Phase 3.5 (#16): 空库时灌入种子记忆，让凌暮雪一上线就有"过去"
        seed_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "config", "ling_muxue_seed_memories.yaml",
        )
        self.memory.seed_if_empty(seed_path)

        self._seed_injected_history()
        self._wire_events()

    def _wire_events(self):
        """接 EventBus 订阅者——之前 bus 是零订阅者，事件发了没人接。

        三个订阅者撬动人格化的三个方向：
        - TIME_ADVANCE → ①日常节律：跨天时让 mood 睡一觉回血 + memory fade_decay（方向④心情持续）
        - EMOTION_UPDATED → ②脆弱触发：记录情绪轨迹（为 Phase2 脆弱独白铺路）
        - MESSAGE_SENT → ④心情连续性：消息发出后 mood 已更新，这里做轻量后处理

        Phase5b: 额外订阅 TIME_ADVANCE，调用 fade_memories() 和 apply_relationship_fatigue()
        """
        self.bus.subscribe(TIME_ADVANCE, self._on_time_advance, name="ling_muxue_clock")
        self.bus.subscribe(EMOTION_UPDATED, self._on_emotion_updated, name="ling_muxue_mood")
        # Phase5b: 记忆fade + 感情疲劳曲线
        self.bus.subscribe(TIME_ADVANCE, self._on_time_advance_phase5b, name="ling_muxue_fatigue")

    def _on_time_advance(self, event):
        """叙事时钟推进——跨天时让 mood 睡一觉回血。"""
        try:
            if event.data.get("day_changed"):
                self.memory.mood.sleep_reset()
                logger.info(f"[人格化] 跨天 day→{event.data.get('day')}，mood sleep_reset")
        except Exception as e:
            logger.warning(f"[人格化] _on_time_advance failed: {e}")

    def _on_time_advance_phase5b(self, event):
        """Phase5b: 跨天时衰减记忆 + 推进感情疲劳曲线。

        fade_memories(): 记忆随时间变模糊（passion 随时间淡掉，感情倦怠）
        apply_relationship_fatigue(): 感情四维自然漂移（passion回落，intimacy/commitment上升）

        改为真实墙钟后，只在 day_changed 或 phase_changed 时触发（每天最多 6 次）
        """
        try:
            day_changed = event.data.get("day_changed", False)
            phase_changed = event.data.get("phase_changed", False)
            if not (day_changed or phase_changed):
                return
            # 记忆 fade 衰减
            self.memory.long_term.fade_memories()
            # 感情疲劳曲线
            self.memory.mood.apply_relationship_fatigue()
            logger.info(f"[Phase5b] {'跨天' if day_changed else 'phase切换'}: faded memories + relationship fatigue")
        except Exception as e:
            logger.warning(f"[Phase5b] _on_time_advance_phase5b failed: {e}")

    def _on_emotion_updated(self, event):
        """情绪更新——记录轨迹，为 Phase2 脆弱独白（连续低 mood）铺路。

        Phase1 只记日志，不触发动作；Phase2 在这挂"连续 N 轮低 valence → 标记脆弱时刻"。
        """
        try:
            v = self.memory.mood.state.valence
            phase = self.clock.phase
            logger.debug(f"[人格化] emotion updated: valence={v:.2f} phase={phase}")
        except Exception as e:
            # 本来就是 debug 级观测点，失败不致命；但别静默——
            # 留痕才能区分"没触发"和"触发了但坏掉"。
            logger.debug(f"[人格化] emotion 观测失败: {type(e).__name__}: {e}")

    def _seed_injected_history(self):
        """Convert injected_history Q&A list into message pairs."""
        injected = self.card.chat.injected_history
        for i in range(0, len(injected) - 1, 2):
            self.history.append({"role": "user", "content": injected[i]})
            self.history.append({"role": "assistant", "content": injected[i + 1]})

    @classmethod
    def list_characters(cls, config_path: str = "config/config.yaml") -> list[str]:
        """List available character names without instantiating."""
        try:
            cfg = load_config(config_path)
            loader = CharacterLoader(cfg.resolve_path(cfg.characters_dir))
            return list(loader.load_all().keys())
        except Exception:
            return []

    def build_messages(self, user_text: str) -> list[dict]:
        """Build OpenAI-format messages: system + history + new user msg."""
        messages = [{"role": "system", "content": self._build_system_prompt()}]
        # Keep within max_history
        max_hist = self.card.chat.max_history
        recent = self.history[-max_hist:] if len(self.history) > max_hist else self.history
        messages.extend(recent)
        messages.append({"role": "user", "content": user_text})
        return messages

    def _build_system_prompt(self) -> str:
        """Assemble system prompt with time/scene awareness (Phase 1.5)."""
        # 每个可选注入段的失败原因记在这里；末尾统一发一条 warning。
        # 为什么要有这个：这些段以前是 `except Exception: pass`，
        # 注入失败你完全看不出来——她只是"少了一块"，行为变淡，
        # 而日志里一个字都没有。这正是 AGENTS.md 里说的静默失效。
        _inject_failures: list[str] = []

        def _inject(section: str, fn):
            """跑一个可选注入段；成功返回值，失败记下段名并返回 None。

            失败不致命（宁可少一段也别让整轮对话挂掉），但必须留痕。
            """
            try:
                return fn()
            except Exception as e:
                _inject_failures.append(f"{section}: {type(e).__name__}: {e}")
                return None

        parts = []
        if self.card.system_prompt:
            parts.append(self.card.system_prompt.strip())
        if self.card.background:
            parts.append(f"【背景】\n{self.card.background.strip()}")

        # === Phase 1.5: time + scene awareness ===
        time_str = self.clock.describe()
        scene_str = self.scene.current_human_readable()
        parts.append(
            f"【当前时间】{time_str}\n"
            f"【当前场景】{scene_str}"
        )

        # Phase/scene behavior hint
        phase = self.clock.phase
        phase_hint = self._phase_behavior_hint(phase)
        if phase_hint:
            parts.append(f"【时段行为提示】{phase_hint}")

        # === Mood injection（借 CLF 情绪层）：把当前心境注入 prompt ===
        # model 看到自己的真实情绪状态，基于它反应，不用编造。
        # 心境会随对话推进 + idle 回归，跨会话持久（mood_state.json）。
        def _mood():
            # reload mood state：server.py 可能在本轮回话里写入了 petty_state
            # （trigger_petty_if_*），cli 的内存实例是旧的，必须重读文件否则会
            # 看不到 angry/sulking，且后续 save() 会覆盖掉 server 写入的状态。
            self.memory.mood.state = self.memory.mood._load()
            mood_snippet = self.memory.mood.get_prompt_snippet()
            if mood_snippet:
                parts.append(mood_snippet)
            # petty_state 注入（生闷气时额外提示 LLM 语气要冷）
            petty_snippet = self.memory.mood.get_petty_snippet()
            if petty_snippet:
                parts.append(petty_snippet)
        _inject("mood/petty", _mood)

        # Phase4: 感情四维软倾向描述（档位提示，不是阈值）注入 prompt
        def _relationship():
            rel_snippet = self.memory.mood.get_relationship_snippet()
            if rel_snippet:
                parts.append(rel_snippet)
        _inject("relationship", _relationship)

        # Phase5a: 环境事件（天气/节日/随机事件）注入 prompt + mood 修正
        def _environment():
            from character_chat.world.environment import (
                get_environment_snippet, get_mood_modifiers
            )
            env_snippet = get_environment_snippet()
            if env_snippet:
                parts.append(env_snippet)
            # 环境 mood 修正加到当前 valence（0.5 倍推动，权重放大后明显影响心境）
            mood_mod, passion_mod = get_mood_modifiers()
            if abs(mood_mod) > 0.001:
                self.memory.mood.state.valence = max(-1.0, min(1.0,
                    self.memory.mood.state.valence + mood_mod * 0.5))
        _inject("environment", _environment)

        # Phase6: 关系近期轨迹——最近几轮主题+情绪峰值，让 model 知道"我们刚聊过什么"
        # 避免话题突兀跳跃，"我们刚说到X"这种连续感有依据（不凭空编）
        def _trajectory():
            traj_snippet = self.memory.mood.get_recent_trajectory_snippet(max_items=5)
            if traj_snippet:
                parts.append(traj_snippet)
        _inject("recent-trajectory", _trajectory)

        # Phase7-2: 未说出口的话——上一轮 inner 独白注入，让回复带欲言又止痕迹
        def _inner():
            inner_snippet = self.memory.mood.get_inner_snippet()
            if inner_snippet:
                parts.append(inner_snippet)
        _inject("inner-monologue", _inner)

        # Phase7-1: 认知切片——她对学长的具体观察（来自真实存档记忆，非硬编码数字）
        # 让 model 带"我观察到的他"的具象认知，而不是抽象亲密=0.7。
        # 切片随记忆库动态生长：聊得越多，她对他的认知锚点越丰富。
        def _cognitive():
            slices = self.memory.long_term.get_cognitive_slices(limit=4)
            if slices:
                body = "\n".join(f"- {s}" for s in slices)
                parts.append(
                    "【我对他的观察（凭记忆，非此刻结论）】\n"
                    f"{body}\n"
                    "（这些是我长期观察到的他的样子，回应时自然带出，不刻意罗列）"
                )
        _inject("cognitive-slices", _cognitive)

        # Scene transition instruction
        parts.append(
            "【场景转移】若对话中场景自然变化（如从天台回家、从黄昏到深夜），"
            "请在回复末尾附加一个场景标签，格式：\n"
            "<scene:新场景名,phase:新相位>\n"
            "phase 可选值：晨/午/黄昏/夜/深夜。若未发生场景转移则不要附加。"
        )

        # === Phase 2: emotion awareness ===
        emotion_hint = self.emotion.get_prompt_hint()
        if emotion_hint:
            parts.append(f"【当前情感】{emotion_hint}")
        # (PATCHED: removed expression_tags injection which used bracket-style
        # stage directions. WeChat mode should use kaomoji only, no brackets.)

        # === Phase 3: memory recall ===
        def _recall():
            if not self.history:
                return
            last_user_msg = ""
            for m in reversed(self.history):
                if m["role"] == "user":
                    last_user_msg = m["content"]
                    break
            if last_user_msg:
                # Plan C: classify current message, recall same-category only.
                # Top 2 + min_score 0.3 to control token noise in WeChat mode.
                from character_chat.memory.pipeline import classify_category
                cur_cat = classify_category(last_user_msg, "", self.scene.current_scene.name)
                cat_filter = cur_cat.value if cur_cat.value != "both" else None
                memories = self.memory.recall(
                    last_user_msg, top_k=2, category=cat_filter, min_score=0.3
                )
                if memories:
                    mem_str = "\n".join(f"  - {m}" for m in memories)
                    parts.append(
                        f"【相关回忆】（以下是真实存档的过去对话片段，可「引用」但不能「发挥」）\n{mem_str}\n"
                        f"【回忆使用铁律】这些回忆是真实发生过的存档。你只能基于其中「明确写明」的内容"
                        f"提及过去——回忆里没写的具体参数、数据、对话原话、日期、文件名、实验结果，"
                        f"一律不许编造。\n"
                        f"【自陈状态铁律】问他「最近怎么样/进展如何」时，你自己的实验/代码进展"
                        f"只能泛泛说「……在调」「……在跑，还没出结果」「……卡在一个地方」，"
                        f"不许编具体：模型名、参数（depth/num_heads/lr）、数据集、指标数字"
                        f"（「3个点」「66%」）、ablation 结果——除非回忆里真有这条。"
                        f"编造技术细节显得「厉害」是欺骗，唐班水平的人不会这样。"
                    )
        _inject("memory-recall", _recall)

        rel = self.card.get_relationship("user")
        if rel.type:
            parts.append(
                f"【关系】与用户是{rel.type}，好感度{rel.affection:.1f}/1.0"
            )

        # 可选注入段的失败统一在这里留痕：每轮最多一条，不刷屏。
        # 看到这条 warning 就说明"她这一轮少了一块"，去查对应段名即可。
        if _inject_failures:
            logger.warning(
                f"[prompt] {len(_inject_failures)} 个注入段失败（该轮人格会被削弱）: "
                + "; ".join(_inject_failures)
            )

        return "\n\n".join(parts)

    def _phase_behavior_hint(self, phase: str) -> str:
        """Behavior hint based on time-of-day phase.

        Phase2 人格化：从查一句静态提示升级为 rhythm.py 的整包内容
        （活动/语气/颜情绪倾向/可能话题），让回复有时段感。
        """
        from character_chat.world.rhythm import rhythm_prompt
        return rhythm_prompt(phase)

    def send(self, user_text: str, skip_record: bool = False) -> str:
        """Send user text, get character reply, update history.

        skip_record=True 时跳过 memory.record + mood.update——用于 scheduler 等内部
        生成的"系统提示"调用，避免把 [系统提示：...] prompt 当真实对话存进记忆库
        污染 recall（之前 scheduler 的 prompt 被当 user_text 存了 37 条垃圾）。
        """
        # Advance narrative clock
        self.clock.advance(self.bus)

        # === petty_state 解冻检测：收到消息先看能不能解冻生闷气 ===
        # 必须在 build_messages 之前——这样 prompt 注入的 petty_snippet 是解冻后的状态
        # 也必须在 trigger_petty_* 之后——server.py 可能在本轮先写了 angry/sulking
        # 状态，cli 内存实例是旧的，必须先 reload 再解冻
        try:
            self.memory.mood.state = self.memory.mood._load()
            self.memory.mood.try_thaw_petty(user_text)
        except Exception as e:
            logger.debug(f"[petty] thaw check failed: {e}")

        # === Phase 2: emotion pipeline (runs BEFORE building messages so
        # the reconciled emotion is available for prompt injection) ===
        recent_user_msgs = [
            m["content"] for m in self.history if m["role"] == "user"
        ]
        self._last_reconciled = self.emotion.process(
            user_text, context=recent_user_msgs
        )
        self.bus.publish(
            EMOTION_UPDATED,
            {
                "label": self._last_reconciled.reconciled_label.value,
                "intensity": self._last_reconciled.reconciled_intensity,
                "confidence": self._last_reconciled.confidence,
            },
            source="emotion_pipeline",
        )
        logger.debug(
            f"[情感] {self._last_reconciled.reconciled_label.value} "
            f"@{self._last_reconciled.reconciled_intensity:.2f} "
            f"(conf={self._last_reconciled.confidence:.2f})"
        )

        self.bus.publish(MESSAGE_RECEIVED, {"text": user_text}, source="user")

        messages = self.build_messages(user_text)

        # === 上网搜索（function calling）：LLM 自决何时调 web_search ===
        # 只在学长问实时/外部信息时触发；日常陪聊不调（不浪费 token、不打断节奏）
        from character_chat.tools.web_search import WEB_SEARCH_TOOL
        try:
            reply = self.llm.chat_with_tools(
                messages, tools=[WEB_SEARCH_TOOL]
            )
        except Exception as e:
            # 工具调用失败 → 回退普通 chat，不让搜索功能挂掉整轮对话
            logger.warning(f"[tool] chat_with_tools failed, fallback to plain chat: {e}")
            reply = self.llm.chat(messages)

        # Parse scene tag if LLM output one (scene transition)
        scene_info = parse_scene_tag(reply)
        if scene_info is not None:
            scene_name, new_phase = scene_info
            success = self.scene.transition(scene_name, new_phase)
            if success:
                logger.info(f"[场景转移] {self.scene.current_human_readable()}")
            else:
                logger.warning(
                    f"[场景转移失败] 从 {self.card.scenario.setting} 转移到 "
                    f"{scene_name} 被相位阻止"
                )

        # Filter bad words
        for w in self.card.chat.filter.bad_words:
            reply = reply.replace(w, "")

        # === Phase7-2: 提取 <inner>...</inner> 内心独白 ===
        # model 可以在 reply 里输出 <inner>她心里想说但没说出口的话</inner>，
        # 这段被剥离不发给学长，但记录到 mood.last_inner，下一轮注入 prompt
        # 让回复带欲言又止的痕迹（"未说出口的话"层）。
        # 如果 model 没输出 <inner> 标签，用 extract_inner_from_reply 从 reply 真实文本里提取
        # （害羞的人 reply 里的省略号+回避词本身就是"没说出口"的痕迹）
        inner_text = ""
        inner_match = INNER_TAG_RE.search(reply)
        if inner_match:
            inner_text = inner_match.group(1).strip()
            # 从 reply 里剥掉 inner 块（学长看不到）
            reply = INNER_TAG_RE.sub("", reply).strip()
        else:
            # 兜底：从 reply 被动提取
            try:
                inner_text = self.memory.mood.extract_inner_from_reply(reply, user_text)
            except Exception as e:
                logger.warning(f"[Phase7-2] extract_inner_from_reply failed: {e}")
        if inner_text:
            try:
                self.memory.mood.record_inner(inner_text)
            except Exception as e:
                logger.warning(f"[Phase7-2] record_inner failed: {e}")

        # === WeChat mode: strip all bracket stage directions ===
        # DeepSeek is stubborn about writing (动作) despite prompt rules.
        # Hard-filter here so WeChat users never see stage directions.
        reply = self._strip_stage_directions(reply)

        # === Phase 3: Record to memory ===
        # skip_record=True 时跳过——scheduler 等内部 prompt 不该当真实对话存
        if not skip_record:
            # reconciled_label 是 EmotionLabel 枚举（str Enum），取 .value 传 str 下去，
            # 避免 mood.update 里 EMOTION_IMPACTS 字典查找和 ("neutral", None) 判断的歧义
            emo_label_val = None
            if self._last_reconciled and self._last_reconciled.reconciled_label:
                emo_label_val = self._last_reconciled.reconciled_label.value
            self.memory.record(
                user_text=user_text,
                reply_text=reply,
                emotion_label=emo_label_val,
                scene=self.scene.current_human_readable().split("|")[0].strip(),  # just the scene name
                time_phase=self.clock.phase,
            )
            # #4 未来脚本：识别学长/凌暮雪给出的带内容承诺，存成 milestone 记忆
            # 让 daemon 端 promise_src 能召回"我们说过要做的事"，在合适时机提醒他
            try:
                from character_chat.memory.promise import detect_and_store_promise
                detect_and_store_promise(
                    self.memory.long_term,
                    user_text=user_text,
                    reply_text=reply,
                    scene=self.scene.current_human_readable().split("|")[0].strip(),
                    time_phase=self.clock.phase,
                )
            except Exception as e:
                logger.debug(f"[Promise] detect/store skipped: {e}")
            # Phase4: 感情四维事件推动——根据情绪标签+用户消息推断事件类型
            self._update_relationship_from_turn(user_text, emo_label_val, reply)

        # Update history
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self.bus.publish(MESSAGE_SENT, {"text": reply}, source="character")
        return reply

    def _update_relationship_from_turn(self, user_text: str, emo_label: str, reply_text: str):
        """根据本轮对话推断感情事件类型，推动四维状态。

        不引入 NLP 分类器，基于情绪标签 + 关键词。一轮可能触发多个事件（比如直球+撒娇），
        但为避免数值跳太快，每轮只推一个最强事件（按优先级）。
        """
        ut = user_text or ""
        rt = reply_text or ""
        # 关键词检测（优先级从高到低）
        if any(k in ut for k in ["想你了", "喜欢你", "想见你", "爱你", "我也想你"]):
            self.memory.mood.update_relationship("direct_response")
        elif any(k in rt for k in ["我也想你了", "再说一遍", "你才突然说"]):
            # 她主动直球回应也算
            self.memory.mood.update_relationship("direct_response")
        elif any(k in ut for k in ["好看", "可爱", "漂亮", "喜欢你今天"]) and "她是谁" not in rt:
            self.memory.mood.update_relationship("flirted")
        elif any(k in ut for k in ["家里", "我妈", "胃", "怕", "担心", "累"]) or any(k in rt for k in ["……要是他们", "我有点怕", "我妈又问"]):
            self.memory.mood.update_relationship("vulnerable")
        elif any(k in ut for k in ["还记得", "上次", "那天", "在一起那天"]) or any(k in rt for k in ["还记得", "那天我们"]):
            self.memory.mood.update_relationship("shared_memory")
        elif any(k in ut for k in ["你刚说", "你上次说", "我记得你"]) or any(k in rt for k in ["你记得", "你居然记得"]):
            self.memory.mood.update_relationship("he_remembered")
        elif emo_label in ("sad", "anxious") and any(k in ut for k in ["不回", "不理", "你怎么", "干嘛"]):
            self.memory.mood.update_relationship("ignored")
        elif emo_label in ("angry",) or any(k in ut for k in ["分手", "别说了", "烦"]):
            self.memory.mood.update_relationship("conflict")
        else:
            # 普通互动算他主动找（attachment 微涨）
            self.memory.mood.update_relationship("he_initiated")

    @staticmethod
    def _strip_stage_directions(reply: str) -> str:
        """Remove bracket-style stage directions for WeChat mode.

        Strips:
          - Full-width brackets: （...） including nested
          - Half-width brackets: (...) when they look like actions
          - Asterisk actions: *低头*
          - <scene:...> tags (scene transition markers)

        Preserves:
          - Kaomoji that happen to use parens (e.g. (//▽//), (￣▽￣)) — these
            contain non-CJK/non-narrative chars and are short
          - Actual dialogue content

        Heuristic: a bracketed span is a "stage direction" if it contains
        narrative verbs/descriptions (Chinese chars describing actions/states).
        Kaomoji are short and mostly punctuation/symbols.
        """
        import re

        # Remove <scene:...> / <task:...> style tags
        reply = re.sub(r"<[a-z_]+:[^>]*>", "", reply, flags=re.IGNORECASE)

        # Remove asterisk actions: *低头*, *smiles*
        reply = re.sub(r"\*[^*\n]{1,40}\*", "", reply)

        # Remove full-width bracket spans: （...）
        # These are always stage directions in this character's style.
        reply = re.sub(r"（[^（）\n]{1,60}）", "", reply)

        # Remove half-width bracket spans that look like stage directions.
        # Keep kaomoji: kaomoji are short (<=10 chars) and contain symbols/digits,
        # not narrative CJK text. A span with >=3 CJK chars is a stage direction.
        def half_width_bracket(m):
            inner = m.group(1)
            # Count CJK chars
            cjk = sum(1 for c in inner if "一" <= c <= "鿿")
            if cjk >= 3:
                return ""  # stage direction, strip it
            return m.group(0)  # likely kaomoji or short aside, keep

        reply = re.sub(r"\(([^()\n]{1,40})\)", half_width_bracket, reply)

        # Clean up: collapse multiple spaces/newlines left by removals
        reply = re.sub(r"[ \t]{2,}", " ", reply)
        reply = re.sub(r"\n{3,}", "\n\n", reply)
        # Trim leading punctuation/whitespace that removals may have orphaned
        reply = reply.strip(" 　")
        # Fix leading punctuation like "，..." or "。..." after a stripped prefix
        reply = re.sub(r"^[，。、；：\s]+", "", reply)
        return reply if reply else "……嗯。"

    def render_reply(self, reply: str) -> str:
        """Render reply with ANSI styling for stage directions."""
        import re

        def dim_tag(m):
            return f"{DIM}{m.group(0)}{RESET}"

        reply = re.sub(r"\*[^*]+\*", dim_tag, reply)
        reply = re.sub(r"（[^（）]+）", dim_tag, reply)
        return reply

    def render_status(self) -> str:
        """Render status line: time + scene + emotion."""
        time_str = self.clock.describe()
        scene_str = self.scene.current_human_readable()
        emo = self._last_reconciled
        emo_str = f" | {emo.reconciled_label.value}@{emo.reconciled_intensity:.2f}" if emo else ""
        return f"{time_str} | {scene_str}{emo_str}"

    def switch(self, name: str) -> bool:
        """Switch to another character (saves current session state later)."""
        card = self.loader.get(name)
        if card is None:
            return False
        self.card = card
        self.history = []
        self._seed_injected_history()
        # Phase 2: reload expression tags for the new character
        self.emotion.load_character_tags(self.card.expression_tags)
        # Keep current world state (time/scene carries over to new character)
        logger.info(f"[切换角色] 切换到 {name}，时间/场景/情感状态保持")
        return True

    def run(self):
        """Main REPL loop."""
        print(f"\n{CYAN}[已加载角色：{self.card.bot_name} | {self.card.scenario.setting}]{RESET}")
        if self.card.scenario.opening:
            opening = self.render_reply(self.card.scenario.opening)
            print(f"{YELLOW}{self.card.bot_name}{RESET}> {opening}\n")

        while True:
            try:
                user_input = input(f"{YELLOW}你{RESET}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[再见]")
                break

            if not user_input:
                continue

            # Commands
            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    break
                continue

            # Normal chat
            try:
                reply = self.send(user_input)
                print(f"{YELLOW}{self.card.bot_name}{RESET}> {self.render_reply(reply)}\n")
            except Exception as e:
                logger.error(f"LLM error: {e}")
                print(f"[错误] {e}\n")

    def _handle_command(self, cmd_line: str) -> bool:
        """Handle slash commands. Returns True if should exit."""
        parts = cmd_line[1:].split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "quit" or cmd == "exit":
            print("[再见]")
            return True
        elif cmd == "list":
            names = self.loader.list_names()
            print(f"[可用角色] {', '.join(names)}")
        elif cmd == "switch":
            if not arg:
                print("[用法] /switch <角色名>")
            elif self.switch(arg):
                print(f"{CYAN}[切换到角色：{self.card.bot_name} | {self.card.scenario.setting}]{RESET}")
                if self.card.scenario.opening:
                    print(f"{YELLOW}{self.card.bot_name}{RESET}> {self.render_reply(self.card.scenario.opening)}\n")
            else:
                print(f"[错误] 角色不存在：{arg}。可用：{', '.join(self.loader.list_names())}")
        elif cmd == "help":
            self._print_help()
        elif cmd == "mood":
            # Phase 2: 显示当前情感状态
            state = self.emotion.get_current_state()
            dominant = state["dominant"]
            intensity = state["dominant_intensity"]
            print(f"{MAGENTA}[当前情感]{RESET} {dominant} @ {intensity:.2f}")
            print(f"  Top 3: {', '.join(f'{e}={v:.2f}' for e, v in state['top_3'])}")
            if self._last_reconciled:
                print(f"  置信度: {self._last_reconciled.confidence:.2f}")
                print(f"  推理: {self._last_reconciled.reasoning}")
        elif cmd == "remember":
            # Phase 3: Force record a memory
            if not arg:
                print("[用法] /remember <要记住的内容>")
            else:
                self.memory.short_term.add(
                    content=f"用户强调: {arg}",
                    importance=MemoryImportance.HIGH,
                    emotion_label=self._last_reconciled.reconciled_label.value if self._last_reconciled else None,
                    scene=self.scene.current_human_readable().split("|")[0].strip(),
                    time_phase=self.clock.phase,
                )
                print(f"{GREEN}[已记住]{RESET} {arg}")
        elif cmd == "sleep":
            # Phase 3: Run hippocampus consolidation
            n = self.memory.consolidate(force_all=True)
            stats = self.memory.get_stats()
            print(f"{CYAN}[记忆巩固完成]{RESET} 巩固了 {n} 条记忆")
            print(f"  短期: {stats['short_term_size']}条 | 长期: {stats['long_term_size']}条")
        elif cmd == "recall":
            # Phase 3: Search memories
            if not arg:
                print("[用法] /recall <关键词>")
            else:
                results = self.memory.recall(arg, top_k=5)
                if results:
                    print(f"{CYAN}[相关回忆]{RESET}")
                    for m in results:
                        print(f"  {m}")
                else:
                    print("[无相关回忆]")
        elif cmd == "memstats":
            # Phase 3: Memory system stats
            stats = self.memory.get_stats()
            print(f"{CYAN}[记忆状态]{RESET}")
            print(f"  短期: {stats['short_term_size']}条")
            print(f"  长期: {stats['long_term_size']}条")
            print(f"  待巩固: {stats['consolidation_candidates']}条")
        else:
            print(f"[未知命令] {cmd}。输入 /help 查看命令")
        return False

    def _print_help(self):
        print(f"{CYAN}[命令]{RESET}")
        print("  /list              列出可用角色")
        print("  /switch <name>     切换角色")
        print("  /mood              查看情感状态")
        print("  /remember <text>   强制记住一段内容")
        print("  /recall <keyword>  搜索相关回忆")
        print("  /sleep             触发记忆巩固(短期->长期)")
        print("  /memstats          查看记忆系统状态")
        print("  /help              帮助")
        print("  /quit              退出")
