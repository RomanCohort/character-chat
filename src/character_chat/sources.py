#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多源学术检索注册表 —— /api/papers 的取数层。

只接**实测可达**的源（2026-10-12 从 SG 服务器逐个量过）。
放弃的都是直连不行的（封数据中心 IP / 反爬挑战 / 要机构 Key），**不写任何绕过**：
  bioRxiv 站点 PDF 403 · Oxford Academic 403 · IEEE Xplore 202 ·
  Zenodo 超时 · RNAcentral 403 · Semantic Scholar 429

统一条目形状（前端只认这一个）：
  {source, source_label, id, title, authors, date, abstract, url, pdf_url}
  pdf_url 只在**开放获取**时才给；付费墙的源这里压根不存在。

每源独立超时、独立失败 —— 一个源挂了不影响其它源。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "romancohort-daily/1.0 (research aggregation; contact via site)"
TIMEOUT = float(os.environ.get("PAPER_SOURCE_TIMEOUT", "8"))
# 对端可达性会抖（实测 EBI 半小时内 200/0.74s 与 12s 超时都出现过）。
# 对策是**重试**，不是加超时 —— 加超时会让每次搜索都等更久，重试只在那一个源上多花几秒。
RETRY_ATTEMPTS = int(os.environ.get("PAPER_SOURCE_RETRY", "2"))
RETRY_GAP_S = float(os.environ.get("PAPER_SOURCE_RETRY_GAP", "1.5"))


def _try_adapter(name, kw):
    """跑一个源，失败重试。返回 (items, 尝试次数, 最后异常)。

    只在**快失败**时重试（< RETRY_SLOW_S 秒）：对端可达性抖一下、连接被重置
    这类错误重试就有用；而如果是卡到超时（慢），再来一次只会把这次的等待翻倍，
    整个请求的预算跟着被拖穿（实测 europepmc 一次尝试 12s，重试后 26s，
    比 as_completed 的预算还长）。慢就认输，交给 status 报「超时未返回」。
    """
    last = None
    tries = max(1, RETRY_ATTEMPTS)
    for i in range(tries):
        t0 = time.time()
        try:
            return (ADAPTERS[name](kw) or []), i + 1, None
        except Exception as e:
            last = e
            if i + 1 < tries and (time.time() - t0) < RETRY_SLOW_S:
                time.sleep(RETRY_GAP_S)
    return [], tries, last
PER_SOURCE = 8
# 2s：快失败（连接被重置、DNS 抽风）才重试。留够余量让最坏的一条链
# （快失败 → 等 1.5s → 重试又挂 8s ≈ 11.5s）落在下面的总预算里。
RETRY_SLOW_S = float(os.environ.get("PAPER_SOURCE_RETRY_SLOW_S", "2"))


def _get(url, data=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _clean(t):
    if not t:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(t))      # 摘要常带 JATS/HTML 标签
    return re.sub(r"\s+", " ", t).strip()


def _item(source, label, sid, title, authors, date, abstract, url, pdf=""):
    return {"source": source, "source_label": label, "id": str(sid or ""),
            "file_label": "PDF",
            "title": _clean(title)[:300], "authors": _clean(authors)[:200],
            "date": (date or "")[:10], "abstract": _clean(abstract)[:1600],
            "url": url or "", "pdf_url": pdf or ""}


# ── arXiv（Fastly，SG 附近有 PoP；开放获取）──────────────────────────────
_ax_lock = threading.Lock()
_ax_last = [0.0]
ARXIV_MIN_GAP_S = float(os.environ.get("ARXIV_MIN_GAP_S", "3"))
ARXIV_COOLDOWN_S = float(os.environ.get("ARXIV_COOLDOWN_S", "900"))
_ax_breaker = {"fails": 0, "until": 0.0}


def _arxiv_gate():
    """arXiv 的 API 明确要求「每 3 秒最多 1 次」。

    多词并搜把请求数翻了 3 倍之后，实测被 Fastly 限流：本地出口直接 429，
    服务器侧表现为「响应头秒回、正文卡死」—— arXiv 那一栏于是整块空掉。
    所以这里做全局限流，并且（见 paper_ask）**只有主检索词会去查 arXiv**。
    """
    with _ax_lock:
        gap = ARXIV_MIN_GAP_S - (time.time() - _ax_last[0])
        if gap > 0:
            time.sleep(min(gap, ARXIV_MIN_GAP_S))
        _ax_last[0] = time.time()


