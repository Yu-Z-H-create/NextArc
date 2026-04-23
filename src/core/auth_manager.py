"""登录态管理器"""

import asyncio
from typing import Optional

import httpx

from pyustc import CASClient, YouthService

from src.utils.logger import get_logger

logger = get_logger("auth")


class AuthManager:
    """管理 CAS 和 YouthService 登录态
    
    注意：YouthService 使用了 ContextVar，必须在同一个异步上下文中使用。
    """

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._last_login_time: Optional[float] = None

    def create_session_once(self, timeout: float = 30.0, retries: int = 2):
        """创建一次性会话上下文管理器
        
        Args:
            timeout: 单次请求超时秒数（默认30秒）
            retries: 登录失败重试次数（默认2次）
        """
        return AuthSessionContext(self.username, self.password, timeout=timeout, retries=retries)

    def is_logged_in(self) -> bool:
        return self._last_login_time is not None


class AuthSessionContext:
    """认证会话上下文管理器"""

    def __init__(self, username: str, password: str, timeout: float = 30.0, retries: int = 2):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.retries = retries
        self._cas_client = None       # CASClient 实例（持有已登录的 httpx client）
        self._service = None          # YouthService 实例
        self._cas_obj = None          # CASClient.__aenter__ 返回值
        self._service_obj = None      # YouthService.__aenter__ 返回值

    async def _do_login(self):
        """执行登录，支持重试"""
        for attempt in range(self.retries + 1):
            try:
                logger.debug(f"正在创建认证会话... (尝试 {attempt + 1}/{self.retries + 1}, 超时 {self.timeout}s)")

                self._cas_client = CASClient.login_by_pwd(self.username, self.password)
                self._cas_obj = await asyncio.wait_for(
                    self._cas_client.__aenter__(),
                    timeout=self.timeout + 10.0,
                )

                self._service = YouthService()
                self._service_obj = await asyncio.wait_for(
                    self._service.__aenter__(),
                    timeout=self.timeout,
                )
                await asyncio.wait_for(
                    self._service_obj.login(self._cas_obj),
                    timeout=self.timeout,
                )

                logger.debug("认证会话创建成功")
                return True

            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException,
                    asyncio.TimeoutError) as e:
                logger.warning(f"认证会话创建超时 (尝试 {attempt + 1}/{self.retries + 1}): {type(e).__name__}")
                if attempt < self.retries:
                    await self._cleanup_partial()
                    import asyncio as aio
                    await aio.sleep(1.0 * (attempt + 1))
                    continue
                raise ConnectionError(
                    f"CAS 登录超时（已重试 {self.retries} 次，每次超时 {self.timeout}s）。"
                    f"请检查 VM 网络是否能访问 passport.ustc.edu.cn"
                ) from e
            except Exception as e:
                logger.error(f"认证会话创建失败: {e}")
                raise

    async def _cleanup_partial(self):
        """清理部分初始化的对象"""
        if self._service:
            try:
                await self._service.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._service = None
                self._service_obj = None
        if self._cas_client:
            try:
                await self._cas_client.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._cas_client = None
                self._cas_obj = None

    async def __aenter__(self):
        await self._do_login()
        return self._service_obj

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        logger.debug("正在关闭认证会话...")
        await self._cleanup_partial()
        logger.debug("认证会话已关闭")

    async def raw_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """使用已登录的 YouthService 会话发起原始 HTTP 请求
        
        用于调用 pyustc 未封装的 API（如 createWxaCodeUnlimit）。
        
        策略（v9）：
        - 直接使用 YouthService._client（已携带 x-access-token）
        - 预热获取 JSESSIONID 并注入 cookie jar
        - 不做重试（由调用方 _handle_get_qr 控制重试逻辑）
        
        关键发现：
        - x-access-token (JWT) 是主要认证凭据，存储在 YouthService._client.headers 中
        - 服务端 /mobile/item/* 路径经过页面路由框架
        - 浏览器也需要多次尝试才能成功 → 服务端本身不稳定
        """
        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW] {method} {full_url}")
        
        if not self._service or not hasattr(self._service, '_client') or not self._service._client:
            raise RuntimeError("YouthService 未登录或 _client 不可用")
        
        client = self._service._client
        
        # ============================================================
        # Step 0: 模拟浏览器完整访问路径
        #   0a. 访问 mobile/index 获取 JSESSIONID
        #   0b. 如果目标与活动相关，访问活动详情页建立页面 session
        # ============================================================
        jsession_id = ''
        try:
            logger.debug("[RAW] 预热0a: GET /mobile/index")
            warm_resp = await client.get(
                base_url + "/mobile/index",
                headers={"Accept": "text/html,application/xhtml+xml"},
                follow_redirects=True,
            )
            for cookie in warm_resp.cookies.jar:
                if cookie.name == 'JSESSIONID':
                    jsession_id = cookie.value
                    break
            
            if jsession_id:
                logger.debug(f"[RAW] 预热获得 JSESSIONID: {jsession_id[:8]}...")
            else:
                logger.debug(f"[RAW] 预热未返回 JSESSIONID, status={warm_resp.status_code}")
            
            # 0b: 如果请求的是 createWxaCodeUnlimit，从 scene 参数提取 activity_id，
            #     先访问活动详情页（模拟浏览器行为）
            payload = kwargs.get('json', {})
            scene = payload.get('scene', '') if isinstance(payload, dict) else ''
            
            if 'createWxaCodeUnlimit' in full_url and scene:
                detail_url = f"{base_url}/mobile/item/projectdt?id={scene}"
                logger.debug(f"[RAW] 预热0b: GET 活动详情页 {detail_url}")
                try:
                    detail_resp = await client.get(
                        detail_url,
                        headers={
                            "Accept": "text/html,application/xhtml+xml",
                            "Referer": f"{base_url}/mobile/index",
                        },
                        follow_redirects=True,
                    )
                    logger.debug(f"[RAW] 详情页响应: status={detail_resp.status_code}, "
                                f"ct={detail_resp.headers.get('content-type','')[:40]}")
                    
                    # 收集新 cookies
                    for cookie in detail_resp.cookies.jar:
                        if cookie.name == 'JSESSIONID':
                            jsession_id = cookie.value
                except Exception as e_detail:
                    logger.warning(f"[RAW] 详情页预热失败(非致命): {e_detail}")

        except Exception as e:
            logger.warning(f"[RAW] 预热失败(非致命): {e}")

        # ============================================================
        # 合并请求头（保留 x-access-token 等认证信息）
        # ============================================================
        merged_headers = dict(client.headers)
        custom_headers = kwargs.pop('headers', {})
        merged_headers.update(custom_headers)

        # 记录关键请求信息（脱敏 token）
        log_h = {}
        for k, v in merged_headers.items():
            v_str = str(v)
            if 'token' in k.lower() or 'authorization' in k.lower():
                log_h[k] = f"{v_str[:20]}...({len(v_str)}ch)"
            else:
                log_h[k] = v_str[:80]
        logger.debug(f"[RAW] headers: {log_h}")
        
        if 'json' in kwargs:
            logger.debug(f"[RAW] body: {kwargs['json']}")

        # ============================================================
        # 发送实际请求
        # ============================================================
        response = await client.request(
            method.upper(),
            full_url,
            headers=merged_headers,
            **kwargs,
        )

        # 记录响应摘要
        logger.info(
            f"[RAW] ← {response.status_code} "
            f"ct={response.headers.get('content-type', '?')[:40]} "
            f"url={str(response.url)[:80]}"
        )
        
        # 诊断：记录发送时的完整 cookies
        try:
            req_cookies = {c.name: c.value[:20] for c in client.cookies.jar}
            if req_cookies:
                logger.debug(f"[RAW] 发送的cookies: {req_cookies}")
        except Exception:
            pass
        
        ct = response.headers.get('content-type', '')
        if 'text/html' in ct.lower():
            # 错误页：记录更多内容帮助诊断
            text = response.text
            logger.debug(f"[RAW] HTML body ({len(text)} chars): {text[:500].replace(chr(10), ' ')}")
            
            # 尝试提取页面标题/错误信息
            import re
            title_match = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                logger.debug(f"[RAW] 页面title: {title}")
        elif 'application/json' in ct.lower():
            text = response.text[:200]
            logger.info(f"[RAW] JSON: {text}")
        else:
            logger.debug(f"[RAW] body ({len(response.content)} bytes): {response.text[:100]}")

        return response
