"""工具权限层 + hook 生命周期。

【为什么要有它】
在这之前，每个工具的自我保护是**分散**的：
  · shell_access.py   —— 命令正则白名单
  · server_ops.py     —— SCOPE_ROOTS 路径前缀
  · website.py        —— 只读写 site.yml 的文本字段
  · personal.py       —— 自己的校验

每处都写得不错，但**没有一个地方能回答「她此刻被允许做什么」**。
想知道答案得把四个文件都读一遍。而且缺三样东西：
  1. 分级模式（想让她「只看不动」时没有任何开关）
  2. 一个所有工具都会经过的拦截点
  3. 调用前后能挂钩子

【设计原则：是加一道闸，不是换锁】
默认模式下**放行一切现有能跑的东西**，所以接进来不会改变她现在的行为。
它的价值在于多出来的三种能力：
  · plan 模式：只读，什么都不写——「先看看别动」终于有开关了
  · 所有工具共用一个可见的策略表（TOOL_RISK），而不是散在四个文件里
  · PreToolUse / PostToolUse 钩子——hooks 和之后的 subagent 都挂在这两个点上

【与 shell_access 的关系】
不是替代。shell_access 仍然负责「这条命令本身合不合法」（防注入），
这一层负责「她此刻该不该碰这类工具」。两道闸各管一段。
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

TZ = timezone(timedelta(hours=8))
MODE_FILE = Path(os.environ.get("LING_PERM_MODE_FILE",
                                "/var/lib/ling-muxue/permission-mode.json"))
AUDIT_LOG = Path("/var/log/ling-muxue/permission.log")

# ------------------------------------------------------------------ 风险分级
# read   —— 只读，无副作用
# write  —— 会改本机状态或对外可见
# exec   —— 跑命令（本身另有 shell_access 白名单兜底）
# danger —— 能造成不可逆或自伤的操作
TOOL_RISK = {
    # 只读
    "web_search": "read",
    "arxiv_search": "read",
    "github_ask": "read",
    "huggingface_ask": "read",
    "lookup": "read",
    "system_status": "read",
    "read_logs": "read",
    "read_file": "read",
    "search_code": "read",
    "site_stats": "read",
    "todo": "read",
    # 委派：它的**效果**是只读的——子代理只能用 read 级工具（见 delegate.py），
    # 所以这里划 read，plan 模式下也能用来调研。
    # 注意它耗 token（一次独立 LLM 循环），但不改状态，所以不进防重那三类。
    "delegate": "read",
    # 有副作用 / 对外可见
    "remind": "write",          # 写 reminders，到点会真的发微信给学长
    "website_manage": "write",  # 改 site.yml 并重新生成线上网站
    "mirror_update": "write",   # 动 git 仓库
    "send_sticker": "write",    # 本地补丁（2026-10-09）：真的往学长微信发图
    "run_command": "exec",      # 另有 shell_access 白名单兜底
    "ip_action": "danger",      # fail2ban 封/解封 IP —— 封错了能把自己关在门外
}

# ------------------------------------------------------------------ 动作级覆盖
#
# 【为什么需要】
# 按**工具名**分级对「一个名字里既有读又有写」的工具是错的。
# 比如 website_manage 既能 list_sections（读）又能 edit_section（写），
# 整只划成 write 之后，她连问两次「网站有几个章节」都会被防重钩子拦掉。
# （2026-10-07 实测踩到：她用 website_manage 数仓库，那是读。）
#
# 覆盖表**只列已知的 action**；表里没有的 action 一律退回工具级（保守）。
# 保守方向是刻意的——漏掉一个写动作会让它被当读放行，比误拦严重。
ACTION_RISK = {
    "website_manage": {
        "list_sections": "read",
        "read_section":  "read",
        "edit_section":  "write",
        "regenerate":    "write",
    },
    "remind": {"list": "read", "add": "write", "remove": "write"},
    "todo":   {"list": "read", "add": "write", "done": "write"},
}

# 每种模式放行哪些风险级
MODE_ALLOWS = {
    "default": {"read", "write", "exec"},   # 默认 == 今天的行为，接进来不改现状
    "auto":    {"read", "write", "exec"},
    "plan":    {"read"},                    # 只看不动
}
MODES = tuple(MODE_ALLOWS)

# 任何模式下都不放行的工具（留作显式黑名单位；目前为空）
ALWAYS_DENY = set()

# 模式文件缺失时的兜底。用 default 而不是 plan——升级上去不该把她锁死。
DEFAULT_MODE = "default"


# ------------------------------------------------------------------ 模式读写
def get_mode() -> str:
    try:
        m = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("mode")
        if m in MODES:
            return m
    except Exception:
        pass
    return DEFAULT_MODE


def set_mode(mode: str, by: str = "unknown") -> str:
    if mode not in MODES:
        return "模式只能是 %s" % " / ".join(MODES)
    try:
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(MODE_FILE) + ".tmp"
        Path(tmp).write_text(json.dumps(
            {"mode": mode, "changed_at": datetime.now(TZ).isoformat(), "by": by},
            ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, MODE_FILE)
    except Exception as e:
        return "写模式失败：%s" % e
    _audit("MODE", "-", "-> %s (by %s)" % (mode, by))
    return "已切到 %s 模式" % mode


# ------------------------------------------------------------------ 审计
def _audit(kind: str, tool: str, detail: str):
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %-8s %-18s %s\n" % (
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), kind, tool, detail[:240]))
    except Exception:
        pass


# ------------------------------------------------------------------ hooks
# 钩子是纯函数，注册进来就行。故意做得极小——先有可挂的点，再谈挂什么。
#
#   PreToolUse : fn(tool_name, arguments) -> Optional[str]
#                返回字符串 = 否决，字符串内容就是拒因；返回 None = 放行
#   PostToolUse: fn(tool_name, arguments, result, elapsed) -> None
#                只观察，返回值被忽略。elapsed 是秒（float），由调用方计时传下来
#                —— 钩子在事后跑，自己拿不到「调用前」的时刻。
PRE_TOOL_USE = []
POST_TOOL_USE = []


def pre_hook(fn):
    PRE_TOOL_USE.append(fn)
    return fn


def post_hook(fn):
    POST_TOOL_USE.append(fn)
    return fn


def _run_pre(tool_name, arguments):
    for fn in PRE_TOOL_USE:
        try:
            verdict = fn(tool_name, arguments)
        except Exception as e:
            logger.warning("[perm] pre-hook %s 出错: %s" % (getattr(fn, "__name__", fn), e))
            continue
        if verdict:
            return verdict
    return None


def _run_post(tool_name, arguments, result, elapsed=None):
    for fn in POST_TOOL_USE:
        try:
            fn(tool_name, arguments, result, elapsed)
        except Exception as e:
            logger.warning("[perm] post-hook %s 出错: %s" % (getattr(fn, "__name__", fn), e))


# ------------------------------------------------------------------ 闸门
def resolve_risk(tool_name: str, arguments: dict):
    """解析一个调用的**实际**风险级。动作级覆盖优先于工具级。

    钩子也应该用这个，而不是直接读 TOOL_RISK —— 否则防重之类按风险级
    做的判断会跟权限闸不一致（一个认为在读、一个认为在写）。
    """
    overrides = ACTION_RISK.get(tool_name)
    if overrides:
        action = str((arguments or {}).get("action", "") or "").strip()
        if action in overrides:
            return overrides[action]
    return TOOL_RISK.get(tool_name)


def check(tool_name: str, arguments: dict) -> tuple:
    """返回 (是否放行, 拒因)。放行时拒因为空串。"""
    if tool_name in ALWAYS_DENY:
        return False, "这个工具被永久禁用"

    risk = resolve_risk(tool_name, arguments)
    if risk is None:
        # 没登记的工具：不放行。宁可漏跑一个，也别让没人看过的工具悄悄执行。
        _audit("DENY", tool_name, "未登记的工具")
        return False, ("工具 %s 没在权限表里登记过 —— 我不能跑没被审查过的工具。"
                       "告诉学长加进 tools/permissions.py 的 TOOL_RISK。" % tool_name)

    mode = get_mode()
    if risk not in MODE_ALLOWS[mode]:
        _audit("DENY", tool_name, "%s 模式不放行 %s 级" % (mode, risk))
        return False, ("现在是「%s」模式，%s 类工具（%s）在这个模式下不执行。"
                       "要动的话学长得先切回 default。" % (mode, risk, tool_name))

    verdict = _run_pre(tool_name, arguments)
    if verdict:
        _audit("DENY", tool_name, "pre-hook: %s" % verdict)
        return False, verdict

    return True, ""


def after(tool_name, arguments, result, elapsed=None):
    _audit("OK", tool_name, "")
    _run_post(tool_name, arguments, result, elapsed)


# ------------------------------------------------------------------ 内置钩子
# 放在文件末尾导入：走到这里 permissions 的机制已经全部定义完，
# hooks_builtin 回头 `from ... import pre_hook` 拿到的是完整模块，不会循环。
# 外面套 try —— 钩子坏了不该让整个权限层失效（宁可少一道护栏，也别让她彻底不能干活）。
try:
    from character_chat.tools import hooks_builtin  # noqa: F401
except Exception as e:  # pragma: no cover
    logger.warning("[perm] 加载内置钩子失败，权限层继续工作但无钩子: %s" % e)


# ------------------------------------------------------------------ CLI
def _main(argv=None):
    """给学长用的开关。

        python -m character_chat.tools.permissions            # 看当前模式
        python -m character_chat.tools.permissions plan       # 只看不动
        python -m character_chat.tools.permissions default    # 恢复
    """
    import sys
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("show", "status"):
        mode = get_mode()
        print("当前模式: %s" % mode)
        print("放行的风险级: %s" % " / ".join(sorted(MODE_ALLOWS[mode])))
        print()
        print("各工具的风险级：")
        for name in sorted(TOOL_RISK, key=lambda n: (TOOL_RISK[n], n)):
            risk = TOOL_RISK[name]
            mark = "ok " if risk in MODE_ALLOWS[mode] else "BLOCKED"
            print("  %-8s %-8s %s" % (mark, risk, name))
        return 0

    if args[0] in MODES:
        print(set_mode(args[0], by=args[1] if len(args) > 1 else "cli"))
        return 0

    print("用法: python -m character_chat.tools.permissions [%s]" % " | ".join(MODES))
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