def arxiv(kw):
    if time.time() < _ax_breaker["until"]:
        # 熔断中：arXiv 那边在限流/堵塞（测量：新出口都吃 429，本机表现为
        # 响应头都不回）。继续每问都撞它，只会既慢又更招人烦。
        raise RuntimeError("arXiv 冷却中（%d 秒后自动重试）"
                           % int(_ax_breaker["until"] - time.time()))
    q = urllib.parse.urlencode({"search_query": "all:%s" % kw, "start": 0,
                                "max_results": PER_SOURCE, "sortBy": "relevance",
                                "sortOrder": "descending"})
    _arxiv_gate()
    try:
        with urllib.request.urlopen(
                # 走 https：http 那个会先 301 一次，白多一个来回
                urllib.request.Request("https://export.arxiv.org/api/query?" + q,
                                       headers={"User-Agent": UA}), timeout=TIMEOUT) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        _ax_breaker["fails"] += 1
        if _ax_breaker["fails"] >= 3:
            _ax_breaker["until"] = time.time() + ARXIV_COOLDOWN_S
        raise
    _ax_breaker["fails"] = 0
    out = []
    for m in re.finditer(r"<entry>(.*?)</entry>", xml, re.S):
        e = m.group(1)

        def g(tag):
            mm = re.search(r"<%s>(.*?)</%s>" % (tag, tag), e, re.S)
            return _clean(mm.group(1)) if mm else ""

        aid = g("id").rsplit("/abs/", 1)[-1]
        if not aid:
            continue
        base = aid.split("v")[0]
        names = re.findall(r"<name>(.*?)</name>", e, re.S)
        out.append(_item("arxiv", "arXiv", base, g("title"),
                         ", ".join(n.strip() for n in names[:4]), g("published"),
                         g("summary"), "https://arxiv.org/abs/" + base,
                         "https://arxiv.org/pdf/" + base))
    return out


