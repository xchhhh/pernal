# ======================================================================
# 管理员鉴权（极简但够用）：签名 Cookie
#
# 思路：不引用户表、不引 JWT 库。登录时校验「管理员口令」是否等于配置的
# ADMIN_TOKEN；一致就下发一个「口令的 HMAC 签名」作为 Cookie（不存明文口令）。
# 之后每个管理员接口都校验这个签名是否有效——伪造不了（不知道 SECRET_KEY 就签不出）。
#
# 用 hmac + SECRET_KEY（配置里已有），零额外依赖，初学者也好懂。
# ======================================================================
import hashlib
import hmac

from fastapi import Cookie, HTTPException, Request

from app.core.config import get_settings

AUTH_COOKIE = "portal_admin"  # Cookie 名


def _sign(value: str) -> str:
    """用 SECRET_KEY 对 value 做 HMAC-SHA256 签名（返回十六进制串）。"""
    s = get_settings()
    return hmac.new(s.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_cookie_value() -> str:
    """生成要下发的 Cookie 值 = 管理员口令的签名。"""
    return _sign(get_settings().admin_token)


def is_authed(admin_auth: str | None = Cookie(default=None, alias=AUTH_COOKIE)) -> bool:
    """判断请求是否带有效签名 Cookie。

    关键：alias=AUTH_COOKIE 不能省！本函数被当作 FastAPI 依赖注入时，
    FastAPI 默认按「参数名」找 Cookie（会去找名叫 admin_auth 的 Cookie，
    而真实 Cookie 叫 portal_admin → 永远拿不到 → 管理员 API 永远 403）。
    alias 显式告诉 FastAPI 真正的 Cookie 名；作为普通函数直接调用时
    （页面路由里传位置参数），alias 无影响，两种用法都正确。
    """
    if not admin_auth:
        return False
    # compare_digest 防止时序攻击（比普通 == 更安全）
    return hmac.compare_digest(admin_auth, make_cookie_value())


def require_admin(admin_auth: str | None = Cookie(default=None, alias=AUTH_COOKIE)) -> bool:
    """FastAPI 依赖：未登录/签名失效直接返回 403。挂到管理员接口上即可。

    同样必须 alias=AUTH_COOKIE（见 is_authed 注释），否则 API 永远 403，
    前端收到 403 又跳登录页、登录页发现已登录又跳回 /admin → 无限刷新转圈。
    """
    if not is_authed(admin_auth):
        raise HTTPException(status_code=403, detail="未登录或登录已失效，请先登录")
    return True
