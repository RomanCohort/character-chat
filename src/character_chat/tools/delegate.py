# -*- coding: utf-8 -*-
"""delegate.py —— 把一件边界清楚的事委派给一个「只读、有限轮次」的子代理。

【直接动机：一个实测出来的形状】
`llm/client.py` 会把工具返回截到 1500 字符。2026-10-07 实测她去查带宽，
`vnstat` 返回 5960 字符 → **她只看到前 1500，而且她不知道**（回复确实断在半句上）。
这是这套系统最典型的失败形状：静默失效——用残缺输入做判断，自己毫无察觉。

子代理正好治这个形状：它自己读全文、翻几轮、只把**结论**带回来。
主上下文不会被大块原始输出撑爆，也不必再靠截断。

【安全设计：为什么敢开这个口子】
之前判断过「subagent 必须先有权限层托底，否则只会放大风险面」。
现在那个前提已经满足了，所以这里：
  1. 子代理**只能用 read 级工具** —— 从 `registry.tools_for_risk("read")` 派生，
     不另写名单（新加的读工具会自动纳入；写工具永远进不来）
  2. 子代理的每一次工具调用都走 **`execute_tool`** —— 因为 `chat_with_tools`
     的 `tool_executor` 默认就是它。所以**权限闸 + 四个钩子全部照常生效**，
     子代理不是一条绕过检查的旁路
  3. **禁止嵌套**：子代理里再调 `delegate` 直接拒（thread-local 记深度）
  4. **轮次上限**：`max_tool_rounds` 兜底，不会无限跑

【成本提醒】
子代理是一次独立的 LLM 循环：外层的 `max_tool_rounds` 是 8，如果她每轮都调
delegate，最坏是 8 × (1+max_rounds) 次模型调用。今天没加限流——先用着看，
真跑飞了再加（跟 tool-calls.log 里攒的基线一起看）。
"""
import threading

from loguru import logger

# LLM 客户端由 Cli.__init__ 注入。不在这里自己构造——那需要读配置、
# 会复制一份配置解析逻辑，而且进程里已经有现成的实例了。
_llm = None

# 子代理深度，thread-local：FastAPI 用线程池跑 cli.send()，全局变量会被串。
_depth = threading.local()


def set_llm(client):
    """注入 LLM 客户端。由 Cli.__init__ 调用一次。"""
    global _llm
    _llm = client


def _current_depth():
    return getattr(_depth, "n", 0)


DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "把一件**边界清楚的只读调研任务**交给子代理去做，只把结论拿回来。\n"
            "\n"
            "什么时候用它：\n"
            "  · 要翻很多日志/很多文件才得出结论（原文太长，直接读会被截断）\n"
            "  · 需要反复试几次才定位的问题（子代理能自己迭代，不占你的上下文）\n"
            "\n"
            "什么时候不要用它：\n"
            "  · 一两步就能查到的事（直接调工具更快更省）\n"
            "  · 任何要改东西的任务（子代理**只能读**，改不了的）\n"
            "  · 需要你跟学长来回确认的事（子代理说话的对象是任务，不是你）\n"
            "\n"
            "任务要写具体：读什么、判什么、结论要什么形状。写得含糊它就只能瞎翻。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "要它做什么。写清：看哪些东西、判断什么、结论要什么格式。",
                },
                "max_rounds": {
                    "type": "integer",
                    "description": "子代理最多几轮工具调用，默认 4，上限 6。翻得越多越贵。",
                },
            },
            "required": ["task"],
        },
    },
}

_SUBAGENT_SYSTEM = (
    "你是一个**受限的调研子代理**，只负责查清一件事并把结论交回去。\n"
    "\n"
    "硬约束：\n"
    "  · 你只有只读工具，改不了任何东西。别尝试改。\n"
    "  · 你面对的是一次性任务，不是对话。不要寒暄、不要问对方问题、不要叫「学长」。\n"
    "  · 不要输出思考过程，只输出结论。\n"
    "\n"
    "输出格式（严格照这个来）：\n"
    "  结论：<一到三句话，直接回答问题>\n"
    "  依据：<你实际看过的关键证据，带文件名/时间戳，两到五条>\n"
    "  不确定：<哪些地方你没能确认，或者查不到。没有就写「无」>\n"
    "\n"
    "「不确定」那一栏必须诚实：宁可写「查不到」，也不要猜一个看起来合理的答案。"
)

# 结论回给主代理时的长度上限。留一点余量给「【子代理结论】」前缀和
# 调用方的 1500 截断线——不然子代理辛苦一整轮，结论又被截了，白干。
MAX_REPLY_CHARS = 1200


def delegate(task: str = "", max_rounds: int = 4) -> str:
    """跑一个只读子代理，返回它的结论。"""
    if _current_depth() >= 1:
        return "(子代理里不能再开子代理。如果要分两步查，直接在主对话里分两次委派。)"

    if _llm is None:
        return ("(委派不可用：LLM 客户端没有注入。cli.py 的 Cli.__init__ 里应该调用 "
                "delegate.set_llm(self.llm) —— 让学长检查一下这一行在不在。)")

    task = (task or "").strip()
    if not task:
        return "(要委派什么？把任务写清楚：看哪些东西、判断什么、结论要什么格式。)"

    try:
        rounds = int(max_rounds)
    except Exception:
        rounds = 4
    rounds = max(1, min(rounds, 6))          # 封顶 6，别让它跑飞

    # 只读工具集从注册表按风险级派生，不另写名单
    from character_chat.tools import registry
    try:
        tools = registry.tools_for_risk("read")
    except Exception as e:
        logger.warning("[delegate] 取只读工具集失败: %s" % e)
        return "(委派失败：拿不到只读工具集（%s）。)" % e
    if not tools:
        return "(委派失败：当前没有任何只读工具可用。)"
    # delegate 自己不该出现在子代理的工具集里（虽然深度守卫也会拦住，
    # 但不给它看见更干净——它不该知道有这条路）
    tools = [t for t in tools if registry.tool_name(t) != "delegate"]

    messages = [
        {"role": "system", "content": _SUBAGENT_SYSTEM},
        {"role": "user", "content": task},
    ]

    _depth.n = _current_depth() + 1
    try:
        out = _llm.chat_with_tools(
            messages,
            tools=tools,
            temperature=0.2,          # 调研任务要稳，不要发挥
            max_tool_rounds=rounds,
        )
    except Exception as e:
        logger.warning("[delegate] 子代理执行失败: %s" % e)
        return "(委派失败：%s: %s)" % (type(e).__name__, e)
    finally:
        _depth.n = _current_depth() - 1

    out = (out or "").strip()
    if not out:
        return "(子代理跑完了但没给出结论 —— 可能是轮次用尽还没查完。把任务写窄一点再试。)"

    if len(out) > MAX_REPLY_CHARS:
        out = out[:MAX_REPLY_CHARS] + "\n…（子代理结论过长，已截断）"

    return "【子代理结论】\n" + out
