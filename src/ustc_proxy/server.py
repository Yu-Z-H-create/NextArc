"""
USTC Young 前端覆写代理服务（纯标准库版本）

在服务器端拦截并修改 young.ustc.edu.cn 的 API 响应，
等效于电脑端 Chrome DevTools Override 的效果。

零外部依赖，仅使用 Python 标准库。

功能：
1. 注入客户端补丁脚本（强制 showSignBtn/showSignOutBtn 返回 true）
2. 拦截 getButton API，扩展 result 为完整按钮列表 [1,7,2,3,4,5,6,8]
3. 拦截 createWxaCodeUnlimit API，提取二维码数据并在页面中展示
"""

import json
import logging
import re
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("ustc_proxy")

# ============================================================
# 配置
# ============================================================

TARGET_HOST = "young.ustc.edu.cn"
TARGET_BASE = f"https://{TARGET_HOST}"

# 要强制显示的完整按钮 ID 列表
FULL_BUTTON_RESULT = ["1", "7", "2", "3", "4", "5", "6", "8"]

# 客户端注入的补丁脚本（对应电脑端对 JS 第7247~7251行的修改）
INJECTION_SCRIPT = """
(function(){
    'use strict';
    console.log('[USTC-Proxy] ✅ 服务端代理已激活');

    // ===== ① 拦截 getButton API 响应（双重保险）=====
    const _fetch = window.fetch;
    window.fetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
        const resp = await _fetch.apply(this, args);
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

        // ===== ③ 拦截二维码响应 =====
        if (String(url).includes('createWxaCodeUnlimit')) {
            const json = await resp.clone().json();
            if (json.success && json.message) {
                console.log('[USTC-Proxy] 📷 截获到二维码! base64长度:', json.message.length);
                window.__ustc_qr_code = json.message;
                window.dispatchEvent(new CustomEvent('ustc-qr-received', { detail: json.message }));
                showQRCodeOverlay(json.message);
            }
            return new Response(JSON.stringify(json), { status: 200, headers: resp.headers });
        }
        return resp;
    };

    // ===== ④ Vue 补丁：强制按钮可见 =====
    function patchVue() {
        let attempts = 0;
        const interval = setInterval(() => {
            attempts++;
            const app = document.querySelector('#app') || document.querySelector('.uni-app') || document.body;

            function findVM(el, depth) {
                if (!el || depth > 15) return null;
                if (el.__vue__) return searchVM(el.__vue__);
                for (const c of el.children || []) { const r = findVM(c, depth+1); if (r) return r; }
                return null;
            }
            function searchVM(vm) {
                if (!vm) return null;
                if (vm.showSignBtn !== undefined) return vm;
                if (vm.$children) for (const c of vm.$children) { const r = searchVM(c); if (r) return r; }
                return null;
            }

            const target = findVM(app, 0);
            if (target) {
                clearInterval(interval);
                try {
                    Object.defineProperty(target, 'showSignBtn', { get: () => true, configurable: true });
                    Object.defineProperty(target, 'showSignOutBtn', { get: () => true, configurable: true });
                    if (target.$options && target.$options.computed) {
                        target.$options.computed.showSignBtn = () => true;
                        target.$options.computed.showSignOutBtn = () => true;
                    }
                    if (target.$forceUpdate) target.$forceUpdate();
                    console.log('[USTC-Proxy] ✅✅ showSignBtn / showSignOutBtn 已强制为 true');
                } catch(e) { console.warn('[USTC-Proxy] ⚠️ Vue 补丁异常:', e); }
                return;
            }
            if (attempts >= 50) { clearInterval(interval); console.warn('[USTC-Proxy] ⚠️ Vue 补丁超时'); }
        }, 100);
    }

    // 二维码浮层
    function showQRCodeOverlay(base64Data) {
        var existing = document.getElementById('ustc-qr-overlay');
        if (existing) existing.remove();

        var overlay = document.createElement('div');
        overlay.id = 'ustc-qr-overlay';
        overlay.style.cssText =
            'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:999997;' +
            'display:flex;align-items:center;justify-content:center;flex-direction:column;padding:20px;box-sizing:border-box;';

        overlay.innerHTML =
            '<div style="background:#fff;border-radius:16px;padding:20px;max-width:90vw;text-align:center;">' +
            '<h3 style="margin:0 0 12px;color:#333;">📋 签到二维码</h3>' +
            '<img src="data:image/png;base64,' + base64Data + '" style="max-width:280px;max-height:280px;border-radius:8px;" />' +
            '<p style="margin:14px 0 4px;color:#666;font-size:13px;">长按图片可保存</p>' +
            '<button id="ustc-qr-close" style="padding:10px 28px;border:none;background:#f44;color:#fff;border-radius:20px;font-size:14px;margin-top:10px;">✕ 关闭</button>' +
            '</div>';

        document.body.appendChild(overlay);
        document.getElementById('ustc-qr-close').onclick = function() { overlay.remove(); };
        overlay.onclick = function(ev) { if (ev.target === overlay) overlay.remove(); };
    }

    // 启动 Vue 补丁
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', patchVue);
    } else {
        patchVue();
    }

    console.log('[USTC-Proxy] ✓ 所有客户端补丁已就绪');
})();
""" % json.dumps(FULL_BUTTON_RESULT)


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
    """反向代理请求处理器"""

    # 抑制默认的日志输出（我们用自己的 logger）
    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")

    def do_GET(self):
        self._proxy_request()

    def do_POST(self):
        self._proxy_request()

    def _proxy_request(self):
        """核心代理逻辑：转发请求并选择性修改响应"""
        
        # 构建目标 URL
        target_url = TARGET_BASE + self.path

        # 准备转发头
        forward_headers = {}
        for key, value in self.headers.items():
            if key.lower() in ('host', 'connection', 'transfer-encoding', 'content-length'):
                continue
            forward_headers[key] = value
        forward_headers['Host'] = TARGET_HOST

        # 读取请求体
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

                # ---- HTML 页面 → 注入补丁脚本 ----
                if is_html_content(content_type):
                    html_body = upstream_resp.read()
                    modified_html = inject_script_into_html(
                        html_body.decode('utf-8', errors='replace')
                    )
                    self._send_text(200, modified_html, 'text/html; charset=utf-8')
                    return

                # ---- getButton API → 扩展按钮列表 ----
                if is_get_button_request(self.path):
                    api_body = upstream_resp.read()
                    modified_body = patch_get_button_response(api_body)
                    ct = content_type or 'application/json; charset=utf-8'
                    self._send_bytes(status, modified_body, ct)
                    return

                # ---- createWxaCodeUnlimit → 记录二维码 ----
                if is_qr_code_request(self.path):
                    api_body = upstream_resp.read()
                    logged_body = log_qr_code_response(api_body)
                    ct = content_type or 'application/json; charset=utf-8'
                    self._send_bytes(status, logged_body, ct)
                    return

                # ---- 默认：原样转发 ----
                resp_body = upstream_resp.read()

                # 写入状态行
                self.send_response(status)

                # 复制响应头
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

    def _send_text(self, status: int, text: str, content_type: str):
        """发送文本响应"""
        encoded = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_bytes(self, status: int, data: bytes, content_type: str):
        """发送二进制响应"""
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP Server（支持并发请求）"""
    daemon_threads = True


# ============================================================
# 辅助页面
# ============================================================


INDEX_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>USTC Young 代理服务</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
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
    <p class="subtitle">前端覆写服务 · 等效于 DevTools Override</p>

    <div class="status">● 服务运行中 · 代理目标: <code>{TARGET_HOST}</code></div>

    <div class="section">
        <div class="label">🔗 访问目标网站（通过代理）</div>
        <a href="/login/ustc-h5-product/" class="link">打开 USTC 青年网站 →</a>
    </div>

    <div class="section">
        <div class="label">📱 手机使用方法</div>
        <div class="info">
            1. 确保手机与服务器在同一网络<br>
            2. 在浏览器访问本页面<br>
            3. 点击上方链接进入目标网站<br>
            4. 登录后进入项目详情页<br>
            5. 签到/签退按钮将自动显示<br>
            6. 点击"签到二维码"可直接查看
        </div>
    </div>

    <div class="section">
        <div class="label">⚙️ 当前生效的修改</div>
        <div class="info">
            ✓ <b>签到按钮</b> 强制显示<br>
            ✓ <b>签退按钮</b> 强制显示<br>
            ✓ <b>功能按钮</b> 全部解锁<br>
            ✓ <b>签到二维码</b> 自动弹出展示
        </div>
    </div>
</div>
</body>
</html>"""


