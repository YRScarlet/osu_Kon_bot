import pymysql
from typing import Optional, Dict, Any
from datetime import datetime

from nonebot import on_command, logger
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageEvent

from .utils import get_osu_token, get_db_connection, get_proxied_http_client

# --- osu! API 函数 ---
async def get_osu_user_info_by_username(username: str) -> Optional[Dict[str, Any]]:
    """根据 osu! 用户名查询用户信息 (主要是获取 osu_uid 和 准确的 username)"""
    token = await get_osu_token()
    if not token:
        logger.error("无法获取 osu! API Token，无法查询用户信息。")
        return None

    api_url = f"https://osu.ppy.sh/api/v2/users/{username}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    logger.info(f"正在通过 osu! API 查询用户: {username}")
    try:
        async with get_proxied_http_client() as client:
            response = await client.get(api_url, headers=headers)
            if response.status_code == 404:
                logger.warning(f"osu! API 未找到用户: {username}")
                return None
            response.raise_for_status()
            user_data = response.json()
            if "id" in user_data and "username" in user_data:
                return {"osu_uid": user_data["id"], "osu_username": user_data["username"]}
            else:
                logger.error(f"从 osu! API 获取的用户数据不完整: {user_data}")
                return None
    except Exception as e:
        logger.error(f"查询 osu! 用户信息时发生错误: {e}")
        return None

# --- 数据库操作函数 ---
async def db_check_qq_binding(qqid: int) -> Optional[Dict[str, Any]]:
    """检查QQ号是否已绑定，若绑定则返回信息"""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT osu_uid, osu_username_at_bind FROM UserBindings WHERE qqid = %s", (qqid,))
            return cursor.fetchone()
    except pymysql.MySQLError as e:
        logger.error(f"查询QQ绑定信息失败: {e}")
        return None
    finally:
        if conn: conn.close()

async def db_check_osu_uid_binding(osu_uid: int) -> Optional[Dict[str, Any]]:
    """检查osu_uid是否已被他人绑定"""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            sql = """
            SELECT ub.qqid, qq.nickname FROM UserBindings ub
            LEFT JOIN QQUsers qq ON ub.qqid = qq.qqid
            WHERE ub.osu_uid = %s
            """
            cursor.execute(sql, (osu_uid,))
            return cursor.fetchone()
    except pymysql.MySQLError as e:
        logger.error(f"查询osu_uid绑定信息失败: {e}")
        return None
    finally:
        if conn: conn.close()

async def db_bind_user(qqid: int, osu_uid: int, osu_username: str, qq_nickname: str) -> bool:
    """执行绑定操作，写入数据库"""
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cursor:
            current_time = datetime.now()
            # 插入或更新 QQUsers 表
            cursor.execute(
                "INSERT INTO QQUsers (qqid, nickname, created_at) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE nickname = VALUES(nickname)",
                (qqid, qq_nickname, current_time)
            )
            # 插入绑定记录
            cursor.execute(
                "INSERT INTO UserBindings (qqid, osu_uid, osu_username_at_bind, bind_time) VALUES (%s, %s, %s, %s)",
                (qqid, osu_uid, osu_username, current_time)
            )
            conn.commit()
            return True
    except pymysql.MySQLError as e:
        logger.error(f"执行绑定操作失败: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()

async def db_unbind_user(qqid: int) -> bool:
    """执行解绑操作，从数据库删除记录"""
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cursor:
            rows_affected = cursor.execute("DELETE FROM UserBindings WHERE qqid = %s", (qqid,))
            conn.commit()
            return rows_affected > 0
    except pymysql.MySQLError as e:
        logger.error(f"执行解绑操作失败 (QQID: {qqid}): {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()

# --- 命令处理器 ---
bind_matcher = on_command("konbind", aliases={"绑定osu", "osu绑定"}, priority=10, block=True)

@bind_matcher.handle()
async def handle_bind_command(event: MessageEvent, args: Message = CommandArg()):
    """处理 /konbind 命令"""
    qqid = int(event.get_user_id())
    osu_username_to_bind = args.extract_plain_text().strip()

    if not osu_username_to_bind:
        await bind_matcher.finish("请输入你要绑定的 osu! 用户名。\n使用方法: /konbind [osu!用户名]")

    # 1. 检查QQ是否已绑定
    if existing_binding := await db_check_qq_binding(qqid):
        await bind_matcher.finish(
            f"你已经绑定了 osu! 账号: {existing_binding['osu_username_at_bind']} (UID: {existing_binding['osu_uid']})。\n"
            "如需换绑，请先使用 /konunbind 解绑。"
        )

    await bind_matcher.send(f"正在查询 osu! 用户 [{osu_username_to_bind}] 的信息并尝试绑定...")

    # 2. 查询 osu! API 获取用户信息
    osu_user_info = await get_osu_user_info_by_username(osu_username_to_bind)
    if not osu_user_info:
        await bind_matcher.finish(f"未能找到 osu! 用户 [{osu_username_to_bind}] 或查询API时出错，请检查用户名是否正确。")
    
    osu_uid = osu_user_info["osu_uid"]
    actual_osu_username = osu_user_info["osu_username"]

    # 3. 检查 osu! 账号是否已被他人绑定
    if other_binding := await db_check_osu_uid_binding(osu_uid):
        bound_qqid = other_binding['qqid']
        bound_user_display = f"QQ用户(昵称: {other_binding.get('nickname', '未知')})"
        await bind_matcher.finish(
            f"osu! 账号 {actual_osu_username} (UID: {osu_uid}) 已经被 {bound_qqid} 绑定了。"
        )

    # 4. 执行绑定
    qq_nickname = event.sender.card or event.sender.nickname or f"User_{qqid}"
    success = await db_bind_user(qqid, osu_uid, actual_osu_username, qq_nickname)

    if success:
        logger.info(f"QQ用户 {qqid} 成功绑定 osu!账号 {actual_osu_username} (UID: {osu_uid})")
        await bind_matcher.finish(
            f"绑定成功！\nQQ: {qq_nickname}\n已绑定到 osu! 用户: {actual_osu_username} (UID: {osu_uid})"
        )
    else:
        await bind_matcher.finish("绑定过程中发生数据库错误！请联系YRScarlet")

unbind_matcher = on_command("konunbind", aliases={"解绑osu", "osu解绑"}, priority=10, block=True)

@unbind_matcher.handle()
async def handle_unbind_command(event: MessageEvent):
    """处理 /konunbind 命令"""
    qqid = int(event.get_user_id())

    existing_binding = await db_check_qq_binding(qqid)
    if not existing_binding:
        await unbind_matcher.finish("你还没有绑定任何 osu! 账号！")

    bound_osu_username = existing_binding['osu_username_at_bind']
    bound_osu_uid = existing_binding['osu_uid']

    if await db_unbind_user(qqid):
        logger.info(f"QQ用户 {qqid} 已成功解绑 osu! 账号 {bound_osu_username} (UID: {bound_osu_uid})。")
        await unbind_matcher.finish(
            f"已成功解绑 osu! 账号：{bound_osu_username} (UID: {bound_osu_uid})！"
        )
    else:
        await unbind_matcher.finish("解绑过程中发生数据库错误，请联系YRScarlet！")