##  from nonebot import on_command
##  from nonebot.adapters.onebot.v11 import Message
##  
##  # 创建 help 命令的事件响应器
##  help_matcher = on_command(
##      "konhelp",  # 使用一个独特的命令名避免冲突
##      aliases={"帮助", "菜单", "help"},
##      priority=5,
##      block=True
##  )
##  
##  # 帮助信息文本
##  HELP_MESSAGE = """
##  Kon! Bot v0.1！！！
##  -------------------------------
##  /bid [谱面ID]
##  功能: 查询指定谱面的信息
##  例如: /bid 129891
##  -------------------------------
##  /konbind [你的osu!用户名]
##  功能: 绑定osu!账号
##  例如: /konbind YRScarlet
##  -------------------------------
##  /推荐 [类型] [谱面ID] [备注]
##  功能: 推荐一张你喜欢的图
##  类型: 串/stream, 跳/jump, 强双/alt, 科技/tech, 其他/others。若不指定，将通过 osu!Oracle 分析
##  谱面ID: 若不指定，则尝试获取你最近游玩的谱面
##  备注: 你对这张图的评价或说明
##  例如:
##    /推荐 串 3946158 经典Oh壁厚
##    /推荐 3197548 PP长跳图
##    /推荐 跳 好听
##    /推荐 草泥马左手按断了
##    (还可用/rec，功能一致)
##  -------------------------------
##  /随机推图 [类型] [数量=N] [筛选条件...]
##  功能: 从谱面库中随机推荐谱面
##  类型: 串/stream, 跳/jump, 强双/alt, 科技/tech, 其他/others
##  数量=N: 默认1，最多5
##  筛选条件: 如 stars=6-6.5, ar>=9, od=8, length<180, bpm>170
##  例如:
##    /随机推图
##    /随机推图 串 数量=3 stars=6.2-6.8 ar>=9.3
##    /随机推图 tech length<180
##    (还可用/suiji，/随机，功能一致)
##  """
##  
##  @help_matcher.handle()
##  async def handle_help_command():
##      """处理帮助命令，发送预设的帮助信息。"""
##      await help_matcher.send(Message(HELP_MESSAGE))

from pathlib import Path
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageSegment

# 创建 help 命令的事件响应器
help_matcher = on_command(
    "konhelp",  # 使用一个独特的命令名
    aliases={"帮助", "菜单", "help"},
    priority=5,
    block=True
)

# --- 图片路径定位 ---
HELP_IMAGE_PATH = Path(__file__).parent / "draw" / "helpdraw.jpg"


@help_matcher.handle()
async def handle_help_command():
    """
    处理帮助命令，发送预设的帮助图片。
    """
    image_message = MessageSegment.image(HELP_IMAGE_PATH.as_uri())
    await help_matcher.send(image_message)
