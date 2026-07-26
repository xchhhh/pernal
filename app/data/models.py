# ======================================================================
# 数据模型（ORM）：用 Python 类描述数据库表结构
# 三张核心表：① 内容板块（9 大板块都存这里）② 联系留言 ③ 图谱三元组
# ======================================================================
from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.core.db import Base


class ContentSection(Base):
    """门户的内容板块（对应那 9 个板块）：一条记录 = 一个板块。

    用一张通用的表存所有板块，比建 9 张表灵活——
    RAG / MCP / 页面展示都从这张表按 section_key 取数据。
    """

    __tablename__ = "content_sections"

    id = Column(Integer, primary_key=True)                 # 主键
    section_key = Column(String(50), unique=True, index=True)  # 板块标识，如 basics/projects/skills
    title = Column(String(200))                             # 板块标题（页面展示用）
    # body 存板块正文；用 JSON 便于存结构化字段（如项目的角色/技术栈/链接）
    body = Column(JSON, nullable=True)                     # 结构化内容（dict / list 都行）
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),   # 首次插入时间
        onupdate=func.now(),         # 每次更新自动刷新
    )


class ContactMessage(Base):
    """联系表单留言：访客提交后落地到这里（SMTP 配了再发邮件）。"""

    __tablename__ = "contact_messages"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)    # 访客名字
    email = Column(String(200), nullable=False)    # 访客邮箱
    message = Column(Text, nullable=False)         # 留言内容
    is_spam = Column(Boolean, default=False)       # 是否疑似垃圾（限流/校验后标记）
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),   # 提交时间
    )


class GraphTriple(Base):
    """知识图谱三元组：主体-关系-客体，例如 (Python, 用于, 项目A)。

    M4 知识图谱的数据源；networkx 会在内存里加载这些三元组做遍历。
    """

    __tablename__ = "graph_triples"

    id = Column(Integer, primary_key=True)
    subject = Column(String(200), index=True)    # 主体（如「Python」）
    relation = Column(String(100), index=True)   # 关系（如「用于」）
    obj = Column(String(200), index=True)        # 客体（如「项目A」）
    source_section = Column(String(50), nullable=True)  # 来源板块，方便溯源


class IngestedDoc(Base):
    """管理员上传的文档（PDF 解析后的原文 + 来源名），供重建索引/审计用。

    上传时：解析出的文本存这里，同时切块实时写进向量库（Chroma+BM25）。
    重建索引时：从这张表把所有上传文档重新切块入库，保证向量库和源文件一致。
    """

    __tablename__ = "ingested_docs"

    id = Column(Integer, primary_key=True)
    source_name = Column(String(200), index=True)   # 来源标识（如上传文件名）
    text = Column(Text)                              # 解析后的正文（Markdown/文本）
    chunk_count = Column(Integer, default=0)         # 当时切成了多少块
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
