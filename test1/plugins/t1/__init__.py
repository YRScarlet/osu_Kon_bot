from nonebot import on_command
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import MessageSegment, MessageEvent

from .config import Config

# 定义插件元信息，用于 help 等场景
__plugin_meta__ = PluginMetadata(
    name="Kon! bot",
    description="一个 osu! 推图bot。",
    usage="""
提供以下 osu! 功能：
- /bid [谱面ID]: 查询谱面详细信息和 Oracle 分类。
- /konbind [用户名]: 绑定你的 osu! 账号。
- /konunbind: 解除 osu! 账号绑定。
- /推荐 [类型] [谱面ID] [备注]: 向谱面库推荐一张图。
- /随机推图 [条件]: 根据条件随机从库中推荐谱面。
- /pending [操作]: (管理员) 管理待审核谱面。
- /konhelp: 显示详细帮助信息。
- /笔哥: 机器人应答。
""",
    config=Config,
)

# 导入包含事件响应器的子模块，使其被 NoneBot 自动加载和注册
from . import beatmap_info
from . import bind
from . import recommend
from . import random_recommend
from . import admin_tool
from . import help

# 保留一个简单的命令在主文件中作为示例
hello_matcher = on_command("笔哥", priority=20, block=True)

@hello_matcher.handle()
async def handle_hello_command(event: MessageEvent):
    """处理 /笔哥 命令，简单回复"""
    user_id = event.get_user_id()
    response_msg = MessageSegment.at(user_id) + MessageSegment.text(" 在")
    await hello_matcher.send(response_msg)