# ── Europe PMC（含开放获取全文）────────────────────────────────────────
def europepmc(kw):
    q = urllib.parse.urlencode({"query": kw, "format": "json",
                                "pageSize": PER_SOURCE, "resultType": "core"})
    d = _get("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + q)
    out = []
    for it in (d.get("resultList", {}) or {}).get("result", []) or []:
        pmid, pmcid = it.get("pmid") or "", it.get("pmcid") or ""
        doi = it.get("doi") or ""
        if pmid:
            url = "https://europepmc.org/article/MED/" + pmid
        elif pmcid:
            url = "https://europepmc.org/article/PMC/" + pmcid
        elif doi:
            url = "https://doi.org/" + doi
        else:
            continue
        pdf = ""
        # 只有明确开放获取、且有 PMC 编号的才给 PDF 直链
        if pmcid and str(it.get("isOpenAccess") or "").upper() == "Y":
            pdf = "https://europepmc.org/articles/%s?pdf=render" % pmcid
            oa_verify(pdf)
        out.append(_item("europepmc", "Europe PMC", pmid or pmcid or doi,
                         it.get("title"), it.get("authorString"),
                         it.get("firstPublicationDate"), it.get("abstractText"),
                         url, pdf))
    return out


# ── OpenAlex（免费元数据，覆盖全学科）──────────────────────────────────
def openalex(kw):
    d = _get("https://api.openalex.org/works?" + urllib.parse.urlencode(
        {"search": kw, "per-page": PER_SOURCE, "mailto": "site@romancohort.cloud"}))
    out = []
    for it in (d.get("results") or []):
        # 摘要以 inverted index 形式给出，要自己还原
        inv = it.get("abstract_inverted_index") or {}
        abs_txt = ""
        if inv:
            pos = []
            for w, idxs in inv.items():
                for i in idxs:
                    pos.append((i, w))
            abs_txt = " ".join(w for _, w in sorted(pos))
        auth = ", ".join((a.get("author") or {}).get("display_name", "")
                         for a in (it.get("authorships") or [])[:4])
        oa = it.get("open_access") or {}
        row = _item("openalex", "OpenAlex", it.get("id", "").rsplit("/", 1)[-1],
                    it.get("title"), auth, it.get("publication_date"), abs_txt,
                    it.get("doi") or it.get("id") or "",
                    oa.get("oa_url") or "")
        # OpenAlex 的 open_access.oa_url 按定义就是开放获取位置（常常落在
        # sciengine / front-sci 这类平台上），记下来让中转也认它。
        if row.get("pdf_url"):
            oa_verify(row["pdf_url"])
        out.append(row)
    return out


# ── Crossref（DOI 元数据，覆盖全学科）──────────────────────────────────
def crossref(kw):
    d = _get("https://api.crossref.org/works?" + urllib.parse.urlencode(
        {"query": kw, "rows": PER_SOURCE, "mailto": "site@romancohort.cloud"}))
    out = []
    for it in ((d.get("message") or {}).get("items") or []):
        t = it.get("title") or []
        title = t[0] if t else ""
        auth = ", ".join(("%s %s" % (a.get("given", ""), a.get("family", ""))).strip()
                         for a in (it.get("author") or [])[:4])
        # 坑：Crossref 的 date-parts 里可能出现 None（如 [[2024, null]]），
        # 直接 "%02d" % None 会抛 TypeError —— 整源就废了。这里先过滤成数字。
        parts = ((it.get("issued") or {}).get("date-parts") or [[]])
        parts = parts[0] if parts else []
        nums = [int(x) for x in (parts or [])[:3] if str(x).isdigit()]
        date = "-".join("%02d" % x for x in nums)
        out.append(_item("crossref", "Crossref", it.get("DOI", ""), title, auth,
                         date, it.get("abstract"),
                         ("https://doi.org/" + it["DOI"]) if it.get("DOI") else ""))
    return out




# ── RCSB PDB（结构，不是论文 —— 统一条目里的第二种类型）─────────────────
PDB_GQL = ("{ entries(entry_ids: [%s]) { rcsb_id struct { title } "
           "rcsb_accession_info { initial_release_date } "
           "rcsb_entry_info { resolution_combined experimental_method } "
           "rcsb_primary_citation { title year journal_abbrev "
           "rcsb_authors pdbx_database_id_DOI } } }")


def _pdb_post(url, payload, timeout=TIMEOUT):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace").strip()
    # 无命中时 RCSB 回 204 / 空体 —— 那是「没结果」，不是错误。
    # 之前 json.loads("") 直接抛，于是每次「搜不到」都白重试一次还报 ok=False。
    return json.loads(raw) if raw else {}


def pdb(kw):
    """先全库搜 PDB ID，再一次 GraphQL 把详情批量取回来（8 次单查会拖爆超时）。"""
    sr = _pdb_post("https://search.rcsb.org/rcsbsearch/v2/query", {
        "query": {"type": "terminal", "service": "full_text",
                  "parameters": {"value": kw}},
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": 5}},
    })
    ids = [x.get("identifier") for x in (sr.get("result_set") or []) if x.get("identifier")][:5]
    if not ids:
        return []
    body = _pdb_post("https://data.rcsb.org/graphql",
                     {"query": PDB_GQL % ", ".join('"%s"' % i for i in ids)})
    out = []
    for e in ((body.get("data") or {}).get("entries") or []):
        pid = e.get("rcsb_id") or ""
        if not pid:
            continue
        cit = e.get("rcsb_primary_citation") or {}
        info = e.get("rcsb_entry_info") or {}
        res = info.get("resolution_combined") or []
        res = res[0] if res else None
        bits = []
        if info.get("experimental_method"):
            bits.append("方法：%s" % info["experimental_method"])
        if res:
            bits.append("分辨率：%s Å" % res)
        if cit.get("journal_abbrev") or cit.get("year"):
            bits.append("出处：%s %s" % (cit.get("journal_abbrev") or "",
                                                 cit.get("year") or ""))
        if cit.get("pdbx_database_id_DOI"):
            bits.append("DOI：" + cit["pdbx_database_id_DOI"])
        it = _item("pdb", "RCSB PDB", pid,
                   (e.get("struct") or {}).get("title") or cit.get("title"),
                   ", ".join(cit.get("rcsb_authors") or []),
                   (e.get("rcsb_accession_info") or {}).get("initial_release_date"),
                   " · ".join(bits),
                   "https://www.rcsb.org/structure/" + pid,
                   "https://files.rcsb.org/download/%s.pdb" % pid)
        it["file_label"] = "PDB"          # 结构文件不是 PDF，标签得对
        out.append(it)
    return out
def search_all(kw, sources=None):
    """并行扇出到各源。返回 (items, status)。

    **必须并行**：串行时一个源超时就把整个请求拖到单源超时 × 源数 ——
    实测 europepmc 卡一次 SSL 握手，访客就等了 18 秒（搜索框看着像死了）。
    并行之后总耗时 ≈ 最慢那个源（≤ TIMEOUT），一个慢源不再拖累其它源。

    每源独立 try：一个源挂了只记进 status，不影响其它源的产出。

    结果按 ADAPTERS 的顺序拼（不是完成的先后 —— ThreadPoolExecutor 谁先
    回来是不定的，那样同一个查询两次跑出来的顺序都不一样），最后跨源去重。
    """
    import concurrent.futures as cf
    names = [s for s in (sources or list(ADAPTERS)) if s in ADAPTERS]
    got, status = {}, {}
    if not names:
        return [], status
    t0 = time.time()
    ex = cf.ThreadPoolExecutor(max_workers=len(names))
    try:
        futs = {ex.submit(_try_adapter, n, kw): n for n in names}
        try:
            for fut in cf.as_completed(futs, timeout=TIMEOUT + 4):
                n = futs[fut]
                ms = int((time.time() - t0) * 1000)
                try:
                    rows, tries, err = fut.result()
                    if err is None:
                        got[n] = rows
                        status[n] = {"ok": True, "n": len(rows), "ms": ms, "tries": tries}
                    else:
                        status[n] = {"ok": False, "ms": ms, "tries": tries,
                                     "err": "%s: %s" % (type(err).__name__, str(err)[:60])}
                except Exception as e:
                    status[n] = {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:60]),
                                 "ms": ms}
        except cf.TimeoutError:
            pass        # 慢的那个源在这里被放弃，下面统一记成「超时未返回」
    finally:
        # wait=False：绝不为一个卡住的源把整次检索按住。原来用 with 语句退出时
        # 会 shutdown(wait=True)，实测 europepmc 抖到 19s 时，整次搜索先抛
        # TimeoutError 把**已经回来的四个源一起丢掉**，访客只看到「没有命中」。
        ex.shutdown(wait=False, cancel_futures=True)
    for n in names:
        status.setdefault(n, {"ok": False, "err": "超时未返回", "ms": int((time.time()-t0)*1000)})
    items = []
    for n in names:
        items.extend(got.get(n) or [])
    items = _dedup(items)
    if items:
        # 补开放获取位置：只对「没有全文、但有 DOI」的条目问 Unpaywall，
        # 有缓存、有上限，整体最多多花一两秒（并行 + 12s 兜底）。
        try:
            enrich_oa(items)
        except Exception:
            pass
    return items, status


