"""
USTC Young 前端覆写代理服务（纯标准库版本）

在服务器端拦截并修改 young.ustc.edu.cn 的 API 响应，
等效于电脑端 Chrome DevTools Override 的效果。

零外部依赖，仅使用 Python 标准库。

功能：
1. 注入客户端补丁脚本（强制 showSignBtn/showSignOutBtn 返回 true）
2. 拦截 getButton API，扩展 result 为完整按钮列表 [1,7,2,3,4,5,6,8]
3. 点击"签到二维码"按钮 → 同时请求签到码+签退码 → 弹窗展示两张二维码
"""

import json
import logging
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("ustc_proxy")

# ============================================================
# 配置
# ============================================================

TARGET_HOST = "young.ustc.edu.cn"
TARGET_BASE = f"https://{TARGET_HOST}"

# 要强制显示的完整按钮 ID 列表
FULL_BUTTON_RESULT = ["1", "7", "2", "3", "4", "5", "6", "8"]

# 客户端注入的补丁脚本
INJECTION_SCRIPT = r"""
(function(){
    'use strict';
    console.log('[USTC-Proxy] ✅ 服务端代理已激活');

    // ===== 全局状态 =====
    window.__ustc = {
        qrSignIn: null,
        qrSignOut: null,
        itemId: null,
        appid: null,
        overlayVisible: false,
        fetching: false,   // 防止重复点击
    };

    // ===== ① 拦截 getButton API 响应（双重保险）=====
    const _origFetch = window.fetch;
    window.fetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
        const resp = await _origFetch.apply(this, args);

        if (String(url).includes('getButton')) {
            const text = await resp.clone().text();
            try {
                const data = JSON.parse(text);
                if (data.success && Array.isArray(data.result)) {
                    const orig = [...data.result];
                    data.result = %s;
                    console.log('[USTC-Proxy] 📋 getButton 已修补:', JSON.stringify(orig), '→', JSON.stringify(data.result));
                    return new Response(JSON.stringify(data), { status: 200, headers: resp.headers });
                }
            } catch(e) {}
        }

        // 记录二维码响应（原样透传）
        if (String(url).includes('createWxaCodeUnlimit')) {
            const json = await resp.clone().json();
            console.log('[USTC-Proxy] 📷 二维码API被调用:', url.substring(0, 80) + '...');
            if (json.success && json.message) {
                console.log('[USTC-Proxy] ✅ 二维码数据长度:', json.message.length);
            }
            return new Response(JSON.stringify(json), { status: 200, headers: resp.headers });
        }
        return resp;
    };

    // 同时拦截 XHR
    const _xhrOpen = XMLHttpRequest.prototype.open;
    const _xhrSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this._ustcUrl = url;
        this._ustcMethod = method;
        return _xhrOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function() {
        const xhr = this;
        const url = String(xhr._ustcUrl || '');

        if (url.includes('getButton')) {
            xhr.addEventListener('load', function() {
                try {
                    let text = xhr.responseText;
                    const data = JSON.parse(text);
                    if (data.success && Array.isArray(data.result)) {
                        data.result = %s;
                        Object.defineProperty(xhr, 'responseText', { value: JSON.stringify(data), configurable: true, writable: true });
                        console.log('[USTC-Proxy] [XHR]📋 getButton 已修补');
                    }
                } catch(e) {}
            });
        }

        if (url.includes('createWxaCodeUnlimit')) {
            xhr.addEventListener('load', function() {
                try {
                    const data = JSON.parse(xhr.responseText);
                    if (data.success && data.message) {
                        console.log('[USTC-Proxy] [XHR]📷 二维码! 长度:', data.message.length);
                    }
                } catch(e) {}
            });
        }

        return _xhrSend.call(this);
    };

    // ===== ② 获取单个二维码 =====
    async function fetchQRCode(itemId, appid, label) {
        console.log(`[USTC-Proxy] 🔍 请求${label}二维码... itemId=${itemId}`);
        try {
            const resp = await fetch('/mobile/item/createWxaCodeUnlimit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    page: 'pagesA/projectdt/projectdt',
                    scene: String(itemId),
                    appId: appid || '',
                }),
            });
            const json = await resp.json();
            if (json.success && json.message) {
                console.log(`[USTC-Proxy] ✅ ${label}码成功! 长度:`, json.message.length);
                return json.message;
            } else {
                console.warn(`[USTC-Proxy] ⚠️ ${label}码接口失败:`, json.message || json);
                return null;
            }
        } catch(e) {
            console.error(`[USTC-Proxy] ❌ ${label}码异常:`, e);
            return null;
        }
    }

    // ===== ③ 显示双二维码面板 =====
    function showQRPanel(signInData, signOutData) {
        var old = document.getElementById('ustc-qr-panel');
        if (old) old.remove();

        var panel = document.createElement('div');
        panel.id = 'ustc-qr-panel';

        var signInImg = signInData ? ('data:image/png;base64,' + signInData) : '';
        var signOutImg = signOutData ? ('data:image/png;base64,' + signOutData) : '';

        panel.innerHTML =
            '<div id="ustc-qr-panel-inner" style="' +
                'position:fixed;top:0;left:0;width:100%;height:100%;' +
                'background:rgba(0,0,0,0.82);z-index:999998;' +
                'display:flex;align-items:center;justify-content:center;flex-direction:column;padding:20px;box-sizing:border-box;">' +

            '<div style="background:#fff;border-radius:20px;padding:24px;max-width:95vw;width:420px;text-align:center;">' +

            '<h2 style="margin:0 0 16px;color:#333;font-size:20px;">📋 签到/签退 二维码</h2>' +
            '<p style="margin:0 0 16px;color:#888;font-size:13px;">长按图片保存到相册</p>' +

            // 签到码（绿色）
            '<div style="margin-bottom:16px;">' +
                '<div style="color:#2e7d32;font-size:15px;font-weight:bold;margin-bottom:8px;display:flex;align-items:center;justify-content:center;gap:6px;">' +
                    '<span style="font-size:18px;">✅</span> 签到码' +
                '</div>' +
                (signInImg ?
                    '<img src="' + signInImg + '" style="max-width:260px;max-height:260px;border-radius:12px;border:3px solid #e8f5e9;" />' :
                    '<div style="padding:30px;background:#f5f5f5;border-radius:12px;color:#999;">加载中...</div>') +
            '</div>' +

            // 签退码（红色）
            '<div>' +
                '<div style="color:#d32f2f;font-size:15px;font-weight:bold;margin-bottom:8px;display:flex;align-items:center;justify-content:center;gap:6px;">' +
                    '<span style="font-size:18px;">⏪</span> 签退码' +
                '</div>' +
                (signOutImg ?
                    '<img src="' + signOutImg + '" style="max-width:260px;max-height:260px;border-radius:12px;border:3px solid #ffebee;" />' :
                    '<div style="padding:30px;background:#f5f5f5;border-radius:12px;color:#999;">加载中...</div>') +
            '</div>' +

            '<button id="ustc-qr-close-btn" style=' +
                '"margin-top:20px;padding:12px 36px;border:none;background:#555;color:#fff;' +
                'border-radius:24px;font-size:15px;cursor:pointer;"' +
            '>关闭</button>' +

            '</div></div>';

        document.body.appendChild(panel);

        // 关闭按钮
        document.getElementById('ustc-qr-close-btn').onclick = function() {
            panel.remove();
            window.__ustc.overlayVisible = false;
        };

        // 点背景关闭
        document.getElementById('ustc-qr-panel-inner').onclick = function(ev) {
            if (ev.target === ev.currentTarget) {
                panel.remove();
                window.__ustc.overlayVisible = false;
            }
        };

        window.__ustc.overlayVisible = true;
    }

    // ===== ④ 核心：点击按钮时获取双二维码 =====
    async function handleQRButtonClick() {
        if (window.__ustc.fetching) {
            console.log('[USTC-Proxy] ⏳ 正在获取中，请稍候...');
            return;
        }
        window.__ustc.fetching = true;

        var itemId = window.__ustc.itemId;
        var appid = window.__ustc.appid || '';

        if (!itemId) {
            alert('USTC Proxy: 未找到项目ID，请确认已在项目详情页。');
            window.__ustc.fetching = false;
            return;
        }

        console.log('[USTC-Proxy] 🎯 按钮被点击! 开始获取签到+签退二维码...');

        // 先弹一个加载面板
        showQRPanel(null, null);

        try {
            var results = await Promise.all([
                fetchQRCode(itemId, appid, '签到'),
                fetchQRCode(itemId, appid, '签退'),
            ]);

            window.__ustc.qrSignIn = results[0];
            window.__ustc.qrSignOut = results[1];

            if (results[0] || results[1]) {
                // 用实际数据重新渲染面板
                showQRPanel(results[0], results[1]);
            } else {
                // 关闭空面板，提示错误
                var panel = document.getElementById('ustc-qr-panel');
                if (panel) panel.remove();
                window.__ustc.overlayVisible = false;
                alert('USTC Proxy: 二维码获取失败！\n\n请检查：\n1. 是否已登录\n2. 网络是否正常\n3. 项目ID是否正确');
            }
        } catch(e) {
            console.error('[USTC-Proxy] ❌ 获取过程出错:', e);
            var panel = document.getElementById('ustc-qr-panel');
            if (panel) panel.remove();
            window.__ustc.overlayVisible = false;
            alert('USTC Proxy: 获取二维码时发生错误: ' + e.message);
        }

        window.__ustc.fetching = false;
    }

    // ===== ⑤ Vue 补丁：强制显示按钮 + 劫持二维码点击事件 =====
    function patchVueComponent() {
        var attempts = 0;
        var maxAttempts = 80;
        var interval = setInterval(function() {
            attempts++;
            var app = document.querySelector('#app') || document.querySelector('.uni-app') || document.body;

            function findVM(el, depth) {
                if (!el || depth > 15) return null;
                if (el.__vue__) return searchVM(el.__vue__);
                for (var c of el.children || []) { var r = findVM(c, depth+1); if (r) return r; }
                return null;
            }
            function searchVM(vm) {
                if (!vm) return null;
                if (vm.showSignBtn !== undefined) return vm;
                if (vm.$children) {
                    for (var i = 0; i < vm.$children.length; i++) {
                        var r = searchVM(vm.$children[i]);
                        if (r) return r;
                    }
                }
                return null;
            }

            var target = findVM(app, 0);

            if (target) {
                clearInterval(interval);
                console.log(`[USTC-Proxy] ✅ 找到目标 Vue 组件 (第${attempts}次)`);

                // --- 强制显示签到/签退按钮 ---
                try {
                    Object.defineProperty(target, 'showSignBtn', { get: function(){return true;}, configurable: true });
                    Object.defineProperty(target, 'showSignOutBtn', { get: function(){return true;}, configurable: true });
                    if (target.$options && target.$options.computed) {
                        target.$options.computed.showSignBtn = function() { return true; };
                        target.$options.computed.showSignOutBtn = function() { return true; };
                    }
                    if (target.$forceUpdate) target.$forceUpdate();
                    console.log('[USTC-Proxy] ✅✅ 签到/签退按钮已强制显示');
                } catch(e) {
                    console.warn('[USTC-Proxy] ⚠️ Vue补丁异常:', e);
                }

                // --- 提取项目信息 ---
                var itemId = target.itemId || (target.content && target.content.id) || '';
                var appid = target.appid || '';
                window.__ustc.itemId = itemId;
                window.__ustc.appid = appid;
                console.log('[USTC-Proxy] 🔑 项目ID:', itemId, ', appId:', appid);

                // --- 劫持 createCode 方法（原生的二维码生成函数）---
                // 方式A: 直接替换 Vue 组件上的 createCode 方法
                if (target.createCode) {
                    var originalCreateCode = target.createCode.bind(target);
                    target.createCode = function() {
                        console.log('[USTC-Proxy] 🎯 createCode 被调用! 劫持为双二维码模式...');
                        handleQRButtonClick();
                    };
                    console.log('[USTC-Proxy] 🔀 createCode 方法已被劫持');
                } else {
                    console.warn('[USTC-Proxy] ⚠️ 未找到 createCode 方法，使用 DOM 事件监听作为备用方案');
                }

                // 方式B: DOM 事件监听（备用方案，通过 MutationObserver 监听按钮出现后绑定点击）
                hookQRButtonDOM();

                console.log('[USTC-Proxy] ✓ 所有客户端补丁已就绪，等待用户点击签到二维码按钮...');
                return;
            }

            if (attempts >= maxAttempts) {
                clearInterval(interval);
                console.warn('[USTC-Proxy] ⚠️ Vue 组件查找超时（非项目详情页属于正常情况）');
            }
        }, 100);
    }

    // ===== ⑥ DOM 备用方案：监听按钮点击 =====
    function hookQRButtonDOM() {
        var observer = new MutationObserver(function(mutations) {
            // 查找所有可能的二维码相关按钮/元素
            var allElements = document.querySelectorAll('*');
            for (var i = 0; i < allElements.length; i++) {
                var el = allElements[i];
                var text = (el.textContent || '').trim();
                var tag = (el.tagName || '').toLowerCase();

                // 匹配包含"签到"、"二维码"、"Sign"、"QR"等关键词的可点击元素
                if ((text.indexOf('签到') !== -1 && text.indexOf('二维码') !== -1 ||
                     text.indexOf('二维码') !== -1 && text.indexOf('签退') === -1 ||
                     text === '签到二维码' || text === '显示二维码' ||
                     text.indexOf('Sign Code') !== -1 || text.indexOf('QR Code') !== -1) &&
                    !el.dataset.ustcHooked &&
                    (tag === 'button' || tag === 'view' || tag === 'div' || tag === 'span' || tag === 'a' || tag === 'p')) {

                    // 排除太大的容器元素（只绑定叶子节点或小容器）
                    if (text.length > 50) continue;

                    el.dataset.ustcHooked = 'true';
                    el.style.cursor = 'pointer';

                    el.addEventListener('click', function(ev) {
                        console.log('[USTC-Proxy] 🎯 DOM按钮被点击:', this.textContent.trim());
                        ev.stopPropagation();
                        ev.preventDefault();
                        handleQRButtonClick();
                    });

                    el.addEventListener('touchend', function(ev) {
                        console.log('[USTC-Proxy] 🎯 DOM按钮被触摸:', this.textContent.trim());
                        handleQRButtonClick();
                    });

                    console.log('[USTC-Proxy] 🔗 已绑定DOM按钮:', text.substring(0, 30));
                }
            }
        });

        observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });

        // 也对已有元素做一次扫描
        setTimeout(function() {
            observer.takeRecords();
        }, 2000);
    }

    // ===== 启动 =====
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', patchVueComponent);
    } else {
        patchVueComponent();

        console.log('[USTC-Proxy] ✓ 所有客户端补丁已就绪');
        console.log('[USTC-Proxy] 💡 使用方式：进入项目详情页 → 点击「签到二维码」按钮 → 自动展示签到码+签退码');
    }
})();
""" % (json.dumps(FULL_BUTTON_RESULT), json.dumps(FULL_BUTTON_RESULT))


