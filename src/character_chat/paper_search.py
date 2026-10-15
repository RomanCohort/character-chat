#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arXiv 检索代理 —— 给 /daily/ 页的搜索框用。

为什么不让浏览器直接打 arxiv.org：① 跨域会被挡；② 请求集中到一处才好限流。

三层抗打（这个模块存在的全部理由，按有效性排序）：
  1. **磁盘缓存** —— 同一关键词 CACHE_TTL_H 小时内只打一次 arXiv。重复查询是
     最常见、也最廉价的那种"攻击"，缓存直接把它变成一次磁盘读。
  2. 全局最小间隔 —— 两次"未命中缓存的"检索之间至少隔 MIN_GAP_S 秒。
  3. 单客户端每日配额 —— 一个 client 一天最多 DAILY_CAP 次。

下载**不中转**：只返回 arxiv.org 的 abs / pdf 链接，字节一个都不经过这台
1Mbps 的机器。这是"下载文献"这条需求里最要紧的一条 —— 别把 arXiv 的流量
引到自己身上。

失败一律返回空列表 + 一句原因，绝不抛给访客。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

CACHE_DIR = os.environ.get("PAPER_CACHE_DIR", "/opt/arxiv-push/cache")
CACHE_TTL_H = float(os.environ.get("PAPER_CACHE_TTL_H", "12"))
MIN_GAP_S = float(os.environ.get("PAPER_MIN_GAP_S", "3"))
DAILY_CAP = int(os.environ.get("PAPER_DAILY_CAP", "30"))
MAX_RESULTS = 10
TIMEOUT = 20

_lock = threading.Lock()
_last_call = [0.0]
_quota = {}          # client -> [date, used]


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _cache_path(kw: str) -> str:
    h = hashlib.sha1(kw.encode("utf-8")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, "%s.json" % h)


def _read_cache(kw: str):
    p = _cache_path(kw)
    try:
        if not os.path.exists(p):
            return None
        if time.time() - os.path.getmtime(p) > CACHE_TTL_H * 3600:
            return None
        with io.open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_cache(kw: str, data) -> None:
    try:
        if not os.path.isdir(CACHE_DIR):
            os.makedirs(CACHE_DIR)
        tmp = _cache_path(kw) + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, _cache_path(kw))
    except Exception:
        pass


def _quota_hit(client: str) -> bool:
    """超过就返回 True。"""
    d = _today()
    rec = _quota.get(client)
    if not rec or rec[0] != d:
        _quota[client] = [d, 1]
        return False
    if rec[1] >= DAILY_CAP:
        return True
    rec[1] += 1
    return False


def _fetch_arxiv(kw: str):
    q = urllib.parse.urlencode({
        "search_query": "all:%s" % kw,
        "start": 0,
        "max_results": MAX_RESULTS,
        "sortBy": "relevance",
        "sortOrder": "descending",
    })
    url = "http://export.arxiv.org/api/query?" + q
    req = urllib.request.Request(url, headers={"User-Agent": "romancohort-daily/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        xml = r.read().decode("utf-8", "replace")

    out = []
    for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.S):
        e = m.group(1)

        def g(tag):
            mm = re.search(r"<%s>(.*?)</%s>" % (tag, tag), e, re.S)
            return re.sub(r"\s+", " ", mm.group(1)).strip() if mm else ""

        aid = g("id").rsplit("/abs/", 1)[-1]
        title = g("title")
        if not aid or not title:
            continue
        authors = re.findall(r"<name>(.*?)</name>", e, re.S)
        out.append({
            "arxiv_id": aid,
            "title": title,
            "authors": ", ".join(a.strip() for a in authors[:4]) + (" et al." if len(authors) > 4 else ""),
            "date": g("published")[:10],
            "abstract": g("summary")[:1600],
            "abs_url": "https://arxiv.org/abs/" + aid.split("v")[0],
            "pdf_url": "https://arxiv.org/pdf/" + aid.split("v")[0],
        })
    return out


def search(kw: str, client: str = "", source: str = "all") -> dict:
    kw = (kw or "").strip()
    if len(kw) < 2:
        return {"query": kw, "items": [], "error": "关键词太短（至少 2 个字符）。"}
    if len(kw) > 120:
        kw = kw[:120]
    key = (source or "all").lower() + "|" + kw.lower()

    cached = _read_cache(key)
    if cached is not None:
        return {"query": kw, "items": cached.get("items", []),
                "status": cached.get("status", {}), "cached": True}

    client = client or "anon"
    with _lock:
        if _quota_hit(client):
            return {"query": kw, "items": [], "error": "今天的检索次数用完了，明天再来。"}
        delta = MIN_GAP_S - (time.time() - _last_call[0])
        if delta > 0:
            time.sleep(min(delta, MIN_GAP_S))
        _last_call[0] = time.time()
        from character_chat import sources
        _srcs = None if (source or "all") == "all" else [source]
        items, status = sources.search_all(kw, _srcs)
    _write_cache(key, {"items": items, "status": status})
    return {"query": kw, "items": items, "status": status, "cached": False}
