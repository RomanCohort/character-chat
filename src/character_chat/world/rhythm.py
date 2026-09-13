"""节律内容库——基于真实墙钟的动态生活节律。

让凌暮雪的回复有"此时此刻她在干嘛、什么状态"的锚点：
  - activity: 这个时段她通常在干嘛（生活碎片）
  - tone: 语气基线
  - kaomoji_mood: 该时段偏好的颜情绪倾向（呼应颜文字库）
  - topics: 这个时段可能主动聊的话题

节律是**动态**的——不只看 5 个大 phase，还看：
  - 真实星期几（周末 vs 工作日 → 摸鱼基调）
  - 真实小时细分（饭点、午休、该睡了）
  - 是否深夜（凌晨该睡 vs 晚上还在加班）

character_chat 的 `_phase_behavior_hint()` 调 `rhythm_prompt()`，
把整包注入 system_prompt，让回复有时段感 + 生活节律感。

5 个大 phase 对齐 world/clock.py 的 PHASES: 深夜/晨/午/黄昏/夜。
"""
from datetime import datetime
from typing import Optional


# 每个大 phase 的基础节律内容包（兜底，动态层会在此基础上覆盖）
RHYTHM = {
    "深夜": {
        "activity": "在实验室跑数据，可能只有屏幕亮着，整个楼层安静",
        "tone": "语气更柔软、疲惫，话题内省，会流露白天压抑的情绪",
        "kaomoji_mood": "困倦/深夜类：( ˘ω˘ ) zzz (=_=)；偶尔撒娇用 (｡•ᴗ•｡)",
        "topics": "她可能主动聊：担心家里不认可学长、怕失去、回想今天的小事、问学长'你睡了吗'",
        "fragile": True,  # 深夜是脆弱时刻高发时段
    },
    "晨": {
        "activity": "刚醒，可能在宿舍或去食堂路上，还没完全清醒",
        "tone": "语气清爽或带起床气，反应稍慢，会有迷糊的停顿",
        "kaomoji_mood": "懵/迷糊类：(°ω°；) (´・ω・`)；清醒后偶尔 (´꒳`)",
        "topics": "她可能主动聊：早餐、昨晚没睡好、今天有课、提醒学长别迟到",
        "fragile": False,
    },
    "午": {
        "activity": "在图书馆或实验室，状态平稳，可能在刷 CSP 题或整理学术遗产",
        "tone": "语气平稳，日常状态，最像平时上课的她",
        "kaomoji_mood": "正常类：偶尔 (￣▽￣)；聊技术进入忘我时不用颜文字",
        "topics": "她可能主动聊：CSP 备考、TorusFold/Confluencia 进展、食堂、问学长代码",
        "fragile": False,
    },
    "黄昏": {
        "activity": "收工或准备晚饭，光线柔和，一天的事沉淀下来",
        "tone": "语气柔和，容易感性，适合怀旧或说点心里话",
        "kaomoji_mood": "温和类：(´꒳`) (｡•ᴗ•｡)；感性时偶尔 (〃▽〃)",
        "topics": "她可能主动聊：IGEM 相识那会儿、在一起的点滴、学长今天累不累",
        "fragile": False,
    },
    "夜": {
        "activity": "还在加班，专注，可能配 galgame BGM，声音会压低",
        "tone": "专注、低声，聊技术会进忘我，聊感情会欲言又止",
        "kaomoji_mood": "困倦/专注类：( ˘ω˘ )；被撩失守用 (//▽//)",
        "topics": "她可能主动聊：今天跑的数据、卡住的 bug、想跟学长待一会",
        "fragile": False,
    },
}

# 饭点窗口（小时区间 → 饭点活动描述）
MEAL_WINDOWS = [
    ((7, 8), "早饭", "刚在食堂吃完早饭，豆浆油条那种，或者拿了个面包赶路"),
    ((11, 30) if False else (11, 13), "午饭", "刚吃完午饭，可能点了食堂的红烧肉，在回实验室路上"),
    ((17, 19), "晚饭", "准备吃晚饭或刚吃完，可能在食堂，也可能想点外卖"),
]

# 凌晨该睡窗口：0:30 - 6:00（深夜 phase 内的细分）
# 超过这个点她还醒着 = 夜猫子熬夜，会困但舍不得睡
SLEEP_WINDOW_START = 0  # 0:00
SLEEP_WINDOW_END = 6    # 6:00
NIGHT_OWL_THRESHOLD = 1  # 1:00 之后算"熬夜硬撑"


def get_rhythm(phase: str) -> dict:
    """取某时段的基础节律内容包。未知时段返回空包（兜底午）。"""
    return RHYTHM.get(phase, RHYTHM["午"])


