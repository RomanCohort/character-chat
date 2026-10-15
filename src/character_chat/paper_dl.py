#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文献中转池 —— 配 GET /api/dl

动机：arXiv / Europe PMC / RCSB 这些**开放获取**的源，国内直连很慢（实测
arXiv PDF 约 45 KB/s），从这台 SG 机器上取快一个数量级。所以访客点「中转」
时由这台机器代取，取回来的副本在池子里留 1 小时，同一条链接再点就吃本地
副本（走 nginx 内部重定向，字节不经过 Python）。

**这不是通用代理。** 白名单之外一律 403 —— 否则这台出网只有 ~1 Mbps 的小
机器会变成别人家的免费 CDN，流量和风险都算我们的。白名单里只有开放获取的
源，付费墙的一个都没有（既慢，也不该由我们代传）。

四道闸：
  · 域名白名单 + 单文件大小上限 + 首字节校验（不转发 HTML，免得我们的域名
    被拿来当 XSS 跳板）
  · 全局并发 1（出口就 ~1 Mbps，同时取两个谁都别想快）
  · 单 IP 每日文件数 / 字节数配额，外加一个全站每日总量兜底
  · 池子由 ling-spool-sweep.timer 每 10 分钟扫一次，过期即删
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SPOOL_DIR = os.environ.get("SPOOL_DIR", "/var/lib/ling/spool")
QUOTA_FILE = os.environ.get("DL_QUOTA_FILE", "/var/lib/ling/dl_quota.json")
TTL_S = int(os.environ.get("SPOOL_TTL_S", "3600"))
MAX_TOTAL = int(os.environ.get("SPOOL_MAX_TOTAL", str(600 * 1024 * 1024)))
IP_FILES_PER_DAY = int(os.environ.get("DL_IP_FILES_PER_DAY", "12"))
IP_BYTES_PER_DAY = int(os.environ.get("DL_IP_BYTES_PER_DAY", str(200 * 1024 * 1024)))
DAY_FILES_CAP = int(os.environ.get("DL_DAY_FILES_CAP", "300"))
DAY_BYTES_CAP = int(os.environ.get("DL_DAY_BYTES_CAP", str(6 * 1024 * 1024 * 1024)))
UPSTREAM_TIMEOUT = float(os.environ.get("DL_UPSTREAM_TIMEOUT", "30"))
CHUNK = 64 * 1024

UA = ("Mozilla/5.0 (compatible; romancohort-spool/1.0; open-access relay; "
      "+https://www.romancohort.cloud/)")

MB = 1024 * 1024
# 后缀 -> (单文件上限, 类型, 显示名)。只放**实测从这台机器取得到**的开放源。
ALLOW = {
    "arxiv.org":            (25 * MB, "pdf", "arXiv"),
    "europepmc.org":        (25 * MB, "pdf", "Europe PMC"),
    "pmc.ncbi.nlm.nih.gov": (25 * MB, "pdf", "PMC"),
    "ncbi.nlm.nih.gov":     (25 * MB, "pdf", "NCBI"),
    "ebi.ac.uk":            (25 * MB, "pdf", "EBI"),
    # 开放获取出版社（2026-10-12 从本机实测能直连取到 PDF）。
    # 只收整站 OA 的：PLOS / Frontiers / BMC。MDPI 实测 403，不收。
    "plos.org":             (25 * MB, "pdf", "PLOS"),
    "frontiersin.org":      (25 * MB, "pdf", "Frontiers"),
    "biomedcentral.com":    (25 * MB, "pdf", "BMC"),
    "rcsb.org":             (12 * MB, "pdb", "RCSB PDB"),
    "wwpdb.org":            (12 * MB, "pdb", "wwPDB"),
    "alphafold.ebi.ac.uk":  (12 * MB, "pdb", "AlphaFold DB"),
    "uniprot.org":          (8 * MB, "txt", "UniProt"),
}
CTYPE = {"pdf": "application/pdf", "pdb": "text/plain; charset=utf-8",
         "txt": "text/plain; charset=utf-8", "gz": "application/gzip"}
_EXT_BY_KIND = {"pdf": ".pdf", "pdb": ".pdb", "txt": ".txt"}

