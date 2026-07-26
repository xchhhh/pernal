# 诊断：用 TestClient 直接打 /api/graph-data，打印原始返回，定位 0 节点根因
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.main import app
from app.core.db import SessionLocal
from app.data.models import GraphTriple

with TestClient(app) as c:
    # 1) 直接在同一个进程里看数据库有多少三元组
    with SessionLocal() as db:
        print("TestClient 上下文中 DB GraphTriple 行数:", db.query(GraphTriple).count())
    # 2) 调用真实接口
    r = c.get("/api/graph-data")
    print("HTTP 状态:", r.status_code)
    data = r.json()
    print("返回顶层 keys:", list(data.keys()))
    elems = data.get("elements", [])
    print("elements 数量:", len(elems))
    print("其中含 source 的边数量:", sum(1 for e in elems if "source" in e.get("data", {})))
    print("其中节点数量:", sum(1 for e in elems if "source" not in e.get("data", {})))
    if elems:
        print("样本:", elems[0])
