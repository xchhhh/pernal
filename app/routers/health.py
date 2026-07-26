# ======================================================================
# 健康检查路由：给 Caddy / docker-compose 判断「容器活着吗 / 能服务吗」
#   /health —— 存活探针：进程在就返回 200（不碰任何外部依赖）
#   /ready  —— 就绪探针：连得上关键依赖才返回 200，否则 503
# ======================================================================
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

router = APIRouter()
log = get_logger("health")


@router.get("/health")
def health() -> dict:
    """存活探针：只要进程还在就 OK，用于快速判断要不要重启容器。"""
    return {"status": "alive"}


@router.get("/ready")
def ready() -> JSONResponse:
    """就绪探针：检查关键依赖（这里是 SQLite 业务库）是否可用。

    只要任一依赖异常，就返回 503，让编排器先别把流量打过来。
    （Chroma 向量库的就绪检查会在接入 AI 模块时补上。）
    """
    checks: dict[str, str] = {}

    # 延迟导入：避免 db 还没准备好时，模块一加载就报错
    try:
        from app.core import db
        import sqlalchemy
        with db.engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001  任何异常都算未就绪
        log.warning("ready.check.failed", dep="database", error=str(e))
        checks["database"] = f"error: {e}"

    # 有任一依赖报错 → 503；否则 200
    if any(str(v).startswith("error") for v in checks.values()):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=checks,
        )
    return JSONResponse(content=checks)
