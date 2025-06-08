import pymysql
import re
import random
import asyncio
from typing import Optional, Dict, Any, Tuple, List, Union
from datetime import datetime

from nonebot import on_command, logger
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment, Bot

from .utils import get_db_connection, get_official_beatmap_info

# 类型别名和有效类型定义
TYPE_ALIASES: Dict[str, str] = {
    "串": "stream", "stream": "stream",
    "跳": "jump", "jump": "jump", "aim": "jump",
    "强双": "alt", "alt": "alt",
    "科技": "tech", "tech": "tech",
    "其他": "others", "其它": "others", "others": "others",
}
VALID_DB_TYPES = set(TYPE_ALIASES.values())

# 筛选字段与数据库字段的映射
FIELD_MAP: Dict[str, Tuple[str, str, bool]] = {
    "ar": ("bi", "ar", True), "od": ("bi", "od", True), "cs": ("bi", "cs", True),
    "hp": ("bi", "hp", True), "stars": ("bi", "star_rating", True),
    "length": ("bi", "length_seconds", True), "bpm": ("bi", "bpm", True),
}

# --- 辅助函数 (模块内专用) ---

async def get_beatmap_recommendations_sample(bid: int, sample_size: int = 3) -> List[Dict[str, Any]]:
    """从 Recommendations 表随机获取指定数量的推荐记录。"""
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            sql = """
            SELECT osu_username_at_recommend_time, recommended_at, recommendation_description
            FROM Recommendations WHERE bid = %s ORDER BY RAND() LIMIT %s
            """
            cursor.execute(sql, (bid, sample_size))
            return cursor.fetchall()
    except pymysql.MySQLError as e:
        logger.error(f"查询谱面 {bid} 的推荐描述失败: {e}")
        return []
    finally:
        if conn: conn.close()

def parse_value_and_operator(value_str: str) -> Tuple[str, Optional[float], Optional[float]]:
    """解析筛选条件的值部分，返回 (operator, value1, value2)"""
    value_str = value_str.strip()
    if match := re.match(r"^(>=|<=|>|<|=)([\d\.]+)$", value_str):
        return match.group(1), float(match.group(2)), None
    if match := re.match(r"^([\d\.]+)-([\d\.]+)$", value_str):
        v1, v2 = float(match.group(1)), float(match.group(2))
        return "=", min(v1, v2), max(v1, v2)
    if match := re.match(r"^([\d\.]+)$", value_str):
        return "=", float(match.group(1)), None
    return "=", None, None

def parse_random_query_args(arg_text: str) -> Dict[str, Any]:
    """解析随机推图命令的参数。"""
    params: Dict[str, Any] = {"type": None, "count": 1, "filters": []}
    parts = arg_text.lower().split()
    
    temp_parts = []
    for part in parts:
        if part.startswith(("n=", "数量=")):
            try:
                count_val = int(part.split("=")[1])
                params["count"] = max(1, min(count_val, 5))
            except (IndexError, ValueError):
                pass
        else:
            temp_parts.append(part)
    parts = temp_parts

    if parts and (matched_type := TYPE_ALIASES.get(parts[0])):
        params["type"] = matched_type
        parts.pop(0)

    for part in parts:
        if not (match := re.match(r"^([a-z_]+)((?:>=|<=|>|<|=)?[\d\.-]+[sm]?)$", part)):
            continue
        field, value_op_str = match.groups()

        if field not in FIELD_MAP:
            continue
        
        db_table, db_col, _ = FIELD_MAP[field]
        multiplier = 60.0 if field == "length" and value_op_str.endswith('m') else 1.0
        value_op_str = value_op_str.rstrip('sm')

        op, v1, v2 = parse_value_and_operator(value_op_str)
        if v1 is None: continue
        
        filter_entry = {"db_table": db_table, "db_field": db_col, "operator": op, "value1": v1 * multiplier}
        if v2 is not None:
            filter_entry["value2"] = v2 * multiplier
        params["filters"].append(filter_entry)
        
    return params

def build_sql_query(parsed_args: Dict[str, Any]) -> Tuple[str, List[Union[str, float, int]]]:
    """根据解析的参数构建SQL查询。"""
    fields = "bi.*, ba.determined_b_type, ba.stream_prob, ba.jump_prob, ba.alt_prob, ba.tech_prob"
    sql = f"SELECT {fields} FROM BeatmapInfo bi JOIN BeatmapAnalysis ba ON bi.bid = ba.bid"
    clauses, params = [], []

    if parsed_args["type"]:
        clauses.append("ba.determined_b_type = %s")
        params.append(parsed_args["type"])

    for f in parsed_args["filters"]:
        clause = f"{f['db_table']}.{f['db_field']} {f['operator']} %s"
        if f.get('value2') is not None:
            clauses.append(f"{f['db_table']}.{f['db_field']} >= %s")
            params.append(f['value1'])
            clauses.append(f"{f['db_table']}.{f['db_field']} <= %s")
            params.append(f['value2'])
        else:
            clauses.append(clause)
            params.append(f['value1'])

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    
    sql += " ORDER BY RAND() LIMIT %s"
    params.append(parsed_args["count"])
    return sql, params

async def execute_random_recommend_query(sql: str, params: List[Any]) -> List[Dict[str, Any]]:
    """执行查询并返回结果。"""
    conn = get_db_connection()
    if not conn: return []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            return cursor.fetchall()
    except pymysql.MySQLError as e:
        logger.error(f"执行随机推图查询失败: {e}")
        return []
    finally:
        if conn: conn.close()

