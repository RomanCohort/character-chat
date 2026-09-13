"""环境事件系统——真实天气 API + 精确农历节日。

集成 wttr.in（免 key，自动 IP 定位）获取真实天气，
用 lunardate 库计算精确农历节日（春节/端午/中秋/元宵）。
失败时 fallback 到原来的季节概率生成。
"""
import random
import logging
from datetime import datetime, date
from typing import Dict, List, Tuple, Optional

import requests
from lunardate import LunarDate


logger = logging.getLogger(__name__)


# ==================== 天气系统 ====================

WEATHER_TYPES = {
    "sunny": {"name": "晴天", "mood_modifier": 0.2, "emoji": "☀️"},
    "cloudy": {"name": "多云", "mood_modifier": 0.0, "emoji": "☁️"},
    "rainy": {"name": "下雨", "mood_modifier": -0.2, "emoji": "🌧️"},
    "snowy": {"name": "下雪", "mood_modifier": 0.1, "emoji": "🌨️"},
    "stormy": {"name": "暴风雨", "mood_modifier": -0.4, "emoji": "⛈️"},
}

# 季节 -> 天气概率分布（fallback 用）
SEASON_WEATHER_PROBS = {
    "spring": {"sunny": 0.4, "cloudy": 0.3, "rainy": 0.25, "snowy": 0.0, "stormy": 0.05},
    "summer": {"sunny": 0.5, "cloudy": 0.2, "rainy": 0.15, "snowy": 0.0, "stormy": 0.15},
    "autumn": {"sunny": 0.45, "cloudy": 0.35, "rainy": 0.15, "snowy": 0.0, "stormy": 0.05},
    "winter": {"sunny": 0.2, "cloudy": 0.3, "rainy": 0.1, "snowy": 0.3, "stormy": 0.1},
}

# 天气缓存：{date: (weather_key, weather_name, temp)}
_WEATHER_CACHE: Dict[date, Tuple[str, str, int]] = {}


def map_wttr_desc_to_key(desc: str) -> str:
    """把 wttr.in 的 weatherDesc 映射到 5 种 key。

    优先级：thunder/storm > rain > snow > cloud > sunny
    """
    d = desc.lower()
    if "thunder" in d or "storm" in d:
        return "stormy"
    if "rain" in d or "drizzle" in d or "shower" in d:
        return "rainy"
    if "snow" in d or "sleet" in d:
        return "snowy"
    if "cloud" in d or "overcast" in d or "fog" in d:
        return "cloudy"
    return "sunny"


def get_weather_real(dt: date, timeout: float = 5.0) -> Optional[Tuple[str, str, int]]:
    """调 wttr.in 真实天气（自动 IP 定位）。

    返回 (weather_key, weather_name, temp_c)，失败返回 None。
    """
    # 检查缓存
    if dt in _WEATHER_CACHE:
        return _WEATHER_CACHE[dt]

    try:
        # wttr.in 自动定位请求 IP
        url = "https://wttr.in/?format=j1"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        current = data["current_condition"][0]
        desc = current["weatherDesc"][0]["value"]
        temp = int(current["temp_C"])

        weather_key = map_wttr_desc_to_key(desc)
        weather_name = WEATHER_TYPES[weather_key]["name"]

        result = (weather_key, weather_name, temp)
        _WEATHER_CACHE[dt] = result
        return result
    except Exception as e:
        logger.warning(f"wttr.in failed: {e}")
        return None


# ==================== 节日系统 ====================

HOLIDAYS = {
    # (月, 日): {"name": "节日名", "passion_modifier": 加成值, "description": "描述"}
    (1, 1): {"name": "元旦", "passion_modifier": 0.1, "description": "新年第一天"},
    (2, 14): {"name": "情人节", "passion_modifier": 0.3, "description": "西方情人节"},
    (3, 8): {"name": "妇女节", "passion_modifier": 0.05, "description": "国际妇女节"},
    (5, 1): {"name": "劳动节", "passion_modifier": 0.05, "description": "劳动节假期"},
    (5, 20): {"name": "520", "passion_modifier": 0.25, "description": "网络情人节"},
    (6, 1): {"name": "儿童节", "passion_modifier": 0.05, "description": "儿童节"},
    (10, 1): {"name": "国庆节", "passion_modifier": 0.1, "description": "国庆节假期"},
    (12, 25): {"name": "圣诞节", "passion_modifier": 0.15, "description": "圣诞节"},
}