class IndexHandler(BaseHTTPRequestHandler):
    """首页处理器"""

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        body = INDEX_HTML.encode('utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # 静默首页日志


# ============================================================
# 启动入口
# ============================================================


def run(host: str = "0.0.0.0", port: int = 8899):
    """启动代理服务"""

    print()
    print("=" * 50)
    print("  🎓 USTC Young 代理服务")
    print("=" * 50)
    print(f"  监听地址: {host}:{port}")
    print(f"  代理目标: {TARGET_HOST}")
    print("=" * 50)
    print()
    print("  📱 手机使用:")
    print(f"     1. 浏览器打开 http://<服务器IP>:{port}")
    print("     2. 点击页面上的链接进入目标网站")
    print("     3. 登录后签到按钮将自动显示")
    print()
    print("  按 Ctrl+C 停止服务")
    print()

    # 创建两个 server：一个处理首页，一个做代理
    # 用 ThreadedHTTPServer 同时处理两者
    
    from functools import partial

    class DualHandler(BaseHTTPRequestHandler):
        """统一处理器：首页走 IndexHandler，其他走 ProxyHandler"""
        
        def __init__(self, *args, **kwargs):
            BaseHTTPRequestHandler.__init__(self, *args, **kwargs)

        def handle_one_request(self):
            # 首页特殊处理
            if self.path == '/' or self.path == '':
                # 直接用 index handler 的逻辑
                self.do_index()
            else:
                # 转发给 ProxyHandler
                ProxyHandler.__init__(self, self.request, self.client_address, self.server)
                ProxyHandler.handle_one_request(self)

        def do_index(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            body = INDEX_HTML.encode('utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            logger.info(f"{self.client_address[0]} - {format % args}")

    server = ThreadedHTTPServer((host, port), ProxyHandler)

    # 特殊处理：重写根路径为首页
    original_handle_one_request = ProxyHandler.handle_one_request

    def patched_handle_one_request(self):
        if hasattr(self, '_path_handled'):
            del self._path_handled
        if self.path in ('/', '', '/index.html'):
            self._serve_index()
        else:
            original_handle_one_request(self)

    def _serve_index(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        body = INDEX_HTML.encode('utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    ProxyHandler.handle_one_request = patched_handle_one_request
    ProxyHandler._serve_index = _serve_index

    logger.info(f"✅ 代理服务已启动: http://{host}:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n✅ 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="USTC Young 前端覆写代理（纯标准库，零依赖）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8899, help="监听端口 (默认 8899)")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    run(host=args.host, port=args.port)
