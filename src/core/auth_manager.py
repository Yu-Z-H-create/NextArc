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
        
        核心发现：
        - YouthService._client.cookies 为空！认证不靠 cookie
        - 真正的认证凭证是 _client.headers 中的 x-access-token (JWT)
        - 必须在请求中携带这个 token 才能通过服务端验证
        """
        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW-REQUEST] {method} {full_url}")
        
        # ============================================================
        # 关键：从 YouthService._client.headers 提取 x-access-token (JWT)
        # ============================================================
        access_token = None
        
        if self._service and hasattr(self._service, '_client') and self._service._client:
            try:
                client_headers = dict(self._service._client.headers)
                access_token = client_headers.get('x-access-token')
                if access_token:
                    logger.info(f"[RAW-REQUEST] ✅ 找到 x-access-token (长度={len(access_token)})")
                else:
                    logger.warning(f"[RAW-REQUEST] ⚠️ YouthService._client 无 x-access-token! headers keys: {list(client_headers.keys())}")
            except Exception as e:
                logger.error(f"[RAW-REQUEST] 读取 headers 失败: {e}")
        
        if not access_token:
            logger.error("[RAW-REQUEST] ❌ 无法获取 x-access-token，请求将失败!")
        
        # 合并自定义 headers，注入 x-access-token
        merged_headers = kwargs.get('headers', {}).copy()
        if access_token:
            merged_headers['x-access-token'] = access_token
            logger.info("[RAW-REQUEST] 已注入 x-access-token 到请求头")
        
        # 也收集 CAS cookies 作为补充
        all_cookies = {}
        if self._cas_client:
            for cas_attr in ["_client", "client"]:
                cas_c = getattr(self._cas_client, cas_attr, None)
                if cas_c is not None and hasattr(cas_c, 'cookies'):
                    try:
                        for k, v in dict(cas_c.cookies).items():
                            all_cookies[k] = str(v)
                    except Exception:
                        pass
        
        # ============================================================
        # 创建独立客户端，手动控制重定向，携带 x-access-token
        # ============================================================
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            verify=True,
        )
        
        kwargs['headers'] = merged_headers
        
        try:
            return await self._do_raw_request_with_cookies(client, full_url, method.upper(), all_cookies, **kwargs)
        finally:
            await client.aclose()

    async def _do_raw_request_with_cookies(self, client, full_url, method, cookies, **kwargs) -> httpx.Response:
        """使用指定 cookie 执行请求，手动处理重定向"""
        max_redirects = 5
        current_url = full_url
        
        for attempt in range(max_redirects + 1):
            req_headers = kwargs.get('headers', {}).copy()
            
            logger.info(f"[RAW-REQUEST] === 请求 #{attempt+1}: {method} {current_url} ===")
            logger.info(f"[RAW-REQUEST] Cookies: {list(cookies.keys())}")
            if 'json' in kwargs:
                logger.info(f"[RAW-REQUEST] Body: {kwargs['json']}")
            
            response = await client.request(
                method, current_url,
                json=kwargs.get('json'),
                data=kwargs.get('data'),
                headers=req_headers,
                cookies=cookies,
            )
            
            logger.info(f"[RAW-REQUEST] 响应 #{attempt+1}: status={response.status_code}, url={str(response.url)}")
            
            if response.status_code not in (301, 302, 303, 307, 308):
                new_cookies_from_response = dict(response.cookies)
                if new_cookies_from_response:
                    logger.info(f"[RAW-REQUEST] 响应 Set-Cookie: {new_cookies_from_response}")
                return response
            
            location = response.headers.get("location", "")
            logger.warning(f"[RAW-REQUEST] 重定向! {response.status_code} → '{location}'")
            logger.info(f"[RAW-REQUEST] 重定向体: {response.text[:200]}")
            
            if not location:
                return response
            
            if not location.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                location = urljoin(current_url.rstrip("/") + "/", location)
            
            new_cookies = dict(response.cookies)
            if new_cookies:
                cookies.update({k: str(v) for k, v in new_cookies.items()})
            
            current_url = location
            
            if response.status_code == 303:
                method = "GET"
                kwargs.pop('json', None)
                kwargs.pop('data', None)
        
        return response
