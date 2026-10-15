# -*- coding: utf-8 -*-
"""表观遗传式特质漂移 —— 从 CLF core/personality/epigenetic.py 搬来的**触发机制**。

CLF 原版是双轨：日常进 KV cache，重大事件把 LoRA 快速权重"甲基化"固化下来。
这里只能搬前一半 —— 那一半要本地模型权重（FastWeightStore 直接操作 nn.Parameter），
而 CC 的语言来自远端 LLM，没有可改的权重。所以：

    CLF 的「甲基化 -> LoRA 权重固化」   ==   CC 的「甲基化 -> trait 漂移固化」

照搬自原版的部分：
  · MethylationTrigger 的三个阈值（情绪 0.7 / 纠错 0.8 / 创伤 0.9），
    以及它的核心思想：**不是每次交互都改人格，只有够强的事件才点火**
  · EpigeneticTag 账本（类型 / 强度 / 时间戳），保留最近 N 条

CC 自己的一处设计（别写成"复现了 CLF 的表观遗传"）：
  · **漂移不写回角色卡。** 卡是手写的基线；漂移单独存 <state_dir>/epigenetic.json，
    实际生效值 = 卡上的值 + 漂移。这样"基线 vs 漂移"随时看得见，也能一键归零。
  · 原版靠 sentiment / user_feedback 两个数值判断点火；CC 没有这两个数，改用
    **已有的事件名**（cli.py 的 _update_relationship_from_turn 每轮判出一个）
    加上情绪强度。
"""
import json
import os
import time

#: CLF 原版阈值
EMOTIONAL_THRESHOLD = 0.70
TRAUMA_THRESHOLD = 0.90

#: 同一种事件累积多少次才固化一步。这是"甲基化"的本意：
#: 一次搭话不该改人格，反复发生才留痕。
COMMIT_AT = 5

#: 强事件（强度 >= TRAUMA_THRESHOLD）一次就留痕，步长加倍。
TRAUMA_STEP_X = 2.0

#: 漂移的上下限。人格可以变，但别漂到不认识了 ——
#: 相对基线最多挪 ±0.25。
MAX_DRIFT = 0.25

#: 事件 -> 固化时推动哪些 trait、各推多少。
#: 事件名取自 cli.py::_update_relationship_from_turn（每轮判出一个，优先级从高到低），
#: 外加三元竞逐的 "task" 支（长期一起做技术 -> 更讲理）。
DRIFT = {
    "direct_response": {"warm": +0.03, "tsundere": -0.02},
    "flirted":         {"tsundere": +0.03, "warm": +0.02},
    "vulnerable":      {"warm": +0.04, "scientific": -0.02},
    "shared_memory":   {"warm": +0.03, "playful": +0.02},
    "he_remembered":   {"warm": +0.04},
    "he_initiated":    {"lively": +0.02, "playful": +0.02},
    "ignored":         {"warm": -0.04, "tsundere": +0.03, "lively": -0.02},
    "conflict":        {"warm": -0.05, "lively": -0.02},
    "milestone":       {"warm": +0.04, "playful": +0.03},
    "task":            {"scientific": +0.03, "knowledgeable": +0.02},
}


class EpigeneticDrift:
    """累积事件 -> 越过阈值 -> 把 trait 挪一点，永久留痕。"""

    def __init__(self, state_path, trait_keys=None):
        """trait_keys: 这张卡实际有的 trait 名。给了就只推这些键。

        为什么需要：DRIFT 表里写的是"概念"，而**卡与卡之间的键名并不一致** ——
        凌暮雪的卡用的是 shy/warm/lively/knowledgeable/playful/scientific，
        而 /ling/ 的默认卡用的是 tsundere/... （schema.py 的文档字符串写的是后者）。
        实测：她的卡上没有 tsundere，于是 flirted 推的那一项会被静默丢掉。
        传进来之后，表里列的概念只有卡上真有的才会生效。
        """
        self._path = state_path
        self._keys = set(trait_keys) if trait_keys else None
        st = self._load()
        self._drift = st.get("drift") or {}
        self._counters = st.get("counters") or {}
        self.tags = st.get("tags") or []

    # ── 持久化 ─────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def save(self):
        try:
            d = os.path.dirname(self._path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"drift": self._drift, "counters": self._counters,
                           "tags": self.tags[-40:]},
                          fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ── 观察 ───────────────────────────────────────────────────────
    def observe(self, event_type, intensity=0.0):
        """记一次事件。返回这次固化出来的 [(trait, 新漂移值), ...]（没固化就空）。"""
        if not event_type or event_type not in DRIFT:
            return []
        try:
            intensity = float(intensity or 0.0)
        except Exception:
            intensity = 0.0

        # CLF 的阈值：够强的事件一次点火，步长加倍
        strong = intensity >= TRAUMA_THRESHOLD
        if not strong:
            self._counters[event_type] = int(self._counters.get(event_type, 0)) + 1
            if self._counters[event_type] < COMMIT_AT:
                return []
            self._counters[event_type] = 0
        mult = TRAUMA_STEP_X if strong else 1.0

        changed = []
        skipped = []
        for trait, delta in DRIFT[event_type].items():
            if self._keys is not None and trait not in self._keys:
                skipped.append(trait)
                continue
            cur = float(self._drift.get(trait, 0.0))
            new = cur + delta * mult
            new = max(-MAX_DRIFT, min(MAX_DRIFT, new))
            if abs(new - cur) < 1e-9:
                continue
            self._drift[trait] = round(new, 4)
            changed.append((trait, self._drift[trait]))
        if not changed:
            # 这张卡上没有对应 trait —— 不算一次固化，别把账记空
            return []

        self.tags.append({
            "ts": time.time(),
            "event": event_type,
            "intensity": round(intensity, 3),
            "strong": strong,
            "changes": {t: v for t, v in changed},
        })
        return changed

    # ── 生效值 ─────────────────────────────────────────────────────
    def effective(self, base_traits):
        """卡上的基线 + 漂移 = 实际生效的人格参数。夹在 [0.05, 0.95]。"""
        out = {}
        for k, v in (base_traits or {}).items():
            try:
                val = float(v) + float(self._drift.get(k, 0.0))
            except Exception:
                val = v
            out[k] = round(max(0.05, min(0.95, val)), 3)
        return out

    def summary(self):
        return {"drift": dict(self._drift), "counters": dict(self._counters),
                "commits": len(self.tags)}
