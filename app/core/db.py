# ======================================================================
# 数据库基础设施：用 SQLAlchemy 连接 SQLite，提供「引擎 / 会话 / 建表」入口
# 业务数据、联系留言、图谱三元组都放在同一个 SQLite 文件里
# ======================================================================
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings

# 1) 读取配置里的数据库连接串（例如 sqlite:///./app/data/portal.db）
settings = get_settings()

# 2) 创建引擎（engine）。check_same_thread=False 让 FastAPI 的多个线程能共用连接
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # SQLite 默认只允许单线程，关掉以适应 Web 并发
    future=True,                                # 使用 SQLAlchemy 2.0 风格 API
)

# 3) 会话工厂：每次请求从这里拿一个 Session 来操作数据库
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,   # 不自动刷写，写操作更可控
    autocommit=False,  # 手动提交，避免误写
    future=True,
)

# 4) 所有 ORM 模型都继承这个 Base（models.py 里会用到它定义表）
Base = declarative_base()


def init_db() -> None:
    """建表：应用启动时调用一次，创建所有表（已存在则跳过）。"""
    # 导入模型模块，让表结构注册到 Base.metadata 上
    from app.data import models  # noqa: F401  仅用于触发模型注册
    Base.metadata.create_all(bind=engine)