def _doi_of(it):
    """从 doi / url 里抠 DOI —— europepmc、crossref、pdb 都把它塞在这两处。"""
    m = re.search(r"10\.\d{4,9}/[^\s\"<>]+",
                  str(it.get("doi") or "") + " " + str(it.get("url") or ""))
    return m.group(0).rstrip(".").lower() if m else ""


def _dedup(items):
    """跨源去重：同一个 DOI（或同一条规范化标题）只留一条。

    实测「circRNA vaccine」的 32 条里有 8 条是 openalex / crossref /
    europepmc 的同一篇 —— 不去重的话页面上是一片重影，导出的 BibTeX
    也跟着重复。留先出现的那条，缺的字段从重复项里补。
    标题这条路只在标题够长（≥25 字母数字）时才敢用，免得把 "Editorial"
    这种短标题的不同文章并成一条。

    **DOI 和标题要各记一份索引**：europepmc 的 url 是 europepmc.org/article/
    MED/xxx（没有 DOI 可抠），同一篇在 openalex 那边是 doi.org/10.xxx —— 只按
    一个键查的话两边永远碰不上。crossref 还会为同一篇给出 .s001/.s004 这类
    附件条目（DOI 后缀不同、标题相同），也是靠标题这条路并掉的。
    """
    seen_d, seen_t, out = {}, {}, []
    for it in items:
        d = _doi_of(it)
        t = re.sub(r"[^a-z0-9]+", "", (it.get("title") or "").lower())
        t = t[:80] if len(t) >= 25 else ""
        keep = (seen_d.get(d) if d else None) or (seen_t.get(t) if t else None)
        if keep is not None:
            for f in ("pdf_url", "abstract", "date", "url"):
                if not keep.get(f) and it.get(f):
                    keep[f] = it[f]
            continue
        if d:
            seen_d[d] = it
        if t:
            seen_t[t] = it
        out.append(it)
    return out


