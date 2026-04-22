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
        """
        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW-REQUEST] {method} {full_url}")
        
        # ============================================================
        # 深度探测 YouthService 对象的内部状态，找到所有可能的 cookie/认证信息
        # ============================================================
        all_cookies = {}
        
        # --- 探测1: YouthService._client.cookies ---
        if self._service and hasattr(self._service, '_client') and self._service._client:
            try:
                ys_cookies = dict(self._service._client.cookies)
                logger.info(f"[RAW-REQUEST] YouthService._client.cookies: {list(ys_cookies.keys())}")
                for k, v in ys_cookies.items():
                    all_cookies[k] = str(v)
            except Exception as e:
                logger.warning(f"[RAW-REQUEST] 读 YouthService._client.cookies 失败: {e}")
        
        # --- 探测2: CASClient cookies ---
        if self._cas_client:
            for cas_attr in ["_client", "client"]:
                cas_c = getattr(self._cas_client, cas_attr, None)
                if cas_c is not None and hasattr(cas_c, 'cookies'):
                    try:
                        cas_cookies = dict(cas_c.cookies)
                        logger.info(f"[RAW-REQUEST] CASClient.{cas_attr}.cookies: {list(cas_cookies.keys())}")
                        for k, v in cas_cookies.items():
                            if k not in all_cookies:
                                all_cookies[k] = str(v)
                    except Exception as e:
                        pass
        
        # --- 探测3: 暴力枚举 YouthService 所有属性 ---
        if self._service:
            logger.info(f"[RAW-REQUEST] === 开始深度探测 YouthService 对象 ===")
            for attr_name in dir(self._service):
                if attr_name.startswith('_'):
                    continue
                try:
                    val = getattr(self._service, attr_name, None)
                    if val is not None:
                        logger.info(f"[RAW-REQUEST]   YouthService.{attr_name} = {type(val).__name__}: {repr(val)[:100]}")
                except:
                    pass
            
            # 探测私有属性中的 httpx 相关对象
            for attr_name in ['_client', 'client', '_session', 'session', '_http', 'http',
                               '_req', 'request_obj', '_base_client', 'base_client']:
                c = getattr(self._service, attr_name, None)
                if c is not None:
                    logger.info(f"[RAW-REQUEST]   探测 YouthService.{attr_name}: type={type(c).__name__}")
                    # 检查是否有 cookies 属性
                    if hasattr(c, 'cookies'):
                        try:
                            ck = dict(c.cookies)
                            logger.info(f"[RAW-REQUEST]     .cookies = {ck}")
                            for k, v in ck.items():
                                if k not in all_cookies:
                                    all_cookies[k] = str(v)
                        except Exception as e:
                            logger.info(f"[RAW-REQUEST]     .cookies 读取失败: {e}")
                    # 检查是否有 headers 属性（可能有 Authorization 等）
                    if hasattr(c, 'headers'):
                        try:
                            logger.info(f"[RAW-REQUEST]     .headers = {dict(c.headers)}")
                        except:
                            pass
                    # 检查是否有 auth 属性
                    if hasattr(c, 'auth'):
                        try:
                            logger.info(f"[RAW-REQUEST]     .auth = {c.auth}")
                        except:
                            pass
                    # 检查 __dict__
                    try:
                        for sub_attr, sub_val in c.__dict__.items():
                            if not sub_attr.startswith('__'):
                                logger.info(f"[RAW-REQUEST]     .{sub_attr} = {type(sub_val).__name__}: {repr(sub_val)[:80]}")
                    except:
                        pass
        
        # --- 探测4: _service_obj (YouthService.__aenter__ 返回值) ---
        if self._service_obj is not None:
            logger.info(f"[RAW-REQUEST] === 探测 _service_obj (type={type(self._service_obj).__name__}) ===")
            for attr_name in dir(self._service_obj):
                if attr_name.startswith('_'):
                    continue
                try:
                    val = getattr(self._service_obj, attr_name, None)
                    if val is not None:
                        logger.info(f"[RAW-REQUEST]   _service_obj.{attr_name} = {type(val).__name__}: {repr(val)[:100]}")
                except:
                    pass
            # 检查 _service_obj 的私有属性中有没有 cookie/jar/session
            for attr_name in ['_client', 'client', '_session', 'cookies', '_cookies', 
                               'jar', '_jar', 'session_id', 'session_cookie', 'jsessionid']:
                c = getattr(self._service_obj, attr_name, None)
                if c is not None:
                    logger.info(f"[RAW-REQUEST]   _service_obj.{attr_name} = {type(c).__name__}: {repr(c)[:150]}")
        
        logger.info(f"[RAW-REQUEST] 最终合并cookies: {list(all_cookies.keys())} (共{len(all_cookies)}个)")
        
        # ============================================================
        # 创建独立的客户端，禁用自动重定向
        # ============================================================
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
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
