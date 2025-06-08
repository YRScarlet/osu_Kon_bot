import httpx
import pymysql
import time
from typing import Optional, Dict, Any, Union

from nonebot import logger, get_plugin_config
from .config import Config

# 获取插件配置实例
plugin_config = get_plugin_config(Config)

# 用于缓存 Access Token 及其过期时间，避免重复请求
OSU_TOKEN_CACHE: Dict[str, Any] = {
    "access_token": None,
    "expires_at": 0,  # Token 过期的时间戳 (秒)
}
# 提前 5 分钟刷新 token，防止在临界点失效
OSU_TOKEN_REFRESH_BEFORE_EXPIRY_SECONDS = 300


def get_db_connection() -> Optional[pymysql.connections.Connection]:
    """
    建立并返回一个数据库连接。
    
    :return: pymysql 连接对象，如果失败则返回 None。
    """
    try:
        conn = pymysql.connect(
            host=plugin_config.db_host,
            port=plugin_config.db_port,
            user=plugin_config.db_user,
            password=plugin_config.db_password,
            database=plugin_config.db_name,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor  # 使查询结果以字典形式返回
        )
        return conn
    except pymysql.MySQLError as e:
        logger.error(f"数据库连接失败: {e}")
        return None


def get_proxied_http_client() -> httpx.AsyncClient:
    """
    根据配置创建并返回一个支持代理的 httpx.AsyncClient。
    
    :return: 配置好的 httpx.AsyncClient 实例。
    """
    proxy_url_to_use = None
    mounts_config = None

    if plugin_config.all_proxy:
        proxy_url_to_use = plugin_config.all_proxy
    elif plugin_config.https_proxy:
        proxy_url_to_use = plugin_config.https_proxy
    elif plugin_config.http_proxy:
        proxy_url_to_use = plugin_config.http_proxy

    if proxy_url_to_use:
        transport_with_proxy = httpx.AsyncHTTPTransport(proxy=proxy_url_to_use)
        mounts_config = {"all://": transport_with_proxy}
    
    # trust_env=False 确保不使用系统环境变量中的代理，完全由配置控制
    return httpx.AsyncClient(mounts=mounts_config, timeout=20.0, trust_env=False)


