# 只做一件事：把微信轮询桥拉起来。用 Start-Process（这条在本机实测能跑通）。
#
# 为什么不让 watchdog.py 管桥：它内部 start_bridge() 调的是
# `node_modules\.bin\wcab-bot`——那是 bash 包装脚本，Windows 上起不来。
# 这里用 `.cmd` 包装器，是能跑的那个。
#
# 本文件必须存 UTF-8 with BOM，否则 PowerShell 按 ANSI 读中文注释会读坏语法。

$bridgeDir = 'D:\wechat-character-bridge'
$account = '54a0145ab8d5-im-bot'
$liveLog = 'D:\ling-muxue-logs\bridge_live.log'

$env:CHARACTER_CHAT_URL = 'http://127.0.0.1:8000'
$env:CHARACTER_CHAT_TIMEOUT_MS = '600000'

# embedding 模型走项目内置目录（models\bge-small-zh-v1.5），不再依赖 HF 网络。
# 原因见 config\deepclaw.env.bat：这台机器上 huggingface.co 被 Steam++ 中间人接管，
# 而 huggingface_hub 用 requests/httpx（信任 certifi，不认加速器 CA），
# 会撞 5 轮 SSL 重试并静默退化成 mean pooling。给本地目录就完全不碰网络。
# 默认路径已写在 embedding.py 里，这里只在需要挪位置时才设。

$arg = '/c node_modules\.bin\wcab-bot.cmd --cwd "{0}" --account {1} >> "{2}" 2>&1' -f $bridgeDir, $account, $liveLog

$p = Start-Process -FilePath 'cmd.exe' -ArgumentList $arg -WorkingDirectory $bridgeDir -WindowStyle Hidden -PassThru
Write-Host ('bridge start requested, cmd pid=' + $p.Id)
