# -*- coding: utf-8 -*-
"""插件自带的本地控制台。

blivechat 插件不允许修改前端，所以手动发弹幕、改规则、看日志都放在这个
本地网页里。只监听回环地址，默认 http://127.0.0.1:12452。

实现用的是标准库的 ``http.server``，避免额外依赖；请求处理跑在独立线程，
真正的发送通过 :meth:`app.App.send_threadsafe` 丢回事件循环。
"""
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import *

from bilibili_live import errors
from logbuf import LOG_BUFFER

logger = logging.getLogger('send-danmu.' + __name__)

MAX_BODY_BYTES = 64 * 1024
SEND_WAIT_SECONDS = 30.0


class PanelServer:
    def __init__(self, app):
        self._app = app
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._handler_cls = _make_handler(app)

    @property
    def url(self) -> str:
        return f'http://{self._app.config.panel_host}:{self._app.config.panel_port}/'

    def start(self):
        handler_cls = self._handler_cls
        self._httpd = ThreadingHTTPServer(
            (self._app.config.panel_host, self._app.config.panel_port), handler_cls
        )
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name='panel-server', daemon=True
        )
        self._thread.start()
        logger.info('控制台已启动：%s', self.url)

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001 关闭失败无所谓
                logger.debug('Failed to stop panel server cleanly', exc_info=True)
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info('控制台已停止')


class PanelError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: Optional[int] = None):
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


