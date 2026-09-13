"""Embedding encoder for RAG — 把记忆文本编码成向量用于语义检索。

升级自 TF-IDF（char n-gram，语义弱）→ 真 embedding（BGE-small-zh，512维）。
- 本地跑，无 API 费用
- 中文优化（BGE 系列在中文 MTEB 上表现好）
- CPU 可用（~50ms/query）

延迟敏感场景（如单条 query 检索）用 lazy 加载——首次编码时才加载模型，
避免 import 时就拖慢服务启动。

加载策略（防 /chat 超时）：
- 首次冷加载 ~100s，在后台线程预热（Cli.__init__ 启动 daemon thread）
- 加载期间 /chat 调 encode_query → _ensure_model 看到 _loading=True 立刻 raise
  → long_term.search fallback TF-IDF（毫秒级），不阻塞 /chat
- 加载完成后进程内复用，后续 encode 秒级
- 加载失败标记 _load_failed，不再重试
"""
import threading
import numpy as np
from typing import List, Optional
from loguru import logger


# 默认模型：BGE-small-zh-v1.5，512维，中文优化
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class EmbeddingEncoder:
    """单例式 embedding 编码器（懒加载，进程内复用）。"""

    _instance: Optional["EmbeddingEncoder"] = None
    _model = None
    _model_name: str = DEFAULT_MODEL
    _dim: Optional[int] = None
    _load_failed: bool = False  # 加载失败后置 True，避免每次 encode 都重试（拖慢 /chat）
    _loading: bool = False       # 正在后台加载中——期间 encode 立刻 fallback，不等
    _lock = threading.Lock()

    def __new__(cls, model_name: str = DEFAULT_MODEL):
        # 单例：整个进程共用一个模型实例（加载一次），但允许换模型名
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._model_name = model_name
        return cls._instance

    def _ensure_model(self):
        """懒加载模型。加载期间/失败时立刻 raise，让 long_term.search 走 TF-IDF fallback。

        - 已加载：直接返回
        - 正在后台加载（_loading=True）：立刻 raise，不等（避免 /chat 阻塞 100s 超时）
        - 加载失败过（_load_failed=True）：立刻 raise，不重试
        - 未加载：加锁加载（首次 ~100s）
        """
        if self._model is not None:
            return
        if self._load_failed:
            raise RuntimeError("embedding model load previously failed, using TF-IDF fallback")
        if self._loading:
            # 后台正在加载——不让 /chat 等它，立刻 fallback
            raise RuntimeError("embedding model still loading in background, using TF-IDF fallback")

        with self._lock:
            # double-check（拿到锁后可能已被别的线程加载完）
            if self._model is not None:
                return
            if self._load_failed:
                raise RuntimeError("embedding model load previously failed")
            self._loading = True
            import time
            t = time.time()
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self._model_name)
                self._dim = self._model.get_sentence_embedding_dimension()
                logger.info(
                    f"[Embedding] loaded {self._model_name} "
                    f"(dim={self._dim}, {round(time.time()-t,1)}s)"
                )
            except Exception as e:
                logger.error(f"[Embedding] load {self._model_name} failed: {e}")
                self._load_failed = True
                raise
            finally:
                self._loading = False

    @property
    def available(self) -> bool:
        """embedding 模型是否可用（加载成功过）。给 long_term 判断要不要走 embedding 路径。"""
        return self._model is not None and not self._loading

    @property
    def dim(self) -> int:
        """向量维度。"""
        self._ensure_model()
        return self._dim

    def encode(self, texts, normalize: bool = True) -> np.ndarray:
        """编码文本列表 → (n, dim) float32 数组。

        Args:
            texts: str 或 List[str]
            normalize: L2 归一化（归一化后 cosine = 点积，检索更快）
        """
        self._ensure_model()
        single = isinstance(texts, str)
        if single:
            texts = [texts]
        emb = self._model.encode(texts, normalize_embeddings=normalize,
                                  show_progress_bar=False)
        arr = np.asarray(emb, dtype=np.float32)
        return arr[0] if single else arr

    def encode_to_bytes(self, text: str) -> bytes:
        """编码单条文本 → 序列化 bytes（存 sqlite BLOB）。"""
        arr = self.encode(text, normalize=True)
        return arr.tobytes()

    @staticmethod
    def bytes_to_vec(b: bytes) -> np.ndarray:
        """反序列化 BLOB → float32 向量。"""
        return np.frombuffer(b, dtype=np.float32)


# 模块级便捷函数（给 long_term.py 用，避免每次拿单例）
_encoder: Optional[EmbeddingEncoder] = None


def get_encoder() -> EmbeddingEncoder:
    global _encoder
    if _encoder is None:
        _encoder = EmbeddingEncoder()
    return _encoder


def encode_query(text: str) -> np.ndarray:
    """编码查询串 → 单条归一化向量。"""
    return get_encoder().encode(text, normalize=True)


def encode_text_to_bytes(text: str) -> bytes:
    """编码文本 → bytes 存库。"""
    return get_encoder().encode_to_bytes(text)


def bytes_to_vec(b: bytes) -> np.ndarray:
    """BLOB → 向量。"""
    return EmbeddingEncoder.bytes_to_vec(b)