# ── 开放获取位置解析（Unpaywall）────────────────────────────────────
# 为什么需要：arXiv / Europe PMC / PDB 之外的结果（OpenAlex、Crossref、DOAJ）
# 只给 DOI，页面上就既没有全文也没有「中转下载」。Unpaywall 能把 DOI 换成
# 合法的开放获取位置（0.7s 实测），于是这些条目也能给「开放获取全文」。
# 只查有 DOI 且自己没有 pdf_url 的条目，并逐个缓存 30 天 —— 它的接口是免费的，
# 但没必要反复问同一篇。
OA_CACHE_DIR = os.environ.get("OA_CACHE_DIR", "/opt/arxiv-push/oa_cache")
OA_EMAIL = os.environ.get("OA_EMAIL", "18806370529@163.com")
OA_TTL_S = int(os.environ.get("OA_CACHE_TTL_DAYS", "30")) * 86400
OA_LIMIT = int(os.environ.get("OA_LOOKUP_LIMIT", "24"))

# 开放获取「已验证 URL」集合：凡是我们从**只给开放获取位置**的接口
# （OpenAlex 的 open_access.oa_url、Europe PMC 的 isOpenAccess=Y 全文、
# DOAJ 的 fulltext、Unpaywall 的 oa_url）拿到的链接，都记在这里。
# 中转白名单因此不再需要手写出版社域名 —— 实测的问题就出在这：OpenAlex
# 给的是 sciengine.com / front-sci.com 这类平台，手写白名单根本追不上，
# 于是「只有前几条（arXiv）有中转按钮」。
OA_VERIFIED_FILE = os.environ.get("OA_VERIFIED_FILE", "/opt/arxiv-push/oa_verified.json")
OA_VERIFIED_MAX = int(os.environ.get("OA_VERIFIED_MAX", "20000"))
_vlock = threading.Lock()
_vload = [False]
_vset = {}


def _vload_now():
    if _vload[0]:
        return
    _vload[0] = True
    try:
        with io.open(OA_VERIFIED_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            _vset.update(d)
    except Exception:
        pass


def _vsave():
    try:
        d = os.path.dirname(OA_VERIFIED_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = OA_VERIFIED_FILE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_vset))
        os.replace(tmp, OA_VERIFIED_FILE)
    except Exception:
        pass


def oa_verify(url):
    """把「确认是开放获取」的 URL 记下来（中转那边据此放行）。"""
    if not url or not url.lower().startswith("http"):
        return
    import hashlib
    h = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:20]
    now = time.time()
    with _vlock:
        _vload_now()
        if h in _vset:
            return
        _vset[h] = {"url": url.strip()[:600], "ts": now}
        if len(_vset) > OA_VERIFIED_MAX:        # 超了先扔最老的
            for k in sorted(_vset, key=lambda x: _vset[x].get("ts", 0))[:len(_vset) - OA_VERIFIED_MAX]:
                _vset.pop(k, None)
        _vsave()


def oa_is_verified(url):
    if not url:
        return False
    import hashlib
    h = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:20]
    with _vlock:
        _vload_now()
        return h in _vset


def _oa_cache_path(doi):
    import hashlib
    return os.path.join(OA_CACHE_DIR, hashlib.sha1(doi.encode("utf-8")).hexdigest()[:20] + ".json")


def _oa_cached(doi):
    try:
        p = _oa_cache_path(doi)
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < OA_TTL_S:
            with io.open(p, encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def _oa_put(doi, rec):
    try:
        if not os.path.isdir(OA_CACHE_DIR):
            os.makedirs(OA_CACHE_DIR)
        tmp = _oa_cache_path(doi) + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False))
        os.replace(tmp, _oa_cache_path(doi))
    except Exception:
        pass