# ============================================================
# 工具函数
# ============================================================


def is_html_content(content_type: str) -> bool:
    """判断是否为 HTML 内容"""
    ct = content_type.lower()
    return 'text/html' in ct or 'application/xhtml' in ct


def is_get_button_request(path: str) -> bool:
    """判断是否为 getButton API 请求"""
    return 'getbutton' in path.lower()


def is_qr_code_request(path: str) -> bool:
    """判断是否为二维码生成 API 请求"""
    return 'createwxacodeunlimit' in path.lower()


def inject_script_into_html(html: str) -> str:
    """在 HTML 中注入补丁脚本"""
    script_tag = f'<script>{INJECTION_SCRIPT}</script>'
    if '</head>' in html:
        return html.replace('</head>', script_tag + '\n</head>', 1)
    if '<body' in html:
        match = re.search(r'(<body[^>]*>)', html)
        if match:
            return html[:match.end()] + script_tag + '\n' + html[match.end():]
    if '</html>' in html:
        return html.replace('</html>', script_tag + '\n</html>', 1)
    return script_tag + '\n' + html


def patch_get_button_response(body: bytes) -> bytes:
    """修补 getButton API 响应，扩展 result 列表"""
    text = body.decode('utf-8')
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get('success') and isinstance(data.get('result'), list):
            original = data['result'].copy()
            merged = []
            for btn_id in FULL_BUTTON_RESULT:
                if btn_id not in merged:
                    merged.append(btn_id)
            data['result'] = merged
            logger.info(f"📋 getButton 已修补: {json.dumps(original)} → {json.dumps(merged)}")
            return json.dumps(data, ensure_ascii=False).encode('utf-8')
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("⚠️ getButton 响应解析失败")
    return body


