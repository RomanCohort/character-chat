# -*- coding: utf-8 -*-
"""站点搜索栏用的检索：字典(FTS5/bm25) + 语义(向量余弦) → RRF 融合。

和访客工具 search_index 的区别：这里返回的是**可以拼成 URL 的结构化结果**
（语料里的 path 本来就是 'blob/xxx.html'，所以 /repos/<repo>/<path> 即可点开），
而 search_index 返回给模型看的是带片段的文本。
"""
import json
import sqlite3
import urllib.request

from character_chat.tools.guest_ops import _build_fts_query, _tidy

INDEX_DB = "/opt/character_chat/data/rag_index.db"
SEM_URL = "http://127.0.0.1:8093/search"
K_RRF = 60.0
W_LEX, W_SEM = 1.0, 1.2


def _lexical(q, want, repo=""):
    """FTS5 召回：返回 [(repo, path, snippet)]，按 bm25 排序。"""
    fts = _build_fts_query(q)
    con = sqlite3.connect("file:%s?mode=ro" % INDEX_DB, uri=True)
    try:
        if repo:
            rows = con.execute(
                "SELECT repo, path, snippet(chunks, 2, '[', ']', '…', 10) "
                "FROM chunks WHERE chunks MATCH ? AND repo = ? "
                "ORDER BY bm25(chunks) LIMIT ?", (fts, repo, want)).fetchall()
        else:
            rows = con.execute(
                "SELECT repo, path, snippet(chunks, 2, '[', ']', '…', 10) "
                "FROM chunks WHERE chunks MATCH ? "
                "ORDER BY bm25(chunks) LIMIT ?", (fts, want)).fetchall()
    finally:
        con.close()
    return [(r, p, s) for r, p, s in rows]


def _semantic(q, want, repo=""):
    """语义召回：走本机 8093（向量在本机）。不可用时返回空，不影响字典侧。"""
    try:
        body = json.dumps({"query": q, "k": max(want * 2, 20)}).encode("utf-8")
        req = urllib.request.Request(SEM_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return []
    out = []
    for h in j.get("hits", []):
        rp, pa = h.get("repo", ""), h.get("path", "")
        if repo and rp != repo:
            continue
        out.append((rp, pa, float(h.get("score", 0.0))))
    return out


def _url_for(repo, path):
    """语料里的 path 已经是展示页路径（blob/xxx.html），直接拼。"""
    p = (path or "").lstrip("/")
    if not p.endswith(".html") and not p.endswith("tree.html"):
        p = p + ".html"
    return "/repos/%s/%s" % (repo, p)


def fuse(q, k=20, repo=""):
    """融合检索，返回 [{repo, path, url, snippet, score, src}]。"""
    q = (q or "").strip()
    if not q:
        return []
    k = max(1, min(int(k or 20), 50))
    wide = max(k * 3, 40)

    lex, sem = [], []
    try:
        lex = _lexical(q, wide, repo)
    except Exception:
        lex = []
    sem = _semantic(q, k, repo)

    score, order, snip, origin = {}, [], {}, {}
    for rank, (r, p, s) in enumerate(lex):
        key = (r, p)
        if key not in score:
            score[key] = 0.0
            order.append(key)
        score[key] += W_LEX / (K_RRF + rank + 1)
        snip[key] = s
        origin[key] = "lex"
    for rank, (r, p, s) in enumerate(sem):
        key = (r, p)
        if key not in score:
            score[key] = 0.0
            order.append(key)
        score[key] += W_SEM / (K_RRF + rank + 1)
        origin[key] = "sem" if origin.get(key) != "lex" else "both"

    order.sort(key=lambda x: -score[x])
    hits = []
    for (r, p) in order[:k]:
        hits.append({
            "repo": r,
            "path": p,
            "url": _url_for(r, p),
            "snippet": _tidy(snip.get((r, p), ""))[:200],
            "score": round(score[(r, p)], 6),
            "src": origin.get((r, p), "sem"),
        })
    return hits
