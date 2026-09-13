"""Lightweight Mood System for 凌暮雪 — 借 CLF 架构，纯 python 实现

核心：
- MoodState: VAD 三维（valence/arousal/dominance）
- Ornstein-Uhlenbeck 均值回归：事件推动偏离，空闲慢慢回到基线
- 跨会话 JSON 持久化：状态存盘，重启/重开会话保持
- 每轮更新 + prompt 注入：model 基于真实心境反应，不用编造
- 高 arousal 自动强化记忆 importance

CLF 原版用 torch + EventBus + 14 脑区，太重。这里是给 character_chat
的轻量版——够用、能跑、能让 model 有内部状态可依凭。
"""
import json
import math
import random
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# =============================================================================
# 情绪标签 → VAD 偏移（事件对心境的推动）
# =============================================================================
# valence: -1(消极)..+1(积极)
# arousal: 0(平静)..1(激动)
# dominance: 0(无力)..1(掌控)
EMOTION_IMPACTS = {
    "happy":     (+0.5, +0.3, +0.2),
    "proud":     (+0.4, +0.2, +0.4),
    "shy":       (+0.2, +0.4, -0.2),  # 害羞：微正效价但高唤醒+低支配
    "embarrassed": (+0.0, +0.5, -0.4),  # 尴尬：中性+高唤醒+低支配
    "anxious":   (-0.3, +0.4, -0.3),
    "sad":       (-0.5, -0.2, -0.3),
    "angry":     (-0.4, +0.5, +0.3),  # 生气：消极但高支配
    "warm":      (+0.4, +0.1, +0.1),
    "neutral":   (+0.0, +0.0, +0.0),
    "flustered": (-0.1, +0.6, -0.3),  # 慌乱（被撩）
    "melting":   (+0.6, +0.3, -0.1),  # 心软/心动
    "jealous":   (-0.3, +0.3, +0.1),  # 吃醋
    "petty":     (-0.15, +0.1, +0.2),  # 小脾气：微负效价、微高唤醒、偏支配
    "tsundere":  (+0.15, +0.2, +0.3),  # 傲娇：微正效价（其实开心）、高唤醒、高支配（嘴硬）
}


@dataclass
class MoodState:
    """心境状态（VAD 三维）"""
    valence: float = 0.0        # 效价 [-1, +1]
    arousal: float = 0.3        # 唤醒 [0, 1]，基线偏平静
    dominance: float = 0.5      # 支配 [0, 1]
    last_update_ts: float = field(default_factory=time.time)
    history: list = field(default_factory=list)  # 最近 N 条情绪事件
    trajectory: list = field(default_factory=list)  # Phase6: 关系近期轨迹 (最近 N 条)
    last_inner: str = ""         # Phase7-2: 上一轮被剥离的内心独白（未说出口的话）
    inner_history: list = field(default_factory=list)  # Phase7-2: 最近 5 条 inner 记录
    # petty_state — 显式生闷气状态（小脾气机制）
    petty_state: str = "none"      # none / sulking / angry
    petty_since: float = 0.0       # 触发时间戳
    petty_duration_hours: float = 3.0  # 默认持续 3 小时（2-6 小时范围）
    petty_trigger: str = ""        # 触发原因描述（方便调试和 prompt 注入）


@dataclass
class MoodParameters:
    """OU 过程参数（决定心境回归基线的速度）"""
    mean_valence: float = 0.0       # 基线效价
    mean_arousal: float = 0.3       # 基线唤醒（偏平静）
    mean_dominance: float = 0.5     # 基线支配
    reversion_speed: float = 0.1    # 均值回归速度（越大越快回到基线）
    volatility: float = 0.2         # 波动性


@dataclass
class RelationshipState:
    """感情四维状态（Sternberg 三角 + 依恋）。

    Phase4 人格化：让凌暮雪对学长的感情不是固定 affection，而是四维独立动态。
    慢衰减（按周/月，不是天），事件推动（直球/撒娇/共同回忆/冷战）。
    软倾向描述映射成短语注入 prompt（不是硬阈值）。
    """
    intimacy: float = 0.55       # 亲密度：真心话/脆弱共享深度。涨=深聊/流露脆弱；衰减较快（按周）
    passion: float = 0.65        # 激情：心跳/脸红/直球回应强度。涨=撩/撒娇/直球；衰减中（按周）
    commitment: float = 0.50     # 承诺：长期稳定/未来感。涨=共同经历/里程碑/提未来；衰减极慢（按月）
    attachment: float = 0.60     # 依恋：分离焦虑/被忽略敏感。涨=他主动找/记她的话；衰减中（按周）
    last_update_ts: float = field(default_factory=time.time)


