# ======================================================================
# 内容服务：页面和后面的 RAG / MCP 都从这里读「板块内容」
#
# 设计原则（资深视角）：
#   - 单一数据源 = 数据库 ContentSection 表。模板不写死数据，RAG/MCP 以后也查同一张表。
#   - 这里只做「读」，写（填简历）由 seed 脚本或后续管理接口负责。
#   - 返回纯 dict，避免把 ORM 对象泄漏到模板层（也避免会话关闭后的懒加载报错）。
# ======================================================================
from app.core.db import SessionLocal
from app.data.models import ContentSection


def get_all_sections() -> dict:
    """读出全部板块，返回 {section_key: {"title":..., "body":...}}。

    空表时返回空 dict；此时模板应渲染占位提示，而不是崩溃。
    """
    with SessionLocal() as db:
        rows = db.query(ContentSection).all()
        return {r.section_key: {"title": r.title, "body": r.body} for r in rows}


def get_section(key: str) -> dict | None:
    """读单个板块；不存在返回 None，调用方自行决定兜底。"""
    with SessionLocal() as db:
        row = db.query(ContentSection).filter_by(section_key=key).first()
        if row is None:
            return None
        # 直接构造 dict 返回，会话关闭后模板仍可安全使用
        return {"key": row.section_key, "title": row.title, "body": row.body}
