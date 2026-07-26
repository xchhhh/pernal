# ======================================================================
# MCP Server（A3）：用官方 MCP SDK 的 FastMCP 把 10 个工具暴露成标准 MCP 协议
#
# 部署形态：同容器 stdio —— LangGraph 智能体通过 MCP client 用 stdio 拉起本模块，
# 用完即走，零额外内存（契合 2c2g）。
#
# 这 10 个工具围着「9 板块 + 图谱 + 匹配度」转，是 Agent 的「手」。
# ======================================================================
from mcp.server.fastmcp import FastMCP

from app.rag import graph as graph_mod
from app.services import content

# 创建 MCP server 实例（名字会出现在客户端的工具来源里）
mcp = FastMCP("portal-ai")


def _section(key: str) -> dict:
    """取一个板块；没有就返回空结构，避免 Agent 拿到 None。"""
    s = content.get_section(key)
    return s if s else {"key": key, "title": key, "body": {}}


@mcp.tool()
def get_basics() -> dict:
    """获取个人定位 / 求职意向等基础信息。"""
    return _section("basics")


@mcp.tool()
def get_education() -> dict:
    """获取教育背景（学校/专业/时间/荣誉）。"""
    return _section("education")


@mcp.tool()
def get_skills(category: str = "") -> dict:
    """获取技术栈矩阵；可按类别筛选（如 category='语言'）。"""
    sec = _section("skills")
    if category and isinstance(sec.get("body"), dict):
        return {category: sec["body"].get(category, [])}
    return sec


@mcp.tool()
def get_projects() -> dict:
    """获取项目经历（角色/技术栈/亮点/链接）。"""
    return _section("projects")


@mcp.tool()
def get_experience() -> dict:
    """获取实习/工作经历。注意：当前板块尚未填写，返回空占位（向前兼容）。"""
    # 计划中该板块被砍掉待补，工具保留定义返回空，避免 Agent 调用报错
    return {"key": "experience", "title": "实习/工作经历", "body": {}, "note": "尚未填写"}


@mcp.tool()
def get_awards() -> dict:
    """获取获奖与证书。"""
    return _section("awards")


@mcp.tool()
def get_outputs() -> dict:
    """获取技术输出（GitHub/博客/开源贡献）。"""
    return _section("outputs")


@mcp.tool()
def search_resume(query: str) -> list[dict]:
    """跨板块关键词检索简历内容；返回命中的板块片段列表。"""
    all_sec = content.get_all_sections()
    hits = []
    q = query.lower()
    for key, sec in all_sec.items():
        blob = str(sec.get("body", "")).lower()
        if q in blob:
            hits.append({"section": key, "title": sec.get("title", key), "snippet": str(sec.get("body", ""))[:200]})
    return hits


@mcp.tool()
def get_graph_relations(entity: str) -> list[dict]:
    """查询知识图谱中某实体的关系（如'Python'关联了哪些项目）。返回 [{entity, relation, direction}]。"""
    return graph_mod.neighbors(entity)


@mcp.tool()
def compute_skill_match(jd_text: str) -> dict:
    """给定一段 JD 文本，计算与个人技术栈的匹配度：返回 matched(已具备) 与 missing(缺口)。"""
    skills_sec = _section("skills")
    # 把技术栈拍平成关键词集合（小写）
    known = set()
    body = skills_sec.get("body", {})
    if isinstance(body, dict):
        for vals in body.values():
            if isinstance(vals, list):
                known.update(str(v).lower() for v in vals)
            else:
                known.add(str(vals).lower())
    elif isinstance(body, list):
        known.update(str(v).lower() for v in body)
    # JD 里出现的技术词（简单按逗号/空格/常见分隔拆词，取长度>1 的片段）
    jd_tokens = {t.strip().lower() for t in jd_text.replace("，", ",").split(",") if len(t.strip()) > 1}
    matched = sorted(jd_tokens & known)
    missing = sorted(jd_tokens - known)
    score = round(len(matched) / max(1, len(jd_tokens)) * 100)
    return {"score": score, "matched": matched, "missing": missing}


# 作为独立进程被 stdio 拉起时，运行这个 server
if __name__ == "__main__":
    mcp.run()