# 衰减半衰期（小时）——值越大衰减越慢。她是稳定的人，远大于 mood 的 OU 衰减
REL_DECAY_HOURS = {
    "intimacy": 24 * 10,      # ~10 天半衰期（一两周没深聊才略掉）
    "passion": 24 * 7,        # ~7 天
    "commitment": 24 * 60,    # ~2 个月（几乎不掉）
    "attachment": 24 * 7,     # ~7 天
}

# 事件 → 各维度 delta（+涨 -降）
# 混合粒度：日常互动极小累积（he_initiated +0.01），大事件正常 delta（+0.10~0.15）
# 不引入 NLP 分类器，基于已有情绪标签 + 关键词推断事件类型
REL_EVENTS = {
    "direct_response":  {"intimacy": +0.05, "passion": +0.15, "commitment": +0.02},  # "我也想你"
    "flirted":          {"passion": +0.10, "intimacy": +0.02},                        # 被撩/被夸好看
    "vulnerable":       {"intimacy": +0.12, "commitment": +0.03},                    # 流露脆弱/家里事
    "shared_memory":    {"intimacy": +0.05, "commitment": +0.08},                    # 共同回忆
    "milestone":        {"intimacy": +0.10, "passion": +0.10, "commitment": +0.15},  # 纪念日/里程碑
    "he_initiated":     {"attachment": +0.01},                                       # 日常他主动找——极小累积
    "he_remembered":    {"attachment": +0.10, "intimacy": +0.03},                    # 他记她说过的话
    "ignored":          {"attachment": -0.10, "intimacy": -0.05},                    # 被冷
    "conflict":         {"intimacy": -0.20, "attachment": -0.15, "commitment": -0.03},  # 吵架——commitment最抗跌
}


def _describe_dim(value: float, dim_name: str) -> str:
    """把维度值映射成软倾向描述短语（不是硬阈值，是档位提示）。

    极端值（<0.2 或 >0.8）才用强提示，中间段连续描述。
    """
    if dim_name == "intimacy":
        if value < 0.2: return "浅，话题停留在日常，不轻易露底"
        if value < 0.4: return "浅偏中，偶尔聊点心事但不深"
        if value < 0.6: return "中等，会聊日常心情，还藏着最软的地方"
        if value < 0.8: return "偏深，会主动说心事，偶尔流露脆弱"
        return "深，敢说脆弱的话，主动分享家里事"
    if dim_name == "passion":
        if value < 0.2: return "淡，回应平稳不太脸红"
        if value < 0.4: return "低偏中，偶尔心跳但克制"
        if value < 0.6: return "中等，被撩会慌，偶尔撒娇"
        if value < 0.8: return "偏高，容易脸红，会主动撒娇"
        return "高，直球回应强烈，会想要更多"
    if dim_name == "commitment":
        if value < 0.2: return "弱，没怎么想过以后"
        if value < 0.4: return "低偏中，当下为主"
        if value < 0.6: return "中等，关系稳定但未来感还不强"
        if value < 0.8: return "偏高，会主动提以后，家庭焦虑减轻"
        return "强，认定了，不怕家里那边"
    if dim_name == "attachment":
        if value < 0.2: return "独立，他冷你也不太在意"
        if value < 0.4: return "低偏中，偶尔想他但不黏"
        if value < 0.6: return "中等，会主动找他但克制"
        if value < 0.8: return "偏高，他冷会慌，会主动找"
        return "高，分离敏感，怕失去，会主动确认他在意"
    return ""


