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
from character_chat.event.types import EMOTION_UPDATED, MESSAGE_RECEIVED, MESSAGE_SENT
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

        # World: narrative clock + scene state
        self.clock = WorldClock(day=1, minute=1020)  # start 17:00 黄昏
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

        self._seed_injected_history()

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
        # Expression tag suggestion based on last reconciled emotion
        if self._last_reconciled is not None:
            label = self._last_reconciled.reconciled_label
            tags = self.emotion.get_expression_tags(label, top_n=2)
            if tags:
                tags_str = "、".join(tags)
                parts.append(
                    f"【本轮可用表情标签】请在回复中自然融入以下标签之一（不要生硬）：{tags_str}"
                )

        # === Phase 3: memory recall ===
        if self.history:
            last_user_msg = ""
            for m in reversed(self.history):
                if m["role"] == "user":
                    last_user_msg = m["content"]
                    break
            if last_user_msg:
                memories = self.memory.recall(last_user_msg, top_k=3)
                if memories:
                    mem_str = "\n".join(f"  - {m}" for m in memories)
                    parts.append(f"【相关回忆】\n{mem_str}")

        rel = self.card.get_relationship("user")
        if rel.type:
            parts.append(
                f"【关系】与用户是{rel.type}，好感度{rel.affection:.1f}/1.0"
            )
        return "\n\n".join(parts)

    def _phase_behavior_hint(self, phase: str) -> str:
        """Behavior hint based on time-of-day phase."""
        hints = {
            "深夜": "深夜氛围安静、私密，角色语气更柔软/疲惫，话题更内省。",
            "夜": "夜晚，角色稍放松，可能流露白天压抑的情绪。",
            "晨": "早晨，角色可能刚醒，语气清爽或带起床气。",
            "午": "午后，日常状态，语气平稳。",
            "黄昏": "黄昏，光线柔和，适合怀旧、感性话题。",
        }
        return hints.get(phase, "")

    def send(self, user_text: str) -> str:
        """Send user text, get character reply, update history."""
        # Advance narrative clock
        self.clock.advance(self.bus)

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

        # === Phase 3: Record to memory ===
        self.memory.record(
            user_text=user_text,
            reply_text=reply,
            emotion_label=self._last_reconciled.reconciled_label if self._last_reconciled else None,
            scene=self.scene.current_human_readable().split("|")[0].strip(),  # just the scene name
            time_phase=self.clock.phase,
        )

        # Update history
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self.bus.publish(MESSAGE_SENT, {"text": reply}, source="character")
        return reply

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
