"""卡片交互处理器"""

import json
import traceback
from typing import TYPE_CHECKING, Optional

from pyustc.young import Status

from src.core import SecondClassFilter
from src.feishu_bot.calendar_service import sync_secondclass_to_calendar
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.core import UserPreferenceManager, AuthManager
    from src.feishu_bot import FeishuBot

logger = get_logger("feishu.card_handler")


class CardActionHandler:
    def __init__(self):
        self._user_preference_manager: Optional["UserPreferenceManager"] = None
        self._auth_manager: Optional["AuthManager"] = None
        self._bot: Optional["FeishuBot"] = None

    def set_dependencies(
            self,
            user_preference_manager: "UserPreferenceManager",
            auth_manager: "AuthManager",
            bot: "FeishuBot"
    ) -> None:
        self._user_preference_manager = user_preference_manager
        self._auth_manager = auth_manager
        self._bot = bot

    async def handle(self, action_value: dict, open_message_id: str) -> dict:
        action = action_value.get("action")
        activity_id = action_value.get("activity_id")
        activity_name = action_value.get("activity_name", "未知活动")

        if not action or (action != "menu_cmd" and not activity_id):
            return {
                "toast": {
                    "type": "error",
                    "content": "无效的操作参数"
                }
            }

        logger.info(f"处理卡片交互: action={action}, activity_id={activity_id}")

        if action == "toggle_ignore":
            return await self._handle_toggle_ignore(activity_id, activity_name, open_message_id)
        elif action == "toggle_interested":
            return await self._handle_toggle_interested(activity_id, activity_name, open_message_id)
        elif action == "join":
            return await self._handle_join(activity_id, activity_name)
        elif action == "view_children":
            return await self._handle_view_children(activity_id, activity_name)
        elif action == "cancel":
            return await self._handle_cancel(activity_id, activity_name)
        elif action == "menu_cmd":
            return await self._handle_menu_cmd(action_value)
        elif action == "get_qr":
            return await self._handle_get_qr(activity_id, activity_name)
        else:
            return {
                "toast": {
                    "type": "error",
                    "content": f"未知的操作类型: {action}"
                }
            }

    async def _handle_toggle_ignore(
            self,
            activity_id: str,
            activity_name: str,
            open_message_id: str
    ) -> dict:
        if not self._user_preference_manager:
            return {
                "toast": {
                    "type": "error",
                    "content": "用户偏好管理器未初始化"
                }
            }

        try:
            success, is_now_ignored = await self._user_preference_manager.toggle_ignored_activity(activity_id)

            if not success:
                return {
                    "toast": {
                        "type": "error",
                        "content": "操作失败，请稍后重试"
                    }
                }

            if is_now_ignored:
                toast_content = f"已将「{activity_name}」加入不感兴趣列表"
            else:
                toast_content = f"已将「{activity_name}」移出不感兴趣列表"

            logger.info(f"切换不感兴趣状态成功: {activity_name}, is_ignored={is_now_ignored}")

            return {
                "toast": {
                    "type": "success",
                    "content": toast_content
                }
            }

        except Exception as e:
            logger.error(f"切换不感兴趣状态失败: {e}")
            return {
                "toast": {
                    "type": "error",
                    "content": f"操作失败: {str(e)}"
                }
            }

    async def _handle_toggle_interested(
            self,
            activity_id: str,
            activity_name: str,
            open_message_id: str
    ) -> dict:
        if not self._user_preference_manager:
            return {
                "toast": {
                    "type": "error",
                    "content": "用户偏好管理器未初始化"
                }
            }

        try:
            success, is_now_interested = await self._user_preference_manager.toggle_interested_activity(activity_id)

            if not success:
                return {
                    "toast": {
                        "type": "error",
                        "content": "操作失败，请稍后重试"
                    }
                }

            if is_now_interested:
                toast_content = f"已将「{activity_name}」标记为感兴趣"
            else:
                toast_content = f"已将「{activity_name}」移出感兴趣列表"

            logger.info(f"切换感兴趣状态成功: {activity_name}, is_interested={is_now_interested}")

            return {
                "toast": {
                    "type": "success",
                    "content": toast_content
                }
            }

        except Exception as e:
            logger.error(f"切换感兴趣状态失败: {e}")
            return {
                "toast": {
                    "type": "error",
                    "content": f"操作失败: {str(e)}"
                }
            }

    async def _handle_join(self, activity_id: str, activity_name: str) -> dict:
        if not self._auth_manager or not self._bot:
            return {
                "toast": {
                    "type": "error",
                    "content": "服务未初始化，请稍后重试"
                }
            }

        logger.info(f"执行卡片报名: {activity_name} ({activity_id})")

        try:
            from pyustc.young import SecondClass, Status

            async with self._auth_manager.create_session_once():
                sc = SecondClass(activity_id, {})
                await sc.update()

                if sc.applied:
                    message = f"您已经报名了「{activity_name}」"
                    await self._bot.send_text(message)
                    return {
                        "toast": {
                            "type": "info",
                            "content": "您已报名该活动"
                        }
                    }

                if sc.status != Status.APPLYING and sc.status != Status.PUBLISHED:
                    message = (
                        f"报名失败\n\n"
                        f"活动：{activity_name}\n"
                        f"原因：当前状态不可报名（{sc.status.text if sc.status else '未知'}）"
                    )
                    await self._bot.send_text(message)
                    return {
                        "toast": {
                            "type": "error",
                            "content": "当前状态不可报名"
                        }
                    }

                if sc.need_sign_info:
                    from pyustc.young.second_class import SignInfo
                    sign_info = await SignInfo.get_self()
                    result = await sc.apply(force=False, auto_cancel=False, sign_info=sign_info)
                else:
                    result = await sc.apply(force=False, auto_cancel=False)

                if result:
                    user_id = self._bot.user_session.user_id if self._bot else None
                    calendar_msg = await sync_secondclass_to_calendar(
                        app_id=self._bot.app_id,
                        app_secret=self._bot.app_secret,
                        user_id=user_id,
                        sc=sc,
                    )

                    success_message = (
                        f"报名成功\n\n"
                        f"活动：{activity_name}\n"
                        f"时间：{sc.hold_time.start.strftime('%m-%d(%a) %H:%M') if sc.hold_time else '待定'} ~ "
                        f"{sc.hold_time.end.strftime('%m-%d(%a) %H:%M') if sc.hold_time else '待定'}\n"
                        f"{calendar_msg}"
                    )
                    await self._bot.send_text(success_message)
                    logger.info(f"卡片报名成功: {activity_name}")
                    return {
                        "toast": {
                            "type": "success",
                            "content": "报名成功"
                        }
                    }
                else:
                    fail_message = (
                        f"报名失败\n\n"
                        f"活动：{activity_name}\n"
                        f"原因：活动不可报名或名额已满"
                    )
                    await self._bot.send_text(fail_message)
                    logger.warning(f"卡片报名失败: {activity_name}")
                    return {
                        "toast": {
                            "type": "error",
                            "content": "报名失败，名额已满或已结束"
                        }
                    }

        except Exception as e:
            logger.error(f"卡片报名失败: {e}")
            error_message = (
                f"报名失败\n\n"
                f"活动：{activity_name}\n"
                f"错误：{str(e)}"
            )
            try:
                await self._bot.send_text(error_message)
            except Exception as send_err:
                logger.error(f"发送报名失败消息失败: {send_err}")

            return {
                "toast": {
                    "type": "error",
                    "content": f"报名失败: {str(e)[:50]}"
                }
            }

    async def _handle_view_children(self, activity_id: str, activity_name: str) -> dict:
        if not self._auth_manager or not self._bot:
            return {
                "toast": {
                    "type": "error",
                    "content": "服务未初始化，请稍后重试"
                }
            }

        logger.info(f"查看系列活动子活动: {activity_name} ({activity_id})")

        try:
            from pyustc.young import SecondClass

            async with self._auth_manager.create_session_once():
                sc = SecondClass(activity_id, {})
                await sc.update()

                if not sc.is_series:
                    return {
                        "toast": {
                            "type": "error",
                            "content": "该活动不是系列活动"
                        }
                    }

                children = await sc.get_children()

                filter = SecondClassFilter().exclude_status([
                    Status.ABNORMAL,
                    Status.APPLY_ENDED,
                    Status.HOUR_PUBLIC,
                    Status.HOUR_APPEND_PUBLIC,
                    Status.PUBLIC_ENDED,
                    Status.HOUR_APPLYING,
                    Status.HOUR_APPROVED,
                    Status.HOUR_REJECTED,
                    Status.FINISHED,
                ])

                children = filter(children)

                if not children:
                    await self._bot.send_text(f'系列活动「{activity_name}」暂无可报名的子活动')
                    return {
                        "toast": {
                            "type": "info",
                            "content": "该系列活动暂无子活动"
                        }
                    }

                for child in children:
                    await child.update()

                from src.utils.formatter import build_activity_card, CardButtonConfig
                from src.core import UserPreferenceManager
                from src.config import get_settings

                ignored_ids = set()
                if self._user_preference_manager:
                    ignored_ids = await self._user_preference_manager.get_all_ignored_ids()

                max_per_card = 20
                try:
                    settings = get_settings()
                    max_per_card = settings.feishu.max_activities_per_card
                except Exception:
                    pass

                button_config = CardButtonConfig()

                total = len(children)
                if total <= max_per_card:
                    card_content = build_activity_card(
                        children,
                        title=f'系列活动「{activity_name}」的子活动',
                        ignored_ids=ignored_ids,
                        button_config=button_config
                    )
                    await self._bot.send_card(card_content)
                else:
                    batches = (total + max_per_card - 1) // max_per_card
                    for batch_idx in range(batches):
                        start = batch_idx * max_per_card
                        end = min(start + max_per_card, total)
                        batch_children = children[start:end]
                        start_index = start + 1

                        batch_title = f'系列活动「{activity_name}」的子活动（{batch_idx + 1}/{batches}）'

                        card_content = build_activity_card(
                            batch_children,
                            title=batch_title,
                            ignored_ids=ignored_ids,
                            start_index=start_index,
                            button_config=button_config
                        )
                        await self._bot.send_card(card_content)

                        if batch_idx < batches - 1:
                            import asyncio
                            await asyncio.sleep(0.5)

                logger.info(f"成功发送系列活动「{activity_name}」的 {len(children)} 个子活动")

                return {
                    "toast": {
                        "type": "success",
                        "content": f"已发送 {len(children)} 个子活动"
                    }
                }

        except Exception as e:
            traceback.print_exc()
            logger.error(f"查看子活动失败: {e}")
            error_message = (
                f"查看子活动失败\n\n"
                f"系列活动：{activity_name}\n"
                f"错误：{str(e)}"
            )
            try:
                await self._bot.send_text(error_message)
            except Exception as send_err:
                logger.error(f"发送查看子活动失败消息失败: {send_err}")

            return {
                "toast": {
                    "type": "error",
                    "content": f"查看子活动失败: {str(e)[:50]}"
                }
            }

    async def _handle_cancel(self, activity_id: str, activity_name: str) -> dict:
        if not self._auth_manager or not self._bot:
            return {
                "toast": {
                    "type": "error",
                    "content": "服务未初始化，请稍后重试"
                }
            }

        logger.info(f"执行卡片取消报名: {activity_name} ({activity_id})")

        try:
            from pyustc.young.second_class import SecondClass

            async with self._auth_manager.create_session_once():
                sc = SecondClass(activity_id, {})
                result = await sc.cancel_apply()

                if result:
                    success_message = (
                        f"取消报名成功\n\n"
                        f"活动：{activity_name}\n"
                    )
                    await self._bot.send_text(success_message)
                    logger.info(f"卡片取消报名成功: {activity_name}")
                    return {
                        "toast": {
                            "type": "success",
                            "content": "取消报名成功"
                        }
                    }
                else:
                    fail_message = (
                        f"取消报名失败\n\n"
                        f"活动：{activity_name}\n"
                        f"原因：无法取消报名，请检查活动状态"
                    )
                    await self._bot.send_text(fail_message)
                    logger.warning(f"卡片取消报名失败: {activity_name}")
                    return {
                        "toast": {
                            "type": "error",
                            "content": "取消报名失败，请检查活动状态"
                        }
                    }

        except Exception as e:
            logger.error(f"卡片取消报名失败: {e}")
            error_message = (
                f"取消报名失败\n\n"
                f"活动：{activity_name}\n"
                f"错误：{str(e)}"
            )
            try:
                await self._bot.send_text(error_message)
            except Exception as send_err:
                logger.error(f"发送取消报名失败消息失败: {send_err}")

            return {
                "toast": {
                    "type": "error",
                    "content": f"取消报名失败: {str(e)[:50]}"
                }
            }

    async def _handle_get_qr(self, activity_id: str, activity_name: str) -> dict:
        """获取活动的签到+签退二维码，通过飞书消息返回"""
        if not self._auth_manager or not self._bot:
            return {
                "toast": {"type": "error", "content": "服务未初始化，请稍后重试"}
            }

        logger.info(f"获取二维码: {activity_name} ({activity_id})")

        try:
            import base64
            import httpx

            qr_api_path = "/mobile/item/createWxaCodeUnlimit"
            qr_payload = {
                "page": "pagesA/projectdt/projectdt",
                "scene": str(activity_id),
                "appId": "",
            }

            logger.info(f"[QR-DEBUG] 开始获取二维码, activity_id={activity_id}, activity_name={activity_name}")
            logger.info(f"[QR-DEBUG] API路径: {qr_api_path}")
            logger.info(f"[QR-DEBUG] 请求参数: {qr_payload}")

            # 复用 auth_manager 已有的 CAS 登录流程，通过 raw_request 调用 API
            session_ctx = self._auth_manager.create_session_once()

            async with session_ctx as service:
                # ===== 调试：检查 CAS 登录状态 =====
                logger.info(f"[QR-DEBUG] CAS 登录成功, service 类型: {type(service).__name__}")

                # ===== 调试：签到码请求 =====
                logger.info(f"[QR-DEBUG] >>> 发送签到码 POST 请求...")
                sign_in_resp = await session_ctx.raw_request(
                    "POST", qr_api_path, json=qr_payload,
                )
                logger.info(f"[QR-DEBUG] <<< 签到码响应:")
                logger.info(f"[QR-DEBUG]   status_code: {sign_in_resp.status_code}")
                logger.info(f"[QR-DEBUG]   headers: {dict(sign_in_resp.headers)}")
                resp_text = sign_in_resp.text
                # 如果是 HTML 错误页，提取关键内容
                if "text/html" in sign_in_resp.headers.get("content-type", ""):
                    import re
                    # 提取 <body> 或 <div> 中的文字
                    body_match = re.search(r'<body[^>]*>(.*?)</body>', resp_text, re.DOTALL)
                    if body_match:
                        clean_text = re.sub(r'<[^>]+>', ' ', body_match.group(1))
                        clean_text = ' '.join(clean_text.split())
                        logger.info(f"[QR-DEBUG]   HTML body 文字: {clean_text[:500]}")
                    else:
                        logger.info(f"[QR-DEBUG]   body(前1000字): {resp_text[:1000]}")
                else:
                    logger.info(f"[QR-DEBUG]   body(前500字): {resp_text[:500]}")
                logger.info(f"[QR-DEBUG]   body长度: {len(resp_text)}")

                # 安全解析 JSON
                sign_in_data = {}
                try:
                    sign_in_data = sign_in_resp.json()
                    logger.info(f"[QR-DEBUG]   JSON 解析成功: {sign_in_data}")
                except Exception as json_err:
                    logger.error(f"[QR-DEBUG]   JSON 解析失败: {json_err}")

                sign_in_b64 = ""
                if isinstance(sign_in_data, dict) and sign_in_data.get("success"):
                    sign_in_b64 = sign_in_data.get("message") or ""
                    logger.info(f"[QR-DEBUG]   签到码 base64 长度: {len(sign_in_b64)}")
                else:
                    logger.warning(f"[QR-DEBUG] 签到码API返回非成功或非JSON: {sign_in_data}")

                # ===== 调试：签退码请求（同一个 activity_id） =====
                logger.info(f"[QR-DEBUG] >>> 发送签退码 POST 请求...")
                sign_out_resp = await session_ctx.raw_request(
                    "POST", qr_api_path, json=qr_payload,
                )
                logger.info(f"[QR-DEBUG] <<< 签退码响应:")
                logger.info(f"[QR-DEBUG]   status_code: {sign_out_resp.status_code}")
                logger.info(f"[QR-DEBUG]   headers: {dict(sign_out_resp.headers)}")
                out_resp_text = sign_out_resp.text
                if "text/html" in sign_out_resp.headers.get("content-type", ""):
                    import re
                    body_match = re.search(r'<body[^>]*>(.*?)</body>', out_resp_text, re.DOTALL)
                    if body_match:
                        clean_text = re.sub(r'<[^>]+>', ' ', body_match.group(1))
                        clean_text = ' '.join(clean_text.split())
                        logger.info(f"[QR-DEBUG]   HTML body 文字: {clean_text[:500]}")
                    else:
                        logger.info(f"[QR-DEBUG]   body(前1000字): {out_resp_text[:1000]}")
                else:
                    logger.info(f"[QR-DEBUG]   body(前500字): {out_resp_text[:500]}")
                logger.info(f"[QR-DEBUG]   body长度: {len(out_resp_text)}")

                sign_out_data = {}
                try:
                    sign_out_data = sign_out_resp.json()
                    logger.info(f"[QR-DEBUG]   JSON 解析成功: {sign_out_data}")
                except Exception as json_err:
                    logger.error(f"[QR-DEBUG]   JSON 解析失败: {json_err}")

                sign_out_b64 = ""
                if isinstance(sign_out_data, dict) and sign_out_data.get("success"):
                    sign_out_b64 = sign_out_data.get("message") or ""
                    logger.info(f"[QR-DEBUG]   签退码 base64 长度: {len(sign_out_b64)}")
                else:
                    logger.warning(f"[QR-DEBUG] 签退码API返回非成功或非JSON: {sign_out_data}")

            if not sign_in_b64 and not sign_out_b64:
                return {
                    "toast": {"type": "error", "content": "二维码获取失败，请检查登录态或活动状态"}
                }

            # 发送文字说明
            msg_parts = [f"📋 {activity_name} 签到/签退二维码\n"]
            if sign_in_b64:
                msg_parts.append("✅ **签到码**：见下方图片")
            if sign_out_b64:
                msg_parts.append("⏪ **签退码**：见下方图片")

            await self._bot.send_text("\n".join(msg_parts))

            # 逐张发送图片
            for label, b64_data in [("签到", sign_in_b64), ("签退", sign_out_b64)]:
                if not b64_data:
                    continue

                try:
                    img_bytes = base64.b64decode(b64_data)

                    token = await self._get_feishu_token()
                    if not token:
                        await self._bot.send_text(f"⚠️ {label}码: 无法上传图片")
                        continue

                    async with httpx.AsyncClient(timeout=15) as upload_client:
                        files = {"image": ("qr.png", img_bytes, "image/png")}
                        upload_resp = await upload_client.post(
                            "https://open.feishu.cn/open-apis/im/v1/images",
                            headers={"Authorization": f"Bearer {token}"},
                            files=files,
                        )
                        upload_result = upload_resp.json()

                    if upload_result.get("code") != 0:
                        logger.warning(f"飞书图片上传失败: {upload_result.get('msg')}")
                        await self._bot.send_text(f"⚠️ {label}码: 图片上传失败 - {upload_result.get('msg', '未知错误')}")
                        continue

                    image_key = upload_result["data"]["image_key"]

                    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                    from lark_oapi import Client

                    lark_client = Client.builder() \
                        .app_id(self._bot.app_id) \
                        .app_secret(self._bot.app_secret) \
                        .build()

                    body = CreateMessageRequestBody.builder() \
                        .receive_id(self._bot.get_chat_id()) \
                        .msg_type("image") \
                        .content(json.dumps({"image_key": image_key})) \
                        .receive_id_type("chat_id") \
                        .build()

                    request = CreateMessageRequest.builder().request_body(body).build()
                    response = lark_client.im.v1.message.create(request)

                    if not response.success():
                        logger.warning(f"发送{label}码图片失败: {response.msg}")

                except Exception as img_err:
                    logger.error(f"处理{label}码图片异常: {img_err}")
                    await self._bot.send_text(f"⚠️ {label}码: 处理失败 - {str(img_err)[:100]}")

            return {
                "toast": {"type": "success", "content": f"正在获取 {activity_name} 的二维码..."}
            }

        except Exception as e:
            logger.error(f"获取二维码失败: {e}")
            import traceback
            traceback.print_exc()

            # 网络错误友好提示
            err_str = str(e)
            is_network = any(kw in err_str.lower() for kw in [
                "timeout", "connect", "connection", "network", "readtimeout",
                "connecttimeout", "resolv", "refused",
            ])
            if is_network:
                toast_content = "连接超时，请检查网络"
                error_msg = (
                    f"⚠️ 二维码获取失败（网络超时）\n\n"
                    f"活动：{activity_name}\n"
                    f"原因：VM 访问 passport.ustc.edu.cn 超时\n\n"
                    f"请在 VM 上执行: ping -c 3 passport.ustc.edu.cn"
                )
            else:
                toast_content = f"获取失败: {str(e)[:50]}"
                error_msg = (
                    f"二维码获取失败\n\n"
                    f"活动：{activity_name}\n"
                    f"错误：{str(e)}"
                )

            try:
                await self._bot.send_text(error_msg)
            except Exception:
                pass

            return {
                "toast": {"type": "error", "content": toast_content}
            }

    @staticmethod
    async def _get_feishu_token() -> str | None:
        """获取飞书 tenant_access_token"""
        try:
            import httpx
            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            # 使用 bot 的凭证（通过 card_handler 拿不到 bot 实例时需要其他方式）
            # 这里从 settings 读取
            from src.config import get_settings
            settings = get_settings()
            payload = {"app_id": settings.feishu.app_id, "app_secret": settings.feishu.app_secret}

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                result = resp.json()

            if result.get("code") == 0:
                return result.get("tenant_access_token")

            logger.error(f"获取 tenant_access_token 失败: {result.get('msg')}")
            return None
        except Exception as e:
            logger.error(f"获取飞书 token 异常: {e}")
            return None

    async def _handle_menu_cmd(self, action_value: dict) -> dict:
        """将菜单按钮转回现有文本指令入口。"""
        if not self._bot or not self._bot.message_handler:
            return {
                "toast": {
                    "type": "error",
                    "content": "服务未就绪"
                }
            }

        cmd = action_value.get("cmd")
        args = action_value.get("args") or []

        if not cmd:
            return {
                "toast": {
                    "type": "error",
                    "content": "无效的菜单命令"
                }
            }

        command_text = f"/{cmd}"
        if args:
            command_text += " " + " ".join(str(arg) for arg in args)

        logger.info(f"执行菜单命令: {command_text}")

        try:
            await self._bot.message_handler(command_text, self._bot.user_session)
        except Exception as e:
            logger.error(f"执行菜单命令失败: {e}")
            return {
                "toast": {
                    "type": "error",
                    "content": f"执行失败: {str(e)[:50]}"
                }
            }

        return {
            "toast": {
                "type": "success",
                "content": f"已执行 {command_text}"
            }
        }
