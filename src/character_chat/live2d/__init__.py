"""Live2D 桥接模块：EventBus 事件 → WebSocket → 前端 Live2D 模型。

Phase 1: WebSocket + 情绪参数映射（已完成）
Phase 2: TTS 集成（GPT-SoVITS，待做）
"""
from character_chat.live2d.bridge import live2d_websocket, get_status
from character_chat.live2d.emotion_params import get_live2d_params

__all__ = ["live2d_websocket", "get_status", "get_live2d_params"]
