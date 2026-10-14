# -*- coding: utf-8 -*-
"""冒烟测试：像 blivechat 那样，把插件当独立进程启动一遍。

这个测试最接近真实使用：

* 用 ``tests/fake_blivechat.py`` 起一个假 blivechat（WebSocket + HTTP）
* 用 ``python main.py`` 启动真正的插件进程，环境变量传 BLC_TOKEN / BLC_PORT
* 让假 blivechat 推一条弹幕过去，验证插件把回复播报回来
* 顺便验证控制台 HTTP 服务真的在监听

和 test_integration.py 的区别：那个是同进程调用，这个是真正的子进程 +
真实的 main.py 启动路径（日志初始化、信号处理、cookie 路径解析等）。
"""
import asyncio
import json
import os
import socket
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'vendor'))

from blcsdk.models import Command

from fake_blivechat import FakeBlivechat

pytestmark = [pytest.mark.smoke]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# 插件是按「自己所在的目录」来定位配置和 cookie 的（真实安装位置是
# <blivechat>/data/plugins/send-danmu/）。所以冒烟测试必须把插件摆成真实布局，
# 不能直接跑仓库里的 main.py，否则它会去读仓库自己的 data/config.ini。
PLUGIN_FILES = [
    'main.py', 'app.py', 'listener.py', 'panel.py', 'config.py',
    'rules.py', 'sender.py', 'rooms.py', 'logbuf.py', 'plugin.json',
]
PLUGIN_DIRS = ['bilibili_live', 'vendor']


def install_plugin(dest):
    """把插件源码按真实布局复制到 dest（相当于 data/plugins/send-danmu）"""
    import shutil

    dest.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        shutil.copy2(os.path.join(_ROOT, name), dest / name)
    for name in PLUGIN_DIRS:
        shutil.copytree(os.path.join(_ROOT, name), dest / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    data_dir = dest / 'data'
    data_dir.mkdir(exist_ok=True)
    shutil.copy2(os.path.join(_ROOT, 'data', 'config.example.ini'),
                 data_dir / 'config.example.ini')
    return dest


CONFIG = """[account]
cookie_file = {cookie_file}
room_id = 0

[danmaku]
min_send_interval = 0
max_length = 20
dry_run = true

[auto_reply]
enabled = true
cooldown = 0
template = {{content}}

[panel]
enabled = true
host = 127.0.0.1
port = {panel_port}
auto_open = false

[sdk]
broadcast = true
broadcast_author = [自动回复]
; 冒烟测试把看门狗超时调短，尽快验证 blivechat 断开后插件会自己退出
connection_timeout = 8

[rule-hi]
name = 打招呼
enabled = true
match_type = contains
keywords = 你好
reply = 你好呀
cooldown = 0
"""


async def test_plugin_runs_as_a_real_subprocess(tmp_path):
    fake = FakeBlivechat(token='subprocess-token')
    await fake.start()

    process = None
    try:
        # 插件目录就当成 <blivechat>/data/plugins/send-danmu
        plugin_dir = install_plugin(tmp_path / 'send-danmu')
        data_dir = plugin_dir / 'data'

        cookie_path = str(data_dir / 'cookie_jar.pickle')
        with open(cookie_path, 'w', encoding='utf-8') as f:
            json.dump({'bilibili.com|': {
                'SESSDATA': {'key': 'SESSDATA', 'value': 'x' * 30, 'path': '/', 'domain': 'bilibili.com'},
                'bili_jct': {'key': 'bili_jct', 'value': 'y' * 32, 'path': '/', 'domain': 'bilibili.com'},
            }}, f)

        panel_port = free_port()
        with open(data_dir / 'config.ini', 'w', encoding='utf-8') as f:
            f.write(CONFIG.format(cookie_file=cookie_path, panel_port=panel_port))

        env = dict(os.environ)
        env['BLC_TOKEN'] = fake.token
        env['BLC_PORT'] = str(fake.port)
        env['PYTHONIOENCODING'] = 'utf-8'
        # 不要设 PYTHONPATH 指向仓库：插件自带 vendor/blcsdk，不需要外部路径，
        # 设了反而会让子进程 import 到仓库里的模块
        env.pop('PYTHONPATH', None)

        # 直接用当前解释器跑 main.py（模拟 run.bat 的行为）
        process = subprocess.Popen(
            [sys.executable, 'main.py'],
            cwd=str(plugin_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        # 1) 插件应该连上来
        await asyncio.wait_for(fake.connected.wait(), timeout=30)

        # 2) 推一条弹幕，插件应该自动回复并播报回来
        await fake.add_room(5440)
        await asyncio.sleep(0.5)
        await fake.send_danmaku('你好呀主播', author='观众甲', uid='1001', message_id='m1')

        texts = await fake.wait_for(Command.ADD_TEXT_REQ, timeout=20)
        assert texts[0]['data']['content'] == '你好呀'

        # 3) 控制台应该真的在监听
        import aiohttp

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for _ in range(40):
                try:
                    async with session.get(f'http://127.0.0.1:{panel_port}/api/status') as res:
                        assert res.status == 200
                        status = await res.json()
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    await asyncio.sleep(0.25)
            else:
                raise AssertionError('控制台一直没有起来')

        assert status['pluginId'] == 'send-danmu-test'
        assert status['primaryRoomId'] == 5440

        # 4) 插件日志应该转发到了 blivechat
        await fake.wait_for_log('打招呼', timeout=20)

    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

            # 留一份输出，调试失败时很有用
            try:
                out = process.stdout.read().decode('utf-8', 'replace') if process.stdout else ''
            except Exception:  # noqa: BLE001
                out = ''
            if out:
                print('--- plugin stdout ---')
                print(out[-4000:])

        await fake.stop()


async def test_plugin_exits_cleanly_when_blivechat_disconnects(tmp_path):
    """blivechat 断开后插件应该自己退出，不要变成僵尸进程"""
    fake = FakeBlivechat(token='tok2')
    await fake.start()

    process = None
    try:
        plugin_dir = install_plugin(tmp_path / 'send-danmu')
        # 故意写一个不存在的 cookie 文件，验证没凭据时插件也能起来
        with open(plugin_dir / 'data' / 'config.ini', 'w', encoding='utf-8') as f:
            f.write(CONFIG.format(cookie_file='missing.pickle', panel_port=free_port()))

        env = dict(os.environ)
        env['BLC_TOKEN'] = fake.token
        env['BLC_PORT'] = str(fake.port)
        env.pop('PYTHONPATH', None)

        process = subprocess.Popen(
            [sys.executable, 'main.py'],
            cwd=str(plugin_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        await asyncio.wait_for(fake.connected.wait(), timeout=30)

        # 关掉 blivechat 端
        await fake.stop()

        # 插件应该自己退出
        for _ in range(60):
            if process.poll() is not None:
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError('blivechat 断开后插件没有退出')

        assert process.returncode == 0, f'退出码 {process.returncode}'
        fake = None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if fake is not None:
            await fake.stop()