# 农历节日（每年公历日期不同，用 lunardate 实时算）
# 格式：(农历月, 农历日) -> 节日信息
LUNAR_HOLIDAY_DEFS = [
    (1, 1, "春节", 0.2, "农历新年"),
    (5, 5, "端午节", 0.1, "端午节"),
    (8, 15, "中秋节", 0.15, "中秋节"),
    (1, 15, "元宵节", 0.1, "元宵节"),
]

# 缓存：{year: {公历日期: 节日信息}}
_LUNAR_HOLIDAYS_CACHE: Dict[int, Dict[date, dict]] = {}


def get_lunar_holidays(year: int) -> Dict[date, dict]:
    """用 lunardate 实时算某年所有农历节日的公历日期。"""
    if year in _LUNAR_HOLIDAYS_CACHE:
        return _LUNAR_HOLIDAYS_CACHE[year]

    holidays = {}
    for lunar_m, lunar_d, name, passion_mod, desc in LUNAR_HOLIDAY_DEFS:
        try:
            # 2026 年春节是 2026-02-17，元宵节是正月十五 = 2026-03-03
            # 但元旦和春节可能在同一年，也可能跨（如元旦在 2025 而春节在 2026）
            # 所以算 year-1 的农历 + year 的农历，覆盖所有情况
            solar = LunarDate(year, lunar_m, lunar_d).to_solar_date()
            holidays[solar] = {
                "name": name,
                "passion_modifier": passion_mod,
                "description": desc,
            }
        except Exception as e:
            logger.warning(f"lunardate {year}-{lunar_m}-{lunar_d} failed: {e}")
            continue
        # 也尝试 year+1 的春节（可能落在 year 的 2 月）
        if lunar_m == 1 and lunar_d == 1:
            try:
                solar_next = LunarDate(year + 1, lunar_m, lunar_d).to_solar_date()
                if solar_next.year == year:
                    holidays[solar_next] = {
                        "name": name,
                        "passion_modifier": passion_mod,
                        "description": desc,
                    }
            except Exception:
                pass

    _LUNAR_HOLIDAYS_CACHE[year] = holidays
    return holidays


def _resolve_lunar_holidays_for(dt: date) -> Optional[dict]:
    """检测当天是否是农历节日（查当年 + 前一年缓存）。"""
    holidays = get_lunar_holidays(dt.year)
    if dt in holidays:
        return holidays[dt]
    # 跨年元旦/春节：春节可能落在前一年
    holidays_prev = get_lunar_holidays(dt.year - 1)
    if dt in holidays_prev:
        return holidays_prev[dt]
    return None


# ==================== 随机事件系统 ====================

