#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2（文件粒度版）：给每个仓库文件建一个语义向量。

为什么不是 chunk 粒度：实测本机 8060S 上 embedding 约 8.3 条/秒（1200 字），
99,850 个 chunk 要 3.5 小时；13,383 个文件只要 ~45 分钟。
分工：**语义层负责"哪个文件/哪个仓库"，词法层（FTS5）负责"具体哪一段"**。

产物（都在 /opt/character_chat/data/rag_sem/）：
  vectors.f16   行号 * 1024 的 fp16 裸矩阵（numpy.memmap 查询，不吃 RSS）
  index.db      file_vec(row, repo, path) + meta
"""
import io
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request

CHUNKDB = "/opt/character_chat/data/rag_index.db"
OUTDIR = "/opt/character_chat/data/rag_sem"
VECFILE = os.path.join(OUTDIR, "vectors.f16")
SEMDB = os.path.join(OUTDIR, "index.db")
EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8092/v1/embeddings")
BATCH = int(os.environ.get("EMBED_BATCH", "32"))
LIMIT = int(os.environ.get("EMBED_LIMIT", "0"))
DOC_CHARS = int(os.environ.get("DOC_CHARS", "2000"))


def embed_batch(texts):
    body = json.dumps({"input": texts, "model": "embed"}).encode("utf-8")
    req = urllib.request.Request(EMBED_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    return [d["embedding"] for d in sorted(j["data"], key=lambda d: d.get("index", 0))]


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    con = sqlite3.connect("file:%s?mode=ro" % CHUNKDB, uri=True)
    # 每个 (repo, path) 取前若干 chunk 拼成一篇"文件级文档"
    files = {}
    for repo, path, text in con.execute(
            "SELECT repo, path, text FROM chunks ORDER BY repo, path, rowid"):
        key = (repo, path)
        if key not in files:
            files[key] = []
        if sum(len(t) for t in files[key]) < DOC_CHARS:
            files[key].append(text)
    con.close()
    docs = [((r, p), (" ".join(ts))[:DOC_CHARS]) for (r, p), ts in files.items()]
    if LIMIT:
        docs = docs[:LIMIT]
    print("文件数 %d，目标 %d 字符/篇，批次 %d" % (len(docs), DOC_CHARS, BATCH), flush=True)

    done = 0
    dim = 0
    if os.path.exists(SEMDB):
        c2 = sqlite3.connect(SEMDB)
        try:
            done = c2.execute("SELECT COUNT(*) FROM file_vec").fetchone()[0]
            dim = c2.execute("SELECT value FROM meta WHERE key='dim'").fetchone()
            dim = int(dim[0]) if dim else 0
        except Exception:
            done = 0
        c2.close()
    print("已完成 %d，维度 %s" % (done, dim or "?"), flush=True)

    c2 = sqlite3.connect(SEMDB)
    c2.execute("CREATE TABLE IF NOT EXISTS file_vec(row INTEGER PRIMARY KEY, repo TEXT, path TEXT)")
    c2.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    f = io.open(VECFILE, "ab")
    t0 = time.time()
    n = done
    try:
        while n < len(docs):
            batch = docs[n:n + BATCH]
            texts = ["%s %s\n%s" % (r, p, t) for (r, p), t in batch]
            try:
                vecs = embed_batch(texts)
            except Exception as e:
                print("  批次失败(%s)，5 秒后重试" % e, flush=True)
                time.sleep(5)
                continue
            if not dim:
                dim = len(vecs[0])
                print("  维度 = %d" % dim, flush=True)
            for (key, _t), v in zip(batch, vecs):
                f.write(struct.pack("<%df" % len(v), *[float(x) for x in v]))
            for (key, _t) in batch:
                c2.execute("INSERT OR REPLACE INTO file_vec(row, repo, path) VALUES(?,?,?)",
                           (n + batch.index((key, _t)), key[0], key[1]))
            n += len(batch)
            f.flush()
            c2.commit()
            c2.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('dim',?)", (str(dim),))
            c2.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('done',?)", (str(n),))
            c2.commit()
            if n % (BATCH * 10) < BATCH:
                el = time.time() - t0
                sp = (n - done) / max(1e-6, el)
                print("  %d/%d  %.1f 文件/秒  剩余 %.1f 分钟"
                      % (n, len(docs), sp, (len(docs) - n) / max(1e-6, sp) / 60), flush=True)
    finally:
        f.close()
        c2.close()
    print("完成 %d/%d，向量文件 %.1f MB，用时 %.1f 分钟"
          % (n, len(docs), os.path.getsize(VECFILE) / 1048576.0, (time.time() - t0) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
