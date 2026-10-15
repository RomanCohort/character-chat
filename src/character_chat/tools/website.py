"""网站管理工具 — 让凌暮雪能看/改学长的个人主页。

网站: https://www.romancohort.cloud/  （配置在 /opt/sitegen/site.yml）

【安全边界 —— 这是它唯一能碰的东西】
  · 只读写 /opt/sitegen/site.yml 的文本字段
  · 每次编辑前自动备份（site.yml.bak.<时间戳>，保留最近 10 份）
  · 编辑后立刻验证 YAML 可解析，坏了自动回滚
  · regenerate 先跑 --dry-run 确认不炸，再真跑
  · **碰不到** nginx / 证书 / 服务器配置 / 系统

用 ruamel.yaml round-trip 模式读写，保留注释和格式。
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

SITE_YML = Path(os.environ.get("SITE_YML", "/opt/sitegen/site.yml"))
SITEGEN = Path(os.environ.get("SITEGEN", "/opt/sitegen/sitegen.py"))
SITEGEN_PY = os.environ.get("SITEGEN_PY", "/usr/bin/python3")
GIT_MIRROR = os.environ.get("GIT_MIRROR", "/usr/local/bin/git-mirror")
BACKUP_KEEP = 10


WEBSITE_TOOL = {
    "type": "function",
    "function": {
        "name": "website_manage",
        "description": (
            "查看和修改学长的个人网站 www.romancohort.cloud。"
            "action=list_sections 先看网站有哪些章节；read_section 读某章节现在的内容；"
            "edit_section 改内容（会自动备份，改坏了自动回滚）；regenerate 重新生成页面。"
            "\n【硬要求】学长让你改网站时（加条目 / 改描述 / 重新生成），**必须立刻调用本工具**。"
            "嘴上说「好，这就加」却没有实际调用 = 欺骗，绝对禁止——他真的会去网站上看，"
            "发现没改会以为你在骗他。**只有本工具返回成功，你才能说改好了。**"
            "标准顺序：先 read_section 看清现状 → edit_section 改 → regenerate 生效。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_sections", "read_section", "edit_section", "regenerate"],
                    "description": "要做的动作",
                },
                "section": {
                    "type": "string",
                    "description": "章节 id（list_sections 会列出可选值），read/edit 时必填",
                },
                "field": {
                    "type": "string",
                    "description": "要改的字段：desc_zh / desc_en（仓库描述）、content_zh / content_en（手写章节）、meta_zh / about_zh 等顶层字段",
                },
                "value": {
                    "type": "string",
                    "description": "新内容（edit_section 时必填）。支持 HTML 片段",
                },
            },
            "required": ["action"],
        },
    },
}


def _load_yaml():
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    with open(SITE_YML, encoding="utf-8") as f:
        return y, y.load(f)


def _dump_yaml(y, data) -> str:
    import io
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


def _verify(text: str) -> bool:
    """把待写入的内容解析一遍，确认 YAML 没坏。"""
    try:
        from ruamel.yaml import YAML
        YAML().load(text)
        return True
    except Exception as e:
        logger.warning(f"[website] YAML 校验失败: {e}")
        return False


def _backup() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = SITE_YML.with_suffix(f".yml.bak.{ts}")
    shutil.copy2(SITE_YML, bak)
    # 只留最近 N 份
    baks = sorted(SITE_YML.parent.glob("site.yml.bak.*"))
    for old in baks[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except Exception:
            pass
    return bak


def _write_atomic(text: str):
    tmp = SITE_YML.with_suffix(".yml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, SITE_YML)


def _sections(data) -> list:
    return data.get("sections") or []


def _find_section(data, sid: str):
    for s in _sections(data):
        if str(s.get("id", "")).strip() == sid.strip():
            return s
    return None


def _brief(val, n: int = 70) -> str:
    s = re.sub(r"\s+", " ", str(val or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


def website_manage(action: str, section: str = "", field: str = "", value: str = "") -> str:
    try:
        if action == "list_sections":
            _, data = _load_yaml()
            secs = _sections(data)
            lines = [f"网站配置 {SITE_YML} 共 {len(secs)} 个章节:"]
            for s in secs:
                sid = s.get("id", "?")
                style = s.get("style", "")
                nrepo = len(s.get("repos") or [])
                has_txt = "content_zh" in s or "content_en" in s
                extra = []
                if style:
                    extra.append(f"style={style}")
                if nrepo:
                    extra.append(f"{nrepo} 个仓库")
                if has_txt:
                    extra.append("手写内容")
                lines.append(f"  · {sid}   ({', '.join(extra) or '—'})")
            lines.append("\n顶层字段: identity / meta_zh / meta_en / about_zh / about_en / contact_note_zh …")
            return "\n".join(lines)

        if action == "read_section":
            _, data = _load_yaml()
            if not section:
                return "(read_section 需要给 section)"
            s = _find_section(data, section)
            if s is None:
                return f"(没有叫 {section} 的章节；先 list_sections 看看)"
            out = [f"章节 {section}:"]
            for k in ("title_zh", "title_en", "content_zh", "content_en"):
                if s.get(k):
                    out.append(f"  [{k}] {_brief(s[k], 300)}")
            for r in (s.get("repos") or []):
                nm = r.get("name", "?")
                dz = _brief(r.get("desc_zh", ""), 50)
                de = _brief(r.get("desc_en", ""), 50)
                out.append(f"  · repo {nm}\n      zh: {dz or '—'}\n      en: {de or '—'}")
            return "\n".join(out)

        if action == "edit_section":
            if not section or not field or not value:
                return "(edit_section 需要 section + field + value 三个都给)"
            y, data = _load_yaml()

            # 支持 repo:<name> 形式改某仓库的描述
            target_desc = None
            sec = _find_section(data, section)
            if sec is None:
                return f"(没有叫 {section} 的章节)"

            if field in ("desc_zh", "desc_en"):
                # 需要指定是哪个 repo —— 从 value 里不好推，要求 section 写成 "projects:TorusFold"
                if ":" in section:
                    sid, rname = section.split(":", 1)
                    sec = _find_section(data, sid)
                    if sec is None:
                        return f"(没有叫 {sid} 的章节)"
                    for r in (sec.get("repos") or []):
                        if str(r.get("name", "")).strip() == rname.strip():
                            r[field] = value
                            target_desc = f"{sid}/{rname}.{field}"
                            break
                    if target_desc is None:
                        return f"(在 {sid} 里没找到仓库 {rname})"
                else:
                    return "(改仓库描述请把 section 写成 \"章节id:仓库名\"，例如 projects:TorusFold)"
            else:
                sec[field] = value
                target_desc = f"{section}.{field}"

            text = _dump_yaml(y, data)
            if not _verify(text):
                return "(改完之后 YAML 解析不了，已放弃这次修改)"

            bak = _backup()
            _write_atomic(text)
            logger.info(f"[website] edited {target_desc}, backup={bak.name}")
            return (
                f"改好了: {target_desc} = {_brief(value, 120)}\n"
                f"（已备份到 {bak.name}）\n"
                f"要生效还得跑一次 regenerate。"
            )

        if action == "regenerate":
            # 先 dry-run 确认不炸，再真跑
            dry = subprocess.run(
                [SITEGEN_PY, str(SITEGEN), "--dry-run"],
                capture_output=True, text=True, timeout=120,
            )
            if dry.returncode != 0:
                return (
                    f"(dry-run 就失败了，没有真跑。请检查刚改的内容)\n"
                    f"{(dry.stderr or dry.stdout or '')[:400]}"
                )
            real = subprocess.run(
                [SITEGEN_PY, str(SITEGEN)],
                capture_output=True, text=True, timeout=180,
            )
            ok = real.returncode == 0
            tail = ((real.stdout or "") + (real.stderr or ""))[-400:]
            logger.info(f"[website] regenerate rc={real.returncode}")
            return (
                f"{'网站已重新生成 ✓' if ok else '生成失败 ✗'}\n{tail}\n"
                f"（https://www.romancohort.cloud/ 已经是最新的了）"
                if ok else f"生成失败，输出:\n{tail}"
            )

        return f"(未知 action: {action})"
    except Exception as e:
        logger.warning(f"[website] {action} failed: {e}")
        return f"(网站操作出错了：{type(e).__name__}: {e})"

# ── 代码镜像更新 ────────────────────────────────────────────────────────
MIRROR_TOOL = {
    "type": "function",
    "function": {
        "name": "mirror_update",
        "description": (
            "更新代码镜像（/repos/）里某个仓库，或全部。**会真的去 GitHub 拉最新代码**，"
            "然后重新生成文件树、tar.gz 和索引。"
            "repo 不填 = 全部 32 个（可能要几分钟）。"
            "\n【硬要求】这是真会改文件的操作。说「更新好了」之前必须看到命令成功退出；"
            "失败就如实报错——绝不编造「我这边卡住了」这种没发生过的事。"
            "没有这个能力就直说做不到。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "仓库名（GitHub 上的名字，如 CircDesign-）。不填=全部"},
            },
            "required": [],
        },
    },
}


def mirror_update(repo: str = "") -> str:
    """跑 /usr/local/bin/git-mirror [repo]。

    注意：**git-mirror 才负责拉取**（它先更新 /srv/git/*.git 再调 git-mirror-site 渲染）。
    git-mirror-site 只管渲染、不拉代码 —— 调错那个会出现「跑成功但版本没变」的假象。
    """
    repo = (repo or "").strip()
    cmd = [GIT_MIRROR]
    if repo:
        cmd.append(repo)

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return "(更新超时（15 分钟没跑完）。全部更新很慢，建议指定单个仓库。)"
    except Exception as e:
        return "(执行失败：%s: %s)" % (type(e).__name__, e)

    out = (r.stdout or "") + (r.stderr or "")
    tail = out[-800:]

    if r.returncode != 0:
        logger.warning("[website] mirror_update rc=%s repo=%r" % (r.returncode, repo))
        return "更新失败（退出码 %s）：\n%s" % (r.returncode, tail)

    if repo:
        revf = Path("/var/www/site/repos/%s/.archive-rev" % repo)
        rev = revf.read_text(encoding="utf-8").strip()[:12] if revf.exists() else "(拿不到 rev)"
        logger.info("[website] mirror updated %s -> %s" % (repo, rev))
        return "%s 更新完成，当前版本 %s\n%s" % (repo, rev, tail)
    return "全部更新完成\n%s" % tail
