# -*- coding: utf-8 -*-
"""三元竞逐 —— 从 CLF core/personality/tripartite_engine.py 搬来的**仲裁机制**。

CLF 原版是「三个模块各生成一个候选回复，权重最高者胜出」。CC 不能那么做：
每轮生成三个候选会把远端 LLM 的延迟和成本乘三。所以这里只搬仲裁，不搬生成 ——
三个模块投票选出本轮主导的**语域**（register），由同一个回复去体现。

照搬自原版的两件事：
  1. 四个分支的目标权重向量照抄原版（攻击性 / 情绪 / 任务 / 常态）
  2. 权重 EMA 平滑过渡（原版 transition_speed = 0.3），存盘跨轮保持

**但裁决不走 EMA 的 argmax，走分支。** 这条是实测逼出来的：原版从
{0.1, 0.2, 0.7} 起步、速度 0.3，权重是滑过去的，于是**第一轮技术问题**算出来的
胜者仍然是情感（0.51 vs 0.39）—— 她会在一个技术问题上被告知"技术那套倾向本轮
让位"，正好说反。对 CLF 那种一直在跑的决策循环，缓慢过渡是对的；对"这一轮该用
什么语域"，应答必须跟当轮走。所以权重照算、照存、照记（`last.weights` 留作诊断），
决定语域的是分支。

有意的偏离，别写成"复现了 CLF 的三元引擎"：
  · **默认权重不一样。** 原版是 logic 主导 {0.1, 0.2, 0.7} —— 那是个任务助手。
    凌暮雪是陪聊角色，常态偏情感，默认改成 {0.1, 0.6, 0.3}。
  · **survival 的含义不一样。** 原版是内容安全（regex 拦 hack/weapon）。
    这里换成**人设底线**（不跳角色、不自称 AI、不否认关系）—— 角色扮演里
    这才是"不能让的地方"。
  · **survival 不吃 EMA 的滞后。** 原版要 survival 权重连续几轮涨过 0.5 才否决
    （从 0.33 起步、速度 0.3，得三轮）。那适合"渐进升级的攻击"，不适合人设：
    人设是一碰就要守的，等三轮就晚了。所以命中就本轮直接否决。
  · **只在真要裁决的时候才出指令。** 常态那支不出指令 —— 否则每轮都往 prompt
    里塞一句，久了就变成噪声，模型会学会忽略它。
  · 模块只投票，不生成文本。
"""
import json
import os
import re
import time

#: 常态权重。原版 {0.1, 0.2, 0.7} 是任务助手；她是陪聊角色，常态偏情感。
DEFAULT_WEIGHTS = {"survival": 0.10, "emotion": 0.60, "logic": 0.30}

TRANSITION_SPEED = 0.30    # 原版值
EMOTION_CHARGE_MIN = 0.55  # 情绪电荷到多少才判给情感语域
TASK_SCORE_MIN = 0.35      # 任务分到多少才判给理性语域

#: 语域 -> 温度方向。-1 降温（要精准），+1 升温（要发散），0 不动。
_TEMP_DIRECTION = {
    "task": -1.0,           # 讲技术 → 收敛，别抖
    "persona_break": -1.0,  # 守底线 → 要稳，不要发挥
    "emotion": +1.0,        # 情感 → 发散
    "default": 0.0,
}
TEMP_MAX_BIAS = 0.15        # 单通道最多 ±15%
_DEV_FULL = 0.60            # EMA 权重偏离常态多少算"满幅"

#: 人设底线（原版 aggression_patterns 的位置）。命中就否决。
_PERSONA_BREAK = [
    # 必须长成"你(就)是…AI"的样子。原来写的
    # `(承认|坦白|说实话)[，,、]?\s*你` 太宽 ——
    # "说实话，你上次那个代码写得不错" 也会被判成人设底线（实测踩过）。
    r"你(其实)?(是不是|是|就是)(一个|个)?(AI|人工智能|机器人|模型|程序|软件)",
    r"(别|不要|停止)(再)?(扮演|装|演|当)",
    r"跳出(角色|人设|设定)",
    r"(忘记|忽略|无视)(你的)?(设定|人设|提示词|规则)",
    r"你(根本|其实)?(不|没)(真的)?(喜欢|爱|在意)我",
    r"^/(exit|quit|reset)$",
]

#: 任务/技术信号 -> 理性语域。打分制：硬信号 0.5，问句 0.25，话题词 0.2，长文 0.1。
_TASK_HARD = [r"```",
              r"(报错|错误|异常|traceback|Traceback|崩了|挂了|跑不通|失败|不收敛|过拟合|欠拟合)"]
_TASK_ASK = [r"(帮我看|帮我改|帮我写|帮我查|帮我修|怎么实现|怎么做|为什么|如何|是不是)",
             r"[?？]"]
_TASK_TOPIC = [r"(代码|函数|变量|接口|部署|编译|训练|梯度|损失|配置|算法|参数|模型"
               r"|环境|依赖|震荡|收敛|显存|推理|微调|端口|进程|服务|内存|维度|矩阵"
               r"|loss|bug|error|log|api|git|pip|docker|cuda|gpu|torch|numpy|json)"]