async def get_osu_token() -> Optional[str]:
    """
    获取 osu! API v2 Access Token。
    优先从缓存中读取，如果 Token 不存在或即将过期，则重新请求。
    
    :return:有效的 Access Token 字符串，如果失败则返回 None。
    """
    current_time = time.time()
    
    # 检查缓存中的 token 是否仍然有效
    if (OSU_TOKEN_CACHE["access_token"] and 
        OSU_TOKEN_CACHE["expires_at"] > current_time + OSU_TOKEN_REFRESH_BEFORE_EXPIRY_SECONDS):
        logger.debug("使用已缓存的 osu! API Access Token")
        return OSU_TOKEN_CACHE["access_token"]

    if not plugin_config.osu_client_id or not plugin_config.osu_client_secret:
        logger.error("osu! Client ID 或 Client Secret 未配置！")
        return None

    token_url = "https://osu.ppy.sh/oauth/token"
    payload = {
        "client_id": plugin_config.osu_client_id,
        "client_secret": plugin_config.osu_client_secret,
        "grant_type": "client_credentials",
        "scope": "public",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        async with get_proxied_http_client() as client:
            logger.info("正在尝试获取新的 osu! API Access Token...")
            response = await client.post(token_url, data=payload, headers=headers)
            response.raise_for_status()
            token_data = response.json()
            
            access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 0)

            if access_token:
                # 更新缓存
                OSU_TOKEN_CACHE["access_token"] = access_token
                OSU_TOKEN_CACHE["expires_at"] = time.time() + expires_in
                logger.info("成功获取并缓存 osu! API Access Token")
                return access_token
            else:
                logger.error("未能从 osu! API 响应中获取 access_token")
                return None
    except httpx.HTTPStatusError as e:
        logger.error(f"获取 osu! Token HTTP 错误: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.error(f"获取 osu! Token 时发生错误: {e}")
    
    return None


async def get_official_beatmap_info(beatmap_id: int) -> Optional[Dict[str, Any]]:
    """
    根据 beatmap_id 从 osu! 官方 API 查询谱面信息。
    
    :param beatmap_id: 谱面ID。
    :return: 包含谱面信息的字典，如果失败则返回 None。
    """
    token = await get_osu_token()
    if not token:
        return None

    api_url = f"https://osu.ppy.sh/api/v2/beatmaps/{beatmap_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    
    logger.info(f"正在从官方 API 获取谱面信息 (bid: {beatmap_id})")
    try:
        async with get_proxied_http_client() as client:
            response = await client.get(api_url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:  # Token 失效
            logger.warning("获取谱面信息时遇到401错误，Token可能已过期，将强制刷新后重试...")
            OSU_TOKEN_CACHE["expires_at"] = 0  # 强制使缓存过期
            # 不再需要手动重试逻辑，下次调用 get_osu_token() 会自动刷新
        logger.error(f"查询谱面信息 HTTP 错误: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.error(f"查询谱面信息时发生未知错误: {e}")
        
    return None


async def get_oracle_classification(beatmap_id: int, return_raw_probs: bool = False) -> Optional[Union[str, Dict[str, float]]]:
    """
    根据 beatmap_id 查询 osu!oracle 的谱面分类。
    
    :param beatmap_id: 谱面ID。
    :param return_raw_probs: 若为 True，返回原始概率字典；否则返回格式化后的字符串。
    :return: 格式化字符串、概率字典或在失败时返回特定错误字符串/None。
    """
    if not plugin_config.osu_oracle_api_url:
        logger.warning("osu!oracle API URL 未在配置中设置。")
        return "查询失败 (Oracle API未配置)" if not return_raw_probs else None

    payload = {"beatmap_ids": [str(beatmap_id)]}

    try:
        async with get_proxied_http_client() as client:
            logger.info(f"向 osu!oracle 查询 bid: {beatmap_id}")
            # 增加超时时间以应对模型预测耗时
            response = await client.post(plugin_config.osu_oracle_api_url, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()

            # 处理 API 可能的错误返回格式
            if "error" in data:
                logger.error(f"osu!oracle API 返回错误: {data['error']}")
                return "查询失败 (Oracle API错误)" if not return_raw_probs else None
            if "detail" in data:
                error_detail = data['detail'][0]['msg'] if isinstance(data['detail'], list) else data['detail']
                logger.error(f"osu!oracle API 请求体错误: {error_detail}")
                return "查询失败 (Oracle API请求错误)" if not return_raw_probs else None

            beatmap_id_str = str(beatmap_id)
            if beatmap_id_str in data and isinstance(data[beatmap_id_str], dict):
                predictions = data[beatmap_id_str]
                if return_raw_probs:
                    return predictions

                if not predictions:
                    return "未知类型 (Oracle无详细分类)"
                
                sorted_predictions = sorted(predictions.items(), key=lambda item: item[1], reverse=True)
                details = [f"{name.capitalize()}: {prob:.2%}" for name, prob in sorted_predictions]
                
                return ", ".join(details) if details else "无详细分类数据 (Oracle)"
            else:
                logger.warning(f"osu!oracle 未返回 bid {beatmap_id} 的有效预测结果: {data}")
                return "查询无结果 (Oracle)" if not return_raw_probs else {}

    except httpx.HTTPStatusError as e:
        logger.error(f"请求 osu!oracle 服务失败: HTTP {e.response.status_code} - {e.response.text}")
        return f"查询失败 (HTTP {e.response.status_code})" if not return_raw_probs else None
    except httpx.RequestError as e:
        logger.error(f"请求 osu!oracle 服务时发生网络错误: {e}")
        return "查询失败 (网络错误)" if not return_raw_probs else None
    except Exception as e:
        logger.error(f"调用 osu!oracle 时发生未知错误: {e}")
        return "查询失败 (未知错误)" if not return_raw_probs else None