RANDOM_EVENTS = {
    "positive": [
        # 食堂/吃饭
        {"name": "食堂今天有红烧肉", "mood_modifier": 0.1, "weight": 0.2},
        {"name": "食堂阿姨多给了一勺菜", "mood_modifier": 0.1, "weight": 0.2},
        {"name": "尝到一家新开的奶茶店", "mood_modifier": 0.15, "weight": 0.15},
        {"name": "早餐的豆浆是热的", "mood_modifier": 0.05, "weight": 0.2},
        # 学习/学术
        {"name": "代码一次跑通了", "mood_modifier": 0.2, "weight": 0.15},
        {"name": "一道卡了三天的题突然想通了", "mood_modifier": 0.25, "weight": 0.1},
        {"name": "CSP 模拟赛分数比上次高", "mood_modifier": 0.2, "weight": 0.1},
        {"name": "图书馆座位很好找", "mood_modifier": 0.1, "weight": 0.2},
        {"name": "论文里看到一个漂亮的想法", "mood_modifier": 0.2, "weight": 0.1},
        {"name": "TorusFold 的 loss 终于降了", "mood_modifier": 0.25, "weight": 0.1},
        # 生活/关系
        {"name": "收到学长的消息", "mood_modifier": 0.3, "weight": 0.15},
        {"name": "学长记得她随口说过的话", "mood_modifier": 0.3, "weight": 0.1},
        {"name": "在走廊碰到学长", "mood_modifier": 0.2, "weight": 0.1},
        {"name": "天气刚好适合穿那件浅蓝衬衫", "mood_modifier": 0.1, "weight": 0.15},
        {"name": "养的蓝鸢尾开了新花", "mood_modifier": 0.2, "weight": 0.1},
        {"name": "耳机里的歌刚好契合心情", "mood_modifier": 0.1, "weight": 0.15},
        {"name": "室友帮她带了饭", "mood_modifier": 0.1, "weight": 0.15},
        {"name": "宿管阿姨夸她有礼貌", "mood_modifier": 0.05, "weight": 0.15},
    ],
    "negative": [
        # 学术压力
        {"name": "导师催进度", "mood_modifier": -0.3, "weight": 0.2},
        {"name": "考试周快到了", "mood_modifier": -0.2, "weight": 0.2},
        {"name": "代码跑了一晚又崩了", "mood_modifier": -0.25, "weight": 0.15},
        {"name": "CSP 模拟赛分数掉下去了", "mood_modifier": -0.25, "weight": 0.1},
        {"name": "论文被审稿人挑刺", "mood_modifier": -0.2, "weight": 0.1},
        {"name": "TorusFold 的 loss 震荡", "mood_modifier": -0.2, "weight": 0.1},
        {"name": "组会要讲但还没准备好", "mood_modifier": -0.2, "weight": 0.1},
        # 身体/环境
        {"name": "图书馆空调太冷", "mood_modifier": -0.1, "weight": 0.2},
        {"name": "食堂排队好长", "mood_modifier": -0.1, "weight": 0.2},
        {"name": "没睡好头疼", "mood_modifier": -0.2, "weight": 0.15},
        {"name": "下雨鞋湿了", "mood_modifier": -0.15, "weight": 0.15},
        {"name": "自习室有人一直说话", "mood_modifier": -0.15, "weight": 0.15},
        {"name": "电脑风扇突然响得吓人", "mood_modifier": -0.1, "weight": 0.15},
        # 关系/情绪
        {"name": "想学长但不敢先发消息", "mood_modifier": -0.15, "weight": 0.15},
        {"name": "又想到家里会不会不同意", "mood_modifier": -0.2, "weight": 0.1},
        {"name": "被室友开玩笑说她天天看手机", "mood_modifier": -0.1, "weight": 0.15},
        {"name": "论文数据对不上", "mood_modifier": -0.2, "weight": 0.1},
    ],
    "neutral": [
        {"name": "今天课好多", "mood_modifier": 0.0, "weight": 0.3},
        {"name": "在实验室待了一天", "mood_modifier": 0.0, "weight": 0.3},
        {"name": "刷了一下午题", "mood_modifier": 0.0, "weight": 0.3},
        {"name": "整理学长学姐的学术遗产", "mood_modifier": 0.0, "weight": 0.3},
        {"name": "在看一篇综述", "mood_modifier": 0.0, "weight": 0.3},
        {"name": "在等云端训练跑完", "mood_modifier": 0.0, "weight": 0.3},
        {"name": "什么特别的事都没发生", "mood_modifier": 0.0, "weight": 0.2},
    ],
}


# ==================== 季节计算 ====================

def get_season(month: int) -> str:
    """根据月份返回季节"""
    if month in [3, 4, 5]:
        return "spring"
    elif month in [6, 7, 8]:
        return "summer"
    elif month in [9, 10, 11]:
        return "autumn"
    else:  # 12, 1, 2
        return "winter"


def get_season_name(season: str) -> str:
    """季节中文名"""
    return {
        "spring": "春天",
        "summer": "夏天",
        "autumn": "秋天",
        "winter": "冬天",
    }.get(season, "未知")


# ==================== 天气生成 ====================

def get_weather_fallback(dt: date) -> str:
    """fallback：根据日期和季节生成天气（用日期 seed 保证同一天一致）"""
    season = get_season(dt.month)
    probs = SEASON_WEATHER_PROBS[season]

    # 用日期作为随机种子
    seed = dt.toordinal()
    rng = random.Random(seed)

    weathers = list(probs.keys())
    weights = list(probs.values())
    return rng.choices(weathers, weights=weights, k=1)[0]


def get_weather_with_temp(dt: date) -> Tuple[str, str, int]:
    """优先调真实天气 API，失败 fallback。

    返回 (weather_key, weather_name, temp_c)。
    temp_c 在 fallback 时按季节给个估算值。
    """
    # 真实天气
    real = get_weather_real(dt)
    if real is not None:
        return real

    # fallback
    weather_key = get_weather_fallback(dt)
    weather_name = WEATHER_TYPES[weather_key]["name"]
    # 季节估算温度（长春基线，夏天 25，冬天 -15，春秋 10）
    season_temps = {"spring": 10, "summer": 25, "autumn": 10, "winter": -15}
    temp_c = season_temps[get_season(dt.month)]
    return (weather_key, weather_name, temp_c)


# ==================== 节日检测 ====================

def get_holiday(dt: date) -> Optional[dict]:
    """检测当天是否是节日（公历 + 精确农历）。"""
    key = (dt.month, dt.day)

    # 先查公历节日
    if key in HOLIDAYS:
        return HOLIDAYS[key]

    # 再查精确农历节日（lunardate 实时算）
    lunar = _resolve_lunar_holidays_for(dt)
    if lunar is not None:
        return lunar

    return None


