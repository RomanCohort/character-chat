from openai import OpenAI
from typing import Optional, Callable
from dataclasses import dataclass, field
from loguru import logger


@dataclass
class LLMConfig:
    """Configuration for LLM API (DeepSeek supported)"""
    api_key: str
    model_id: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = 0.7


class LLMClient:
    """Simple DeepSeek API client (no external dependencies like zerolan.data)"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        self.model_id = config.model_id

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        """Send messages to LLM and return response (no tools)."""
        temp = temperature if temperature is not None else self.config.temperature
        clean_messages = self._sanitize_messages(messages)
        response = self.client.chat.completions.create(
            model=self.model_id,
            messages=clean_messages,
            temperature=temp,
            stream=False
        )
        return response.choices[0].message.content

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Optional[Callable[[str, dict], str]] = None,
        temperature: Optional[float] = None,
        max_tool_rounds: int = 2,
    ) -> str:
        """带 function calling 的对话：LLM 自决调用工具，结果回填后再生成回复。

        流程（最多 max_tool_rounds 轮，避免无限调用）：
          1. 发 messages + tools 给 LLM
          2. 若 LLM 返回 tool_calls → 执行工具 → 把结果作为 tool 角色消息塞回 messages
             → 再调一次 LLM 生成最终回复
          3. 若无 tool_calls → 直接返回 content

        Args:
            messages: 对话历史（含 system）
            tools: OpenAI function-calling tool schema 列表
            tool_executor: fn(tool_name, arguments_dict) -> str；默认用 tools 包的 dispatcher
            temperature: 可选温度
            max_tool_rounds: 最多允许连续工具调用轮次（防 LLM 反复调工具刷搜索）

        Returns:
            最终回复文本。
        """
        if tool_executor is None:
            from character_chat.tools.web_search import execute_tool as tool_executor

        temp = temperature if temperature is not None else self.config.temperature
        clean_messages = self._sanitize_messages(messages)

        for _ in range(max_tool_rounds + 1):
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=clean_messages,
                tools=tools,
                temperature=temp,
                stream=False,
            )
            msg = response.choices[0].message

            # 没有 tool_calls → 终态，直接返回 content
            if not msg.tool_calls:
                return msg.content or ""

            # 把 assistant 的 tool_calls 消息加进历史（OpenAI 协议要求）
            clean_messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # 执行每个 tool_call，结果作为 tool 角色消息回填
            for tc in msg.tool_calls:
                fname = tc.function.name
                try:
                    import json as _json
                    args = _json.loads(tc.function.arguments or "{}")
                except Exception as e:
                    args = {}
                    logger.warning(f"[tool] parse args failed for {fname}: {e}")

                logger.info(f"[tool] LLM 调用 {fname}({args})")
                try:
                    result = tool_executor(fname, args)
                except Exception as e:
                    result = f"(工具执行出错: {e})"
                # 截断超长结果，防 token 爆炸
                if len(result) > 1500:
                    result = result[:1500] + "…(截断)"

                clean_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            # 还有轮次 → 再调一次 LLM，它会基于工具结果生成回复或继续调工具

        # 超 max_tool_rounds：不带 tools 兜底收尾，强制出文本回复
        logger.warning(f"[tool] 超 max_tool_rounds({max_tool_rounds})，强制收尾")
        response = self.client.chat.completions.create(
            model=self.model_id,
            messages=clean_messages,
            temperature=temp,
            stream=False,
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Clean messages: strip invalid UTF-8 surrogates (Windows stdin bug).
        保留非 content 字段（tool_calls / tool_call_id / name 等）。"""
        clean = []
        for m in messages:
            new_m = dict(m)
            content = m.get("content", "")
            if isinstance(content, str):
                content = content.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")
            new_m["content"] = content
            clean.append(new_m)
        return clean

    def chat_stream(self, messages: list[dict], stream: bool = True) -> str:
        """Stream chat completion (TODO: implement streaming)"""
        if not stream:
            return self.chat(messages)
        raise NotImplementedError("Streaming not implemented in Phase 1")
