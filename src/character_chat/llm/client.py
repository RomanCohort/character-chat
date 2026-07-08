from openai import OpenAI
from typing import Optional
from dataclasses import dataclass, field


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
        """
        Send messages to LLM and return response.

        Args:
            messages: List of {role: str, content: str} messages.
            temperature: Optional. Defaults to config.temperature.

        Returns:
            Assistant's response text.
        """
        temp = temperature if temperature is not None else self.config.temperature
        clean_messages = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                # Sanitize: strip invalid UTF-8 surrogates (Windows stdin bug)
                content = content.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")
            clean_messages.append({"role": m["role"], "content": content})
        response = self.client.chat.completions.create(
            model=self.model_id,
            messages=clean_messages,
            temperature=temp,
            stream=False
        )
        return response.choices[0].message.content

    def chat_stream(self, messages: list[dict], stream: bool = True) -> str:
        """Stream chat completion (TODO: implement streaming)"""
        if not stream:
            return self.chat(messages)
        raise NotImplementedError("Streaming not implemented in Phase 1")