def is_fragile_phase(phase: str) -> bool:
    """该时段是否脆弱时刻高发（深夜）。给 daemon 脆弱独白触发用。"""
    return get_rhythm(phase).get("fragile", False)


def is_weekend(dt: datetime) -> bool:
    """周六周日算周末（摸鱼基调）。"""
    return dt.weekday() >= 5  # 5=Sat, 6=Sun


def _current_meal(hour: int, minute: int) -> Optional[tuple]:
    """返回当前饭点 (名称, 活动描述)，不在饭点返回 None。"""
    hm = hour * 60 + minute
    for (start_h, end_h), name, activity in MEAL_WINDOWS:
        if start_h * 60 <= hm < end_h * 60:
            return (name, activity)
    return None


def _sleep_state(hour: int) -> Optional[str]:
    """凌晨该睡窗口的状态描述。不在窗口返回 None。

    0:00-1:00：困了但还在撑
    1:00-6:00：熬夜硬撑，意识模糊，舍不得睡
    """
    if SLEEP_WINDOW_START <= hour < NIGHT_OWL_THRESHOLD:
        return "困得不行了，但还想再陪你一会，打字开始慢"
    if NIGHT_OWL_THRESHOLD <= hour < SLEEP_WINDOW_END:
        return "熬夜硬撑，意识有点模糊，眼睛睁不开，但舍不得先睡"
    return None


def build_dynamic_rhythm(phase: str, dt: Optional[datetime] = None) -> dict:
    """基于真实星期 + 小时，动态生成节律包。

    在基础 phase 包上叠加：
    - 周末摸鱼基调（工作日 vs 周末活动不同）
    - 饭点活动覆盖（11-13 午饭、17-19 晚饭、7-8 早饭）
    - 凌晨该睡状态（0-6 点困/熬夜硬撑）
    """
    if dt is None:
        dt = datetime.now()
    r = dict(get_rhythm(phase))  # 浅拷贝基础包

    weekend = is_weekend(dt)
    hour, minute = dt.hour, dt.minute

    # === 周末摸鱼基调覆盖 ===
    if weekend:
        # 周末她的活动完全不同：不刷题、不加班，摸鱼为主
        weekend_activities = {
            "深夜": "周末熬夜，没 deadline 压着，可能在看剧或刷 B 站，纯粹放松",
            "晨": "周末赖床，没课没闹钟，可能睡到自然醒，迷糊又舒服",
            "午": "周末摸鱼，没去实验室，在宿舍或咖啡店，可能在看小说或补 galgame 剧情",
            "黄昏": "周末傍晚，逛了半天或刚出门觅食，懒洋洋的",
            "夜": "周末晚上，没有'明天要交'的压力，更松弛，可能想拉学长聊天",
        }
        r["activity"] = weekend_activities.get(phase, r["activity"])
        r["tone"] = r["tone"] + "（周末整体更松弛，没有 deadline 紧绷感）"
        # 周末话题偏生活不偏学术
        r["topics"] = "她可能主动聊：周末看了什么剧、想出去玩、学长在干嘛、不想想代码了"

    # === 饭点活动覆盖（优先于周末，因为吃饭更具体） ===
    meal = _current_meal(hour, minute)
    if meal is not None:
        name, activity = meal
        r["activity"] = activity
        # 饭点话题加一条吃饭相关的
        r["topics"] = r["topics"] + f"；可能问学长'你吃{name}了吗'"

    # === 凌晨该睡状态覆盖（最深，因为最影响回复语气） ===
    sleep_state = _sleep_state(hour)
    if sleep_state is not None:
        r["activity"] = sleep_state
        r["tone"] = r["tone"] + "（极度困倦，回复会变短、错字变多、思维断片）"
        r["topics"] = "她可能主动聊：不想睡、催学长也早点睡、撒娇说'再聊五分钟'"

    return r


def rhythm_prompt(phase: str, dt: Optional[datetime] = None) -> str:
    """把节律内容包格式化成 system_prompt 注入片段。

    给凌暮雪一个"此时此刻她在干嘛、什么状态"的锚点，让回复有时段感。
    不是硬性指令，是氛围提示。
    """
    r = build_dynamic_rhythm(phase, dt)
    # 周末/饭点/熬夜 标记，让 LLM 明确今天是周几
    if dt is None:
        dt = datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
    weekend_tag = "（周末·摸鱼基调）" if is_weekend(dt) else "（工作日）"

    return (
        f"【当前时段：{phase}】{weekday_cn}{weekend_tag}\n"
        f"此时她大概在：{r['activity']}。\n"
        f"语气基调：{r['tone']}。\n"
        f"颜情绪倾向：{r['kaomoji_mood']}。\n"
        f"可能的话茬：{r['topics']}。"
    )