def _oa_lookup(doi):
    """返回 {"oa_url": …, "host": "publisher|repository", "license": …} 或 None。"""
    hit = _oa_cached(doi)
    if hit is not None:
        return hit or None
    rec = {}
    try:
        url = ("https://api.unpaywall.org/v2/%s?email=%s"
               % (urllib.parse.quote(doi), urllib.parse.quote(OA_EMAIL)))
        d = _get(url, timeout=6)
        loc = d.get("best_oa_location") or {}
        if d.get("is_oa") and (loc.get("url_for_pdf") or loc.get("url")):
            rec = {"oa_url": loc.get("url_for_pdf") or loc.get("url"),
                   "host": loc.get("host_type") or "",
                   "license": loc.get("license") or "",
                   "version": loc.get("version") or ""}
    except Exception:
        return None                     # 失败不缓存，下次还可以再问
    _oa_put(doi, rec)
    return rec or None


def enrich_oa(items, limit=OA_LIMIT):
    """给缺全文的条目补上开放获取位置（原地改 item，加 oa_url/oa_host）。"""
    import concurrent.futures as cf
    todo = []
    for it in items[:limit]:
        if it.get("pdf_url") or it.get("oa_url"):
            continue
        doi = _doi_of(it)
        if doi:
            todo.append((it, doi))
    if not todo:
        return items
    with cf.ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
        futs = {ex.submit(_oa_lookup, doi): it for it, doi in todo}
        for fut in cf.as_completed(futs, timeout=12):
            it = futs[fut]
            try:
                rec = fut.result()
            except Exception:
                continue
            if rec and rec.get("oa_url"):
                it["oa_url"] = rec["oa_url"]
                it["oa_host"] = rec.get("host") or ""
                oa_verify(rec["oa_url"])
    return items


# ── DOAJ（开放获取期刊目录；实测 0.27s，只收 OA 期刊）───────────────
def doaj(kw):
    q = urllib.parse.quote(kw)
    d = _get("https://doaj.org/api/v2/search/articles/%s?pageSize=%d" % (q, PER_SOURCE))
    out = []
    for r in (d.get("results") or []):
        b = r.get("bibjson") or {}
        title = b.get("title") or ""
        if not title:
            continue
        doi = ""
        for ident in (b.get("identifier") or []):
            if (ident.get("type") or "").lower() == "doi":
                doi = ident.get("id") or ""
                break
        full = ""
        for lk in (b.get("link") or []):
            if (lk.get("type") or "").lower() == "fulltext" and lk.get("url"):
                full = lk["url"]
                break
        url = full or (("https://doi.org/" + doi) if doi else "")
        if not url:
            continue
        authors = ", ".join((a.get("name") or "").strip()
                            for a in (b.get("author") or [])[:4])
        journal = ((b.get("journal") or {}).get("title") or "")
        it = _item("doaj", "DOAJ", doi or title[:40], title, authors,
                   str(b.get("year") or ""), b.get("abstract") or "",
                   url, full if full.lower().endswith(".pdf") else "")
        # DOAJ 只收开放获取期刊，它给的 fulltext 链接就是 OA 位置
        if full:
            oa_verify(full)
        if journal:
            it["abstract"] = (journal + u" · " + (it.get("abstract") or ""))[:1600]
        out.append(it)
    return out


