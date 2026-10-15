#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2：给 rag_index.db 里的 chunk 建语义向量。

数据流：
  rag_index.db(chunks, FTS5)  --文本-->  embedding 服务(经反向隧道到本机 8092)
                                          |
                                          v
  rag_vec/vectors.f16  （row_index * DIM 的 fp16 原始矩阵，numpy.memmap 查询）
  rag_vec/meta.json    （count / dim / model / 构建时间）

为什么不用 SQLite 存向量：查询时要全量算余弦，读 200MB BLOB 不如直接 memmap
一个裸矩阵——操作系统页缓存会留住热页，RSS 不涨。

可续跑：已写入的行数记在 meta.json 的 done 里，重跑从那里接。
"""
import io
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request

DB = "/opt/character_chat/data/rag_index.db"
OUTDIR = "/opt/character_chat/data/rag_vec"
VECFILE = os.path.join(OUTDIR, "vectors.f16")
METAFILE = os.path.join(OUTDIR, "meta.json")
EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8092/v1/embeddings")
BATCH = int(os.environ.get("EMBED_BATCH", "24"))
LIMIT = int(os.environ.get("EMBED_LIMIT", "0"))   # 0 = 全部


def embed_batch(texts):
    body = json.dumps({"input": texts, "model": "embed"}).encode("utf-8")
    req = urllib.request.Request(EMBED_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    data = sorted(j["data"], key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    meta = {}
    if os.path.exists(METAFILE):
        try:
            meta = json.load(io.open(METAFILE, encoding="utf-8"))
        except Exception:
            meta = {}
    done = int(meta.get("done", 0))
    dim = int(meta.get("dim", 0))

    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    cur = con.execute("SELECT rowid, repo, path, text FROM chunks ORDER BY rowid")
    # 流式：服务器只有 338MB 可用内存，fetchall 会把 90MB 正文 + 对象开销全吃进来。
    skipped = 0
    while skipped < done:
        if cur.fetchone() is None:
            break
        skipped += 1
    if LIMIT:
        total = min(total, done + LIMIT)
    print("chunk 总数 %d，已完成 %d，维度 %s，批次 %d" % (total, done, dim or "?", BATCH))

    t0 = time.time()
    n = done
    rows_last_id = [None]
    f = io.open(VECFILE, "ab")
    try:
        while n < total:
            batch = []
            for row in cur:
                batch.append(row)
                if len(batch) >= BATCH:
                    break
            if not batch:
                break
            texts = [(r[1] + " " + r[2] + "\n" + r[3])[:2400] for r in batch]
            try:
                vecs = embed_batch(texts)
            except Exception as e:
                print("  批次失败(%s)，5 秒后重试" % e)
                time.sleep(5)
                # 重新定位：直接按 rowid 重新取这一批
                cur = con.execute(
                    "SELECT rowid, repo, path, text FROM chunks WHERE rowid > ? "
                    "ORDER BY rowid LIMIT ?", (rows_last_id[0], BATCH))
                continue
            rows_last_id[0] = batch[-1][0]
            if not dim:
                dim = len(vecs[0])
                print("  维度 = %d" % dim)
            for v in vecs:
                f.write(struct.pack("<%df" % len(v), *[float(x) for x in v]))
            n += len(vecs)
            f.flush()
            if n % (BATCH * 20) < BATCH:
                el = time.time() - t0
                sp = (n - done) / max(1e-6, el)
                eta = (total - n) / max(1e-6, sp)
                print("  %d/%d  %.1f chunk/s  预计剩余 %.1f 分钟"
                      % (n, total, sp, eta / 60))
                io.open(METAFILE, "w", encoding="utf-8").write(json.dumps(
                    {"done": n, "dim": dim, "total": total, "model": "Qwen3-Embedding-0.6B-Q8_0",
                     "updated": time.strftime("%F %T")}, ensure_ascii=False, indent=2))
    finally:
        f.close()
    io.open(METAFILE, "w", encoding="utf-8").write(json.dumps(
        {"done": n, "dim": dim, "total": total, "model": "Qwen3-Embedding-0.6B-Q8_0",
         "updated": time.strftime("%F %T"), "complete": n >= total}, ensure_ascii=False, indent=2))
    size = os.path.getsize(VECFILE)
    print("完成 %d/%d  向量文件 %.1f MB  用时 %.1f 分钟"
          % (n, total, size / 1048576.0, (time.time() - t0) / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
