#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""《柏林1945：虎王车长》网页试玩 —— 配 /api/tank/*

游戏本体是站主自己的项目（github.com/RomanCohort/berlin-tank-commander），
放在 /opt/tank-demo/game.py，**一个字都不改**：它本来就是「InputStream + print」
的写法，所以这里只接三样东西：
  1. 每个玩家一个子进程，cwd 与 HOME 都指向各自的临时目录 —— 游戏写在
     ~/<游戏名>/ 下的存档、事件记录、崩溃报告因此全落在临时目录里，会话结束就删
  2. 读它的 stdout（一个读线程 + 增量解码 + 环形缓冲），网页按 offset 增量取
  3. 把网页送来的那一行写进它的 stdin

为什么敢公开：
  · 只 import 标准库，不联网（实测全文没有 socket/urllib/requests）
  · 没有 eval/exec/__import__；唯一的"后门"是游戏内调试菜单（输入 20070529
    改数值），那是设计好的玩法，不是代码执行入口
  · 单局常驻内存实测 ~19MB，冷启动 ~0.4s（它自己跑完一局也就 0.4s）

护栏（公开端点，必须的）：并发上限 · 单 IP 一个会话 · 闲置回收 · 生命周期上限 ·
输入限速 · 输出缓冲上限 · 内存不足时拒新会话。
"""
from __future__ import annotations

import codecs
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time

GAME = os.environ.get("TANK_GAME", "/opt/tank-demo/game.py")
AUTOPLAY = os.environ.get("TANK_AUTOPLAY", "/opt/tank-demo/autoplay.py")
PYTHON = os.environ.get("TANK_PYTHON", "/opt/character_chat/venv/bin/python")

# 一局实测常驻 ~52MB（不是启动时的 19MB —— 玩起来状态会长）。这台机器 896MB，
# 常态可用 ~400MB，所以同时 4 局是留了余量的上限。
MAX_SESSIONS = int(os.environ.get("TANK_MAX_SESSIONS", "4"))
# 「一个人一局」按 client_id 算（浏览器本地 id），不按 IP：同一个校园网出口后面
# 坐着好几个人，按 IP 卡会把同学挡在门外（实测踩到过）。per-IP 只用来防一个人
# 开一堆把名额占满。
MAX_PER_CLIENT = int(os.environ.get("TANK_MAX_PER_CLIENT", "1"))
MAX_PER_IP = int(os.environ.get("TANK_MAX_PER_IP", "3"))
IDLE_FREE_S = float(os.environ.get("TANK_IDLE_FREE_S", "120"))
IDLE_KILL_S = float(os.environ.get("TANK_IDLE_S", "600"))
LIFE_S = float(os.environ.get("TANK_LIFE_S", "3600"))
BUF_CHARS = int(os.environ.get("TANK_BUF_CHARS", str(256 * 1024)))
MIN_GAP_S = float(os.environ.get("TANK_INPUT_GAP_S", "0.25"))
MAX_LINE = 200
MIN_FREE_MB = int(os.environ.get("TANK_MIN_FREE_MB", "150"))
GC_EVERY_S = 30.0

LOG = logging.getLogger("character_chat.tank_demo")
_lock = threading.Lock()
_sessions = {}


class TankError(Exception):
    """带 HTTP 状态码的拒绝理由 —— 给访客看的都是人话。"""

    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.code = code


def _free_mb():
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 9999


def _pdeathsig():
    """让子进程跟着服务一起死。

    踩过：服务重启后，已经开着的游戏进程变成孤儿继续跑（ppid=1），谁也认领不了，
    白占 45MB。PR_SET_PDEATHSIG 让内核在父进程消失时直接送走它。
    """
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)
    except Exception:
        pass


class Session:
    def __init__(self, sid, kind, ip, client):
        self.sid = sid
        self.kind = kind                 # play | auto
        self.ip = ip
        self.client = client
        self.started = time.time()
        self.touched = time.time()
        self.last_input = 0.0
        self.alive = True
        self.exit_code = None
        self.buf = u""                   # 字符缓冲（不是字节：切片不会切断多字节字符）
        self.base = 0                    # buf[0] 对应的绝对字符偏移
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.lock = threading.Lock()
        self.tmp = tempfile.mkdtemp(prefix="tank-")
        script = AUTOPLAY if kind == "auto" else GAME
        # 只给最小环境：别把服务进程的环境（里面有各种 key）漏给子进程
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmp, "TMPDIR": self.tmp,
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "SEED": str(random.randint(1, 10 ** 9))}
        self.proc = subprocess.Popen(
            [PYTHON, script], cwd=self.tmp, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            preexec_fn=_pdeathsig)
        threading.Thread(target=self._reader, daemon=True).start()

    # ── 输出 ────────────────────────────────────────────────────────
    def _reader(self):
        fd = self.proc.stdout
        try:
            while True:
                chunk = fd.read(4096)
                if not chunk:
                    break
                text = self._dec.decode(chunk)
                if text:
                    self._append(text)
        except Exception:
            pass
        try:
            self.exit_code = self.proc.wait()
        except Exception:
            self.exit_code = -1
        with self.lock:
            self.alive = False

    def _append(self, text):
        with self.lock:
            self.buf += text
            over = len(self.buf) - BUF_CHARS
            if over > 0:
                self.buf = self.buf[over:]
                self.base += over

    def read(self, off):
        """返回 (文本, 新偏移, 还活着, 是否被截断)。"""
        with self.lock:
            truncated = off < self.base
            start = max(off, self.base) - self.base
            return self.buf[start:], self.base + len(self.buf), self.alive, truncated

    # ── 输入 ────────────────────────────────────────────────────────
    def send(self, line):
        if self.kind == "auto":
            raise TankError("旁观模式不接受输入。", 400)
        if not self.alive:
            raise TankError("这一局已经结束了，重开一局吧。", 409)
        now = time.time()
        if now - self.last_input < MIN_GAP_S:
            raise TankError("手速太快了，等一下再输。", 429)
        line = "".join(c for c in (line or "") if c.isprintable())[:MAX_LINE]
        self.last_input = now
        self.touched = now
        try:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            raise TankError("这一局断了，重开一局吧。", 409)
        return True

    def kill(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                for _ in range(30):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.1)
                if self.proc.poll() is None:
                    self.proc.kill()
        except Exception:
            pass
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        try:
            shutil.rmtree(self.tmp, ignore_errors=True)
        except Exception:
            pass

    def info(self):
        with self.lock:
            alive = self.alive
        return {"sid": self.sid, "kind": self.kind, "alive": alive,
                "age": int(time.time() - self.started),
                "idle": int(time.time() - self.touched)}


# ── 会话表 ─────────────────────────────────────────────────────────
def _drop(sid):
    s = _sessions.pop(sid, None)
    if s:
        threading.Thread(target=s.kill, daemon=True).start()
    return s


def gc():
    now = time.time()
    for sid, s in list(_sessions.items()):
        with s.lock:
            alive = s.alive
        if (not alive) or (now - s.touched > IDLE_KILL_S) or (now - s.started > LIFE_S):
            _drop(sid)
    _sweep_orphans()


def _sweep_orphans():
    """清掉孤儿临时目录：会话该删的没删掉（进程还占着、或重启时被打断），
    超过 1 小时的一律收走 —— 别让 /tmp 慢慢堆起来。"""
    import glob
    live = {s.tmp for s in _sessions.values()}
    for p in glob.glob(os.path.join(tempfile.gettempdir(), "tank-*")):
        if p in live or not os.path.isdir(p):
            continue
        try:
            if time.time() - os.stat(p).st_mtime > 3600:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
    _reap_orphan_procs()


def _reap_orphan_procs():
    """送走没人认领的游戏进程（服务重启前开的那些）。

    判据：命令行走的是我们的游戏脚本，**而且父进程不是本服务** —— 自己刚拉起来
    的（哪怕还没登记进会话表）父进程就是本进程，不会被误杀。
    """
    import signal
    me = os.getpid()
    for ent in os.listdir("/proc"):
        if not ent.isdigit() or int(ent) == me:
            continue
        try:
            with open("/proc/%s/cmdline" % ent, "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace")
            if GAME not in cmd and AUTOPLAY not in cmd:
                continue
            with open("/proc/%s/stat" % ent, "r") as fh:
                ppid = int(fh.read().split(") ", 1)[1].split()[1])
            if ppid == me:
                continue
            os.kill(int(ent), signal.SIGKILL)
            LOG.warning("[tank] 送走孤儿游戏进程 pid=%s ppid=%s" % (ent, ppid))
        except Exception:
            continue


def _gc_loop():
    while True:
        time.sleep(GC_EVERY_S)
        try:
            gc()
        except Exception:
            pass


threading.Thread(target=_gc_loop, daemon=True).start()


def create(kind, ip, client=""):
    kind = "auto" if (kind or "").strip().lower() == "auto" else "play"
    ip = (ip or "unknown")[:64]
    client = (client or "anon")[:64]
    gc()
    with _lock:
        live = [s for s in _sessions.values() if s.alive]
        # 先看「你自己有没有在跑的局」：那条提示才可操作（可以点结束），
        # 比「名额满了」有用。服务器满不满是之后的事。
        same = [s for s in live if s.client == client]
        if same:
            s0 = same[0]
            if time.time() - s0.touched < IDLE_FREE_S:
                raise TankError("你已有一局在跑（同一个浏览器只能开一局）—— 先玩那一局，"
                                "或者点「结束这一局」再开新的。", 429)
            _drop(s0.sid)                      # 那一局闲置太久了，直接换掉
            live = [s for s in _sessions.values() if s.alive]
        if len(live) >= MAX_SESSIONS:
            raise TankError("同时最多 %d 局，现在满了，过一会儿再点。" % MAX_SESSIONS, 503)
        if len([s for s in live if s.ip == ip]) >= MAX_PER_IP:
            raise TankError("同一个网络出口已经开了 %d 局，等一会儿再点。" % MAX_PER_IP, 429)
        if _free_mb() < MIN_FREE_MB:
            raise TankError("服务器内存吃紧，暂时开不了新局。", 503)
        sid = "%08x%04x" % (random.getrandbits(32), random.getrandbits(16))
        s = Session(sid, kind, ip, client)
        _sessions[sid] = s
    time.sleep(0.6)                       # 等它把开场画面打出来
    text, off, alive, _ = s.read(0)
    return {"sid": sid, "kind": kind, "text": text, "off": off, "alive": alive}


def get(sid):
    s = _sessions.get((sid or "").strip())
    if not s:
        raise TankError("这个会话不在了（可能被回收了），重开一局吧。", 410)
    return s


def poll(sid, off):
    s = get(sid)
    s.touched = time.time()
    text, new_off, alive, truncated = s.read(int(off or 0))
    return {"sid": s.sid, "text": text, "off": new_off, "alive": alive,
            "truncated": truncated, "kind": s.kind}


def send(sid, line):
    s = get(sid)
    s.send(line)
    return {"ok": True}


def close(sid):
    _drop((sid or "").strip())
    return {"ok": True}


def mine(client):
    """按 client_id 找「这个浏览器还有没有在跑的局」。

    为什么需要：页面的局部 sid 经不起刷新 —— 刷新/另开标签后页面手里没有 sid，
    可服务器那局还活着，于是「开始新游戏」说"你已有一局在跑"、「结束这一局」
    却说"没有在跑的局"（真事故）。所以给页面一个按身份查询的口子。
    """
    client = (client or "anon")[:64]
    gc()
    with _lock:
        for s in _sessions.values():
            if s.client == client and s.alive:
                return {"sid": s.sid, "kind": s.kind, "alive": True,
                        "age": int(time.time() - s.started),
                        "idle": int(time.time() - s.touched)}
    return {"sid": "", "alive": False}


def close_client(client):
    """按身份关掉（页面不知道 sid 时用），顺带回答「本来有几局」。"""
    client = (client or "anon")[:64]
    n = 0
    for sid, s in list(_sessions.items()):
        if s.client == client:
            _drop(sid)
            n += 1
    return {"ok": True, "closed": n}


def stats():
    gc()
    return {"sessions": len(_sessions), "alive": sum(1 for s in _sessions.values() if s.alive),
            "max": MAX_SESSIONS, "free_mb": _free_mb()}
