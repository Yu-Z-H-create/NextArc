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
        
        策略：
        1. 优先从 YouthService 实例获取已认证的客户端（base_url=young.ustc.edu.cn）
        2. 若不可用，则创建新客户端 + 从已登录会话复制 cookie
        
        Args:
            method: HTTP 方法 ("GET", "POST", etc.)
            url: 完整 URL 或相对路径（相对路径自动拼接 young.ustc.edu.cn）
            **kwargs: 传递给 httpx 的其他参数（json, data, headers 等）
            
        Returns:
            httpx.Response
            
        Raises:
            RuntimeError: 如果尚未登录（未进入上下文管理器）
        """
        import copy as _copy_module

        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW-REQUEST] {method} {full_url}")
        
        # ============================================================
        # 策略 1：从 YouthService 获取客户端（正确的 base_url）
        # ============================================================
        client = None
        client_source = None
        
        if self._service:
            # 探测 YouthService 内部的 httpx 客户端属性
            for attr_name in ["_client", "client", "session", "http_client", "_http", "_session"]:
                c = getattr(self._service, attr_name, None)
                if c is not None and hasattr(c, 'request'):
                    client = c
                    client_source = f"YouthService.{attr_name}"
                    logger.info(f"[RAW-REQUEST] 找到客户端: {client_source}, type={type(c).__name__}, "
                               f"base_url={getattr(c, 'base_url', 'N/A')}")
                    break
        
        # ============================================================
        # 策略 2：如果 YouthService 没有可用的客户端，创建新的 + 复制 cookie
        # ============================================================
        if client is None:
            logger.info("[RAW-REQUEST] YouthService 无可用客户端，尝试创建新客户端+复制cookie")
            
            # 收集所有可能的 cookie 来源
            all_cookies = {}
            
            # 从 CASClient 的客户端收集 cookie（CAS 登录后的 cookie）
            if self._cas_client:
                for cas_attr in ["_client", "client"]:
                    cas_c = getattr(self._cas_client, cas_attr, None)
                    if cas_c is not None and hasattr(cas_c, 'cookies'):
                        try:
                            for name, value in dict(cas_c.cookies).items():
                                if name not in all_cookies:
                                    all_cookies[name] = str(value)
                        except Exception:
                            pass
            
            # 创建专门针对 young.ustc.edu.cn 的客户端
            client = httpx.AsyncClient(
                base_url=base_url,
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
                cookies=all_cookies,
            )
            client_source = f"新建客户端(复制了{len(all_cookies)}个cookie)"
            
            logger.info(f"[RAW-REQUEST] {client_source}: cookies={list(all_cookies.keys()) if all_cookies else '(空)'}")
            
            # 使用完毕后需要关闭这个临时客户端
            needs_close = True
        else:
            needs_close = False
        
        try:
            return await self._do_raw_request(client, full_url, method.upper(), **kwargs)
        finally:
            if needs_close:
                await client.aclose()

    async def _do_raw_request(self, client, full_url, method, **kwargs) -> httpx.Response:
        """执行实际的原始请求，包含手动重定向处理"""
        max_redirects = 5
        response = await client.request(method, full_url, **kwargs)
        
        logger.info(f"[RAW-REQUEST] 初始响应: status={response.status_code}, url={str(response.url)}")
        logger.info(f"[RAW-REQUEST] 初始响应 headers: {dict(response.headers)}")
        
        # 打印发送的请求信息
        logger.info(f"[RAW-REQUEST] 实际请求URL: {full_url}, method={method}")
        if 'headers' in kwargs:
            logger.info(f"[RAW-REQUEST] 自定义headers: {kwargs['headers']}")
        if 'json' in kwargs:
            logger.info(f"[RAW-REQUEST] 请求body(json): {kwargs['json']}")
        
        for redirect_count in range(max_redirects):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            
            location = response.headers.get("location", "")
            logger.warning(f"[RAW-REQUEST] 重定向#{redirect_count + 1}: {response.status_code} → Location={location}")
            
            # 打印完整响应头帮助诊断
            resp_headers = dict(response.headers)
            logger.info(f"[RAW-REQUEST] 重定向#{redirect_count+1} 响应体前300字: {response.text[:300]}")
            
            if not location:
                logger.warning(f"[RAW-REQUEST] {response.status_code} 无 Location header (重定向#{redirect_count})")
                break
            
            # 正确拼接绝对 URL
            if not location.startswith(("http://", "https://")):
                from urllib.parse import urljoin
                base_domain = full_url.split("/", 3)[0] + "//" + full_url.split("/", 2)[2]
                location = urljoin(base_domain + "/", location)
            
            logger.info(f"[RAW-REQUEST] 重定向 #{redirect_count + 1}: {response.status_code} → {location}")
            
            # 保留原始方法 POST + body（API 需要 body 数据）
            response = await client.request(method, location, **kwargs)
        
        logger.info(f"[RAW-REQUEST] 最终响应 status={response.status_code}, url={str(response.url)}")
        return response