# ── PubMed（NCBI E-utilities；实测 0.34s，比 EBI 稳）────────────────
def pubmed(kw):
    """esearch 找 PMID，再 efetch 取摘要（XML，标准库解析）。

    为什么在有了 Europe PMC 之后还要它：两者语料重叠，但**可达性不一样** ——
    实测同一时间点，EBI 那侧三次查询全部握手超时（0 条），PubMed 三次都稳。
    多这一条，Europe PMC 抽风时生物医学这部分不至于整块消失。
    """
    import xml.etree.ElementTree as ET
    q = urllib.parse.urlencode({"db": "pubmed", "term": kw, "retmode": "json",
                                "retmax": PER_SOURCE, "sort": "relevance"})
    d = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + q)
    ids = [str(x) for x in ((d.get("esearchresult") or {}).get("idlist") or [])]
    if not ids:
        return []
    q2 = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids),
                                 "retmode": "xml", "rettype": "abstract"})
    req = urllib.request.Request(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + q2,
        headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        xml_text = r.read().decode("utf-8", "replace")
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    for art in root.iter("PubmedArticle"):
        def txt(node):
            return _clean("".join(node.itertext())) if node is not None else ""
        pmid = txt(art.find(".//PMID"))
        title = txt(art.find(".//ArticleTitle"))
        if not title:
            continue
        abst = _clean(" ".join("".join(a.itertext()) for a in art.iter("AbstractText")))
        doi = ""
        for aid in art.iter("ArticleId"):
            if (aid.get("IdType") or "") == "doi":
                doi = (aid.text or "").strip()
        names = []
        for a in art.iter("Author"):
            ln, fn = txt(a.find("LastName")), txt(a.find("ForeName"))
            if ln:
                names.append((fn + " " + ln).strip())
        year = txt(art.find(".//PubDate/Year")) or txt(art.find(".//ArticleDate/Year"))
        url = (("https://doi.org/" + doi) if doi
               else ("https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid))
        out.append(_item("pubmed", "PubMed", pmid or doi, title,
                         ", ".join(names[:4]), year, abst, url, ""))
        if len(out) >= PER_SOURCE:
            break
    return out


# ── 导出格式（服务端拼字段，不依赖任何第三方）────────────────────────
# 注册表放在文件末尾：这些名字只在**调用时**才被引用（search_all / server 那边），
# 而适配器函数定义在上面 —— 放在中间的话，加了新适配器忘了调整顺序就会
# NameError（刚才 doaj 就是这么炸的，import 都不给过）。
ADAPTERS = {"arxiv": arxiv, "europepmc": europepmc, "pubmed": pubmed,
            "openalex": openalex, "crossref": crossref, "doaj": doaj, "pdb": pdb}

LABELS = {"arxiv": "arXiv", "europepmc": "Europe PMC", "pubmed": "PubMed",
          "openalex": "OpenAlex", "crossref": "Crossref", "doaj": "DOAJ",
          "pdb": "RCSB PDB"}


def _btex(v):
    """BibTeX 字段值转义。反斜杠必须先转，否则会把后加的转义再转一遍。"""
    t = str(v or "")
    # 用 raw 字符串：不然 Python 会对每个 \X 报 SyntaxWarning（每次启动都刷屏）
    for a, b in (("\\", r"\textbackslash{}"), ("{", r"\{"), ("}", r"\}"),
                 ("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"), ("_", r"\_")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def _bkey(it, i):
    k = re.sub(r"[^A-Za-z0-9_.:-]", "", str(it.get("id") or ""))[:40]
    return k or ("item%d" % i)


def to_bibtex(items):
    blocks = []
    for i, it in enumerate(items, 1):
        f = []
        if it.get("title"):
            f.append(("title", it["title"]))
        if it.get("authors"):
            f.append(("author", it["authors"]))
        if it.get("date"):
            f.append(("year", it["date"][:4]))
        if it.get("url"):
            f.append(("url", it["url"]))
        if it.get("pdf_url"):
            f.append(("howpublished", it["pdf_url"]))
        note = " | ".join(x for x in (it.get("source_label"), it.get("id")) if x)
        if note:
            f.append(("note", note))
        body = ",\n".join("  %s = {%s}" % (k, _btex(v)) for k, v in f if v)
        blocks.append("@misc{%s,\n%s\n}" % (_bkey(it, i), body))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_ris(items):
    out = []
    for it in items:
        out.append("TY  - JOUR")
        if it.get("title"):
            out.append("TI  - " + re.sub(r"\s+", " ", it["title"]))
        for a in (it.get("authors") or "").split(","):
            a = a.strip()
            if a:
                out.append("AU  - " + a)
        if it.get("date"):
            out.append("PY  - " + it["date"][:4])
        if it.get("url"):
            out.append("UR  - " + it["url"])
        if it.get("pdf_url"):
            out.append("L1  - " + it["pdf_url"])
        if it.get("abstract"):
            out.append("AB  - " + re.sub(r"\s+", " ", it["abstract"])[:900])
        if it.get("source_label"):
            out.append("DB  - " + it["source_label"])
        if it.get("id"):
            out.append("ID  - " + str(it["id"]))
        out.append("ER  - ")
        out.append("")
    return "\n".join(out)
