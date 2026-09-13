"""Live2D WebSocket 桥 — 把 EventBus 事件实时推给前端。

架构（简化后，不再用独立进程）：
  character_chat server (8000) 同时跑 HTTP + WebSocket
    cli.bus.publish(EMOTION_UPDATED / MESSAGE_SENT)  ← 进程内同步
      ↓ bridge.subscribe(handler) 订阅
      ↓ handler 把事件塞进每条 WebSocket 的 asyncio.Queue
    WebSocket /ws/live2d  ← 前端连这里
      ↓ 读 queue 推 JSON

之所以能这么干：EventBus 是 cli 实例的进程内同步 bus，server.py 跑在
同一进程，直接拿 cli_instances["default"].bus 订阅即可，零跨进程开销。

EventBus.publish 是同步调用（直接跑 handler），handler 里只做"塞 queue"
这件 O(1) 事，不阻塞 cli.send 主流程。
"""
import asyncio
import json
import time
from typing import Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from character_chat.event.types import EMOTION_UPDATED, MESSAGE_SENT
from character_chat.live2d.emotion_params import get_live2d_params, params_diff


# 活跃 WebSocket 连接：client_id -> Live2DClient
_clients: Dict[str, "Live2DClient"] = {}
# 是否已经订阅过 bus（多连接共用一份订阅，避免重复 subscribe）
_subscribed: bool = False
# 上次推给前端的参数快照（算增量 diff，减少前端 setParameter 调用）
_last_params: Dict[str, float] = {}
# 单实例锁（单人场景只有一个前端）
_lock = asyncio.Lock()


class Live2DClient:
    """一个 WebSocket 连接的上下文。"""

    def __init__(self, client_id: str, websocket: WebSocket):
        self.client_id = client_id
        self.ws = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        # 前端是否已 hello（握手前不发事件，避免堆积）
        self.ready: bool = False

    async def send(self, payload: dict) -> bool:
        """推一条 JSON 给前端。队列满则丢最旧的，保证实时性。"""
        try:
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(payload)
            return True
        except Exception as e:
            logger.debug(f"[live2d] enqueue failed for {self.client_id}: {e}")
            return False


def _broadcast(payload: dict) -> None:
    """给所有 ready 的连接塞一条事件。同步调用（在 bus.publish 线程里跑）。"""
    if not _clients:
        return
    for client in list(_clients.values()):
        if client.ready:
            # send 是 async，但这里在同步上下文——用 create_task 调度
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(client.send(payload))
            except RuntimeError:
                # 没有 running loop（线程上下文）——直接 put_nowait
                try:
                    client.queue.put_nowait(payload)
                except Exception:
                    pass


def _on_emotion_updated(event) -> None:
    """EMOTION_UPDATED handler — 同步，只做参数计算 + 入队。"""
    data = event.data or {}
    label = data.get("label", "neutral")
    intensity = data.get("intensity", 0.0)
    confidence = data.get("confidence", 0.0)

    global _last_params
    curr = get_live2d_params(label, intensity)
    diff = params_diff(_last_params, curr)
    _last_params = curr

    if not diff:
        return  # 参数没变化，不推

    payload = {
        "type": "emotion_updated",
        "label": label,
        "intensity": round(float(intensity), 3),
        "confidence": round(float(confidence), 3),
        "params": diff,          # 增量参数
        "ts": time.monotonic(),
    }
    _broadcast(payload)


def _on_message_sent(event) -> None:
    """MESSAGE_SENT handler — 凌暮雪说了话，前端可触发 TTS + lip sync。"""
    data = event.data or {}
    text = data.get("text", "")
    if not text:
        return
    payload = {
        "type": "message_sent",
        "text": text,
        "ts": time.monotonic(),
    }
    _broadcast(payload)


def _ensure_subscribed(bus) -> None:
    """订阅 cli.bus 的事件（只订阅一次）。"""
    global _subscribed
    if _subscribed:
        return
    bus.subscribe(EMOTION_UPDATED, _on_emotion_updated, name="live2d_emotion")
    bus.subscribe(MESSAGE_SENT, _on_message_sent, name="live2d_message")
    _subscribed = True
    logger.info("[live2d] subscribed to EMOTION_UPDATED + MESSAGE_SENT")


async def live2d_websocket(websocket: WebSocket):
    """WebSocket 端点 /ws/live2d 的处理函数。

    协议：
      前端连上后发 {"type":"hello"} → 服务端回 {"type":"ready"} 后开始推事件
      前端可发 {"type":"ping"} → 服务端回 {"type":"pong"}
      服务端推：emotion_updated / message_sent / tts_status（Phase 3）
    """
    await websocket.accept()

    # 拿 cli 的 bus（单人场景，直接取第一个可用的 cli）
    from character_chat.server import cli_instances
    cli = None
    for c in cli_instances.values():
        if getattr(c, "bus", None):
            cli = c
            break

    if cli is None:
        # cli 还没起来——告知前端稍后重连
        await websocket.send_json({"type": "error", "msg": "character not ready, retry later"})
        await websocket.close()
        return

    _ensure_subscribed(cli.bus)

    client_id = f"ws_{id(websocket)}"
    client = Live2DClient(client_id, websocket)
    _clients[client_id] = client

    logger.info(f"[live2d] client connected: {client_id} (total={len(_clients)})")

    # 后台任务：从 queue 取事件推给前端
    async def pusher():
        try:
            while True:
                payload = await client.queue.get()
                if client.ws.client_state.value != "CONNECTED":
                    break
                await client.ws.send_json(payload)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"[live2d] pusher exit {client_id}: {e}")

    pusher_task = asyncio.create_task(pusher())

    try:
        # 主循环：收前端消息（hello / ping）
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "hello":
                client.ready = True
                await websocket.send_json({
                    "type": "ready",
                    "character": getattr(getattr(cli, "card", None), "bot_name", "凌暮雪"),
                    "snapshot": _last_params,  # 当前表情快照（重连后能立刻恢复）
                })
            elif mtype == "ping":
                await websocket.send_json({"type": "pong", "ts": time.monotonic()})
            elif mtype == "request_snapshot":
                await websocket.send_json({"type": "snapshot", "params": _last_params})
    except WebSocketDisconnect:
        logger.info(f"[live2d] client disconnected: {client_id}")
    except Exception as e:
        logger.warning(f"[live2d] ws error {client_id}: {e}")
    finally:
        pusher_task.cancel()
        _clients.pop(client_id, None)
        logger.info(f"[live2d] cleanup {client_id} (remaining={len(_clients)})")


def get_status() -> dict:
    """供 HTTP /live2d/status 端点查连接数 + 订阅状态。"""
    return {
        "connected_clients": len(_clients),
        "subscribed": _subscribed,
        "last_params_keys": list(_last_params.keys()),
    }
