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
        
        核心策略：
        - 完全绕过 YouthService 的内部客户端（它可能有 follow_redirects 等干扰行为）
        - 创建独立的 AsyncClient，手动收集所有可用 cookie
        - 手动控制重定向过程
        
        Args:
            method: HTTP 方法 ("GET", "POST", etc.)
            url: 完整 URL 或相对路径（相对路径自动拼接 young.ustc.edu.cn）
            **kwargs: 传递给 httpx 的其他参数（json, data, headers 等）
            
        Returns:
            httpx.Response
            
        Raises:
            RuntimeError: 如果尚未登录（未进入上下文管理器）
        """
        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW-REQUEST] {method} {full_url}")
        
        # ============================================================
        # 收集所有可用的 cookie（从多个来源）
        # ============================================================
        all_cookies = {}
        
        # 来源1: YouthService._client 的 cookie jar
        if self._service and hasattr(self._service, '_client') and self._service._client:
            try:
                ys_cookies = dict(self._service._client.cookies)
                logger.info(f"[RAW-REQUEST] YouthService._client cookies: {list(ys_cookies.keys())}")
                for k, v in ys_cookies.items():
                    all_cookies[k] = str(v)
            except Exception as e:
                logger.warning(f"[RAW-REQUEST] 读取 YouthService._client cookies 失败: {e}")
        
        # 来源2: CASClient._client 的 cookie jar
        if self._cas_client:
            for cas_attr in ["_client", "client"]:
                cas_c = getattr(self._cas_client, cas_attr, None)
                if cas_c is not None and hasattr(cas_c, 'cookies'):
                    try:
                        cas_cookies = dict(cas_c.cookies)
                        logger.info(f"[RAW-REQUEST] CASClient.{cas_attr} cookies: {list(cas_cookies.keys())}")
                        for k, v in cas_cookies.items():
                            if k not in all_cookies:  # 不覆盖 YouthService 的
                                all_cookies[k] = str(v)
                    except Exception as e:
                        logger.warning(f"[RAW-REQUEST] 读取 CASClient.{cas_attr} cookies 失败: {e}")
        
        logger.info(f"[RAW-REQUEST] 最终合并cookies: {list(all_cookies.keys())} (共{len(all_cookies)}个)")
        
        # ============================================================
        # 创建独立的、完全受控的 HTTP 客户端
        # 关键：不使用 follow_redirects，我们自己处理重定向
        # ============================================================
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,  # 禁用自动重定向！
            verify=True,
        )
        
        try:
            return await self._do_raw_request_with_cookies(client, full_url, method.upper(), all_cookies, **kwargs)
        finally:
            await client.aclose()

    async def _do_raw_request_with_cookies(self, client, full_url, method, cookies, **kwargs) -> httpx.Response:
        """使用指定 cookie 执行请求，手动处理重定向"""
        max_redirects = 5
        
        current_url = full_url
        
        for attempt in range(max_redirects + 1):
            # 构建请求头（合并自定义 headers 和 cookie）
            req_headers = kwargs.get('headers', {}).copy()
            
            logger.info(f"[RAW-REQUEST] === 请求 #{attempt+1}: {method} {current_url} ===")
            logger.info(f"[RAW-REQUEST] Cookies: {cookies}")
            if 'json' in kwargs:
                logger.info(f"[RAW-REQUEST] Body: {kwargs['json']}")
            
            response = await client.request(
                method, current_url,
                json=kwargs.get('json'),
                data=kwargs.get('data'),
                headers=req_headers,
                cookies=cookies,  # 显式传入 cookie
            )
            
            logger.info(f"[RAW-REQUEST] 响应 #{attempt+1}: status={response.status_code}, url={str(response.url)}")
            logger.info(f"[RAW-REQUEST] 响应 headers: {dict(response.headers)}")
            
            # 检查是否是重定向
            if response.status_code not in (301, 302, 303, 307, 308):
                # 非重定向，更新 cookie（服务端可能通过 Set-Cookie 更新）并返回
                new_cookies_from_response = dict(response.cookies)
                if new_cookies_from_response:
                    logger.info(f"[RAW-REQUEST] 响应 Set-Cookie: {new_cookies_from_response}")
                return response
            
            # 处理重定向
            location = response.headers.get("location", "")
            logger.warning(f"[RAW-REQUEST] 重定向! {response.status_code} → Location='{location}'")
            logger.info(f"[RAW-REQUEST] 重定向响应体前200字: {response.text[:200]}")
            
            if not location:
                logger.error("[RAW-REQUEST] 重定向无 Location header，终止")
                return response
            
            # 拼接绝对 URL
            if not location.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                parsed_base = current_url.rstrip("/")
                location = urljoin(parsed_base + "/", location)
            
            # 收集响应中的新 cookie
            new_cookies = dict(response.cookies)
            if new_cookies:
                cookies.update({k: str(v) for k, v in new_cookies.items()})
                logger.info(f"[RAW-REQUEST] 更新cookie后: {list(cookies.keys())}")
            
            current_url = location
            
            # 对于 303，方法必须改为 GET
            if response.status_code == 303:
                method = "GET"
                # 移除 body 相关参数
                kwargs.pop('json', None)
                kwargs.pop('data', None)
        
        logger.warning(f"[RAW-REQUEST] 达到最大重定向次数 ({max_redirects})")
        return response