def format_beatmap_result_for_display(
    beatmap_data: Dict[str, Any], recommendations: Optional[List[Dict[str, Any]]] = None
) -> str:
    """格式化单个谱面数据用于QQ消息显示。"""
    length = beatmap_data.get('length_seconds')
    length_str = f"{length // 60}m{length % 60}s" if length is not None else "N/A"
    probs = {
        "Stream": beatmap_data.get('stream_prob'), "Jump": beatmap_data.get('jump_prob'),
        "Alt": beatmap_data.get('alt_prob'), "Tech": beatmap_data.get('tech_prob'),
    }
    probs_list = [f"{k}: {v:.2%}" for k, v in probs.items() if v is not None]
    
    parts = [
        f"谱面ID: {beatmap_data.get('bid')} ({beatmap_data.get('beatmap_status', 'N/A').capitalize()})",
        f"标题: {beatmap_data.get('artist', 'N/A')} - {beatmap_data.get('title', 'N/A')} [{beatmap_data.get('diff_name', 'N/A')}]",
        f"Mapper: {beatmap_data.get('creator_username', 'N/A')}",
        f"BPM: {beatmap_data.get('bpm')} | ★: {float(beatmap_data.get('star_rating', 0.0)):.2f} | 时长: {length_str}",
        f"CS: {beatmap_data.get('cs')} | AR: {beatmap_data.get('ar')} | OD: {beatmap_data.get('od')} | HP: {beatmap_data.get('hp')}",
        f"链接: https://osu.ppy.sh/b/{beatmap_data.get('bid')}",
        f"谱面库分类: 【{beatmap_data.get('determined_b_type', '未知').upper()}】",
        f"Oracle概率: {', '.join(probs_list) if probs_list else '无详细概率数据'}",
        ""
    ]
    if recommendations:
        for rec in recommendations:
            time_str = rec.get('recommended_at').strftime('%Y年%m月%d日') if isinstance(rec.get('recommended_at'), datetime) else "某时"
            rec_user = rec.get('osu_username_at_recommend_time', '一位热心玩家')
            rec_desc = rec.get('recommendation_description')

            base_text = f"{rec_user} 在{time_str}推荐了这张图"
            
            if rec_desc == "TA没有填写描述！":
                parts.append(f"{base_text}！")
            else:
                parts.append(f"{base_text}：{rec_desc}")
    else:
        parts.append("还没有人推过这张图呢！")
        
    return "\n".join(parts)

# --- 命令处理器 ---
random_recommend_matcher = on_command("随机推图", aliases={"roll图", "抽图", "随机谱面", "随机推荐", "suiji", "随机"}, priority=10, block=True)

@random_recommend_matcher.handle()
async def handle_random_recommend_command(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    """处理 /随机推图 命令，根据结果数量选择发送方式。"""
    parsed_args = parse_random_query_args(args.extract_plain_text().strip())
    
    sql, params = build_sql_query(parsed_args)
    results = await execute_random_recommend_query(sql, params)

    if not results:
        await random_recommend_matcher.finish("没有找到符合你条件的谱面！尝试放宽一点筛选条件吧")

    # --- 根据结果数量决定发送方式 ---
    if len(results) == 1:
        # --- 情况1：只找到一张图，直接发送 ---
        beatmap_data = results[0]
        bid = beatmap_data.get('bid')

        official_info, recs = await asyncio.gather(
            get_official_beatmap_info(bid),
            get_beatmap_recommendations_sample(bid)
        )
        text_part = format_beatmap_result_for_display(beatmap_data, recs)
        
        response_msg = Message()
        if official_info and (cover_url := official_info.get("beatmapset", {}).get("covers", {}).get("cover@2x")):
            try:
                response_msg.append(MessageSegment.image(cover_url))
            except Exception as e:
                logger.warning(f"发送单张谱面封面图失败 (bid: {bid}): {e}")
        
        response_msg.append(text_part)
        await random_recommend_matcher.send(response_msg)

    else:
        # --- 情况2：找到多张图，合并转发 ---
        forward_nodes = []
        bot_uin = bot.self_id
        bot_name = "Kon! Bot"

        for beatmap_from_db in results:
            bid = beatmap_from_db.get('bid')
            if not bid: continue

            official_info, recs = await asyncio.gather(
                get_official_beatmap_info(bid),
                get_beatmap_recommendations_sample(bid)
            )
            text_part = format_beatmap_result_for_display(beatmap_from_db, recs)
            
            content_msg = Message()
            if official_info and (cover_url := official_info.get("beatmapset", {}).get("covers", {}).get("cover@2x")):
                try:
                    content_msg.append(MessageSegment.image(cover_url))
                except Exception as e:
                    logger.warning(f"添加到合并转发时，发送谱面封面图失败 (bid: {bid}): {e}")
            
            content_msg.append(text_part)

            map_node = {
                "type": "node",
                "data": {"name": bot_name, "uin": bot_uin, "content": content_msg}
            }
            forward_nodes.append(map_node)

        try:
            if event.message_type == "group":
                await bot.send_group_forward_msg(group_id=event.group_id, messages=forward_nodes)
            elif event.message_type == "private":
                await bot.send_private_forward_msg(user_id=event.user_id, messages=forward_nodes)
        except Exception as e:
            logger.error(f"发送合并转发消息失败: {e}")
            await random_recommend_matcher.send("发送结果时出错，请联系管理员。")

    logger.info(f"成功为QQ用户 {event.get_user_id()} 推荐了 {len(results)} 张随机谱面。")