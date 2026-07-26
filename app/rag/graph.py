# ======================================================================
# M4 知识图谱（轻量版，不引 Neo4j）
#
# 数据来自数据库 graph_triples 表（主体-关系-客体三元组），
# 在内存里用 networkx 构造成有向图，提供：
#   1) 邻居查询：某实体关联了哪些实体/关系
#   2) 多跳子图：给定起点，沿关系走 N 步，给 graph-RAG 提供多跳上下文
#   3) 可视化导出：to_cytoscape() 输出前端 Cytoscape.js 要的 nodes/edges JSON
#
# 纯逻辑、无外部依赖（不调 LLM），可独立单测。
# ======================================================================
import networkx as nx

from app.core.db import SessionLocal
from app.data.models import GraphTriple


def build_graph() -> nx.DiGraph:
    """从数据库三元组表加载，构造成有向图。

    同一对(主体,客体)若有多条关系，关系用「/」拼起来，避免覆盖。
    """
    g = nx.DiGraph()
    with SessionLocal() as db:
        triples = db.query(GraphTriple).all()
        for t in triples:
            # 节点不存在则创建（附带 label 方便前端显示）
            if not g.has_node(t.subject):
                g.add_node(t.subject, label=t.subject)
            if not g.has_node(t.obj):
                g.add_node(t.obj, label=t.obj)
            # 边：把关系写进 edge 的 relation 属性
            if g.has_edge(t.subject, t.obj):
                existing = g.edges[t.subject, t.obj]["relation"]
                g.edges[t.subject, t.obj]["relation"] = f"{existing}/{t.relation}"
            else:
                g.add_edge(t.subject, t.obj, relation=t.relation)
    return g


def neighbors(entity: str, graph: nx.DiGraph | None = None) -> list[dict]:
    """查某实体的直接邻居（出边 + 入边），返回 [{entity, relation, direction}]。"""
    g = graph or build_graph()
    if entity not in g:
        return []
    result = []
    # 出边：entity --relation--> 其它
    for _, nxt, data in g.out_edges(entity, data=True):
        result.append({"entity": nxt, "relation": data["relation"], "direction": "out"})
    # 入边：其它 --relation--> entity
    for prev, _, data in g.in_edges(entity, data=True):
        result.append({"entity": prev, "relation": data["relation"], "direction": "in"})
    return result


def multi_hop(entity: str, max_depth: int = 2, graph: nx.DiGraph | None = None) -> nx.DiGraph:
    """从 entity 出发沿关系走最多 max_depth 步，返回截出来的子图（graph-RAG 用）。"""
    g = graph or build_graph()
    if entity not in g:
        return nx.DiGraph()
    # 收集 N 跳内能到达的所有节点
    visited = {entity}
    frontier = {entity}
    for _ in range(max_depth):
        nxt_frontier = set()
        for node in frontier:
            for nb in list(g.successors(node)) + list(g.predecessors(node)):
                if nb not in visited:
                    visited.add(nb)
                    nxt_frontier.add(nb)
        frontier = nxt_frontier
        if not frontier:
            break
    return g.subgraph(visited).copy()


def search_triples(query: str, graph: nx.DiGraph | None = None) -> list[dict]:
    """按关键词在三元组里检索相关关系，给「图谱 worker」用。

    做法：把查询拆词，凡是主体/客体/关系里命中任意一词的三元组都返回，
    按命中词数排序（命中越多越相关）。对应问题如「许成合会哪些技术」「他做过什么项目」。
    """
    g = graph or build_graph()
    # 拆词：中英文都按非中文/非字母数字切，简单够用
    import re
    tokens = [t for t in re.split(r"[\s，。、？！,.:；;]+", query) if len(t) >= 1]
    hits = []
    with SessionLocal() as db:
        triples = db.query(GraphTriple).all()
    for t in triples:
        text = f"{t.subject} {t.relation} {t.obj}"
        score = sum(1 for tok in tokens if tok and tok in text)
        if score > 0:
            hits.append({
                "subject": t.subject,
                "relation": t.relation,
                "obj": t.obj,
                "score": score,
            })
    # 命中词多的排前面
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits


def to_cytoscape(graph: nx.DiGraph | None = None) -> dict:
    """导出 Cytoscape.js 需要的格式：{elements:[{data:{id,label}}, {data:{id,source,target,label}}]}。"""
    g = graph or build_graph()
    elements = []
    for n, data in g.nodes(data=True):
        elements.append({"data": {"id": n, "label": data.get("label", n)}})
    for u, v, data in g.edges(data=True):
        # 边 id 用 起点->终点 保证唯一
        elements.append({
            "data": {
                "id": f"{u}->{v}",
                "source": u,
                "target": v,
                "label": data.get("relation", ""),
            }
        })
    return {"elements": elements}
