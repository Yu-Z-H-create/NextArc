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
        """使用已登录的 CAS session 发起原始 HTTP 请求
        
        用于调用 YouthService 未封装的 API（如 createWxaCodeUnlimit）。
        
        Args:
            method: HTTP 方法 ("GET", "POST", etc.)
            url: 完整 URL 或相对路径（相对路径自动拼接 young.ustc.edu.cn）
            **kwargs: 传递给 httpx 的其他参数（json, data, headers 等）
            
        Returns:
            httpx.Response
            
        Raises:
            RuntimeError: 如果尚未登录（未进入上下文管理器）
        """
        if not self._cas_client or not hasattr(self._cas_client, "_client"):
            raise RuntimeError("raw_request 需要先通过 async with 进入会话")

        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW-REQUEST] {method} {full_url}")
        
        # 探测 CASClient 内部的 httpx client 属性
        cas = self._cas_client
        client = getattr(cas, "_client", None)
        if client is None:
            # 尝试其他可能的属性名
            for attr_name in ["client", "session", "http_client", "_http"]:
                client = getattr(cas, attr_name, None)
                if client is not None:
                    logger.info(f"[RAW-REQUEST] 找到 httpx client 在属性: {attr_name}")
                    break
        
        if client is None:
            logger.error(f"[RAW-ERROR] CASClient 上找不到 httpx client！可用属性: {[a for a in dir(cas) if not a.startswith('__')]}")
            raise RuntimeError("无法从 CASClient 获取 httpx client")
        
        logger.info(f"[RAW-REQUEST] client 类型: {type(client).__name__}, base_url: {getattr(client, 'base_url', 'N/A')}")
        cookies_dict = dict(client.cookies)
        logger.info(f"[RAW-REQUEST] cookies(发送前): {list(cookies_dict.keys()) if cookies_dict else '(空)'}")
        
        response = await client.request(method.upper(), full_url, **kwargs)
        
        logger.info(f"[RAW-REQUEST] 响应 status={response.status_code}, url={response.url}")
        return response