_gate = threading.Semaphore(1)
_gate_since = [0.0]
_qlock = threading.Lock()


class DlError(Exception):
    """带 HTTP 状态码的拒绝理由 —— 给访客看的都是人话。"""

    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.code = code


def _today():
    return time.strftime("%Y-%m-%d")


def _ext(path, kind):
    low = (path or "").lower()
    for e in (".cif.gz", ".pdb.gz", ".pdf", ".pdb", ".cif", ".gz", ".txt"):
        if low.endswith(e):
            return e
    return _EXT_BY_KIND.get(kind, ".bin")


def _ctype(kind, ext):
    return CTYPE["gz"] if ext.endswith(".gz") else CTYPE.get(kind, "application/octet-stream")


def suffixes():
    """给前端判断「哪些链接可以中转」，白名单只有这一份。"""
    return sorted(ALLOW)


def resolve(u):
    """校验 URL 并归一化。返回 (干净 url, 上限, 类型, 源名, 扩展名)。"""
    p = urllib.parse.urlsplit((u or "").strip())
    if p.scheme not in ("http", "https"):
        raise DlError("只支持 http/https 链接。")
    if p.port and p.port not in (80, 443):
        raise DlError("端口不对。")
    host = (p.hostname or "").lower()
    hit = None
    for suf, cfg in ALLOW.items():
        if host == suf or host.endswith("." + suf):
            if hit is None or len(suf) > len(hit[0]):
                hit = (suf, cfg)
    if hit is None:
        # 白名单是手写的，追不上开放获取平台的长尾（实测：OpenAlex 给的
        # 开放获取 PDF 落在 sciengine.com / front-sci.com 这类域名上，
        # 于是「只有前几条 arXiv 有中转按钮」）。所以再放一条路：凡是
        # **我们自己的开放获取接口**（OpenAlex 的 oa_url、Europe PMC 的
        # OA 全文、DOAJ、Unpaywall）确认过的 URL，也允许中转。
        # 这个集合只由服务端写入，访客改不了 —— 依旧不是通用代理。
        from character_chat import sources as _src
        if not _src.oa_is_verified(u):
            raise DlError("这个链接不是我们确认过的开放获取位置，本站不代取。", 403)
        cap, kind, label = 25 * MB, "pdf", "开放获取"
        return (urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, "")),
                cap, kind, label, _ext(p.path, kind))
    cap, kind, label = hit[1]
    clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
    return clean, cap, kind, label, _ext(p.path, kind)


def name_for(url, ext):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:24] + ext


def filename_for(url, kind, ext):
    """Content-Disposition 里给的文件名 —— 取原路径基名，清掉可疑字符。"""
    base = os.path.basename(urllib.parse.urlsplit(url).path) or ("paper" + ext)
    base = "".join(c for c in base if c.isalnum() or c in "._-")[:80]
    if not base:
        base = ("paper" if kind == "pdf" else "structure") + ext
    # arXiv 的 PDF 路径没有后缀（/pdf/2610.13305），得自己补，不然浏览器存下来
    # 是个没后缀的文件。
    if not base.lower().endswith((".pdf", ".pdb", ".cif", ".gz", ".txt")):
        base += ext
    return base


def _load_quota():
    try:
        with io.open(QUOTA_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_quota(d):
    try:
        parent = os.path.dirname(QUOTA_FILE)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        tmp = QUOTA_FILE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(d))
        os.replace(tmp, QUOTA_FILE)
    except Exception:
        pass


def _quota_today(d):
    day = d.get(_today())
    return day if isinstance(day, dict) else {}


def quota_check(ip):
    with _qlock:
        day = _quota_today(_load_quota())
    rec = day.get(ip) or [0, 0]
    if rec[0] >= IP_FILES_PER_DAY:
        raise DlError("今天中转的次数用完了，明天再来。", 429)
    if rec[1] >= IP_BYTES_PER_DAY:
        raise DlError("今天中转的流量用完了，明天再来。", 429)
    tot = day.get("*") or [0, 0]
    if tot[0] >= DAY_FILES_CAP or tot[1] >= DAY_BYTES_CAP:
        raise DlError("今天站上的中转额度用完了，明天再来。", 429)


