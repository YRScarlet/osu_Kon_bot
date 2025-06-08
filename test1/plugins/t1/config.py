from pydantic import BaseModel, Extra
from typing import Optional

class Config(BaseModel, extra=Extra.ignore):
    """
    插件的配置模型。
    NoneBot 会自动从 .env 文件或配置中读取并填充这些字段。
    """
    # --- osu! API 相关配置 ---
    osu_client_id: int = 40940  
    osu_client_secret: str  
    
    osu_oracle_api_url: str = "http://localhost:7777/predict"

    # --- 数据库连接配置 ---
    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str 
    db_name: str = "kon_bot_db"

    # --- 网络代理配置 ---
    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None
    all_proxy: Optional[str] = None