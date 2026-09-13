REM ============================================================
REM  character_chat environment defaults
REM ============================================================
REM  Sourced by the desktop launcher and by start_bridge.ps1.
REM  Kept in its own file so a fix lives in one place instead of
REM  being duplicated across scripts.
REM
REM  ASCII only: a .bat with Chinese text renders as mojibake under
REM  the default GBK console codepage.
REM ============================================================

REM ------------------------------------------------------------
REM  Embedding model: served from the project's own copy.
REM
REM  src\character_chat\memory\embedding.py now prefers
REM    <project>\models\bge-small-zh-v1.5
REM  and only falls back to the HF model name when that directory is
REM  missing. Given a local directory, SentenceTransformer never touches
REM  the network, so none of the trouble below can happen.
REM
REM  Measured cold-start, same box:
REM    local dir, no HF_* vars at all          26.6s   <- current
REM    HF_HUB_OFFLINE=1 (cache)                30.2s
REM    HF_ENDPOINT=https://hf-mirror.com      163.7s   (mirror unreliable)
REM    model name, default env                142.0s + silent degradation
REM
REM  Why the model name was so bad here: the hosts file points
REM  huggingface.co at 127.0.0.1, where Steam++.Accelerator.exe listens on
REM  443 and MITMs it with its own root CA. That accelerator is fine -- it
REM  forwards to the real host, and urllib (SSL default context => Windows
REM  cert store) reads it in 5.3s. But huggingface_hub goes through
REM  requests/httpx, which trust certifi (a static PEM bundle) and therefore
REM  reject that CA:
REM      urllib    OK    5.3s
REM      requests  FAIL  SSLError
REM      httpx     FAIL  SSLError
REM  Five retries later it gave up and silently degraded:
REM    "No sentence-transformers model found ... Creating a new one with
REM     mean pooling."  -- semantic search quietly got worse.
REM
REM  So: do not "fix" this by trusting that CA or by disabling verification.
REM  Give the library a local directory instead.
REM ------------------------------------------------------------
REM  Override the location if you move the model:
REM set "DSH_EMBEDDING_MODEL_DIR=D:\somewhere\else\bge-small-zh-v1.5"
