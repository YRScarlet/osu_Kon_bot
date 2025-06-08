import httpx
import pymysql
import re
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime
import asyncio

from nonebot import on_command, logger, get_plugin_config
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

from .utils import get_db_connection, get_osu_token, get_official_beatmap_info, get_oracle_classification, get_proxied_http_client
from .config import Config

plugin_config = get_plugin_config(Config)

# 类型别名和有效类型定义
TYPE_ALIASES: Dict[str, str] = {
    "串": "stream", "stream": "stream",
    "跳": "jump", "jump": "jump", "aim": "jump",
    "强双": "alt", "alt": "alt",
    "科技": "tech", "tech": "tech",
    "其他": "others", "其它": "others", "others": "others",
}
VALID_TYPES = set(TYPE_ALIASES.values())

# --- 辅助函数 (模块内专用) ---

async def get_user_binding_info(qqid: int) -> Optional[Dict[str, Any]]:
    """获取用户的 osu! 绑定信息 (osu_uid, osu_username_at_bind)"""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            sql = "SELECT osu_uid, osu_username_at_bind FROM UserBindings WHERE qqid = %s"
            cursor.execute(sql, (qqid,))
            return cursor.fetchone()
    except pymysql.MySQLError as e:
        logger.error(f"查询用户绑定信息失败: {e}")
        return None
    finally:
        if conn: conn.close()

async def get_user_recent_beatmap_id(osu_uid: int) -> Optional[int]:
    """获取用户最近游玩的谱面ID"""
    token = await get_osu_token()
    if not token: return None
    api_url = f"https://osu.ppy.sh/api/v2/users/{osu_uid}/scores/recent?limit=1&include_fails=1"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    
    try:
        async with get_proxied_http_client() as client:
            response = await client.get(api_url, headers=headers)
            response.raise_for_status()
            scores = response.json()
            if scores and isinstance(scores, list) and scores[0].get("beatmap", {}).get("id"):
                return int(scores[0]["beatmap"]["id"])
            logger.warning(f"用户 {osu_uid} 最近游玩记录为空或格式不正确: {scores}")
            return None
    except Exception as e:
        logger.error(f"获取用户 {osu_uid} 最近谱面失败: {e}")
        return None

