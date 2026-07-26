# ======================================================================
# 应用装配根（composition root）
# 整个 FastAPI 应用的「入口装配点」，职责只有一件事：把零散模块拼成可运行应用。
#
# 资深视角（为什么这样分层）：
#   - 用工厂函数 create_app() 创建实例，测试时可单独构造、可注入配置，不依赖全局单例；
#   - 所有「wiring」（路由挂载、模板引擎、限流器、静态资源）只发生在这里，
#     其余模块只管自己的业务，互不 import 对方，解耦清晰、便于单测和排错；
#   - 启动初始化收敛到 lifespan 一个地方，关闭清理也在这里，生命周期一目了然。
# ======================================================================
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 限流（防刷）：SlowAPI。按客户端 IP 限流，关键路由再用装饰器细化粒度。
# 注意：这里只装「基础设施」，不设置全局默认额度，避免把 /health 探针也限流掉。
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core.limiter import limiter  # 限流器单例（定义见 app/core/limiter.py）

# 本项目的各个模块（只 import，不在本文件写业务逻辑）
from app.core import db
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.routers import health, pages, assistant, admin
from app.rag import api as ai_api  # AI 模块路由（#21：RAG / Agent / 多Agent / 图谱数据）

# 读取一次配置（全局复用同一份，避免重复解析环境变量）
settings = get_settings()
log = get_logger("app")

# 模板引擎：页面路由通过 request.app.state.templates 拿到它渲染 HTML。
# 目录先建好（哪怕暂时空），否则启动时 StaticFiles / Jinja2Templates 会因目录不存在报错。
templates = Jinja2Templates(directory="app/templates")

    # 限流器：已在 app.core.limiter 定义并导入为 limiter（key=客户端 IP）；
    # 各路由用 @limiter.limit(...) 控制额度（如 AI 接口限更严，防止烧钱）。


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动做初始化，关闭做清理。

    这里只放「全局一次性」的初始化，业务相关的懒加载不要塞进来。
    """
    # 1) 配置结构化日志（JSON 打到 stdout，容器运行时由 docker 接管）
    configure_logging(settings.log_level)
    # 2) 建表（SQLite 表不存在则创建，已存在则跳过；业务/留言/图谱三张表都在这步建好）
    db.init_db()
    # 2.1) 板块内容种子：表为空才写入占位内容，绝不覆盖你之后填的真实简历
    from app.data import seed

    seed.seed_if_empty()
    log.info("app.startup", env=settings.environment, app=settings.app_name)
    yield
    # 3) 关闭时的清理逻辑写这里（当前无需要释放的资源）


def create_app() -> FastAPI:
    """应用工厂：集中完成所有装配，返回可直接运行的 FastAPI 实例。"""
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="AI 应用开发门户：RAG + LangGraph + MCP + 知识图谱 一体化演示",
        lifespan=lifespan,
    )

    # 把模板引擎与限流器挂到 app 上，供路由模块共享（解耦：路由不自己 new 这些对象）
    app.state.templates = templates
    app.state.limiter = limiter

    # 限流超限的统一返回：返回 429，而不是直接抛 500
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # 挂载静态资源（CSS / JS / 图片）：URL 前缀 /static 映射到 app/static 目录
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # 装配路由：健康检查 + 页面 + AI 模块（#21）+ 助手对话 + 管理员后台
    app.include_router(health.router)
    app.include_router(pages.router)
    app.include_router(ai_api.router)
    app.include_router(assistant.router)   # 个人助手：流式问答（多agent/RAG/rerank）
    app.include_router(admin.router)       # 管理员：PDF 上传 / 实时更新向量库

    return app


# 模块级单例：uvicorn 直接 `import app.main:app` 就能拿到运行实例
app = create_app()
