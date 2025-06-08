from typing import Optional

from nonebot import on_command, logger
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

from .utils import get_official_beatmap_info, get_oracle_classification
from .__init__ import __plugin_meta__

# 创建 /bid 命令的事件响应器
bid_matcher = on_command("bid", aliases={"谱面信息", "查询谱面"}, priority=10, block=True)

@bid_matcher.handle()
async def handle_bid_command(event: MessageEvent, args: Message = CommandArg()):
    """处理 /bid 命令，查询并显示谱面信息"""
    arg_text = args.extract_plain_text().strip()
    if not arg_text:
        await bid_matcher.finish("请输入谱面ID (bid)！\n使用方法：" + __plugin_meta__.usage)

    try:
        beatmap_id = int(arg_text)
    except ValueError:
        await bid_matcher.finish("谱面ID必须是数字！")
        return

#    await bid_matcher.send(f"正在查询 bid: {beatmap_id} 的信息，请稍候...")

    # 并行获取官方信息和 Oracle 分类
    info, oracle_classification_display = await asyncio.gather(
        get_official_beatmap_info(beatmap_id),
        get_oracle_classification(beatmap_id)
    )

    if info:
        # 提取谱面信息
        beatmapset = info.get("beatmapset", {})
        title = beatmapset.get("title", "N/A")
        artist = beatmapset.get("artist", "N/A")
        creator = beatmapset.get("creator", "N/A")
        version = info.get("version", "N/A")
        status = info.get("status", "N/A").capitalize()
        bpm = info.get("bpm", "N/A")
        cs = info.get("cs", "N/A")
        ar = info.get("ar", "N/A")
        od = info.get("accuracy", "N/A")
        hp = info.get("drain", "N/A")
        stars = info.get("difficulty_rating", 0.0)
        beatmap_url = info.get("url", f"https://osu.ppy.sh/b/{beatmap_id}")

        # 格式化星级
        stars_str = f"{stars:.2f}" if isinstance(stars, (int, float)) else "N/A"

        # 构建回复消息
        response_text = (
            f"谱面信息 ({status}):\n"
            f"标题: {artist} - {title} [{version}]\n"
            f"Mapper: {creator}\n"
            f"BPM: {bpm} | ★: {stars_str}\n"
            f"CS: {cs} | AR: {ar} | OD: {od} | HP: {hp}\n"
            f"链接: {beatmap_url}"
        )
        
        # 添加 Oracle 分类信息
        if isinstance(oracle_classification_display, str):
            response_text += f"\nOracle分类: {oracle_classification_display}"
        else:
            response_text += "\nOracle分类: 未查询到或查询失败"
            
        await bid_matcher.send(Message(response_text))
    else:
        # 即使官方API查询失败，也尝试显示 Oracle 分类
        error_msg = f"未能查询到 bid: {beatmap_id} 的官方信息。"
        if isinstance(oracle_classification_display, str):
            error_msg += f"\nOracle分类: {oracle_classification_display}"
        else:
            error_msg += "\nOracle分类: 未查询到或查询失败"
        await bid_matcher.send(error_msg)