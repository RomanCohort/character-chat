"""情绪 label → Live2D 参数映射表。

EMOTION_UPDATED 事件 payload {label, intensity, confidence} 落到 Live2D 参数。
参数范围 Live2D 通常 -1~1，表里是 intensity=1.0 时的最大偏移，
get_live2d_params 按实际 intensity 线性插值（低强度退回中性避免噪声）。

参数命名遵循 Cubism 标准 (ParamEyeSmile / ParamMouthForm / ParamCheek 等)，
多数开源 Live2D 模型都带这些 ParamId，前端直接 setParameterValueById。
"""
from typing import Dict

# intensity=1.0 时各情绪对应的最大参数偏移
EMOTION_TO_PARAMS: Dict[str, Dict[str, float]] = {
    # 基础 9 类
    "happy": {
        "ParamMouthForm": 0.8,    # 嘴角上扬
        "ParamEyeSmile": 0.6,     # 眼睛弯成笑眼
        "ParamCheek": 0.4,       # 脸颊微红
        "ParamBrowLY": 0.1,
        "ParamBrowRY": 0.1,
    },
    "excited": {
        "ParamMouthForm": 1.0,
        "ParamMouthOpenY": 0.4,
        "ParamEyeSmile": 0.5,
        "ParamEyeOpenY": 0.9,     # 眼睛睁大
        "ParamBodyAngleX": 0.25, # 身体前倾
        "ParamCheek": 0.5,
        "ParamBrowLY": 0.2,
        "ParamBrowRY": 0.2,
    },
    "calm": {
        "ParamMouthForm": 0.0,
        "ParamCheek": 0.0,
        "ParamEyeSmile": 0.0,
        "ParamBodyAngleX": 0.0,
    },
    "curious": {
        "ParamBrowLY": -0.25,    # 眉毛挑起（负值在多数模型=上扬）
        "ParamBrowRY": -0.25,
        "ParamBodyAngleX": -0.15, # 身体微侧
        "ParamEyeOpenY": 0.95,
        "ParamMouthForm": 0.1,
    },
    "sad": {
        "ParamMouthForm": -0.6,   # 嘴角下垂
        "ParamEyeSmile": -0.3,
        "ParamEyeOpenY": 0.5,     # 眼睛半闭
        "ParamCheek": -0.2,
        "ParamBrowLY": 0.3,       # 八字眉
        "ParamBrowRY": 0.3,
        "ParamBodyAngleX": -0.1,
    },
    "angry": {
        "ParamBrowLY": -0.5,      # 眉毛皱下
        "ParamBrowRY": -0.5,
        "ParamMouthForm": -0.3,
        "ParamEyeSmile": -0.4,
        "ParamCheek": -0.1,
        "ParamBodyAngleX": 0.1,
    },
    "shy": {
        "ParamCheek": 1.0,        # 脸红到耳根（凌暮雪核心特征）
        "ParamEyeSmile": -0.4,    # 眼睛躲闪
        "ParamEyeOpenY": 0.6,
        "ParamMouthForm": -0.15,
        "ParamBrowLY": 0.1,
        "ParamBrowRY": 0.1,
        "ParamBodyAngleX": -0.2,   # 身体微躲
    },
    "proud": {
        "ParamBodyAngleX": 0.3,
        "ParamEyeSmile": 0.3,
        "ParamMouthForm": 0.4,
        "ParamCheek": 0.2,
        "ParamBrowLY": 0.15,
        "ParamBrowRY": 0.15,
    },
    "neutral": {
        "ParamMouthForm": 0.0,
        "ParamCheek": 0.0,
        "ParamEyeSmile": 0.0,
        "ParamBrowLY": 0.0,
        "ParamBrowRY": 0.0,
        "ParamBodyAngleX": 0.0,
    },
    # 扩展 12 类（凌暮雪特有，复用基础参数语义）
    "embarrassed": {              # 尴尬/窘迫
        "ParamCheek": 0.8,
        "ParamEyeSmile": -0.3,
        "ParamMouthForm": -0.2,
        "ParamEyeOpenY": 0.5,
        "ParamBodyAngleX": -0.15,
    },
    "warm": {                     # 温暖/被偏爱
        "ParamMouthForm": 0.5,
        "ParamEyeSmile": 0.5,
        "ParamCheek": 0.5,
        "ParamBrowLY": 0.05,
        "ParamBrowRY": 0.05,
    },
    "flustered": {                 # 心慌/失守
        "ParamCheek": 0.9,
        "ParamEyeOpenY": 1.0,
        "ParamEyeSmile": -0.2,
        "ParamMouthOpenY": 0.2,
        "ParamBodyAngleX": -0.1,
    },
    "melting": {                   # 融化/撒娇
        "ParamMouthForm": 0.6,
        "ParamEyeSmile": 0.7,
        "ParamCheek": 0.7,
        "ParamEyeOpenY": 0.55,
        "ParamBodyAngleX": 0.15,
    },
    "jealous": {                   # 吃醋
        "ParamBrowLY": -0.2,
        "ParamBrowRY": -0.2,
        "ParamMouthForm": -0.1,
        "ParamCheek": 0.3,
        "ParamEyeSmile": -0.2,
    },
}

# 低强度阈值：intensity 低于此值时回退中性，避免微小情绪噪声驱动表情抖动
LOW_INTENSITY_THRESHOLD = 0.2


def get_live2d_params(label: str, intensity: float) -> Dict[str, float]:
    """情绪 label + intensity → Live2D 参数 dict。

    Args:
        label: 情绪标签（happy/excited/shy/melting/...）
        intensity: 0~1 强度

    Returns:
        {ParamId: value} 字典，intensity 按比例缩放；
        未知 label 或低强度返回空 dict（前端保持当前/中性）。
    """
    if label is None or intensity is None:
        return {}
    try:
        intensity = float(intensity)
    except (TypeError, ValueError):
        return {}

    if intensity < LOW_INTENSITY_THRESHOLD:
        return {}

    base = EMOTION_TO_PARAMS.get(label)
    if not base:
        # 未知 label：退回 neutral 但乘上 intensity，让模型至少回到中性
        neutral = EMOTION_TO_PARAMS["neutral"]
        return {k: v * intensity for k, v in neutral.items()}

    return {k: v * intensity for k, v in base.items()}


def params_diff(prev: Dict[str, float], curr: Dict[str, float]) -> Dict[str, float]:
    """算两次参数的差集，用于增量推送（前端只 update 变化项）。

    返回 curr 中与 prev 不同的项 + prev 有但 curr 没有（置 0 回中性）。
    """
    diff: Dict[str, float] = {}
    keys = set(prev) | set(curr)
    for k in keys:
        old = prev.get(k, 0.0)
        new = curr.get(k, 0.0)
        if abs(old - new) > 1e-3:
            diff[k] = new
    return diff
