"""登录态管理器"""

import asyncio
from typing import Optional
from urllib.parse import parse_qs, urlparse

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
        self._web_client = None       # 用于前端页面请求的独立 httpx client（带 JSESSIONID）

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

                # 建立前端页面的 SSO session（用于调用 /mobile/* 等前端 API）
                await self._init_web_session()

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

    async def _init_web_session(self):
        """建立前端页面的 SSO session
        
        浏览器访问 young.ustc.edu.cn 的流程：
        1. 访问 young.ustc.edu.cn 的页面 → 被重定向到 CAS
        2. CAS 验证通过（已有 SOURCEID_TGC cookie）→ 重定向回 young.ustc.edu.cn 并带 ticket
        3. young.ustc.edu.cn 用 ticket 建立 JSESSIONID
        
        我们需要模拟这个流程来获得有效的 JSESSIONID，
        用于调用 /mobile/item/createWxaCodeUnlimit 等前端 API。
        """
        base_url = "https://young.ustc.edu.cn"
        
        # 从 CASClient 获取 CAS 票据
        cas_service_url = f"{base_url}/login/sc-wisdom-group-learning/"
        logger.debug(f"[WEB-SESSION] 获取 CAS ticket for {cas_service_url}")
        
        ticket = await self._cas_obj.get_ticket(cas_service_url)
        
        # 用 ticket 访问 young.ustc.edu.cn 建立 SSO session
        logger.debug(f"[WEB-SESSION] 用 ticket 建立 JSESSIONID...")
        
        self._web_client = httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=True,
            timeout=self.timeout,
        )
        await self._web_client.__aenter__()
        
        # 访问 SSO 回调地址，让服务端建立 JSESSIONID
        sso_callback = f"{base_url}/cas/client/checkSsoLogin?ticket={ticket}&service={cas_service_url}"
        resp = await self._web_client.get(sso_callback)
        
        # 记录获得的 cookies
        cookies = {c.name: c.value[:20] for c in self._web_client.cookies.jar}
        logger.info(f"[WEB-SESSION] SSO 回调完成, status={resp.status_code}, cookies={cookies}")
        
        # 再访问一下首页确保 session 稳定
        try:
            index_resp = await self._web_client.get("/mobile/index")
            logger.debug(f"[WEB-SESSION] 首页访问: status={index_resp.status_code}")
        except Exception as e:
            logger.warning(f"[WEB-SESSION] 首页访问失败(非致命): {e}")

    async def warmup_activity_page(self, activity_id: str):
        """预访问活动详情页，为前端 API 调用建立 session context
        
        前端的 createWxaCodeUnlimit 需要在一个已访问过活动详情页的 session 中才能工作，
        否则会被重定向到 main.psp（"访问地址无效"提示页）。
        
        浏览器中，用户是先打开活动详情页 /mobile/item/projectdt?id=xxx，
        然后点击按钮触发 createWxaCodeUnlimit AJAX 请求。
        我们需要模拟这个过程。
        """
        if not self._web_client:
            logger.warning("[WARMUP] web_client 未初始化，跳过")
            return
        
        url = f"/mobile/item/projectdt?id={activity_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            resp = await self._web_client.get(url, headers=headers)
            ct = resp.headers.get('content-type', '')
            is_html = 'text/html' in ct.lower()
            final_url = str(resp.url)
            logger.info(
                f"[WARMUP] 活动页 {activity_id}: "
                f"status={resp.status_code}, ct={'html' if is_html else ct[:30]}, "
                f"url={final_url[:80]}"
            )
        except Exception as e:
            logger.warning(f"[WARMUP] 活动页访问失败(非致命): {e}")

    async def _cleanup_partial(self):
        """清理部分初始化的对象"""
        # 先清理 web_client
        if self._web_client:
            try:
                await self._web_client.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._web_client = None
        
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

    async def encrypted_request(self, url: str, method: str = "post", json_data: dict | None = None) -> dict:
        """通过 pyustc 的加密通道 URL 发起请求（不加密参数）

        复用 YouthService 的 _client（已认证、带 x-access-token），
        但不使用 service.request()（它会 AES 加密参数）。

        某些 API（如 createWxaCodeUnlimit）虽然注册在
        /login/wisdom-group-learning-bg/ 路径下，但不走 AES 解密流程，
        期望收到原始 JSON body。

        所以我们直接用 _client 发请求，手动拼接加密通道路径。
        """
        if not self._service or not hasattr(self._service, '_access_token'):
            raise RuntimeError("YouthService 未登录或无 access_token")

        service = self._service
        client = service._client

        # 拼接到加密通道路径（但不加密参数）
        effective_url = url if url.startswith("/") else f"/{url}"
        bg_url = f"/login/wisdom-group-learning-bg{effective_url}"

        logger.info(f"[ENCRYPTED-RAW] {method.upper()} {bg_url}")
        if json_data:
            logger.debug(f"[ENCRYPTED-RAW] payload: {json_data}")

        try:
            resp = await client.request(method, bg_url, json=json_data)
            result = resp.json()
            logger.info(f"[ENCRYPTED-RAW] 响应: {str(result)[:200]}")
            return result
        except Exception as e:
            logger.error(f"[ENCRYPTED-RAW] 请求失败: {e}")
            raise

    async def encrypted_request_aes(self, url: str, method: str = "post", json_data: dict | None = None) -> dict:
        """通过 pyustc 加密通道 + AES 加密参数发起请求

        使用 pyustc YouthService 的标准 request() 方法，
        参数经过 AES 加密（requestParams 字段）。

        这适用于走标准 AES 解密流程的 API。
        """
        if not self._service or not hasattr(self._service, '_access_token'):
            raise RuntimeError("YouthService 未登录或无 access_token")

        service = self._service
        effective_url = url if url.startswith("/") else f"/{url}"

        logger.info(f"[ENCRYPTED-AES] {method.upper()} {effective_url}")
        if json_data:
            logger.debug(f"[ENCRYPTED-AES] payload: {json_data}")

        try:
            result = await service.request(
                effective_url,
                method,
                json=json_data,
                need_token=True,
            )
            logger.info(f"[ENCRYPTED-AES] 响应: {str(result)[:200]}")
            return result
        except Exception as e:
            logger.error(f"[ENCRYPTED-AES] 请求失败: {e}")
            raise

    async def raw_request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """使用前端 session（JSESSIONID）发起原始 HTTP 请求
        
        用于调用 pyustc 未封装的前端 API（如 createWxaCodeUnlimit）。
        
        v10 策略：
        - 使用独立的 web_client（通过 SSO 回调建立的 JSESSIONID）
        - 不走 pyustc 的加密 API 通道
        - 模拟浏览器前端 JS 的请求方式
        
        与 pyustc 的 request() 方法的区别：
        - pyustc 走 /login/wisdom-group-learning-bg/ + AES 加密参数 + x-access-token
        - 这里走 /mobile/* 前端路由 + JSESSIONID cookie（与浏览器一致）
        """
        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW] {method} {full_url}")
        
        if not self._web_client:
            raise RuntimeError("Web session (JSESSIONID) 未初始化")
        
        client = self._web_client

        # 合并请求头（不包含 x-access-token，模拟纯浏览器行为）
        custom_headers = kwargs.pop('headers', {})
        default_headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        default_headers.update(custom_headers)

        if 'json' in kwargs:
            logger.debug(f"[RAW] body: {kwargs['json']}")
        
        # 记录发送的 cookies
        try:
            req_cookies = {c.name: c.value[:20] for c in client.cookies.jar}
            if req_cookies:
                logger.info(f"[RAW] cookies: {req_cookies}")
        except Exception:
            pass

        # 发送实际请求
        response = await client.request(
            method.upper(),
            full_url,
            headers=default_headers,
            **kwargs,
        )

        # 记录响应
        logger.info(
            f"[RAW] ← {response.status_code} "
            f"ct={response.headers.get('content-type', '?')[:40]} "
            f"url={str(response.url)[:80]}"
        )
        
        ct = response.headers.get('content-type', '')
        if 'text/html' in ct.lower():
            text = response.text
            logger.debug(f"[RAW] HTML body ({len(text)} chars): {text[:500].replace(chr(10), ' ')}")
            import re
            title_match = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
                logger.info(f"[RAW] 页面title: {title}")
        elif 'application/json' in ct.lower():
            logger.info(f"[RAW] JSON: {response.text[:200]}")

        return response