def log_qr_code_response(body: bytes) -> bytes:
    """记录二维码 API 响应"""
    text = body.decode('utf-8')
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get('success'):
            qr_data = data.get('message', '')
            logger.info(f"📷 截获签到二维码! base64 长度: {len(qr_data)} 字符")
        else:
            logger.warning(f"⚠️ 二维码接口返回非成功状态")
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("⚠️ 二维码响应解析失败")
    return body


# ============================================================
# 请求处理器
# ============================================================


class ProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")

    def do_GET(self):
        self._proxy_request()

    def do_POST(self):
        self._proxy_request()

    def _proxy_request(self):
        target_url = TARGET_BASE + self.path

        forward_headers = {}
        for key, value in self.headers.items():
            if key.lower() in ('host', 'connection', 'transfer-encoding', 'content-length'):
                continue
            forward_headers[key] = value
        forward_headers['Host'] = TARGET_HOST

        body = None
        if self.command == 'POST':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length)

        try:
            req = Request(target_url, data=body, headers=forward_headers, method=self.command)

            with urlopen(req, timeout=30) as upstream_resp:
                content_type = upstream_resp.headers.get('Content-Type', '')
                status = upstream_resp.status

                # HTML 页面 → 注入脚本
                if is_html_content(content_type):
                    html_body = upstream_resp.read()
                    modified_html = inject_script_into_html(
                        html_body.decode('utf-8', errors='replace')
                    )
                    self._send_text(200, modified_html, 'text/html; charset=utf-8')
                    return

                # getButton API → 修补 result
                if is_get_button_request(self.path):
                    api_body = upstream_resp.read()
                    modified_body = patch_get_button_response(api_body)
                    ct = content_type or 'application/json; charset=utf-8'
                    self._send_bytes(status, modified_body, ct)
                    return

                # 二维码 API → 日志记录（原样透传）
                if is_qr_code_request(self.path):
                    api_body = upstream_resp.read()
                    logged_body = log_qr_code_response(api_body)
                    ct = content_type or 'application/json; charset=utf-8'
                    self._send_bytes(status, logged_body, ct)
                    return

                # 其他请求原样转发
                resp_body = upstream_resp.read()
                self.send_response(status)
                skip_headers = {'transfer-encoding', 'content-encoding', 'content-length', 'connection'}
                for key, value in upstream_resp.headers.items():
                    if key.lower() not in skip_headers:
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

        except HTTPError as e:
            logger.error(f"✗ HTTP 错误 {e.code}: {self.path}")
            self._send_text(e.code, f"Proxy Error: HTTP {e.code}", 'text/plain')
        except URLError as e:
            logger.error(f"✗ 连接错误: {self.path} - {e.reason}")
            self._send_text(502, f"Proxy Error: {str(e.reason)}", 'text/plain')
        except Exception as e:
            logger.error(f"✗ 代理异常: {self.path} - {e}")
            import traceback
            traceback.print_exc()
            self._send_text(500, f"Proxy Error: {str(e)}", 'text/plain')

    def _send_text(self, status, text, content_type):
        encoded = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


