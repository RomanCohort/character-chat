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
    # 本地 Qwen 补丁：主端点连不上/超时就落到 fallback_*；留空 = 维持原行为
    timeout: float = 60.0
    # 本地 Qwen 补丁：一次调用最多生成多少 token。
    # 不设的话 RP 模型复读时会无限生成，把唯一槽位占死（真发生过）。
    max_tokens: int = 900
    fallback_api_key: str = ""
    fallback_base_url: str = ""
    fallback_model_id: str = ""


class LLMClient:
    """Simple DeepSeek API client (no external dependencies like zerolan.data)"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url,
                              timeout=config.timeout)
        self.model_id = config.model_id
        # 本地 Qwen 补丁：可选兜底端点
        self.last_backend = "primary"
        self._fb_client = None
        self._fb_model = ""
        if config.fallback_base_url:
            self._fb_client = OpenAI(api_key=config.fallback_api_key or "unused",
                                     base_url=config.fallback_base_url,
                                     timeout=config.timeout)
            self._fb_model = config.fallback_model_id or config.model_id
            logger.info("[llm] 主端点 %s / 兜底 %s"
                        % (config.base_url, config.fallback_base_url))

    @staticmethod
    def _is_empty(resp) -> bool:
        """主端点是否返回了"空内容"。

        思维链模型（Qwen3.6-35B-A3B 等）在推理预算被吃光时，
        会用 HTTP 200 返回 content="" —— 这在语义上是失败，必须走兜底。
        但带 tool_calls 的响应即使 content 为空也是有效输出。
        """
        try:
            msg = resp.choices[0].message
        except Exception:
            return True
        if getattr(msg, "tool_calls", None):
            return False
        return not (getattr(msg, "content", None) or "").strip()

    def _create(self, **kwargs):
        """主端点优先；任何异常落到兜底端点。返回原始 OpenAI response。"""
        try:
            self.last_backend = "primary"
            kw_primary = dict(kwargs)
            # 本地优先客户端（配了兜底端点）→ 主端点就是本机 Qwen。
            # 显式关掉思维链：网关是 setdefault，客户端传的优先。
            # 人设对话那个客户端没有 fallback，不受影响。
            if self._fb_client is not None:
                eb = dict(kw_primary.get("extra_body") or {})
                ctk = dict(eb.get("chat_template_kwargs") or {})
                ctk.setdefault("enable_thinking", False)
                eb["chat_template_kwargs"] = ctk
                kw_primary["extra_body"] = eb
            _resp = self.client.chat.completions.create(**kw_primary)
            if self._is_empty(_resp):
                raise RuntimeError("主端点返回空内容（多为思维链吃光 max_tokens）")
            return _resp
        except Exception as e:
            if self._fb_client is None:
                raise
            logger.warning("[llm] 主端点失败（%s: %s），改走兜底 %s"
                           % (type(e).__name__, e, self.config.fallback_base_url))
            self.last_backend = "fallback"
            kw = dict(kwargs)
            kw["model"] = self._fb_model
            # 兜底端点（DeepSeek）不认 chat_template_kwargs，但**必须**关掉思维链：
            # deepseek-flash 会把整个 max_tokens 预算烧在 reasoning 上，返回半截
            # JSON —— 表现为"模型（A 段）没给出合法 JSON"。主端点用
            # chat_template_kwargs 关，兜底用 DeepSeek 自己的 thinking 字段关。
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
            return self._fb_client.chat.completions.create(**kw)

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        """Send messages to LLM and return response (no tools)."""
        temp = temperature if temperature is not None else self.config.temperature
        clean_messages = self._sanitize_messages(messages)
        response = self._create(
            model=self.model_id,
            messages=clean_messages,
            temperature=temp,
            max_tokens=self.config.max_tokens,
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
            response = self._create(
                model=self.model_id,
                messages=clean_messages,
                tools=tools,
                temperature=temp,
                max_tokens=self.config.max_tokens,
                stream=False,
            )
            msg = response.choices[0].message
            try:
                import json as _j, pathlib as _pp
                _pp.Path("/tmp/tool_debug.json").write_text(_j.dumps({
                    "finish_reason": response.choices[0].finish_reason,
                    "msg_keys": list(msg.model_dump().keys()),
                    "tool_calls": str(getattr(msg, "tool_calls", None))[:400],
                    "content_head": (msg.content or "")[:400],
                    "n_tools": len(tools),
                    "system_len": len(clean_messages[0].get("content", "")),
                    "n_messages": len(clean_messages),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

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

        # 超 max_tool_rounds：不带 tools 兜底收尾，强制出文本回复。
        #
        # 坑（实测踩过）：此刻 clean_messages 结尾是一条 tool 角色消息，而这次调用
        # **不带 tools**。模型于是顺着工具调用的格式继续写，把
        # `<｜｜DSML｜｜ invoke name="read_file">` 这类**标记当正文**吐出来，
        # 原样漏给用户。所以收尾前先塞一条明确的指令把它掰回自然语言。
        logger.warning(f"[tool] 超 max_tool_rounds({max_tool_rounds})，强制收尾")
        clean_messages.append({
            "role": "user",
            "content": (
                "[系统] 工具调用轮次已用尽，不能再调工具了。"
                "**不要再输出任何工具调用、标签或特殊标记**（不要出现 invoke / parameter "
                "/ DSML 之类的字样），直接用自然语言把已经查到的内容整理成回答。"
                "没查到的就直接说没查到。"
            ),
        })
        response = self._create(
            model=self.model_id,
            messages=clean_messages,
            temperature=temp,
            max_tokens=self.config.max_tokens,
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