class MoodSystem:
    """心境系统：OU 均值回归 + 事件推动 + 跨会话持久化"""

    def __init__(self, state_path: str = "data/mood_state.json"):
        self._path = Path(state_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.params = MoodParameters()
        self.state = self._load()
        self.relationship = self._load_relationship()

    # =========================================================================
    # 持久化
    # =========================================================================
    def _load(self) -> MoodState:
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                # relationship 字段单独存，不进 MoodState
                d.pop("relationship", None)
                return MoodState(**d)
            except Exception:
                pass
        return MoodState()

    def _load_relationship(self) -> RelationshipState:
        """从 mood_state.json 的 relationship 子对象加载。"""
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                rel = d.get("relationship")
                if rel:
                    return RelationshipState(**rel)
            except Exception:
                pass
        return RelationshipState()

    def save(self):
        try:
            d = asdict(self.state)
            d["relationship"] = asdict(self.relationship)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"[Mood] save failed: {e}")

    # =========================================================================
    # OU 均值回归（按时间差计算 idle 期间的自然回归）
    # =========================================================================
    def _apply_time_decay(self):
        """根据上次更新到现在的 idle 时间，让心境回归基线"""
        now = time.time()
        dt_hours = max(0.0, (now - self.state.last_update_ts) / 3600.0)
        if dt_hours <= 0:
            return

        # OU 过程：dX = θ(μ - X)dt，解：X(t) = μ + (X0 - μ) * exp(-θ * t)
        # θ 用 reversion_speed，t 用小时
        p = self.params
        decay = math.exp(-p.reversion_speed * dt_hours)

        self.state.valence = p.mean_valence + (self.state.valence - p.mean_valence) * decay
        self.state.arousal = p.mean_arousal + (self.state.arousal - p.mean_arousal) * decay
        self.state.dominance = p.mean_dominance + (self.state.dominance - p.mean_dominance) * decay

        # clamp
        self.state.valence = max(-1.0, min(1.0, self.state.valence))
        self.state.arousal = max(0.0, min(1.0, self.state.arousal))
        self.state.dominance = max(0.0, min(1.0, self.state.dominance))

        self.state.last_update_ts = now

    def sleep_reset(self):
        """跨天时让心境"睡一觉回血"——比 OU 衰减更强的回归。

        叙事时钟跨天（WorldClock.day +=1）时由 TIME_ADVANCE 订阅者调用。
        模拟"睡了一觉，负面心境大幅消解，但不是完全清零"：
        向基线回归 70%（留 30% 残留，昨天的事不会全忘）。
        arousal 强制压低（睡醒偏平静），valence 向正微推（休息后回血）。
        """
        p = self.params
        # 向基线回归 70%
        self.state.valence = p.mean_valence + (self.state.valence - p.mean_valence) * 0.3
        self.state.arousal = p.mean_arousal + (self.state.arousal - p.mean_arousal) * 0.3
        self.state.dominance = p.mean_dominance + (self.state.dominance - p.mean_dominance) * 0.3
        # 睡醒微回血：valence +0.1（但不超 1）
        self.state.valence = min(1.0, self.state.valence + 0.1)
        # clamp
        self.state.valence = max(-1.0, min(1.0, self.state.valence))
        self.state.arousal = max(0.0, min(1.0, self.state.arousal))
        self.state.dominance = max(0.0, min(1.0, self.state.dominance))
        self.state.last_update_ts = time.time()
        self.save()

    # =========================================================================
    # 感情四维（Sternberg + 依恋）—— Phase4 人格化
    # =========================================================================
    def _apply_relationship_decay(self):
        """感情四维慢衰减（按周/月，不是天）。她是稳定的人。

        按 REL_DECAY_HOURS 半衰期衰减。每次 update 前先算，跟 _apply_time_decay 一样。
        """
        now = time.time()
        dt_hours = max(0.0, (now - self.relationship.last_update_ts) / 3600.0)
        if dt_hours <= 0:
            return
        r = self.relationship
        for dim, half_life_h in REL_DECAY_HOURS.items():
            val = getattr(r, dim)
            # 指数衰减到 0.5（中性基线）：X = 0.5 + (X0 - 0.5) * exp(-0.693 * t / half_life)
            decay = math.exp(-0.693 * dt_hours / half_life_h)
            new_val = 0.5 + (val - 0.5) * decay
            new_val = max(0.0, min(1.0, new_val))
            setattr(r, dim, new_val)
        r.last_update_ts = now

    # Phase5b: 感情自然疲劳曲线（"在一起越久激情略回落，亲密承诺上升"）
    # 与 _apply_relationship_decay 区别：衰减向中性 0.5，疲劳曲线是单向漂移
    # （passion 向下、intimacy/commitment/attachment 向上），叠加在衰减之上。
    def apply_relationship_fatigue(self):
        """Phase5b: 感情自然疲劳曲线。

        根据距离上次 update 的小时数推动单向漂移：
        - passion: 每天约 -0.003（激情自然回落，需事件维护）
        - intimacy: 每天约 +0.0015（亲密深层化）
        - commitment: 每天约 +0.0008（承诺稳固）
        - attachment: 每天约 +0.001（依恋深化）

        叠加在 _apply_relationship_decay 的中性衰减之上——衰减把极端值拉回 0.5，
        疲劳曲线再把"在一起时长"的净效应叠加进去。结果是：长久没事件推动时，
        passion 慢慢从高位往 0.5 走再继续往下漂（回落），其他三维往上漂（深化）。
        这就是用户说的"感情厌倦"：激情褪去，转为稳定的深层依恋。

        被 cli.py 的 TIME_ADVANCE 订阅在跨天时调用（每天一次）。
        """
        now = time.time()
        dt_hours = max(0.0, (now - self.relationship.last_update_ts) / 3600.0)
        if dt_hours <= 0:
            return
        dt_days = dt_hours / 24.0
        r = self.relationship
        # 单向漂移（疲劳曲线）：每天固定增量
        r.passion    = max(0.0, min(1.0, r.passion    - 0.003 * dt_days))
        r.intimacy   = max(0.0, min(1.0, r.intimacy   + 0.0015 * dt_days))
        r.commitment = max(0.0, min(1.0, r.commitment + 0.0008 * dt_days))
        r.attachment = max(0.0, min(1.0, r.attachment + 0.001  * dt_days))
        r.last_update_ts = now
        self.save()

    def update_relationship(self, event_type: str):
        """感情事件推动四维状态。

        Args:
            event_type: 事件类型，从 REL_EVENTS 查 delta。
                已知类型：direct_response / flirted / vulnerable / shared_memory /
                         milestone / he_initiated / he_remembered / ignored / conflict
                未知类型跳过（不报错，容错）。
        """
        self._apply_relationship_decay()
        deltas = REL_EVENTS.get(event_type)
        if not deltas:
            return
        r = self.relationship
        for dim, delta in deltas.items():
            val = getattr(r, dim)
            # 软封顶：涨的时候越接近 1.0 越难涨（永远到不了 1.0）。
            # 真人感情没有"满级"，是永远在变的。降的时候不衰减（正常跌）。
            if delta > 0:
                # 距离 1.0 的余量越小，delta 衰减越多：有效 delta = delta * (1 - val)
                eff_delta = delta * (1.0 - val)
                # 但日常极小 delta（<0.03）在高值时几乎不涨——符合"积累到高处难再涨"
                new_val = val + eff_delta
            else:
                new_val = val + delta
            setattr(r, dim, max(0.0, min(1.0, new_val)))
        r.last_update_ts = time.time()
        self.save()

    def get_relationship_snippet(self) -> str:
        """感情四维软倾向描述（档位提示，不是阈值），注入 prompt。

        极端值（<0.2 或 >0.8）强提示，中间段连续描述。
        格式：【感情状态：亲密度 X（描述），激情 X（描述），承诺 X（描述），依恋 X（描述）】

        高值时额外加动态行为提示——让 model 知道"我现在该怎样反应"：
          - attachment > 0.7 → "你现在很依恋他，会主动找他、想确认他在意你"
          - passion > 0.7 → "你现在激情高，被撩容易失守，会想撒娇"
          - intimacy > 0.7 → "你现在敢说心事，偶尔流露脆弱"
          - commitment > 0.7 → "你会主动提以后，家庭焦虑减轻"
        """
        self._apply_relationship_decay()
        r = self.relationship
        dims = [
            ("亲密度", r.intimacy, "intimacy"),
            ("激情", r.passion, "passion"),
            ("承诺", r.commitment, "commitment"),
            ("依恋", r.attachment, "attachment"),
        ]
        parts = [f"{name} {val:.2f}（{_describe_dim(val, key)}）" for name, val, key in dims]
        snippet = f"【感情状态：{', '.join(parts)}】"

        # 动态行为提示——高值时告诉 model 现在该怎样反应
        behavior_hints = []
        if r.attachment > 0.7:
            behavior_hints.append("你现在很依恋他，会主动找他、想确认他在意你，他冷一点会慌")
        if r.passion > 0.7:
            behavior_hints.append("你现在激情高，被撩容易失守耳红，会想撒娇、贪心想要更多")
        if r.intimacy > 0.7:
            behavior_hints.append("你现在敢说心事，偶尔流露脆弱，不藏最软的地方")
        if r.commitment > 0.7:
            behavior_hints.append("你会主动提以后，家庭焦虑减轻，认定他了")
        if behavior_hints:
            snippet += "\n【当下心境】" + "；".join(behavior_hints) + "。这些会自然流露在回复里，但别生硬点题。"
        return snippet

    # =========================================================================
    # 事件推动
    # =========================================================================
    # 关键词规则：直球/撩/夸 → 强制情绪推动（不依赖 emotion 模型识别）
    # emotion 模型对"想你了"这种直球常识别成 neutral，导致 mood 不推动 → prompt 注入基线 →
    # model 反应冷淡。用关键词兜底，确定性高。
    KEYWORD_TRIGGERS = {
        # 直球重击 → melting（心动/失守）
        "想你了": "melting", "想你": "melting", "喜欢你": "melting", "想见你": "melting",
        "爱你": "melting", "在一起": "melting", "做我女朋友": "melting",
        # 撩 → flustered（慌乱）
        "好看": "flustered", "漂亮": "flustered", "可爱": "flustered", "喜欢你今天": "flustered",
        "头发": "flustered", "眼神": "flustered",
        # 夸技术 → shy + proud
        "写得很干净": "tsundere", "厉害": "tsundere", "聪明": "tsundere", "牛逼": "tsundere",
        "写得好": "tsundere", "很强": "tsundere", "不错": "tsundere", "谢谢": "tsundere",
        "辛苦了": "tsundere", "麻烦你了": "tsundere",
        # 吃醋 → jealous
        "她": "jealous", "那个女生": "jealous", "学姐": "jealous",
        # 负面 → sad/anxious
        "生气": "angry", "烦": "anxious", "累": "sad", "失败": "sad", "报错": "anxious",
    }

    def detect_emotion_from_text(self, user_text: str, reply_text: str = "") -> Optional[str]:
        """关键词检测：从用户消息推断情绪标签（兜底 emotion 模型）"""
        text = user_text or ""
        for kw, emo in self.KEYWORD_TRIGGERS.items():
            if kw in text:
                return emo
        return None

    def update(self, emotion_label: Optional[str] = None, custom_impact: Optional[tuple] = None,
               user_text: str = "", reply_text: str = ""):
        """一次事件更新心境

        Args:
            emotion_label: 标准情绪标签（见 EMOTION_IMPACTS）。若为 None/neutral，
                           会用关键词从 user_text 兜底检测。
            custom_impact: (dV, dA, dD) 自定义偏移，优先于标签
            user_text: 用户消息，用于关键词兜底检测情绪
        """
        self._apply_time_decay()

        # 关键词兜底：emotion 模型没识别出强情绪时，用关键词检测
        if (not emotion_label or emotion_label in ("neutral", None)) and user_text:
            detected = self.detect_emotion_from_text(user_text, reply_text)
            if detected:
                emotion_label = detected

        if custom_impact:
            dv, da, dd = custom_impact
        elif emotion_label and emotion_label in EMOTION_IMPACTS:
            dv, da, dd = EMOTION_IMPACTS[emotion_label]
        else:
            # 未知标签或 None：微随机扰动（避免完全静态）
            dv = random.uniform(-0.05, 0.05)
            da = 0.0
            dd = 0.0

        # 推动 + 小噪声
        self.state.valence += dv + random.uniform(-0.05, 0.05)
        self.state.arousal += da + random.uniform(-0.02, 0.02)
        self.state.dominance += dd + random.uniform(-0.05, 0.05)

        # clamp
        self.state.valence = max(-1.0, min(1.0, self.state.valence))
        self.state.arousal = max(0.0, min(1.0, self.state.arousal))
        self.state.dominance = max(0.0, min(1.0, self.state.dominance))

        # 记录历史（保留最近 50 条）
        self.state.history.append({
            "ts": time.time(),
            "emotion": emotion_label,
            "valence": self.state.valence,
            "arousal": self.state.arousal,
            "dominance": self.state.dominance,
        })
        if len(self.state.history) > 50:
            self.state.history = self.state.history[-50:]

        # Phase6: 关系近期轨迹——记录本轮主题 + 情绪 + 时间戳
        # 主题用 user_text 前 16 字（足够 prompt 注入时辨识"刚聊过什么"，不做 NLP 摘要）
        topic = (user_text or "").strip()[:16]
        if topic:
            self.state.trajectory.append({
                "ts": time.time(),
                "topic": topic,
                "emotion": emotion_label or "neutral",
                "valence": round(self.state.valence, 2),
            })
            # 保留最近 8 条（一轮约 2-4 条 * 2-3 轮）
            if len(self.state.trajectory) > 8:
                self.state.trajectory = self.state.trajectory[-8:]

        self.state.last_update_ts = time.time()
        self.save()

    # =========================================================================
    # Phase7-2: 未说出口的话（inner 独白层）
    # =========================================================================
    def record_inner(self, inner_text: str):
        """记录一轮被剥离的 <inner>...</inner> 内心独白。

        inner_text 是 model 输出的"她心里想说但没说出口的话"。
        写入 last_inner（下一轮 prompt 注入）+ inner_history（最近 5 条累积）。
        """
        if not inner_text or not inner_text.strip():
            return
        self.state.last_inner = inner_text.strip()[:200]  # 截断防长
        self.state.inner_history.append({
            "ts": time.time(),
            "inner": self.state.last_inner,
        })
        # 保留最近 5 条 inner 历史
        if len(self.state.inner_history) > 5:
            self.state.inner_history = self.state.inner_history[-5:]
        self.save()

    def get_inner_snippet(self) -> str:
        """生成注入 prompt 的 inner 片段——"你上一轮心里想说但没说出口的话"。

        格式：【未说出口】上一轮你心里想："……其实我也挺怕你不在意的"
        让 model 知道自己在憋什么，下一轮回复可以带欲言又止的痕迹。
        """
        last = self.state.last_inner
        if not last:
            return ""
        return f"【未说出口】上一轮你心里想说但没说出口的话：「{last}」\n（让下一轮的回复隐约带一点欲言又止的痕迹，但不要直接复述。可以欲言又止、突然沉默、或者用别的话题岔开。）"

    # =========================================================================
    # Phase7-2 (C方案): 从 reply 被动提取"未说出口的话"
    # =========================================================================
    # DeepSeek 对 <inner> 标签指令遵循度低，改为从 reply 本身的欲言又止痕迹提取。
    # 害羞的人 reply 里的省略号、回避、转移话题本身就是"没说出口的话"的痕迹。
    #
    # 提取规则：
    # 1. 强情绪触发词（想你了/被撩/被夸/撒娇/脆弱）时才提取，纯技术讨论不提
    # 2. 找 reply 里最"欲言又止"的片段：省略号开头的句子 + 回避/否定词
    # 3. 提取后转成第一人称内心独白
    INNER_TRIGGER_KEYWORDS = [
        "想你了", "想你", "喜欢你", "想见你", "爱你",   # 直球
        "好看", "漂亮", "可爱", "头发",                 # 被撩/被夸外貌
        "干净", "厉害", "聪明", "牛逼", "强", "棒", "优秀", "好",  # 被夸能力
        "她是谁", "学姐", "那个女生",                   # 吃醋
        "对不起", "抱歉", "我错了",                     # 脆弱/道歉
        "没事", "没什么", "别多想",                     # 欲言又止标志
    ]

    # 回避/否定词——reply 里省略号后跟这些词说明"本可以但没说"
    AVOIDANCE_MARKERS = [
        "没什么", "别多想", "没事", "算了", "不说这个", "别说这个",
        "没什么啦", "没什么特别的", "就这样", "不知道",
        "没有啦", "没有", "没、没有",   # 结巴式否定（被夸时典型）
        "不是啦", "才没有", "才不是",   # 否认
        "随便", "随便扎的", "随便弄的",  # 贬低自己回避夸奖
        "别说", "别说了", "别提", "别问", "别夸",  # 直接回避
        "别闹", "别扯", "别扯了",     # 转移
    ]

    # 回避粒子——省略号后短句含这些字即视为回避（更宽泛的兜底）
    AVOIDANCE_PARTICLES = set("别没不才休莫")

    def extract_inner_from_reply(self, reply: str, user_text: str) -> str:
        """从 reply 提取未说出口的话（C方案，被动提取）。

        只在触发强情绪关键词时提取，避免每轮都生成假 inner。
        提取逻辑：找 reply 里"省略号开头 + 回避/否定词"的句子，
        那句话本身就是她"说出口但欲言又止"的部分——把它转成内心独白。
        不编造内容，只从 reply 真实文本里取。
        提不到就返回空字符串（不强求）。
        """
        if not reply:
            return ""

        # 1. 检查是否触发强情绪（user_text 或 reply 含触发词）
        combined = (user_text or "") + reply
        triggered = any(kw in combined for kw in self.INNER_TRIGGER_KEYWORDS)
        if not triggered:
            return ""

        # 2. 找 reply 里"省略号+回避"的片段——这是欲言又止的痕迹
        #    害羞的人 reply 里的省略号本身就是"她停顿了没说完"的信号。
        #    触发强情绪时，取第一个省略号后的短句作为 inner（她没说完/回避的部分）。
        import re as _re
        # 用省略号/句号/感叹号/问号统一拆句（一段可能含多个省略号分隔的小句）
        segments = _re.split(r"[。！？]|(?:……|\.{2,})", reply)
        candidates = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            # 剥颜文字 (xxx)
            seg = _re.sub(r"\([^)]{1,15}\)", "", seg)
            seg = _re.sub(r"\s+", " ", seg).strip()  # 压多空格为单
            if not seg:
                continue
            # 排除纯技术内容
            if any(w in seg for w in ["step", "loss", "epoch", "batch", "optimizer", "import", "def ", "class "]):
                continue
            # 短句（≤15字）直接加入；超长段按中文逗号拆取第一短句
            if len(seg) <= 15:
                candidates.append(seg)
            else:
                # 按中文逗号/分号拆分，取第一个短句
                for sub in _re.split(r"[，；]", seg):
                    sub = sub.strip()
                    if sub and len(sub) <= 20 and not any(w in sub for w in ["step", "loss", "epoch", "batch", "optimizer"]):
                        candidates.append(sub)
                        break

        if not candidates:
            return ""

        # 3. 取最短的一条（最欲言又止的通常最短），转成内心独白
        inner = min(candidates, key=len)
        # 加省略号前缀保持内心独白语感
        return f"……{inner}"[:200]

    def get_recent_trajectory_snippet(self, max_items: int = 5) -> str:
        """Phase6: 关系近期轨迹摘要——最近几轮的主题 + 情感事件。
        【近期轨迹】14:30 "想你了"→melting(积极), 14:05 "这个loss怎么"→shy(中性)...

        给 model 知道"我们刚聊过什么、情绪峰值在哪"，避免话题突兀跳跃，
        也让"我们刚说到X"这种连续感有依据（不是凭空编）。
        """
        traj = self.state.trajectory
        if not traj:
            return ""

        recent = traj[-max_items:]
        now = time.time()
        parts = []
        for rec in recent:
            elapsed = now - rec["ts"]
            if elapsed < 60:
                time_str = "刚刚"
            elif elapsed < 3600:
                time_str = f"{int(elapsed // 60)}分钟前"
            elif elapsed < 86400:
                time_str = f"{int(elapsed // 3600)}小时前"
            else:
                time_str = f"{int(elapsed // 86400)}天前"

            topic = rec["topic"]
            emo = rec.get("emotion", "neutral")
            v = rec.get("valence", 0.0)
            if v > 0.2:
                v_tag = "积极"
            elif v < -0.2:
                v_tag = "负面"
            else:
                v_tag = "中性"
            parts.append(f'{time_str}"{topic}"→{emo}({v_tag})')

        return f"【近期轨迹】{', '.join(parts)}"

    # =========================================================================
    # 查询接口
    # =========================================================================
    def get_prompt_snippet(self) -> str:
        """生成注入 system prompt 的心境描述片段

        model 看到这段就知道自己当前情绪，反应基于真实状态而不是编。
        """
        s = self.state
        v_label = self._valence_label(s.valence)
        a_label = self._arousal_label(s.arousal)
        d_label = self._dominance_label(s.dominance)

        # 组合一句自然描述
        if s.arousal < 0.3:
            state_desc = f"当前心境：{v_label}，平静"
        else:
            state_desc = f"当前心境：{v_label}，{a_label}"

        if s.dominance < 0.3:
            state_desc += "，有点没底气"
        elif s.dominance > 0.7:
            state_desc += "，有掌控感"

        # tsundere 傲娇倾向：passion 高 + dominance 高时倾向嘴硬
        # 不单独存状态，是 mood 衍生属性——高激情(被撩过)+高支配(有底气) = 傲娇
        tsundere_hint = ""
        try:
            rel = self.relationship
            if rel.passion > 0.7 and s.dominance > 0.6:
                tsundere_hint = (
                    "【语气倾向：稍带傲娇】嘴上会硬一点（'才不是''顺手''别误会''哼'），"
                    "但行动上继续关心、回应继续推进。回复结构：先嘴硬一句，"
                    "再关心/回应（语气软下来），可带耳根红或嘴角压不住的侧写。"
                    "不是真拒绝，是害羞的反向表达。"
                )
        except Exception:
            pass

        return f"[心境状态：{state_desc}。基于这个状态反应，不要编造过去的事。]\n{tsundere_hint}".strip()

    # =========================================================================
    # petty_state 显式生闷气机制（小脾气/冷学长）
    # =========================================================================
    # 触发条件（调用方传 reason）：
    #   - cold_streak: 连续 N 次被冷（waiting_mode=cold 累积）
    #   - overboard_joke: 开过头玩笑（关键词检测）
    #   - ignored_long: 长时间被忽视（idle 过久 + 没主动找她）
    # 解冻条件：
    #   - he_initiated: 学长主动找她（非冷淡消息）
    #   - apologize: 关键词「对不起」「抱歉」「我错了」
    #   - timeout: 超过 petty_duration_hours 自动缓和
    PETTY_TRIGGERS = {
        "cold_streak": ("sulking", 3.0),      # 连续被冷 → 闷 3 小时
        "overboard_joke": ("sulking", 2.0),    # 过头玩笑 → 闷 2 小时
        "ignored_long": ("sulking", 4.0),      # 长期忽视 → 闷 4 小时
        "jealous_spike": ("angry", 2.0),       # 吃醋累积 → 气愤 2 小时
    }
    PETTY_APOLOGY_KEYWORDS = ["对不起", "抱歉", "我错了", "别生气", "我道歉", "是我不好", "原谅"]
    PETTY_THAW_KEYWORDS = ["想你了", "想你", "喜欢你", "爱你", "在吗", "你在干嘛", "吃饭了吗", "你今天"]  # he_initiated 信号

    def trigger_petty(self, reason: str, duration_hours: float = None) -> bool:
        """触发生闷气状态。返回是否真的触发（已 sulking 且等级更高则不覆盖）。

        reason: cold_streak / overboard_joke / ignored_long / jealous_spike
        """
        if reason not in self.PETTY_TRIGGERS:
            return False
        level, default_dur = self.PETTY_TRIGGERS[reason]
        # angry 等级 > sulking，不降级
        if self.state.petty_state == "angry" and level == "sulking":
            return False
        self.state.petty_state = level
        self.state.petty_since = time.time()
        self.state.petty_duration_hours = duration_hours if duration_hours else default_dur
        self.state.petty_trigger = reason
        # 推一点 mood：微负效价 + 支配提升（有资格冷他）
        self.state.valence = max(-1.0, self.state.valence - 0.15)
        self.state.dominance = min(1.0, self.state.dominance + 0.1)
        self.save()
        import logging
        logging.getLogger(__name__).info(
            f"[petty] triggered: {level} (reason={reason}, dur={self.state.petty_duration_hours}h)"
        )
        return True

    def try_thaw_petty(self, user_text: str = "") -> bool:
        """尝试解冻生闷气状态。返回是否解冻成功。

        解冻途径：
        1. 超时（now - petty_since > duration）
        2. 道歉关键词
        3. he_initiated 信号（主动找她）
        """
        if self.state.petty_state == "none":
            return False

        now = time.time()
        # 1. 超时自动解冻
        elapsed_h = (now - self.state.petty_since) / 3600.0
        if elapsed_h >= self.state.petty_duration_hours:
            self._clear_petty("timeout")
            return True

        text = user_text or ""
        # 2. 道歉关键词 → 立即解冻（且 mood 回暖）
        for kw in self.PETTY_APOLOGY_KEYWORDS:
            if kw in text:
                self._clear_petty("apologize")
                # 被哄了，valence 回暖
                self.state.valence = min(1.0, self.state.valence + 0.25)
                self.state.dominance = max(0.0, self.state.dominance - 0.1)
                self.save()
                return True

        # 3. he_initiated 信号（非冷淡的主动消息）
        for kw in self.PETTY_THAW_KEYWORDS:
            if kw in text:
                # he_initiated 解冻较慢——软化而非清空
                # 降级 angry→sulking，sulking 缩短剩余时间
                if self.state.petty_state == "angry":
                    self.state.petty_state = "sulking"
                    self.state.petty_duration_hours = 1.0  # 再闷 1 小时就消
                    self.state.petty_since = now
                    self.save()
                    return False  # 还没完全解冻
                else:
                    self._clear_petty("he_initiated")
                    self.state.valence = min(1.0, self.state.valence + 0.1)
                    self.save()
                    return True
        return False

    def _clear_petty(self, reason: str):
        self.state.petty_state = "none"
        self.state.petty_since = 0.0
        self.state.petty_trigger = ""
        import logging
        logging.getLogger(__name__).info(f"[petty] cleared (reason={reason})")
        self.save()

    def get_petty_snippet(self) -> str:
        """生成 petty_state 的 prompt 注入片段。无生闷气返回空串。"""
        if self.state.petty_state == "none":
            return ""
        now = time.time()
        elapsed_min = (now - self.state.petty_since) / 60.0
        remaining_min = max(0, self.state.petty_duration_hours * 60 - elapsed_min)

        if self.state.petty_state == "angry":
            tone_hint = "你正在气他。回话短、冷、带刺，不主动找话题，会直接怼回去但不是真分手那种。"
            thaw_hint = "需要他认真道歉或主动哄你才会软化。"
        else:  # sulking
            tone_hint = "你在生闷气。回话变短、敷衍、'哦''嗯'那种，不主动延展话题，但不是真生气。"
            thaw_hint = "他主动找你/说想你/道歉，你就会软化（但要嘴硬一下再原谅）。"

        return (
            f"\n[生闷气状态：{self.state.petty_state}（{self.state.petty_trigger}），"
            f"已闷{int(elapsed_min)}分钟，预计还有{int(remaining_min)}分钟自然消解。"
            f"{tone_hint}{thaw_hint}]"
        )

    def importance_from_arousal(self, base_importance: str) -> str:
        """根据当前唤醒度自动提升记忆 importance
        高唤醒（被撩/慌乱/吃醋/忘我）的记忆更值得长期保存
        """
        if self.state.arousal >= 0.7:
            return "high"  # 高唤醒 → 升级为 high
        return base_importance

    # =========================================================================
    # 标签映射
    # =========================================================================
    @staticmethod
    def _valence_label(v: float) -> str:
        if v >= 0.4: return "开心"
        if v >= 0.15: return "微开心"
        if v <= -0.4: return "低落"
        if v <= -0.15: return "微低落"
        return "中性"

    @staticmethod
    def _arousal_label(a: float) -> str:
        if a >= 0.7: return "很激动"
        if a >= 0.5: return "有点激动"
        if a <= 0.2: return "很平静"
        return "正常"

    @staticmethod
    def _dominance_label(d: float) -> str:
        if d >= 0.7: return "有底气"
        if d <= 0.3: return "没底气"
        return "正常"
