# ======================================================================
# 日志配置：用 structlog 输出 JSON 结构化日志
# 好处：JSON 日志方便以后接 Loki/ELK 集中采集；现在先打到标准输出
# ======================================================================
import logging
import sys

import structlog


def configure_logging(log_level: str = "INFO") -> None:
    """配置全局日志：把标准库 logging 和 structlog 接到一起，输出 JSON。"""
    # 1) 设标准库根日志级别，structlog 会复用这一级别
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    # 2) structlog 渲染成 JSON（每行一条，便于机器解析）
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,   # 合并上下文字段（如 request_id）
            structlog.processors.add_log_level,        # 加 level 字段
            structlog.processors.TimeStamper(fmt="iso"),  # 加 ISO 时间戳
            structlog.processors.JSONRenderer(),       # 最终渲染成 JSON 字符串
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(),  # 打到 stdout（被 docker 接管）
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "ai_portal"):
    """业务代码统一用这个取带名字的结构化 logger。"""
    return structlog.get_logger(name)
