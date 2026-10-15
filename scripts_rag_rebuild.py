#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像更新后重建检索索引。

做两件事：
  1. FTS5 词法索引（data/rag_index.db 的 chunks 表）——增量：只重建"页面变了"的仓库，
     用 manifest 里的 (路径, 大小, mtime) 指纹判断；仓库被删掉的行也清掉。
  2. 导出文件级语料 data/rag_docs.jsonl —— 本机算语义向量用（GPU 在那台机器上）。

为什么粒度是"文件"不是"chunk"：语义层负责"哪个文件"，词法层负责"哪一段"。
分词必须和查询侧一致：CJK 逐字加空格（FTS5 没有 trigram，见 guest_ops 的说明）。

可反复执行，幂等。
"""
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time

SITE = "/var/www/site"
REPOS = os.path.join(SITE, "repos")
DB = "/opt/character_chat/data/rag_index.db"
DOCS = "/opt/character_chat/data/rag_docs.jsonl"
MANIFEST = "/opt/character_chat/data/rag_manifest.json"
CHUNK = 1200
OVERLAP = 200
DOC_CHARS = 1200
SITE_DOC_CHARS = 6000
MIN_PIECE = 80

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
TAG_RE = re.compile(r"(?is)<(script|style|svg)[^>]*>.*?</\1>")
PRE_RE = re.compile(r'(?s)<pre class="blob">(.*?)</pre>')
ANY_TAG = re.compile(r"(?s)<[^>]+>")
WS = re.compile(r"[ \t\r\f\v]+")
CJK_ONE = re.compile("([%s])" % CJK)

SKIP_FILES = {"search.html"}


MAIN_RE = re.compile(r"(?is)<main[^>]*>(.*?)</main>")
CHROME_RE = re.compile(r"(?is)<(nav|header|footer|aside)[^>]*>.*?</\1>")
TAGDROP_RE = re.compile(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1\s*>")


def _plain(raw):
    s = ANY_TAG.sub("\n", raw)
    s = (s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return "\n".join(l.strip() for l in WS.sub(" ", s).splitlines() if l.strip())


# <main> 抽出来不到这个长度就当它没正文：留言板的 <main> 只有表单
# （33 字："昵称 / 留言 / 提交 / 还没有公开的留言"），整页文本才有那句
# "留言先给管理员看过再显示"。太短就退回整页，不然这一页整体掉出索引。
SITE_MAIN_MIN = 200


def extract(raw, site=False):
    if site:
        # torusfold/viewer.html 的 <main> 里塞着几十万字的行内脚本/数据 ——
        # 不剥掉的话光这一页就产出上千个 chunk，把索引和本机 embedding 都拖垮。
        raw = TAGDROP_RE.sub(" ", raw)
        stripped = CHROME_RE.sub(" ", raw)
        m = MAIN_RE.search(stripped)
        if m:
            body = _plain(m.group(1))
            if len(body) >= SITE_MAIN_MIN:
                return body
        return _plain(stripped)
    m = PRE_RE.search(raw)
    if m:
        raw = m.group(1)
    else:
        raw = TAG_RE.sub(" ", raw)
    return _plain(raw)


def cjk_space(s):
    return CJK_ONE.sub(r" \1 ", s)


# 站点自身页面不再硬编码：以前是白名单，导致 board/ 、torusfold/ 这些
# 后来加的页面永远进不了索引（问答官"看不到网站"就是这么来的）。
SITE_SKIP_DIRS = {"repos", "assets", ".git", "dl"}


def collect():
    """[(repo, rel, abspath)]，repo=__site__ 表示站点自身页面。"""
    out = []
    if os.path.isdir(REPOS):
        for repo in sorted(os.listdir(REPOS)):
            rd = os.path.join(REPOS, repo)
            if not os.path.isdir(rd) or repo.startswith("."):
                continue
            for root, dirs, files in os.walk(rd):
                dirs[:] = [d for d in dirs if d not in (".git",)]
                for f in sorted(files):
                    if not f.endswith(".html") or f in SKIP_FILES:
                        continue
                    p = os.path.join(root, f)
                    out.append((repo, os.path.relpath(p, rd).replace(os.sep, "/"), p))
    for root, dirs, files in os.walk(SITE):
        dirs[:] = [d for d in dirs if d not in SITE_SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".html"):
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, SITE).replace(os.sep, "/")
            out.append(("__site__", rel, p))
    return out


def fingerprint(items):
    """{repo: 指纹}，指纹是 (相对路径, 大小, mtime) 的哈希。"""
    per = {}
    for repo, rel, path in items:
        try:
            st = os.stat(path)
        except OSError:
            continue
        per.setdefault(repo, []).append("%s|%d|%d" % (rel, st.st_size, int(st.st_mtime)))
    return dict((r, hashlib.sha1("\n".join(sorted(v)).encode("utf-8")).hexdigest())
                for r, v in per.items())


def main():
    force = "--force" in sys.argv
    t0 = time.time()
    items = collect()
    print("扫描到 %d 个页面，%d 个仓库" % (len(items), len(set(r for r, _p, _f in items))), flush=True)

    old = {}
    if os.path.exists(MANIFEST) and not force:
        try:
            old = json.load(io.open(MANIFEST, encoding="utf-8"))
        except Exception:
            old = {}
    now = fingerprint(items)

    changed = set(r for r in now if old.get(r) != now[r])
    gone = set(old) - set(now)
    print("需要重建: %d 个仓库（新增/改动），删除: %d 个" % (len(changed), len(gone)), flush=True)

    con = sqlite3.connect(DB)
    for repo in sorted(changed | gone):
        con.execute("DELETE FROM chunks WHERE repo = ?", (repo,))
    con.commit()

    n_files = n_chunks = 0
    for repo, rel, path in items:
        if repo not in changed:
            continue
        try:
            raw = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        text = cjk_space(extract(raw, site=(repo == "__site__")))
        if not text.strip():
            continue
        if repo == "__site__":
            # 站点页正文里通常没有自己的路径/URL 字样（"tank" 只出现在 <title> 和
            # 导航里，而导航已被剥掉）。访客问"tank 是什么"时字典那一路就永远搜不到
            # 这一页，只剩语义那一路，权重又低。把路径和 URL 明写进正文最前面。
            url = "/" if rel == "index.html" else (
                "/" + rel[: -len("index.html")] if rel.endswith("/index.html") else "/" + rel)
            text = cjk_space("站内页面 %s URL %s " % (rel, url)) + text
        n_files += 1
        for i in range(0, max(1, len(text)), CHUNK - OVERLAP):
            piece = text[i:i + CHUNK]
            if len(piece) < MIN_PIECE:
                continue
            con.execute("INSERT INTO chunks(repo, path, text) VALUES(?,?,?)", (repo, rel, piece))
            n_chunks += 1

    # ── 站点页路径清单（合成文档）───────────────────────────────────────
    # 实测问答官会编 URL（说留言板在 /message-board.html，实际是 /board/），
    # 也会照着路径名瞎猜页面内容（把 /opensearch/ 说成"给搜索引擎用的格式"）。
    # 所以这张表不只写 URL，还带上每页正文的头两行当摘要 —— 短问句（"tank 是什么"）
    # 往往只命中这张表，摘要就是模型唯一能看到的东西。
    rows = con.execute("SELECT path, text FROM chunks WHERE repo='__site__' "
                       "AND path != '_sitemap' ORDER BY path, rowid").fetchall()
    head_of = {}
    for rel, text in rows:
        head_of.setdefault(rel, text)

    def _despace(s):
        s = re.sub(r"\s+", " ", s).strip()
        for _ in range(4):
            s = re.sub(r"(?<=[%s])\s+(?=[%s])" % (CJK, CJK), "", s)
        return s

    lines = []
    for rel in sorted(head_of):
        rel = rel.replace("\\", "/")
        if rel == "index.html":
            url = "/"
        elif rel.endswith("/index.html"):
            url = "/" + rel[: -len("index.html")]
        else:
            url = "/" + rel
        parts = [_despace(l) for l in head_of[rel].splitlines() if l.strip()]
        title = parts[0] if parts else ""
        digest = " ".join(parts[1:4])[:200] if len(parts) > 1 else ""
        lines.append("%s -> %s | %s | %s" % (rel, url, title, digest))
    con.execute("DELETE FROM chunks WHERE repo='__site__' AND path='_sitemap'")
    if lines:
        sitemap_text = cjk_space(
            "站点页面清单：镜像内路径 -> 公开 URL | 页面标题 | 正文摘要。"
            "被问到站内某页在哪、某个栏目是什么内容，照这里的摘要素描回答，"
            "不要只看路径名猜，也不要自己编 URL：\n"
            + "\n".join(lines))
        for i in range(0, max(1, len(sitemap_text)), CHUNK - OVERLAP):
            piece = sitemap_text[i:i + CHUNK]
            if not piece.strip():
                continue
            con.execute("INSERT INTO chunks(repo, path, text) VALUES(?,?,?)",
                        ('__site__', '_sitemap', piece))
            n_chunks += 1

    con.commit()
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()

    # 导出文件级语料（顺序固定：repo, path），供本机算语义向量
    os.makedirs(os.path.dirname(DOCS), exist_ok=True)
    by_file = {}
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    for repo, path, text in con.execute("SELECT repo, path, text FROM chunks ORDER BY repo, path, rowid"):
        key = (repo, path)
        cap = SITE_DOC_CHARS if repo == "__site__" else DOC_CHARS
        buf = by_file.setdefault(key, [])
        if sum(len(t) for t in buf) < cap:
            buf.append(text)
    con.close()
    tmp = DOCS + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        for (repo, path) in sorted(by_file):
            cap = SITE_DOC_CHARS if repo == "__site__" else DOC_CHARS
            f.write(json.dumps({"repo": repo, "path": path,
                                "text": " ".join(by_file[(repo, path)])[:cap]},
                               ensure_ascii=False) + "\n")
    os.replace(tmp, DOCS)

    # 同时留一份 gzip：本机每小时要拉它，19MB 裸文本在隧道上要十几分钟，
    # gzip 后约 1/4，且 sha 一致——传输量大头是重复的模板文字。
    import gzip
    gz = DOCS + ".gz"
    tmpgz = gz + ".tmp"
    with io.open(DOCS, "rb") as fi, gzip.open(tmpgz, "wb", compresslevel=6) as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)
    os.replace(tmpgz, gz)

    json.dump(now, io.open(MANIFEST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("重建 %d 个文件 / %d 个 chunk；索引共 %d chunk；语料 %d 篇 -> %s；用时 %.0fs"
          % (n_files, n_chunks, total, len(by_file), DOCS, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
