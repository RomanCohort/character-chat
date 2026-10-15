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
import os
import threading
from pathlib import Path

import numpy as np
from typing import List, Optional
from loguru import logger


# 默认模型：BGE-small-zh-v1.5，512维，中文优化
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

# 项目内置模型目录。优先用它，而不是 HF 的模型名。
#
# 为什么不用模型名（2026-09-13 实测）：
#   这台机器的 hosts 里 huggingface.co 指向 127.0.0.1，由 Steam++.Accelerator
#   在 443 上做中间人（它转发的目标是对的，加速本身没问题）。但 huggingface_hub
#   走 requests/httpx，它们信任 certifi 这个静态 PEM，不认加速器的根证书，于是：
#     urllib   OK    5.3s   （用 ssl 默认上下文 = Windows 证书库，认）
#     requests FAIL SSLError
#     httpx    FAIL SSLError
#   结果是每次加载都撞 5 轮 SSL 重试（冷启实测 142s），最后静默退化成
#   "Creating a new one with mean pooling" —— 语义检索质量下降而没人知道。
#
# 传本地目录给 SentenceTransformer 就完全不碰网络（实测 15s 就绪）。
# 可通过 DSH_EMBEDDING_MODEL 覆盖；目录不存在时回落到模型名。
_LOCAL_MODEL_DIR = os.environ.get(
    "DSH_EMBEDDING_MODEL_DIR",
    str(Path(__file__).resolve().parents[3] / "models" / "bge-small-zh-v1.5"),
)


class _OnnxBgeModel:
    """sentence-transformers 的最小替身 —— 只实现 encode() / get_sentence_embedding_dimension()。

    本地补丁（2026-10-09）：为什么要它
    --------------------------------
    这台服务器只有 896MB 内存。torch 一步就吃 ~230MB（当初把 torch 裁掉就是为了
    把 character_chat 从 438MB 压到 186MB），所以 sentence_transformers 装不上。
    但 `onnxruntime + tokenizers` 跑**同一个** BGE 模型只要 ~47MB（实测），
    于是 RAG 从"TF-IDF 字面匹配"回到真向量。

    为什么是替身而不是改 encode()：encode() 只用到 `.encode(...)` 和
    `.get_sentence_embedding_dimension()` 两个方法。做成同形状的对象，
    调用方一行都不用动。

    BGE 的正确用法：取 **CLS 位置**（last_hidden_state[:, 0]）+ L2 归一化。
    """

    def __init__(self, onnx_path: str, tok_path: str, dim: int = 512, max_len: int = 512):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2      # 小机器，别把 CPU 铺满
        so.inter_op_num_threads = 1
        # 本地补丁（2026-10-09）：必须关掉内存 arena。
        # 默认 ORT 会把每次推理的临时分配缓存在 arena 里不还，实测连续编码 60 多条
        # 就把 chat 进程从 266MB 顶到 416MB，服务器只剩 114MB 可用。
        # 关掉之后每次 run 结束就释放（代价是一点点重复分配，不值得省）。
        so.enable_cpu_mem_arena = False
        so.enable_mem_pattern = False
        self._sess = ort.InferenceSession(
            onnx_path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._tok = Tokenizer.from_file(tok_path)
        self._tok.enable_truncation(max_length=max_len)
        self._tok.enable_padding()       # 批内对齐长度，单条也不会出错
        self._dim = dim
        self._out_names = [o.name for o in self._sess.get_outputs()]

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    # 本地补丁（2026-10-09）：内部子批上限。
    # 为什么必须自己切：调用方（backfill_all）一次给 64 条、每条约 2000 字，
    # padding 到 512 token 后，64x512 的注意力中间张量直接把进程 OOM 掉
    # （实测 3 秒被 Killed）。把限制放在编码器里，**任何调用方都不可能撑爆它**，
    # 比要求每个调用方自己注意批大小可靠。
    _MAX_SUBBATCH = 8

    def _encode_one_batch(self, texts):
        encs = self._tok.encode_batch([str(t) for t in texts])
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        ttype = np.zeros_like(ids)
        out = self._sess.run(None, {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": ttype,
        })[0]
        return out[:, 0].astype(np.float32)          # BGE: CLS pooling

    def encode(self, texts, normalize_embeddings: bool = True,
               show_progress_bar: bool = False, **kwargs):
        if isinstance(texts, str):
            texts = [texts]
        texts = [str(t) for t in texts]
        chunks = []
        n_sub = self._MAX_SUBBATCH
        for i in range(0, len(texts), n_sub):
            chunks.append(self._encode_one_batch(texts[i:i + n_sub]))
        vecs = np.vstack(chunks) if len(chunks) > 1 else chunks[0]
        if normalize_embeddings:
            norm = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.maximum(norm, 1e-12)
        return vecs


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
                # ── 本地补丁（2026-10-09）：优先 ONNX 后端 ──────────────────
                # 这台机器装不了 torch（896MB 内存），所以先用不等 torch 的路。
                # 两个文件都在就绪 → 直接返回，不再往下走 sentence_transformers。
                _onnx = Path(_LOCAL_MODEL_DIR) / "onnx" / "model_quantized.onnx"
                _tokj = Path(_LOCAL_MODEL_DIR) / "tokenizer.json"
                if _onnx.is_file() and _tokj.is_file():
                    self._model = _OnnxBgeModel(str(_onnx), str(_tokj))
                    self._dim = self._model.get_sentence_embedding_dimension()
                    logger.info(
                        "[Embedding] ONNX 后端就绪 (model_quantized.onnx, dim=%d, %.1fs)"
                        % (self._dim, time.time() - t)
                    )
                    return

                from sentence_transformers import SentenceTransformer
                # 本地目录优先：完全离线，不走 HF 网络校验。
                if Path(_LOCAL_MODEL_DIR).is_dir():
                    load_from = _LOCAL_MODEL_DIR
                    logger.info(f"[Embedding] 用项目内置模型目录：{load_from}")
                else:
                    load_from = self._model_name
                    logger.warning(
                        f"[Embedding] 内置模型目录不存在（{_LOCAL_MODEL_DIR}），"
                        f"回落到模型名 {load_from!r}——这会走网络，"
                        f"在这台机器上可能撞 SSL 重试并退化成 mean pooling"
                    )
                self._model = SentenceTransformer(load_from)
                self._dim = self._model.get_sentence_embedding_dimension()
                logger.info(
                    f"[Embedding] loaded {self._model_name} "
                    f"(from={load_from}, dim={self._dim}, {round(time.time()-t,1)}s)"
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
