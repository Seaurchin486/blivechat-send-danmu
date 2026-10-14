#!/bin/sh
# ===========================================================================
# blivechat「发弹幕」插件启动脚本（Linux / macOS）
#
# 插件目录必须放在 blivechat 的 data/plugins/ 下面。
# blivechat 会通过环境变量传入 BLC_TOKEN 和 BLC_PORT。
# ===========================================================================
cd "$(dirname "$0")" || exit 1

if [ -z "$BLC_TOKEN" ]; then
    echo "[警告] 没有检测到 BLC_TOKEN 环境变量。" >&2
    echo "       这个插件需要由 blivechat 启动，请不要直接运行。" >&2
fi

if command -v uv >/dev/null 2>&1; then
    echo "[send-danmu] 使用 uv 启动"
    exec uv run --frozen --no-dev python main.py
fi

if [ -x ".venv/bin/python" ]; then
    echo "[send-danmu] 使用插件自带的虚拟环境启动"
    exec .venv/bin/python main.py
fi

if command -v python3 >/dev/null 2>&1; then
    echo "[send-danmu] 使用系统 python3 启动"
    echo "[send-danmu] 如果提示缺少 aiohttp，请先执行： python3 -m pip install -r requirements.txt"
    exec python3 main.py
fi

echo "[错误] 找不到 uv、.venv 或 python3。" >&2
echo "       请安装 uv（推荐）或 Python 3.10+。" >&2
exit 1