def quota_spend(ip, nbytes):
    with _qlock:
        d = _load_quota()
        day = _quota_today(d)
        for key in (ip, "*"):
            rec = day.get(key) or [0, 0]
            day[key] = [int(rec[0]) + 1, int(rec[1]) + int(nbytes)]
        _save_quota({_today(): day})     # 只留今天，文件不会长


def spool_path(nm):
    return os.path.join(SPOOL_DIR, nm)


def fresh(path):
    try:
        return time.time() - os.stat(path).st_mtime < TTL_S
    except Exception:
        return False


def _acquire_gate():
    """占并发闸，带一个兜底：万一上次的生成器没走到 finally（客户端半路消失、
    进程里别的怪事），闸不能被永久占住 —— 超过 10 分钟就强制放掉再试。
    """
    if _gate.acquire(blocking=False):
        _gate_since[0] = time.time()
        return True
    if time.time() - _gate_since[0] > 600:
        try:
            _gate.release()
        except Exception:
            pass
        if _gate.acquire(blocking=False):
            _gate_since[0] = time.time()
            return True
    return False


def _open_upstream(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        return urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise DlError("源站返回 %d，取不到。" % e.code, 502)
    except Exception as e:
        raise DlError("连源站失败（%s）。" % type(e).__name__, 502)


def _stream(resp, head, dest_tmp, dest, cap, kind, ip):
    """一边写池子一边吐给访客。异常一律在 finally 里收尾（含并发闸）。"""
    done = False
    fh = None
    n = 0
    try:
        if not os.path.isdir(SPOOL_DIR):
            os.makedirs(SPOOL_DIR)
        fh = open(dest_tmp, "wb")
        fh.write(head)
        n = len(head)
        if head:
            yield head
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            n += len(chunk)
            if n > cap:
                raise DlError("文件超过中转大小上限（%d MB）。" % (cap // MB), 413)
            fh.write(chunk)
            yield chunk
        fh.close()
        fh = None
        os.replace(dest_tmp, dest)
        done = True
    finally:
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
        if not done:
            try:
                os.remove(dest_tmp)
            except Exception:
                pass
        else:
            quota_spend(ip, n)
        _gate.release()


def begin(u, ip):
    """一次中转请求的全部前置：校验 → 配额 → 命中池子 → 占闸 → 连源站。

    返回 {"internal": ...}（命中池子，交给 nginx 发）或
        {"gen": 生成器工厂, "ctype": ..., "filename": ...}（边取边发）。
    """
    url, cap, kind, label, ext = resolve(u)
    quota_check(ip)
    dest = spool_path(name_for(url, ext))
    fn = filename_for(url, kind, ext)
    ctype = _ctype(kind, ext)
    if fresh(dest):
        # 池子命中也要计配额：省的是**取**的功夫，出口带宽照样是我们的
        try:
            quota_spend(ip, os.path.getsize(dest))
        except Exception:
            pass
        return {"internal": "/_spool/" + os.path.basename(dest), "ctype": ctype,
                "filename": fn, "label": label, "hits": "spool"}
    if not _acquire_gate():
        raise DlError("正在给别人取文献，等一分钟再点。", 429)
    try:
        resp = _open_upstream(url)
        clen = resp.headers.get("Content-Length")
        if clen and clen.isdigit() and int(clen) > cap:
            resp.close()
            raise DlError("文件超过中转大小上限（%d MB）。" % (cap // MB), 413)
        # 首字节校验放在**返回之前**：进了流式响应之后再发现不是 PDF，
        # 客户端只会收到一个 0 字节的 200（实测踩到：开放获取平台给的
        # 有的是「在线阅读」页，点了就是个空文件）。这里提前判、提前报错。
        head = resp.read(8) or b""
        if kind == "pdf" and not head.startswith(b"%PDF"):
            resp.close()
            raise DlError("这个链接不是 PDF（多半是阅读页或跳转页），不代取。", 502)
    except Exception:
        _gate.release()
        raise
    tmp = dest + ".tmp"
    return {"gen": lambda: _stream(resp, head, tmp, dest, cap, kind, ip),
            "ctype": ctype, "filename": fn, "label": label, "hits": "upstream"}