INDEX_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>USTC Young 代理服务</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkSystemFont, "Segoe UI", sans-serif;
       background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
       min-height: 100vh; display: flex; align-items: center; justify-content: center;
       padding: 20px; }}
.card {{ background: #fff; border-radius: 20px; padding: 32px; width: 100%; max-width: 480px;
        box-shadow: 0 20px 60px rgba(0,0,0,0.3); }}
h1 {{ color: #333; font-size: 22px; margin-bottom: 6px; }}
.subtitle {{ color: #888; font-size: 13px; margin-bottom: 24px; }}
.status {{ background: #e8f5e9; color: #2e7d32; padding: 12px 16px; border-radius: 10px;
          margin-bottom: 20px; font-size: 14px; }}
.section {{ margin-bottom: 18px; }}
.label {{ color: #666; font-size: 13px; margin-bottom: 6px; }}
.link {{ display: block; background: linear-gradient(135deg, #667eea, #764ba2); color: #fff;
         text-decoration: none; padding: 14px 20px; border-radius: 12px; font-size: 15px;
         text-align: center; transition: transform 0.2s; }}
.link:active {{ transform: scale(0.97); }}
.info {{ background: #f5f5f5; border-radius: 10px; padding: 14px; font-size: 13px; color: #555;
       line-height: 1.6; }}
</style>
</head>
<body>
<div class="card">
    <h1>🎓 USTC Young 代理</h1>
    <p class="subtitle">前端覆写 · 一键获取签到/签退二维码</p>

    <div class="status">● 运行中 · 目标: <code>{TARGET_HOST}</code></div>

    <div class="section">
        <div class="label">🔗 进入目标网站</div>
        <a href="/login/ustc-h5-product/" class="link">打开 USTC 青年网站 →</a>
    </div>

    <div class="section">
        <div class="label">📱 使用方法</div>
        <div class="info">
            1. 点击上方链接进入<br>
            2. 登录后进入<strong>项目详情页</strong><br>
            3. 点击活动卡片上的<strong>「签到二维码」</strong>按钮<br>
            4. 页面将同时弹出<strong>签到码 + 签退码</strong><br>
            5. 长按图片保存到相册
        </div>
    </div>

    <div class="section">
        <div class="label">⚙️ 功能列表</div>
        <div class="info">
            ✓ <b>签到按钮</b> 强制显示<br>
            ✓ <b>签退按钮</b> 强制显示<br>
            ✓ <b>所有功能按钮</b> 解锁<br>
            ✓ <b>点击签到二维码</b> → 同时获取签到+签退码<br>
            ✓ 零扩展，手机浏览器直接用
        </div>
    </div>
</div>
</body>
</html>"""


def patched_handle_one_request(self):
    if hasattr(self, '_path_handled'):
        del self._path_handled
    if self.path in ('/', '', '/index.html'):
        self._serve_index()
    else:
        ProxyHandler.handle_one_request_original(self)


def _serve_index(self):
    self.send_response(200)
    self.send_header('Content-Type', 'text/html; charset=utf-8')
    body = INDEX_HTML.encode('utf-8')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)
    self.close_connection = True


def run(host="0.0.0.0", port=8899):

    print()
    print("=" * 50)
    print("  🎓 USTC Young 代理服务 v4")
    print("=" * 50)
    print(f"  监听地址: {host}:{port}")
    print(f"  代理目标: {TARGET_HOST}")
    print("=" * 50)
    print()
    print("  📱 使用:")
    print(f"     手机浏览器打开 http://<服务器IP>:{port}")
    print("     → 登录 → 进项目详情页 → 点击「签到二维码」按钮")
    print()
    print("  按 Ctrl+C 停止")
    print()

    # 保存原始 handle_one_request
    ProxyHandler.handle_one_request_original = ProxyHandler.handle_one_request
    ProxyHandler.handle_one_request = patched_handle_one_request
    ProxyHandler._serve_index = _serve_index

    server = ThreadedHTTPServer((host, port), ProxyHandler)
    logger.info(f"✅ 代理服务已启动: http://{host}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n✅ 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="USTC Young 前端覆写代理 v4（纯标准库）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    run(host=args.host, port=args.port)
