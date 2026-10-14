# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法（在插件目录下）：

    uv run pyinstaller -y send-danmu.spec

产物在 dist/send-danmu/ 下，把整个 dist/send-danmu/ 目录当成插件目录
（里面要有 plugin.json、data/config.example.ini）放进 blivechat 的
data/plugins/ 即可。plugin.json 里的 "run" 要改成 "send-danmu.exe"。

也可以直接跑 `uv run pyinstaller -y send-danmu.spec` 后执行本文件末尾的打包步骤。
"""
import os

block_cipher = None

# blcsdk 是随插件分发的，必须一起打进去
hiddenimports = [
    'blcsdk',
    'blcsdk.api',
    'blcsdk.client',
    'blcsdk.exc',
    'blcsdk.handlers',
    'blcsdk.models',
    'panel',
    'listener',
    'rooms',
    'rules',
    'sender',
    'logbuf',
    'bilibili_live',
    'bilibili_live.credentials',
    'bilibili_live.danmaku',
    'bilibili_live.errors',
]

datas = [
    ('data/config.example.ini', 'data'),
]

a = Analysis(
    ['main.py'],
    pathex=['.', 'vendor'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 测试相关的东西不要打进产物
        'pytest',
        'pytest_asyncio',
        'tests',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='send-danmu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='send-danmu',
)
