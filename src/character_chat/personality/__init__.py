# -*- coding: utf-8 -*-
"""personality —— 从 CLF 的人格机制里搬过来的那一层。

现在有两件，都接在 CLF core/personality/ 上：
  · tripartite —— 三元竞逐（语域仲裁 + 语域->采样温度）
  · epigenetic —— 表观遗传式特质漂移
各自文件顶部写着照搬了什么、故意偏离了什么。
"""
from .epigenetic import (          # noqa: F401
    COMMIT_AT,
    DRIFT,
    EpigeneticDrift,
    MAX_DRIFT,
    TRAUMA_THRESHOLD,
)
from .tripartite import (          # noqa: F401
    DEFAULT_WEIGHTS,
    DIRECTIVES,
    TRANSITION_SPEED,
    TripartiteArbiter,
    pick_branch,
)

__all__ = [
    "TripartiteArbiter",
    "DEFAULT_WEIGHTS",
    "DIRECTIVES",
    "TRANSITION_SPEED",
    "pick_branch",
    "EpigeneticDrift",
    "DRIFT",
    "COMMIT_AT",
    "MAX_DRIFT",
    "TRAUMA_THRESHOLD",
]
