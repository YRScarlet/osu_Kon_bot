import pymysql
from typing import Optional, Dict, Any, List
from datetime import datetime

from nonebot import on_command, logger
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.permission import SUPERUSER
from nonebot.exception import FinishedException

from .utils import get_db_connection
from .recommend import TYPE_ALIASES, VALID_TYPES

# --- 辅助函数 (模块内专用) ---

async def get_pending_list_from_db(count: int = 5) -> List[Dict[str, Any]]:
    """从数据库获取最近N条待审谱面记录"""
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            sql = "SELECT bid, reason_for_pending, added_to_queue_at FROM PendingBeatmapReviews ORDER BY added_to_queue_at DESC LIMIT %s"
            cursor.execute(sql, (count,))
            return cursor.fetchall()
    except pymysql.MySQLError as e:
        logger.error(f"获取待审列表失败: {e}")
        return []
    finally:
        if conn: conn.close()

async def get_beatmap_analysis_info(bid: int) -> Optional[Dict[str, Any]]:
    """获取指定bid在BeatmapAnalysis表中的信息"""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            sql = "SELECT bid, determined_b_type, is_auto_typed FROM BeatmapAnalysis WHERE bid = %s"
            cursor.execute(sql, (bid,))
            return cursor.fetchone()
    except pymysql.MySQLError as e:
        logger.error(f"查询 BeatmapAnalysis 表中 bid {bid} 失败: {e}")
        return None
    finally:
        if conn: conn.close()

async def update_beatmap_classification(bid: int, new_type: str, admin_id: str) -> bool:
    """更新谱面分类，标记为人工审核，并从待审队列移除。"""
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cursor:
            # 1. 检查记录是否存在
            cursor.execute("SELECT bid FROM BeatmapAnalysis WHERE bid = %s", (bid,))
            if not cursor.fetchone():
                logger.warning(f"管理员尝试处理不存在于 BeatmapAnalysis 的 bid: {bid}")
                return False

            # 2. 更新 BeatmapAnalysis 表
            sql_update = """
            UPDATE BeatmapAnalysis SET determined_b_type = %s, is_auto_typed = 0, 
            manual_review_at = %s, reviewed_by_admin_id = %s WHERE bid = %s
            """
            cursor.execute(sql_update, (new_type, datetime.now(), admin_id, bid))
            
            # 3. 从 PendingBeatmapReviews 表中删除
            cursor.execute("DELETE FROM PendingBeatmapReviews WHERE bid = %s", (bid,))
            
            conn.commit()
            logger.info(f"管理员 {admin_id} 已成功处理谱面 {bid}，新类型为 {new_type}。")
            return True
    except pymysql.MySQLError as e:
        logger.error(f"处理待审谱面 {bid} 失败: {e}")
        if conn: conn.rollback()
        return False
    finally:
        if conn: conn.close()

# --- 命令处理器 ---
pending_matcher = on_command("pending", aliases={"待审", "审核"}, permission=SUPERUSER, priority=5, block=True)

@pending_matcher.handle()
async def handle_pending_command(event: MessageEvent, args: Message = CommandArg()):
    """处理 /pending 命令"""
    arg_list = args.extract_plain_text().strip().split()
    
    if not arg_list:
        await pending_matcher.finish(
            "请提供操作指令！\n用法:\n  /pending list [数量]\n  /pending [谱面ID] [新类型]"
        )

    action = arg_list[0].lower()

    if action == "list":
        count = int(arg_list[1]) if len(arg_list) > 1 and arg_list[1].isdigit() else 5
        count = max(1, min(count, 20))
        
        pending_maps = await get_pending_list_from_db(count)
        if not pending_maps:
            await pending_matcher.finish("太棒了！当前没有需要审核的谱面。")
        
        parts = [f"最近 {len(pending_maps)} 条待审谱面："]
        for item in pending_maps:
            time_str = item['added_to_queue_at'].strftime('%Y-%m-%d %H:%M')
            parts.append(f"- BID: {item['bid']} | 原因: {item.get('reason_for_pending', '无')} | 时间: {time_str}")
        await pending_matcher.send("\n".join(parts))

    elif len(arg_list) == 2:
        bid_str, raw_new_type = arg_list
        if not bid_str.isdigit():
            await pending_matcher.finish("谱面ID必须是数字！请检查输入。")
        
        bid = int(bid_str)
        new_type = TYPE_ALIASES.get(raw_new_type.lower())
        if not new_type or new_type not in VALID_TYPES:
            await pending_matcher.finish(f"无效的新类型: {raw_new_type}...")

        if not await get_beatmap_analysis_info(bid):
            await pending_matcher.finish(f"错误：谱面ID {bid} 在谱面分析库中未找到记录...")

        admin_id = event.get_user_id()
        try:
            if await update_beatmap_classification(bid, new_type, admin_id):
                await pending_matcher.finish(
                    f"谱面 {bid} 已成功处理！\n"
                    f"新类型设置为: 【{new_type.upper()}】\n"
                    f"已标记为人工审核，并从待审列表中移除。"
                )
            else:
                await pending_matcher.finish(
                    f"处理谱面 {bid} 失败。\n"
                    "可能原因：谱面分析记录不存在，或数据库操作错误。请检查日志。"
                )
        except Exception as e:
            logger.exception(f"处理 /pending 命令时发生未知错误: {e}")
#            await pending_matcher.finish("处理命令时发生内部未知错误，请查看日志。")
    else:
        await pending_matcher.finish(
            "无效的 pending 命令格式。\n用法:\n  /pending list [数量]\n  /pending [谱面ID] [新类型]"
        )