def _make_handler(app):
    """为每个 App 生成一个绑定到它的请求处理器（同时让基类可复用）"""

    class PanelHandler(BaseHTTPRequestHandler):
        server_version = 'blivechat-send-danmu-panel'

        # -------------------------------------------------------- 路由分发

        def do_GET(self):  # noqa: N802 标准库要求的命名
            parsed = urllib.parse.urlparse(self.path)
            try:
                if parsed.path in ('/', '/index.html'):
                    self._send_html(INDEX_HTML)
                elif parsed.path == '/api/status':
                    self._send_json(app.status())
                elif parsed.path == '/api/logs':
                    query = urllib.parse.parse_qs(parsed.query)
                    limit = _safe_int(query.get('limit', ['200'])[0], 200, 1, 500)
                    self._send_json(LOG_BUFFER.snapshot(limit=limit))
                elif parsed.path == '/api/config':
                    self._handle_get_config()
                elif parsed.path == '/favicon.ico':
                    self._send_bytes(b'', 'image/x-icon')
                else:
                    self._send_json({'error': 'not found'}, status=404)
            except PanelError as e:
                self._send_json({'error': e.message}, status=e.status)
            except Exception as e:  # noqa: BLE001 单个请求出错不能影响插件
                logger.exception('Panel GET %s failed:', self.path)
                self._send_json({'error': f'服务器内部错误：{e}'}, status=500)

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            try:
                body = self._read_json_body()
                if parsed.path == '/api/send':
                    self._handle_send(body)
                elif parsed.path == '/api/config':
                    self._handle_save_config(body)
                elif parsed.path == '/api/reload':
                    self._handle_reload()
                elif parsed.path == '/api/reload-credentials':
                    self._handle_reload_credentials()
                elif parsed.path == '/api/danmaku-config':
                    self._handle_danmaku_config(body)
                else:
                    self._send_json({'error': 'not found'}, status=404)
            except PanelError as e:
                self._send_json({'error': e.message}, status=e.status)
            except Exception as e:  # noqa: BLE001
                logger.exception('Panel POST %s failed:', self.path)
                self._send_json({'error': f'服务器内部错误：{e}'}, status=500)

        # ------------------------------------------------------ 具体接口

        def _handle_send(self, body: dict):
            content = str(body.get('content') or '').strip()
            if not content:
                raise PanelError('弹幕内容不能为空')

            room_id = body.get('roomId')
            if room_id in (None, '', 0):
                room_id = None
            else:
                try:
                    room_id = int(room_id)
                except (TypeError, ValueError):
                    raise PanelError('roomId 必须是数字') from None

            future = app.send_threadsafe(content, room_id)
            try:
                result = future.result(timeout=SEND_WAIT_SECONDS)
            except errors.DanmakuError as e:
                self._send_json({
                    'ok': False,
                    'error': str(e),
                    'errorCode': e.code,
                    'retryable': e.retryable,
                    'authError': e.auth_error,
                })
                return
            except TimeoutError:
                self._send_json({'ok': False, 'error': '发送超时，请稍后在日志里确认结果'})
                return
            except Exception as e:  # noqa: BLE001
                self._send_json({'ok': False, 'error': f'发送失败：{e}'})
                return

            self._send_json({'ok': True, 'result': result.to_dict()})

        def _handle_get_config(self):
            path = app.config_store.path
            try:
                with open(path, 'r', encoding='utf-8-sig') as f:
                    text = f.read()
            except OSError as e:
                raise PanelError(f'读取配置文件失败：{e}', status=500) from e
            self._send_json({'path': path, 'text': text})

        def _handle_save_config(self, body: dict):
            text = body.get('text')
            if not isinstance(text, str):
                raise PanelError('缺少 text 字段')

            # 先校验能不能解析，避免把坏配置写进去让插件下次起不来
            import configparser

            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(text)
            except configparser.Error as e:
                raise PanelError(f'配置格式错误，未保存：{e}') from None

            path = app.config_store.path
            try:
                # 先写临时文件再替换，避免写一半把配置弄坏
                tmp_path = path + '.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.write(text)
                import os

                os.replace(tmp_path, path)
            except OSError as e:
                raise PanelError(f'保存配置文件失败：{e}', status=500) from e

            future = app.reload_config_threadsafe()
            try:
                result = future.result(timeout=10)
            except Exception as e:  # noqa: BLE001
                self._send_json({'ok': True, 'warning': f'已保存，但重载失败：{e}'})
                return
            self._send_json({'ok': True, 'result': result})

        def _handle_reload(self):
            future = app.reload_config_threadsafe()
            try:
                result = future.result(timeout=10)
            except Exception as e:  # noqa: BLE001
                raise PanelError(f'重载失败：{e}', status=500) from e
            self._send_json({'ok': True, 'result': result})

        def _handle_reload_credentials(self):
            future = app.reload_credentials_threadsafe()
            try:
                creds = future.result(timeout=20)
            except errors.DanmakuError as e:
                raise PanelError(str(e), status=400, code=e.code) from e
            except Exception as e:  # noqa: BLE001
                raise PanelError(f'重新读取凭据失败：{e}', status=500) from e
            self._send_json({'ok': True, 'credentials': creds.to_dict() if creds else None})

        def _handle_danmaku_config(self, body: dict):
            room_id = body.get('roomId')
            if room_id in (None, '', 0):
                room_id = app.rooms.primary_room_id
            if not room_id:
                raise PanelError('需要先连接房间，或指定 roomId')

            import asyncio

            import sender as sender_mod

            loop = sender_mod.get_loop()
            if loop is None:
                raise PanelError('插件事件循环未运行', status=500)

            async def _fetch():
                assert app.client is not None
                return await app.client.get_danmaku_config(int(room_id))

            future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
            try:
                result = future.result(timeout=20)
            except Exception as e:  # noqa: BLE001
                raise PanelError(f'查询弹幕配置失败：{e}', status=500) from e
            self._send_json({'ok': True, 'config': result})

        # -------------------------------------------------------- 底层工具

        def _read_json_body(self) -> dict:
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                raise PanelError('Content-Length 不合法') from None

            if length > MAX_BODY_BYTES:
                raise PanelError('请求体过大', status=413)
            if length <= 0:
                return {}

            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError) as e:
                raise PanelError(f'请求体不是合法 JSON：{e}') from None
            if not isinstance(data, dict):
                raise PanelError('请求体必须是 JSON 对象')
            return data

        def _send_json(self, payload, *, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self._send_bytes(body, 'application/json; charset=utf-8', status=status)

        def _send_html(self, html: str):
            self._send_bytes(html.encode('utf-8'), 'text/html; charset=utf-8')

        def _send_bytes(self, body: bytes, content_type: str, *, status: int = 200):
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                # 本地工具，禁掉缓存和跨源请求
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Access-Control-Allow-Origin', 'null')
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 浏览器提前断开（比如刷新页面），不是错误
                pass

        def log_message(self, fmt, *args):  # noqa: A003 把访问日志降到 DEBUG
            logger.debug('panel %s - %s', self.address_string(), fmt % args)

    return PanelHandler


def _safe_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


INDEX_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>blivechat 发弹幕插件 · 控制台</title>
<style>
  :root {
    --bg: #14161a; --panel: #1c1f26; --panel2: #232733; --border: #2e3340;
    --fg: #e6e8ee; --dim: #9aa3b2; --accent: #00a1d6; --ok: #3ecf8e; --err: #ff6b6b; --warn: #ffb454;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 14px/1.6 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }
  header { padding: 16px 20px; background: var(--panel); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .badge { font-size: 12px; padding: 3px 8px; border-radius: 10px; background: var(--panel2); color: var(--dim); }
  .badge.ok { color: var(--ok); } .badge.err { color: var(--err); } .badge.warn { color: var(--warn); }
  main { display: grid; grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr); gap: 16px; padding: 16px; }
  @media (max-width: 860px) { main { grid-template-columns: 1fr; } }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  section > h2 { font-size: 14px; margin: 0; padding: 12px 14px; border-bottom: 1px solid var(--border);
                 background: var(--panel2); font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
  .body { padding: 14px; }
  label { display: block; font-size: 12px; color: var(--dim); margin: 10px 0 4px; }
  input[type=text], input[type=number], select, textarea {
    width: 100%; padding: 8px 10px; background: #0f1115; color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; font: inherit; }
  textarea { resize: vertical; font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; line-height: 1.55; }
  button { padding: 8px 14px; border: 0; border-radius: 6px; background: var(--accent); color: #fff;
           font: inherit; font-weight: 600; cursor: pointer; }
  button:hover { filter: brightness(1.1); } button:disabled { opacity: .5; cursor: not-allowed; }
  button.ghost { background: var(--panel2); color: var(--fg); border: 1px solid var(--border); font-weight: 400; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .row > * { flex: 0 0 auto; } .row > .grow { flex: 1 1 120px; }
  .msg { margin-top: 10px; padding: 8px 10px; border-radius: 6px; font-size: 13px; display: none; white-space: pre-wrap; }
  .msg.show { display: block; }
  .msg.ok { background: rgba(62,207,142,.12); color: var(--ok); border: 1px solid rgba(62,207,142,.3); }
  .msg.err { background: rgba(255,107,107,.12); color: var(--err); border: 1px solid rgba(255,107,107,.3); }
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 13px; }
  .kv dt { color: var(--dim); } .kv dd { margin: 0; word-break: break-all; }
  #logs { height: 260px; overflow: auto; font-family: ui-monospace, Consolas, monospace; font-size: 12px;
          background: #0f1115; border: 1px solid var(--border); border-radius: 6px; padding: 8px; }
  .log { white-space: pre-wrap; word-break: break-word; }
  .log .t { color: #6b7280; } .log.ERROR .m { color: var(--err); } .log.WARNING .m { color: var(--warn); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--dim); font-weight: 500; }
  .hint { font-size: 12px; color: var(--dim); margin-top: 6px; }
  code { background: #0f1115; padding: 1px 4px; border-radius: 4px; font-size: 12px; }
  details { margin-top: 10px; } summary { cursor: pointer; color: var(--dim); font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>blivechat 发弹幕插件</h1>
  <span class="badge" id="b-login">登录态：检查中…</span>
  <span class="badge" id="b-room">房间：-</span>
  <span class="badge" id="b-auto">自动回复：-</span>
  <span class="badge" id="b-dry">-</span>
  <span style="flex:1"></span>
  <button class="ghost" id="btn-reload">重载配置</button>
  <button class="ghost" id="btn-cred">重读Cookie</button>
</header>

<main>
  <section>
    <h2>手动发弹幕</h2>
    <div class="body">
      <label for="room">房间</label>
      <select id="room"></select>
      <label for="content">弹幕内容</label>
      <textarea id="content" rows="3" maxlength="30" placeholder="输入要发送的弹幕，回车发送（Shift+回车换行）"></textarea>
      <div class="hint" id="len-hint">0 / 20</div>
      <div class="row" style="margin-top:10px">
        <button id="btn-send">发送</button>
        <button class="ghost" id="btn-clear">清空</button>
      </div>
      <div class="msg" id="send-msg"></div>
    </div>
  </section>

  <section>
    <h2>运行状态</h2>
    <div class="body">
      <dl class="kv" id="status-kv"></dl>
      <details>
        <summary>当前生效的自动回复规则</summary>
        <div style="margin-top:8px" id="rules"></div>
      </details>
    </div>
  </section>

  <section style="grid-column: 1 / -1">
    <h2>自动回复规则（data/config.ini）<span class="badge" id="cfg-path"></span></h2>
    <div class="body">
      <textarea id="cfg" rows="18" spellcheck="false"></textarea>
      <div class="row" style="margin-top:10px">
        <button id="btn-save">保存并重载</button>
        <button class="ghost" id="btn-revert">放弃修改</button>
        <span class="hint">保存前会先校验 INI 格式；保存后立即生效，不用重启 blivechat。</span>
      </div>
      <div class="msg" id="cfg-msg"></div>
    </div>
  </section>

  <section style="grid-column: 1 / -1">
    <h2>日志 <button class="ghost" id="btn-clearlog" style="font-size:12px;padding:4px 10px">清空显示</button></h2>
    <div class="body"><div id="logs"></div></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);

function showMsg(el, text, ok) {
  el.textContent = text;
  el.className = 'msg show ' + (ok ? 'ok' : 'err');
}
function hideMsg(el) { el.className = 'msg'; }

async function api(path, options) {
  const res = await fetch(path, options);
  let data;
  try { data = await res.json(); } catch (e) { throw new Error('服务器返回了非 JSON 内容'); }
  if (!res.ok && data && data.error) throw new Error(data.error);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return data;
}

/* ---------------------------------------------------------------- 状态 */

let lastStatus = null;

async function refreshStatus() {
  try {
    const s = await api('/api/status');
    lastStatus = s;
    renderStatus(s);
  } catch (e) {
    $('b-login').textContent = '插件状态获取失败';
    $('b-login').className = 'badge err';
  }
}

function renderStatus(s) {
  const c = s.credentials;
  const login = $('b-login');
  if (c && c.complete) {
    login.textContent = '登录态：' + (c.userName ? c.userName + ' (' + c.uid + ')' : '已登录');
    login.className = 'badge ok';
  } else {
    login.textContent = '登录态：未就绪';
    login.className = 'badge err';
  }
  $('b-room').textContent = '房间：' + (s.primaryRoomId || s.configuredRoomId || '未连接');
  $('b-auto').textContent = '自动回复：' + (s.autoReplyEnabled ? '开' : '关');
  $('b-auto').className = 'badge ' + (s.autoReplyEnabled ? 'ok' : '');
  $('b-dry').textContent = s.dryRun ? '试运行模式（不会真发）' : '';
  $('b-dry').className = 'badge ' + (s.dryRun ? 'warn' : '');

  const kv = [
    ['插件ID', s.pluginId || '-'],
    ['blivechat 版本', s.blcVersion || '-'],
    ['SDK 版本', s.sdkVersion || '-'],
    ['凭据来源', c ? c.source : '（未加载）'],
    ['SESSDATA', c ? (c.sessdataPreview || '缺失') : '-'],
    ['csrf (bili_jct)', c ? (c.hasCsrf ? '已就绪' : '缺失') : '-'],
    ['发送间隔', s.minSendInterval + ' 秒'],
    ['最大长度', s.maxLength + ' 字符'],
    ['已发送 / 失败', (s.sendStats.sent || 0) + ' / ' + (s.sendStats.failed || 0)],
    ['待发队列', String(s.queuePending || 0)],
    ['配置文件', s.configPath + (s.configLoaded ? '' : '（读取失败！）')],
  ];
  $('status-kv').innerHTML = kv.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('');
  if (s.configError) {
    $('status-kv').innerHTML += `<dt style="color:var(--err)">配置错误</dt><dd style="color:var(--err)">${esc(s.configError)}</dd>`;
  }
  $('cfg-path').textContent = s.configPath;

  renderRooms(s);
  renderRules(s.rules || []);
}

function renderRooms(s) {
  const sel = $('room');
  const current = sel.value;
  const options = [];
  (s.rooms || []).forEach(r => {
    options.push({ value: String(r.roomId || ''), label: '房间 ' + r.displayName + (r.initSuccess ? '' : '（未初始化）') });
  });
  if (s.configuredRoomId && !options.some(o => o.value === String(s.configuredRoomId))) {
    options.unshift({ value: String(s.configuredRoomId), label: '配置指定：' + s.configuredRoomId });
  }
  options.unshift({ value: '', label: '自动（当前房间）' });

  const html = options.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
  if (sel.innerHTML !== html) {
    sel.innerHTML = html;
    if (current) sel.value = current;
  }
}

function renderRules(rules) {
  if (!rules.length) {
    $('rules').innerHTML = '<div class="hint">没有启用任何规则。在下面的配置里加 <code>[rule-xxx]</code> 段即可。</div>';
    return;
  }
  const rows = rules.map(r => `<tr>
    <td>${esc(r.id)}</td>
    <td>${esc(r.matchType)}${r.regexOk ? '' : ' <span style="color:var(--err)">正则错误</span>'}</td>
    <td>${esc((r.keywords || []).join('、'))}</td>
    <td>${esc((r.replies || []).join(' / '))}</td>
    <td>${esc(String(r.cooldown))}s</td>
  </tr>`).join('');
  $('rules').innerHTML = `<table><thead><tr><th>规则</th><th>匹配</th><th>关键词</th><th>回复</th><th>冷却</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function esc(v) {
  return String(v).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

/* ---------------------------------------------------------------- 发送 */

async function send() {
  const content = $('content').value.trim();
  if (!content) { showMsg($('send-msg'), '弹幕内容不能为空', false); return; }

  const btn = $('btn-send');
  btn.disabled = true;
  btn.textContent = '发送中…';
  hideMsg($('send-msg'));
  try {
    const roomId = $('room').value;
    const data = await api('/api/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, roomId: roomId ? Number(roomId) : null }),
    });
    if (data.ok) {
      const r = data.result || {};
      showMsg($('send-msg'), r.dryRun ? '试运行：没有真的发送。' : '发送成功。', true);
      $('content').value = '';
      updateLen();
    } else {
      showMsg($('send-msg'), data.error || '发送失败', false);
    }
  } catch (e) {
    showMsg($('send-msg'), e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = '发送';
    refreshStatus();
  }
}

function updateLen() {
  const max = (lastStatus && lastStatus.maxLength) || 20;
  const n = $('content').value.length;
  $('len-hint').textContent = n + ' / ' + max;
  $('len-hint').style.color = n > max ? 'var(--err)' : '';
}

/* ---------------------------------------------------------------- 配置 */

let configDirty = false;

async function loadConfig() {
  try {
    const data = await api('/api/config');
    $('cfg').value = data.text;
    configDirty = false;
  } catch (e) {
    showMsg($('cfg-msg'), '读取配置失败：' + e.message, false);
  }
}

async function saveConfig() {
  const btn = $('btn-save');
  btn.disabled = true;
  try {
    const data = await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('cfg').value }),
    });
    if (data.warning) showMsg($('cfg-msg'), data.warning, false);
    else showMsg($('cfg-msg'), '已保存并重载，规则数：' + ((data.result || {}).rules ?? '?'), true);
    configDirty = false;
    refreshStatus();
  } catch (e) {
    showMsg($('cfg-msg'), e.message, false);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 日志 */

let lastSeq = 0;

async function refreshLogs() {
  try {
    const data = await api('/api/logs?limit=300');
    if (!data.entries || !data.entries.length) return;
    const box = $('logs');
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    box.innerHTML = data.entries.map(e =>
      `<div class="log ${esc(e.level)}"><span class="t">${esc(e.time)}</span> <span class="m">${esc(e.message)}</span></div>`
    ).join('');
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) { /* 日志拉取失败不用打扰用户 */ }
}

/* ---------------------------------------------------------------- 绑定 */

$('btn-send').onclick = send;
$('btn-clear').onclick = () => { $('content').value = ''; updateLen(); hideMsg($('send-msg')); };
$('content').addEventListener('input', updateLen);
$('content').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

$('btn-save').onclick = saveConfig;
$('btn-revert').onclick = loadConfig;
$('cfg').addEventListener('input', () => { configDirty = true; });

$('btn-reload').onclick = async () => {
  try {
    const data = await api('/api/reload', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    showMsg($('cfg-msg'), '已重载，规则数：' + ((data.result || {}).rules ?? '?'), true);
    loadConfig(); refreshStatus();
  } catch (e) { showMsg($('cfg-msg'), e.message, false); }
};

$('btn-cred').onclick = async () => {
  try {
    await api('/api/reload-credentials', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    showMsg($('cfg-msg'), '已重新读取 cookie。', true);
    refreshStatus();
  } catch (e) { showMsg($('cfg-msg'), e.message, false); }
};

$('btn-clearlog').onclick = () => { $('logs').innerHTML = ''; };

window.addEventListener('beforeunload', (e) => {
  if (configDirty) { e.preventDefault(); e.returnValue = ''; }
});

$('room').addEventListener('change', refreshStatus);

refreshStatus().then(loadConfig).then(refreshLogs).then(updateLen);
setInterval(refreshStatus, 3000);
setInterval(refreshLogs, 2000);
</script>
</body>
</html>
'''
