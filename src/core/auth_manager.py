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
        
        策略演进：
        v1-v5: 各种独立客户端方案 → 全部失败（被重定向到 main.psp）
        v6: 直接用 YouthService._client → 仍然失败
        v7 (当前): 先预热获取 JSESSIONID，再用完整认证信息发请求
        
        关键发现：
        - 服务端对 /mobile/item/* 路径有页面路由框架（Spring MVC?）
        - 无 JSESSIONID 时，POST 被当成表单提交→重定向到错误页面
        - 需要先通过 GET 建立服务端 session，获得有效 JSESSIONID
        """
        base_url = "https://young.ustc.edu.cn"
        full_url = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")

        logger.info(f"[RAW-REQUEST] {method} {full_url}")
        
        if not self._service or not hasattr(self._service, '_client') or not self._service._client:
            raise RuntimeError("YouthService 未登录或 _client 不可用")
        
        client = self._service._client
        
        # ============================================================
        # Step 0: 预热 - 访问一个页面获取 JSESSIONID
        # ============================================================
        logger.info("[RAW-REQUEST] Step 0: 预热获取 JSESSIONID...")
        try:
            warm_resp = await client.get(base_url + "/mobile/index", headers={"Accept": "text/html"})
            warm_cookies = dict(warm_resp.cookies)
            logger.info(f"[RAW-REQUEST] 预热响应: status={warm_resp.status_code}, cookies={list(warm_cookies.keys())}")
            
            # 提取 JSESSIONID
            jsession_id = warm_cookies.get('JSESSIONID', '')
            if jsession_id:
                logger.info(f"[RAW-REQUEST] ✅ 获取到 JSESSIONID: {jsession_id}")
        except Exception as e:
            logger.warning(f"[RAW-REQUEST] ⚠️ 预热失败(非致命): {e}")
            jsession_id = ''
        
        # ============================================================
        # 构建最终请求头
        # ============================================================
        merged_headers = dict(client.headers)
        custom_headers = kwargs.pop('headers', {})
        merged_headers.update(custom_headers)
        
        log_headers = {k: (str(v)[:30]+'...' if len(str(v))>30 else str(v)) for k,v in merged_headers.items()}
        logger.info(f"[RAW-REQUEST] 最终headers: {log_headers}")
        
        if 'json' in kwargs:
            logger.info(f"[RAW-REQUEST] Body: {kwargs['json']}")
        
        # ============================================================
        # 发送实际请求
        # ============================================================
        response = await client.request(
            method.upper(),
            full_url,
            headers=merged_headers,
            **kwargs,
        )
        
        logger.info(f"[RAW-REQUEST] 响应: status={response.status_code}, url={str(response.url)}")
        logger.info(f"[RAW-REQUEST] content-type: {response.headers.get('content-type', 'N/A')}")
        
        # 记录响应的 Set-Cookie
        try:
            resp_cookies = dict(response.cookies)
            if resp_cookies:
                logger.info(f"[RAW-REQUEST] 响应Set-Cookie: {resp_cookies}")
        except Exception:
            pass
        
        ct = response.headers.get('content-type', '')
        if 'text/html' in ct.lower():
            body_preview = response.text[:300].replace('\n', '\\n')
            logger.warning(f"[RAW-REQUEST] ⚠️ HTML响应: {body_preview}")
        elif 'application/json' in ct.lower():
            logger.info(f"[RAW-REQUEST] ✅ JSON响应: {response.text[:200]}")
        
        return response