async def fetch_and_store_beatmap_info(bid: int) -> Optional[Dict[str, Any]]:
    """获取官方谱面信息并存入 BeatmapInfo 表"""
    official_info = await get_official_beatmap_info(bid)
    if not official_info:
        return None

    conn = get_db_connection()
    if not conn: return official_info

    try:
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO BeatmapInfo (bid, title, artist, creator_username, creator_id, diff_name, star_rating, beatmap_status, ar, od, cs, hp, length_seconds, bpm, api_data_last_fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                title = VALUES(title), artist = VALUES(artist), creator_username = VALUES(creator_username), creator_id = VALUES(creator_id),
                diff_name = VALUES(diff_name), star_rating = VALUES(star_rating), beatmap_status = VALUES(beatmap_status), ar = VALUES(ar), 
                od = VALUES(od), cs = VALUES(cs), hp = VALUES(hp), length_seconds = VALUES(length_seconds), bpm = VALUES(bpm), 
                api_data_last_fetched_at = VALUES(api_data_last_fetched_at);
            """
            beatmapset = official_info.get("beatmapset", {})
            params = (
                bid,
                beatmapset.get("title", "N/A"),
                beatmapset.get("artist", "N/A"),
                beatmapset.get("creator"),
                beatmapset.get("user_id"),
                official_info.get("version", "N/A"),
                official_info.get("difficulty_rating", 0.0),
                official_info.get("status", "unknown"),
                official_info.get("ar"),
                official_info.get("accuracy"),
                official_info.get("cs"),
                official_info.get("drain"),
                official_info.get("total_length"),
                official_info.get("bpm"),
                datetime.now()
            )
            cursor.execute(sql, params)
            conn.commit()
    except pymysql.MySQLError as e:
        logger.error(f"存储谱面信息 {bid} 到数据库失败: {e}")
        if conn: conn.rollback()
    finally:
        if conn: conn.close()
    return official_info

async def get_oracle_analysis_results(bid: int) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """
    调用 osu!oracle API 获取原始概率，并根据规则判断类型。
    返回 (概率字典, oracle判定的类型字符串)
    """
    raw_probs = await get_oracle_classification(bid, return_raw_probs=True)
    if not isinstance(raw_probs, dict):
        return None, "others"

    if not raw_probs:
        return raw_probs, "others"

    normalized_probs = {}
    for key, prob in raw_probs.items():
        normalized_key = TYPE_ALIASES.get(key.lower(), key.lower())
        if normalized_key in VALID_TYPES:
            normalized_probs[normalized_key] = normalized_probs.get(normalized_key, 0) + prob

    if not normalized_probs:
        return raw_probs, "others"
    
    for type_name, probability in normalized_probs.items():
        if probability > 0.5:
            return raw_probs, type_name
            
    return raw_probs, "others"

async def store_beatmap_analysis(bid: int, probs: Optional[Dict[str, float]], 
                                 determined_type: str, is_auto_typed: bool):
    """存储或更新 BeatmapAnalysis 表。"""
    conn = get_db_connection()
    if not conn: return

    try:
        with conn.cursor() as cursor:
            # 只有当记录是自动分类时，才允许 /推图 命令更新其分类和状态
            sql = """
            INSERT INTO BeatmapAnalysis (bid, determined_b_type, is_auto_typed, stream_prob, jump_prob, alt_prob, tech_prob, oracle_last_run_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                determined_b_type = IF(is_auto_typed = 1, VALUES(determined_b_type), determined_b_type),
                is_auto_typed = IF(is_auto_typed = 1, VALUES(is_auto_typed), is_auto_typed),
                stream_prob = VALUES(stream_prob),
                jump_prob = VALUES(jump_prob),
                alt_prob = VALUES(alt_prob),
                tech_prob = VALUES(tech_prob),
                oracle_last_run_at = VALUES(oracle_last_run_at);
            """
            p = probs or {}
            params = (
                bid, determined_type, is_auto_typed,
                p.get("stream"), p.get("jump"), p.get("alt"), p.get("tech"),
                datetime.now() if probs is not None else None
            )
            cursor.execute(sql, params)
            conn.commit()
    except pymysql.MySQLError as e:
        logger.error(f"存储谱面分析 {bid} 到数据库失败: {e}")
    finally:
        if conn: conn.close()

async def store_beatmap_analysis_probabilities_only(bid: int, probs: Dict[str, float]):
    """对于已人工审核的谱面，仅更新其概率和Oracle运行时间。"""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            sql = """
            UPDATE BeatmapAnalysis SET 
                stream_prob = %s, jump_prob = %s, alt_prob = %s, tech_prob = %s, oracle_last_run_at = %s
            WHERE bid = %s AND is_auto_typed = 0
            """
            p = probs or {}
            params = (p.get("stream"), p.get("jump"), p.get("alt"), p.get("tech"), datetime.now(), bid)
            cursor.execute(sql, params)
            conn.commit()
    except pymysql.MySQLError as e:
        logger.error(f"仅更新谱面 {bid} 概率失败: {e}")
    finally:
        if conn: conn.close()

async def add_to_pending_review(bid: int, reason: str, recommendation_id: Optional[int] = None):
    """将谱面添加到待审核列表。"""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO PendingBeatmapReviews (bid, reason_for_pending, triggered_by_recommendation_id, added_to_queue_at)
            VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE
                reason_for_pending = VALUES(reason_for_pending),
                triggered_by_recommendation_id = VALUES(triggered_by_recommendation_id),
                added_to_queue_at = VALUES(added_to_queue_at);
            """
            cursor.execute(sql, (bid, reason, recommendation_id, datetime.now()))
            conn.commit()
    except pymysql.MySQLError as e:
        logger.error(f"添加谱面 {bid} 到待审核列表失败: {e}")
    finally:
        if conn: conn.close()

async def store_recommendation(
    qqid: int, bid: int, osu_username: Optional[str],
    user_req_type: Optional[str], actual_rec_type: str, description: str
) -> Optional[int]:
    """存储推荐记录并返回其ID。"""
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor() as cursor:
            sql = """
            INSERT INTO Recommendations (qqid, bid, osu_username_at_recommend_time, user_requested_type, 
                                         actual_recommend_type, recommendation_description, recommended_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (qqid, bid, osu_username, user_req_type, actual_rec_type, description, datetime.now()))
            conn.commit()
            return cursor.lastrowid
    except pymysql.MySQLError as e:
        logger.error(f"存储推荐记录失败: {e}")
        return None
    finally:
        if conn: conn.close()

def parse_recommend_args(arg_text: str) -> Tuple[Optional[str], Optional[int], str]:
    """智能解析推图命令的参数，返回 (用户指定类型, bid, 备注)"""
    parts = arg_text.split()
    user_type: Optional[str] = None
    bid: Optional[int] = None
    description_parts: List[str] = []
    
    used_indices = set()

    # 优先识别类型和BID
    for i, part in enumerate(parts):
        if part.isdigit() and bid is None:
            bid = int(part)
            used_indices.add(i)
            continue
        
        normalized_part = part.lower()
        if user_type is None:
            for alias, db_type in TYPE_ALIASES.items():
                if normalized_part == alias:
                    user_type = db_type
                    used_indices.add(i)
                    break
    
    # 剩余部分为备注
    for i, part in enumerate(parts):
        if i not in used_indices:
            description_parts.append(part)

    description = " ".join(description_parts) if description_parts else "TA没有填写描述！"
    
    return user_type, bid, description

# --- 命令处理器 ---
recommend_matcher = on_command("推图", aliases={"推荐图", "推荐", "rec"}, priority=10, block=True)

@recommend_matcher.handle()
async def handle_recommend_command(event: MessageEvent, args: Message = CommandArg()):
    """处理 /推图 命令"""
    qqid = int(event.get_user_id())

    # --- 1. 前置绑定检查 ---
    binding_info = await get_user_binding_info(qqid)
    if not binding_info:
        await recommend_matcher.finish(
            "请先使用 /konbind [你的osu!用户名] 绑定账号！"
        )
        return # 确保逻辑中断

    # --- 2. 解析参数并确定 BID ---
    arg_text = args.extract_plain_text().strip()
    user_specified_type, bid_from_arg, description_from_arg = parse_recommend_args(arg_text)
    
    final_bid = bid_from_arg
    if not final_bid:
        await recommend_matcher.send("你没有提供谱面ID，正在尝试获取你最近游玩的谱面...")
        osu_uid = binding_info["osu_uid"]
        final_bid = await get_user_recent_beatmap_id(osu_uid)
        if not final_bid:
            await recommend_matcher.finish("没有找到你最近游玩的谱面！")

    # --- 3. 获取并处理谱面数据 (后续逻辑保持不变) ---
    official_beatmap_data, (raw_oracle_probabilities, oracle_determined_type) = await asyncio.gather(
        fetch_and_store_beatmap_info(final_bid),
        get_oracle_analysis_results(final_bid)
    )

    if not official_beatmap_data:
        await recommend_matcher.finish(f"未能查询到谱面ID: {final_bid} 的官方信息！请检查ID是否正确")

    oracle_probs_text_list = []
    if raw_oracle_probabilities:
        for type_name, prob_val in sorted(raw_oracle_probabilities.items(), key=lambda item: item[1], reverse=True):
            oracle_probs_text_list.append(f"{type_name.capitalize()}: {prob_val:.2%}")
    oracle_probs_display_text = ", ".join(oracle_probs_text_list) if oracle_probs_text_list else "无详细概率数据"

    if oracle_determined_type is None:
        oracle_determined_type = "others"

    # 4. 确定最终推荐类型和数据库操作
    actual_recommend_type: str
    final_determined_b_type_for_db: str
    additional_messages: List[str] = []
    
    conn_check = get_db_connection()
    existing_analysis_info = None
    if conn_check:
        with conn_check.cursor() as cursor:
            cursor.execute("SELECT determined_b_type, is_auto_typed FROM BeatmapAnalysis WHERE bid = %s", (final_bid,))
            existing_analysis_info = cursor.fetchone()
        conn_check.close()
    
    is_auto_typed_in_db = existing_analysis_info.get("is_auto_typed", 1) == 1 if existing_analysis_info else True
    db_determined_b_type = existing_analysis_info.get("determined_b_type") if existing_analysis_info else None

    if not is_auto_typed_in_db:
        final_determined_b_type_for_db = db_determined_b_type
        if user_specified_type:
            actual_recommend_type = user_specified_type
            if user_specified_type != db_determined_b_type:
                additional_messages.append(
                    f"此谱面已被管理员定义为【{db_determined_b_type.upper()}】类型！\n"
                    f"记录了你的【{user_specified_type.upper()}】，但谱面库中分类不变"
                )
        else:
            actual_recommend_type = db_determined_b_type
        if raw_oracle_probabilities is not None:
             await store_beatmap_analysis_probabilities_only(final_bid, raw_oracle_probabilities)
    else:
        if user_specified_type:
            actual_recommend_type = user_specified_type
            final_determined_b_type_for_db = user_specified_type
            additional_messages.append(f"你已将这张图定为【{user_specified_type.upper()}】！")
            if oracle_determined_type and user_specified_type != oracle_determined_type:
                additional_messages.append(f"但osu!Oracle 分析认为此图更偏向【{oracle_determined_type.upper()}】！差异已记录")
                await add_to_pending_review(final_bid, reason=f"User specified '{user_specified_type}', oracle: '{oracle_determined_type}'")
            elif not raw_oracle_probabilities:
                 await add_to_pending_review(final_bid, reason="oracle_failed_user_specified_type")
                 additional_messages.append("osu!oracle 分析失败或未返回有效结果！")
        else:
            actual_recommend_type = oracle_determined_type
            final_determined_b_type_for_db = oracle_determined_type
            if not raw_oracle_probabilities: 
                additional_messages.append("osu!oracle 分析失败或未返回有效结果，谱面暂定为【OTHERS】！")
                await add_to_pending_review(final_bid, reason="oracle_ambiguous_or_failed_no_user_type")
            elif oracle_determined_type == "others":
                await add_to_pending_review(final_bid, reason="oracle_ambiguous_result")
        await store_beatmap_analysis(final_bid, raw_oracle_probabilities, final_determined_b_type_for_db, True)

    # 5. 存储推荐并反馈
    osu_username_for_rec = binding_info.get("osu_username_at_bind") # 直接使用已获取的绑定信息
    
    recommendation_id = await store_recommendation(
        qqid, final_bid, osu_username_for_rec, user_specified_type, actual_recommend_type, description_from_arg
    )
    
    if recommendation_id is None:
        await recommend_matcher.send("存储推荐记录到数据库失败，请联系YRScarlet！")
        return

    # 6. 构建并发送最终消息
    title = official_beatmap_data.get("beatmapset", {}).get("title", "N/A")
    artist = official_beatmap_data.get("beatmapset", {}).get("artist", "N/A")
    version = official_beatmap_data.get("version", "N/A")
    creator = official_beatmap_data.get("beatmapset", {}).get("creator", "N/A")
    status = official_beatmap_data.get("status", "N/A").capitalize()
    bpm = official_beatmap_data.get("bpm", "N/A")
    cs = official_beatmap_data.get("cs", "N/A")
    ar = official_beatmap_data.get("ar", "N/A")
    od = official_beatmap_data.get("accuracy", "N/A")
    hp = official_beatmap_data.get("drain", "N/A")
    stars = float(official_beatmap_data.get("difficulty_rating", 0.0))
    length_seconds = official_beatmap_data.get("total_length") 
    length_str = f"{length_seconds // 60}m{length_seconds % 60}s" if length_seconds is not None else "N/A"
    beatmap_url = official_beatmap_data.get("url", f"https://osu.ppy.sh/b/{final_bid}")
    cover_url = official_beatmap_data.get("beatmapset", {}).get("covers", {}).get("cover@2x")

    response_parts = [
        f"谱面ID: {final_bid} ({status})",
        f"标题: {artist} - {title} [{version}]",
        f"Mapper: {creator}",
        f"BPM: {bpm} | ★: {stars:.2f} | 时长: {length_str}",
        f"CS: {cs} | AR: {ar} | OD: {od} | HP: {hp}",
        f"{beatmap_url}",
        "",
        f"分析概率: {oracle_probs_display_text}",
        f"谱面库当前分类: 【{final_determined_b_type_for_db.upper()}】 ",
        "",
        f"你的备注: {description_from_arg}"
    ]
    
    if additional_messages:
        response_parts.extend(additional_messages)

    is_type_match_success = (user_specified_type and user_specified_type == actual_recommend_type) or \
                            (not user_specified_type and actual_recommend_type != "others" and raw_oracle_probabilities)

    response_parts.append("")
    if is_type_match_success:
        response_parts.append("✔ 推荐已成功记录，谱面信息已更新！")
    else:
        response_parts.append("✔ 推荐已记录")

    final_response_text = "\n".join(response_parts)
    response_msg = Message()
    if cover_url:
        try:
            response_msg.append(MessageSegment.image(cover_url))
        except Exception as e:
            logger.warning(f"发送谱面封面图失败: {e}")
    response_msg.append(final_response_text)
    await recommend_matcher.send(response_msg)