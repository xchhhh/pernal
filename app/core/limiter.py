# ======================================================================
# 限流器单例：集中定义，避免各路由模块重复创建、也避开 main.py 的导入顺序坑
# key 用客户端 IP；每个路由用 @limiter.limit("额度/分钟") 控制粗细。
# ======================================================================
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