# ==================== 随机事件生成 ====================

def get_random_event(dt: date) -> Optional[Dict]:
    """按小时生成随机事件（同一小时稳定）。

    seed = date.toordinal() * 24 + hour，保证同一天同一小时返回同一事件。
    不同小时的事件不同，让一天内情绪随小时波动。
    """
    # 支持 datetime 和 date：datetime 才有 hour，date 默认 12（中午）
    hour = dt.hour if isinstance(dt, datetime) else 12
    seed = dt.toordinal() * 24 + hour
    rng = random.Random(seed)

    # 70% 概率有事件
    if rng.random() < 0.3:
        return None

    # 按权重随机选事件类型
    all_events = []
    for category, events in RANDOM_EVENTS.items():
        for event in events:
            all_events.append((category, event))

    # 随机选一个
    chosen_category, chosen_event = rng.choice(all_events)

    return {
        "category": chosen_category,
        **chosen_event,
    }


# ==================== 主接口 ====================

def get_environment_state(dt=None) -> Dict:
    """获取当前环境状态（真实天气 + 精确农历节日 + 按小时事件）。

    dt 可以是 datetime（用其 hour 算按小时事件）或 date（默认中午 12 点）。
    """
    if dt is None:
        dt = datetime.now()
    # 统一成 date 用于天气/节日判断
    dt_date = dt.date() if isinstance(dt, datetime) else dt

    season = get_season(dt_date.month)
    weather_key, weather_name, temp_c = get_weather_with_temp(dt_date)
    weather = WEATHER_TYPES[weather_key]
    holiday = get_holiday(dt_date)
    random_event = get_random_event(dt)  # 传原 dt（可能带 hour）

    # 计算总心情修正
    total_mood_modifier = weather["mood_modifier"]
    # 温度心情修正：太冷或太热都会让心情略降（2 倍权重）
    if temp_c < -5:
        total_mood_modifier -= 0.1
    elif temp_c > 35:
        total_mood_modifier -= 0.1
    elif 18 <= temp_c <= 26:
        total_mood_modifier += 0.06  # 舒适温度小加（2 倍）
    if random_event:
        total_mood_modifier += random_event["mood_modifier"]

    # 计算 passion 修正（节日加成）
    passion_modifier = 0.0
    if holiday:
        passion_modifier = holiday.get("passion_modifier", 0.0)

    return {
        "date": dt_date,
        "season": season,
        "season_name": get_season_name(season),
        "weather": weather_key,
        "weather_name": weather_name,
        "weather_emoji": weather["emoji"],
        "temp_c": temp_c,
        "holiday": holiday,
        "random_event": random_event,
        "mood_modifier": total_mood_modifier,
        "passion_modifier": passion_modifier,
    }


def get_environment_snippet(dt: date = None) -> str:
    """生成环境描述，用于注入 system prompt（带真实温度）。"""
    state = get_environment_state(dt)

    parts = []

    # 季节、天气、温度（真实数据）
    temp_str = f"{state['temp_c']}°C" if state['temp_c'] >= 0 else f"{state['temp_c']}°C"
    parts.append(
        f"【现在是{state['season_name']}，"
        f"天气{state['weather_name']}{state['weather_emoji']}，"
        f"气温{temp_str}】"
    )

    # 节日
    if state["holiday"]:
        parts.append(f"【今天是{state['holiday']['name']}：{state['holiday']['description']}】")
        parts.append(f"【今天是{state['holiday']['name']}：{state['holiday']['description']}】")

    # 随机事件
    if state["random_event"]:
        event = state["random_event"]
        category_label = {
            "positive": "开心",
            "negative": "烦心",
            "neutral": "日常",
        }.get(event["category"], "")
        parts.append(f"【{category_label}的事：{event['name']}】")

    return "\n".join(parts)


def get_mood_modifiers(dt: date = None) -> Tuple[float, float]:
    """返回 (mood_modifier, passion_modifier)"""
    state = get_environment_state(dt)
    return state["mood_modifier"], state["passion_modifier"]


# ==================== 测试 ====================

if __name__ == "__main__":
    test_date = date(2026, 2, 14)  # 情人节
    print(f"测试日期: {test_date}")
    print(get_environment_snippet(test_date))
    print(f"\n心情修正: {get_mood_modifiers(test_date)}")

    print("\n" + "="*50 + "\n")

    test_date = date(2026, 7, 15)  # 夏天随机一天
    print(f"测试日期: {test_date}")
    print(get_environment_snippet(test_date))
    print(f"\n心情修正: {get_mood_modifiers(test_date)}")
