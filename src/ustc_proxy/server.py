"""
USTC Young 前端覆写代理服务

在服务器端拦截并修改 young.ustc.edu.cn 的 API 响应，
等效于电脑端 Chrome DevTools Override 的效果。

功能：
1. 注入客户端补丁脚本（强制 showSignBtn/showSignOutBtn 返回 true）
2. 拦截 getButton API，扩展 result 为完整按钮列表 [1,7,2,3,4,5,6,8]
3. 拦截 createWxaCodeUnlimit API，提取二维码数据并在页面中展示
"""

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from aiohttp import web

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
                // 存储到全局变量，供页面内组件使用
                window.__ustc_qr_code = json.message;
                // 触发自定义事件
                window.dispatchEvent(new CustomEvent('ustc-qr-received', { detail: json.message }));
                // 同时弹出浮层
                showQRCodeOverlay(json.message);
            }
            return new Response(JSON.stringify(json), { status: 200, headers: resp.headers });
        }
        return resp;
    };

    // ===== ④ Vue 补丁：强制按钮可见 =====
    function patchVue() {
        const maxAttempts = 50;
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
            if (attempts >= maxAttempts) {
                clearInterval(interval);
                console.warn('[USTC-Proxy] ⚠️ Vue 补丁超时');
            }
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
# HTTP 反向代理核心逻辑
# ============================================================


async def proxy_request(request: web.Request) -> web.StreamResponse:
    """反向代理主入口：转发请求到 young.ustc.edu.cn 并选择性修改响应"""

    # 构建目标 URL
    path = request.path_qs
    target_url = TARGET_BASE + path

    logger.info(f"→ Proxy: {request.method} {path}")

    # 准备转发头
    forward_headers = {}
    for key, value in request.headers.items():
        # 跳过 hop-by-hop 头
        if key.lower() in ('host', 'connection', 'transfer-encoding', 'content-length'):
            continue
        forward_headers[key] = value
    forward_headers['Host'] = TARGET_HOST

    # 读取请求体
    body = None
    if request.body_exists:
        body = await request.read()

    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                data=body,
                allow_redirects=True,
            ) as upstream_resp:

                content_type = forward_headers.get('content-type', '') or ''

                # ---- 特殊处理：HTML 页面 → 注入补丁脚本 ----
                if is_html_content(content_type):
                    html_body = await upstream_resp.text()
                    modified_html = inject_script_into_html(html_body)
                    return web.Response(
                        text=modified_html,
                        status=upstream_resp.status,
                        content_type='text/html; charset=utf-8',
                    )

                # ---- 特殊处理：getButton API → 扩展按钮列表 ----
                if is_get_button_request(path):
                    api_body = await upstream_resp.text()
                    modified_body = patch_get_button_response(api_body)
                    return web.Response(
                        text=modified_body,
                        status=upstream_resp.status,
                        content_type=content_type or 'application/json; charset=utf-8',
                    )

                # ---- 特殊处理：createWxaCodeUnlimit → 提取二维码 ----
                if is_qr_code_request(path):
                    api_body = await upstream_resp.text()
                    log_qr_code_response(api_body)
                    return web.Response(
                        text=api_body,
                        status=upstream_resp.status,
                        content_type=content_type or 'application/json; charset=utf-8',
                    )

                # ---- 默认：原样转发 ----
                resp_body = await upstream_resp.read()

                response = web.Response(
                    body=resp_body,
                    status=upstream_resp.status,
                )

                # 复制响应头
                for key, value in upstream_resp.headers.items():
                    if key.lower() not in (
                        'transfer-encoding', 'content-encoding',
                        'content-length', 'connection',
                    ):
                        response.headers[key] = value

                return response

    except Exception as e:
        logger.error(f"✗ 代理请求失败: {e}")
        return web.Response(
            text=f"Proxy Error: {str(e)}",
            status=502,
            content_type="text/plain",
        )


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
    """在 HTML 中注入补丁脚本（在 </head> 或 <body> 前）"""
    
    script_tag = f'<script>{INJECTION_SCRIPT}</script>'
    
    # 优先插入到 </head> 之前
    if '</head>' in html:
        return html.replace('</head>', script_tag + '\n</head>', 1)
    
    # 其次插入到 <body> 之后
    if '<body' in html:
        # 找到第一个 > 后面插入
        import re
        match = re.search(r'(<body[^>]*>)', html)
        if match:
            return html[:match.end()] + script_tag + '\n' + html[match.end():]
    
    # 最后兜底：插到 </html> 或文档末尾
    if '</html>' in html:
        return html.replace('</html>', script_tag + '\n</html>', 1)
    
    return script_tag + '\n' + html


def patch_get_button_response(body: str) -> str:
    """修补 getButton API 响应，扩展 result 列表"""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get('success') and isinstance(data.get('result'), list):
            original = data['result'].copy()
            
            # 合并去重，保持顺序
            merged = []
            for btn_id in FULL_BUTTON_RESULT:
                if btn_id not in merged:
                    merged.append(btn_id)
            
            data['result'] = merged
            
            logger.info(
                f"📋 getButton 已修补: {json.dumps(original)} → {json.dumps(merged)}"
            )
            return json.dumps(data, ensure_ascii=False)
        
    except json.JSONDecodeError:
        logger.warning("⚠️ getButton 响应不是有效 JSON")
    
    return body


def log_qr_code_response(body: str) -> None:
    """记录二维码 API 响应"""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get('success'):
            qr_data = data.get('message', '')
            logger.info(f"📷 截获签到二维码! base64 长度: {len(qr_data)} 字符")
        else:
            logger.warning(f"⚠️ 二维码接口返回非成功状态: {data}")
    except json.JSONDecodeError:
        logger.warning("⚠️ 二维码响应不是有效 JSON")


# ============================================================
# 辅助页面
# ============================================================


async def index_page(request: web.Request) -> web.Response:
    """代理服务首页——使用说明和快捷链接"""

    html = f"""<!DOCTYPE html>
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
.code {{ background: #263238; color: #80cbc4; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
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
    return web.Response(text=html, content_type='text/html; charset=utf-8')


# ============================================================
# 应用创建
# ============================================================


def create_app(host: str = "0.0.0.0", port: int = 8899) -> web.Application:
    """创建并配置代理应用"""

    app = web.Application()
    app.router.add_get('/', index_page)
    app.router.add_route('*', '/{path:.*}', proxy_request)

    logger.info("=" * 50)
    logger.info("USTC Young 代理服务")
    logger.info(f"监听地址: {host}:{port}")
    logger.info(f"代理目标: {TARGET_HOST}")
    logger.info("=" * 50)

    return app


def run(host: str = "0.0.0.0", port: int = 8899):
    """启动代理服务（同步阻塞方式）"""
    app = create_app(host, port)
    web.run_app(app, host=host, port=port, print=None)


async def run_async(host: str = "0.0.0.0", port: int = 8899):
    """启动代理服务（异步方式，可与其他协程共存）"""
    app = create_app(host, port)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"✅ 代理服务已启动: http://{host}:{port}")

    # 返回 runner 以便后续关闭
    return runner


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="USTC Young 前端覆写代理")
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
