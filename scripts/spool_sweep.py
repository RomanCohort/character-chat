#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫中转池：过期的删掉，总量超了从最旧的开始删。

由 ling-spool-sweep.timer 每 10 分钟跑一次（systemd 那边是 oneshot）。
用系统 python3 跑，不依赖 venv。半截的 .tmp（上一个请求被打断留下的）也清。
"""
import os
import time

SPOOL = os.environ.get("SPOOL_DIR", "/var/lib/ling/spool")
TTL = int(os.environ.get("SPOOL_TTL_S", "3600"))
MAX_TOTAL = int(os.environ.get("SPOOL_MAX_TOTAL", str(600 * 1024 * 1024)))


def main():
    if not os.path.isdir(SPOOL):
        print("[spool] %s 不存在，跳过" % SPOOL)
        return 0
    now = time.time()
    kept, dropped, freed = [], 0, 0
    for fn in os.listdir(SPOOL):
        p = os.path.join(SPOOL, fn)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except Exception:
            continue
        if fn.endswith(".tmp") or now - st.st_mtime > TTL:
            try:
                os.remove(p)
                dropped += 1
                freed += st.st_size
            except Exception:
                pass
        else:
            kept.append((st.st_mtime, st.st_size, p))
    total = sum(k[1] for k in kept)
    kept.sort()
    for _, size, p in kept:
        if total <= MAX_TOTAL:
            break
        try:
            os.remove(p)
            total -= size
            dropped += 1
            freed += size
        except Exception:
            pass
    print("[spool] removed=%d freed=%.1fMB left=%.1fMB"
          % (dropped, freed / 1048576.0, total / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
