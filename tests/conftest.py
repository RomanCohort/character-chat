"""pytest 全局配置——让测试在**没有网络**的机器上也能跑完。

## 为什么需要这个文件（2026-09-12 加的）

`tests/test_memory.py` 里 `TestMemoryPipeline._make_pipeline()` 会构造**真的**
`MemoryPipeline`，于是 `long_term` 会调 `get_encoder().encode(...)`，
触发 `EmbeddingEncoder._ensure_model()` → `SentenceTransformer("BAAI/bge-small-zh-v1.5")`
→ 去 huggingface 下模型。

这台机器连不上 huggingface，而 sentence-transformers 会对**每个缺失文件**重试 5 轮
（每轮之间 1/2/4/8/8 秒，单个文件超时 10 秒）。一个模型有十几个文件要拉 →
**单个测试就能耗掉几分钟**，整个 `test_memory.py` 跑不完（实测 >260 秒仍未结束）。
没有 conftest、没有离线开关、没有 mock，也没有 pytest-timeout。

## 怎么修的（两件事，都不用改生产代码）

1. **环境变量**：在导入任何东西之前设 `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE`，
   让 huggingface_hub 直接拒绝联网（而不是傻等超时）。
2. **短路编码器**：`EmbeddingEncoder` 的 `_load_failed` / `_loading` / `_model`
   都是**类属性**。把它标成 `_load_failed = True`，`_ensure_model()` 就会立刻
   raise `RuntimeError` —— 这正是生产代码里"加载失败"的既有路径，
   `long_term` 会照常 fallback 到 TF-IDF。测试故意依赖真实模型时再单独开。

第 2 条是关键：光靠第 1 条，sentence-transformers 仍会尝试并抛异常，
而每次构造 pipeline 都要重来一遍，慢且吵。
"""

import os
import sys
from pathlib import Path

import pytest

# --- 1. 离线开关：必须在 sentence_transformers / huggingface_hub 被导入之前设好 ---
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# 让弃用告警别刷屏（tf/keras 在这里只是被间接 import）
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

# --- 让 tests/ 能 import character_chat（不用先 pip install -e .）---
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _encoder_cls():
    """拿到 EmbeddingEncoder 类；拿不到就返回 None（不因为 conftest 把测试搞崩）。"""
    try:
        from character_chat.memory.embedding import EmbeddingEncoder
        return EmbeddingEncoder
    except Exception:
        return None


@pytest.fixture(autouse=True)
def _no_network_embedding():
    """每个测试前后都把 embedding 编码器钉在"加载失败"状态。

    为什么每个测试都要重设：`_load_failed` 是类属性，
    而 `EmbeddingEncoder.__new__` 是单例——某个测试若把它清掉，
    后面的测试又会去联网。所以用 autouse + 前后都设。

    要跑真实模型的测试，用 `@pytest.mark.real_embedding` 标记，
    这个 fixture 会让开（见下面）。
    """
    cls = _encoder_cls()
    if cls is None:
        yield
        return

    saved = (cls._model, cls._loading, cls._load_failed)
    # 短路：让 _ensure_model 立刻 raise，走生产代码里既有的 TF-IDF fallback
    cls._model = None
    cls._loading = False
    cls._load_failed = True
    try:
        yield
    finally:
        cls._model, cls._loading, cls._load_failed = saved


@pytest.fixture
def tmp_db_path(tmp_path):
    """给需要 sqlite 文件的测试一个干净路径（比 tempfile.mktemp 安全）。"""
    return str(tmp_path / "test_memory.db")
