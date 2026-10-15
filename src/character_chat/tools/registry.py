"""工具注册表 —— 所有工具声明的**唯一来源**。

【为什么要建它】
在这之前，工具声明是 `cli.py` 里一段**函数内的局部 import** + 一个字面量列表。
后果是：任何别处想拿「工具全集」都只能再抄一份，两边早晚漂移。

逼出这次抽取的具体需求是 subagent（`delegate.py`）：它需要一个
「只读子集」。抄一份只读列表意味着以后新加的读工具不会被自动纳入，
而漂移的方向恰好是**不安全的那一侧**（子代理拿不到它该有的工具，
或者更糟——有人图省事直接给它全集）。

所以：声明在这里，子集**从风险表派生**（见 `tools_for_risk`），
不另写名单。
"""
from character_chat.tools.web_search import WEB_SEARCH_TOOL
from character_chat.tools.arxiv_search import ARXIV_SEARCH_TOOL
from character_chat.tools.github import GITHUB_TOOL
from character_chat.tools.huggingface import HF_TOOL
from character_chat.tools.website import WEBSITE_TOOL, MIRROR_TOOL
from character_chat.tools.lookup import LOOKUP_TOOL
from character_chat.tools.server_ops import (
    SYSTEM_STATUS_TOOL, READ_LOGS_TOOL, IP_ACTION_TOOL,
    READ_FILE_TOOL, SEARCH_CODE_TOOL,
)
from character_chat.tools.personal import REMIND_TOOL, TODO_TOOL, SITE_STATS_TOOL
# 本地补丁（2026-10-09）：表情包 —— **已 disable**（2026-10-09 学长决定）。
# 原因：微信 CDN 从新加坡过去又慢又抽风，入站图片根本下不下来；
# 颜文字够用。代码全留着，想开就把下面这行 import 和 ALL_TOOLS 里那行取消注释。
# from character_chat.tools.stickers import SEND_STICKER_TOOL
from character_chat.tools.shell_access import RUN_COMMAND_TOOL
# delegate 放在最后 import：它不回头 import 本模块（只在函数内延迟 import），
# 所以这里不会循环。
from character_chat.tools.delegate import DELEGATE_TOOL


ALL_TOOLS = (
    WEB_SEARCH_TOOL,
    ARXIV_SEARCH_TOOL,
    GITHUB_TOOL,
    HF_TOOL,
    WEBSITE_TOOL,
    MIRROR_TOOL,
    LOOKUP_TOOL,
    SYSTEM_STATUS_TOOL,
    READ_LOGS_TOOL,
    IP_ACTION_TOOL,
    READ_FILE_TOOL,
    SEARCH_CODE_TOOL,
    REMIND_TOOL,
    TODO_TOOL,
    SITE_STATS_TOOL,
    # SEND_STICKER_TOOL,   # ← disable（2026-10-09）；见上方注释
    RUN_COMMAND_TOOL,
    DELEGATE_TOOL,
)


def tool_name(decl):
    return decl["function"]["name"]


def tools_for_risk(*risks):
    """按风险级筛出工具声明。

    判据用的是 `permissions.resolve_risk` —— 跟权限闸、防重钩子**同一套**。
    这一点很重要：如果这里自己判一遍，就会出现「委派允许了、权限闸拦掉」
    这种自相矛盾，而且是静默的（子代理会以为自己有那个工具）。

    注意 `resolve_risk(name, {})` 传空参数：对有动作级覆盖的工具
    （website_manage/remind/todo），空参数会退回**工具级**风险，
    也就是保守的那一侧——子代理拿不到它们。这是刻意的。
    """
    from character_chat.tools import permissions as P
    want = set(risks)
    return [t for t in ALL_TOOLS if P.resolve_risk(tool_name(t), {}) in want]
