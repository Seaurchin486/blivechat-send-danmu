@echo off
REM ===========================================================================
REM blivechat「发弹幕」插件启动脚本
REM
REM 插件目录必须放在 blivechat 的 data\plugins\ 下面，例如：
REM     blivechat\data\plugins\send-danmu\
REM
REM blivechat 会用这个脚本启动插件，并通过环境变量传入 BLC_TOKEN 和 BLC_PORT。
REM 所以这个脚本本身不需要你手动运行，除非你要调试。
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not defined BLC_TOKEN (
    echo [警告] 没有检测到 BLC_TOKEN 环境变量。
    echo        这个插件需要由 blivechat 启动，请不要直接双击运行。
    echo        blivechat 会通过环境变量把 BLC_TOKEN 和 BLC_PORT 传给插件。
    echo.
)

REM ---- 选择运行方式：优先 uv，其次已存在的虚拟环境，最后系统 python ----

where uv >nul 2>nul
if %errorlevel%==0 (
    echo [send-danmu] 使用 uv 启动
    uv run --frozen --no-dev python main.py
    goto :done
)

if exist ".venv\Scripts\python.exe" (
    echo [send-danmu] 使用插件自带的虚拟环境启动
    ".venv\Scripts\python.exe" main.py
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    echo [send-danmu] 使用系统 python 启动
    echo [send-danmu] 如果提示缺少 aiohttp，请先执行： python -m pip install -r requirements.txt
    python main.py
    goto :done
)

echo [错误] 找不到 uv，也没有找到 python，也没有 .venv 虚拟环境。
echo        请任选一种方式：
echo          1. 安装 uv（推荐）：https://docs.astral.sh/uv/
echo          2. 安装 Python 3.10+ 后执行： python -m pip install -r requirements.txt
echo          3. 使用打包好的 send-danmu.exe 版本

:done
REM 插件进程退出后 blivechat 会认为插件已停止。留一个暂停方便用户看到错误，
REM 但如果是从 blivechat 启动的（有 BLC_TOKEN），就不要卡住。
if not defined BLC_TOKEN pause
endlocal