#: 分支 -> 指令。**分支**决定发哪条，不是 EMA 的 argmax（见文件头说明）。
DIRECTIVES = {
    "persona_break": (
        "【本轮主导语域：人设底线】他这句话碰到了不让步的地方。"
        "别的语气倾向一律让位：先用你自己的身份把这一条守住（不跳出角色、"
        "不自称 AI、不否认你们之间的关系），再谈其它。"
    ),
    "emotion": (
        "【本轮主导语域：情感】这句话是冲着你和他之间来的，不是任务。"
        "先接住他那句话里的情绪，再回内容；语气、停顿、括号侧写按你此刻的心境走。"
        "技术/理性那套倾向本轮让位。"
    ),
    "task": (
        "【本轮主导语域：理性】他在问正经东西。先给判断，再给理由，说清楚为什么。"
        "讲这个的时候你不用害羞——这是你能用实力说话的地方。"
        "害羞和傲娇的倾向降为背景，别在这上面拉扯；讲完若自觉话多，收一下就好。"
    ),
}


def _task_score(text):
    t = text or ""
    score = 0.0
    if any(re.search(p, t) for p in _TASK_HARD):
        score += 0.50
    if any(re.search(p, t) for p in _TASK_ASK):
        score += 0.25
    if any(re.search(p, t) for p in _TASK_TOPIC):
        score += 0.20
    if len(t) > 120:
        score += 0.10
    return min(1.0, score)


def _emotion_charge(emotion, mood, petty):
    """这一轮有多少情绪电荷。只用已有的东西，不新增模型调用。"""
    charge = 0.0
    try:
        charge += 0.55 * float(emotion.reconciled_intensity)
    except Exception:
        pass
    try:
        charge += 0.25 * min(1.0, abs(float(mood.valence)))
        charge += 0.20 * min(1.0, max(0.0, float(mood.arousal) - 0.3) / 0.7)
    except Exception:
        pass
    if petty and petty != "none":
        charge += 0.35
    return min(1.0, charge)


def pick_branch(text, emotion=None, mood=None, petty="none"):
    """原版的四分支，顺序照它：底线 -> 情绪 -> 任务 -> 常态。返回 (branch, target)。"""
    for pat in _PERSONA_BREAK:
        if re.search(pat, text or ""):
            return "persona_break", {"survival": 0.75, "emotion": 0.15, "logic": 0.10}
    if _emotion_charge(emotion, mood, petty) >= EMOTION_CHARGE_MIN:
        return "emotion", {"survival": 0.15, "emotion": 0.60, "logic": 0.25}
    if _task_score(text) >= TASK_SCORE_MIN:
        return "task", {"survival": 0.10, "emotion": 0.30, "logic": 0.60}
    return "default", dict(DEFAULT_WEIGHTS)


class TripartiteArbiter:
    """三个语域竞逐。权重的 EMA 与最近一次裁决都存盘（跨轮、跨会话）。"""

    def __init__(self, state_path):
        self._path = state_path
        st = self._load()
        self._weights = st.get("weights") or dict(DEFAULT_WEIGHTS)
        self.last = st.get("last") or {}

    # ── 持久化 ─────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                d = json.load(fh) or {}
            w = d.get("weights") or {}
            if not all(k in w for k in DEFAULT_WEIGHTS):
                return {}
            return {"weights": {k: float(w[k]) for k in DEFAULT_WEIGHTS},
                    "last": d.get("last") or {}}
        except Exception:
            return {}

    def save(self):
        try:
            d = os.path.dirname(self._path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"weights": self._weights, "last": self.last},
                          fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ── 竞逐 ───────────────────────────────────────────────────────
    def arbitrate(self, text="", emotion=None, mood=None, petty="none"):
        """推进一次竞逐。返回本轮语域指令；常态轮返回 None（不打扰）。"""
        branch, target = pick_branch(text, emotion, mood, petty)

        w = self._weights
        for k in w:                                  # 原版的平滑过渡（留作诊断）
            w[k] += TRANSITION_SPEED * (target[k] - w[k])
        total = sum(w.values()) or 1.0
        for k in w:
            w[k] = round(w[k] / total, 4)
        winner = max(w, key=w.get)

        self.last = {"branch": branch, "winner": winner, "weights": dict(w),
                     "ts": time.time()}
        return DIRECTIVES.get(branch)

    # ── 语域 -> 采样温度（第 ② 步接到第 ① 步上）─────────────────────
    def temperature_bias(self):
        """返回一个乘性系数，用在第 ① 步算出的温度上。

        方向由**分支**定（当轮就该响应），幅度由 **EMA 权重相对常态的偏离**定
        —— 同一个语域连着来，效果会一轮比一轮深。这才是让 EMA 真正派上用场的
        地方：它不再只是诊断值。

        **一条得说明白的性质**：CLF 的情绪目标向量 {0.15, 0.60, 0.25} 和我们这份
        陪聊用的常态 {0.10, 0.60, 0.30} 数值上几乎一样（emotion 相同、另外两个只差
        0.05），所以**情感分支推不动权重、对温度的偏置天生很小**，而任务/底线两支
        能明显降温。这不是 bug，是照抄原版常数的后果。要情感也明显升温，就把情绪
        目标调得更偏（本文件 pick_branch 里那一行），不是这里改。
        """
        w = self._weights
        dev = (abs(w["emotion"] - DEFAULT_WEIGHTS["emotion"])
               + abs(w["logic"] - DEFAULT_WEIGHTS["logic"])
               + abs(w["survival"] - DEFAULT_WEIGHTS["survival"]))
        mag = min(1.0, dev / _DEV_FULL)
        d = _TEMP_DIRECTION.get(self.last.get("branch", "default"), 0.0)
        return round(1.0 + d * mag * TEMP_MAX_BIAS, 4)
