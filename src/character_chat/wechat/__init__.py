"""微信 → DSH 通道。

- `acp_client`：以 ACP v1 客户端驱动 `dsh --profile acp` 常驻进程
- `bridge`：命令路由与派发（`!dsh <任务>`）
- `sender`：绕过 bridge 进程直接调 ilink bot API 发消息
"""
from . import acp_client, bridge, sender  # noqa: F401

__all__ = ["acp_client", "bridge", "sender"]
