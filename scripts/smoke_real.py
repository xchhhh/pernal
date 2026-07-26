# 全应用真实内容冒烟测试（不联网）+ tiktoken 参数检测
import os  # 导入标准库 os，用于获取文件路径
import sys  # 导入标准库 sys，用于修改模块搜索路径

# 把 portfolio 根目录加到模块搜索路径，这样下面才能 import app 包（解决沙箱里 ModuleNotFoundError）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # FastAPI 自带的测试客户端，无需真正起服务即可请求接口
from langchain_openai import OpenAIEmbeddings  # 仅用来检测 tiktoken 参数是否受支持
from app.main import app  # 导入我们自己的 FastAPI 应用实例

with TestClient(app) as c:
    home = c.get("/").text
    assert "许成合" in home, "首页缺真实姓名"
    print("首页含真实姓名 许成合: OK")
    for p in ["/about", "/projects", "/resume", "/graph"]:
        r = c.get(p)
        assert r.status_code == 200, (p, r.status_code)
        print("GET", p, "-> 200")
    # /api/graph-data 返回的是 Cytoscape 格式 {"elements":[{data:{id,label}}, {data:{id,source,target,label}}]}
    # 节点 = 不带 source 的元素；边 = 带 source 的元素。注意：不是 {"nodes":[], "edges":[]} 结构。
    gd = c.get("/api/graph-data").json()
    elements = gd.get("elements", [])
    nodes = [e for e in elements if "source" not in e.get("data", {})]  # 没有 source 的就是节点
    edges = [e for e in elements if "source" in e.get("data", {})]      # 有 source 的是边
    print("图谱节点数:", len(nodes), "| 边关系类型:", list({e["data"].get("label", "") for e in edges})[:6])
    assert any("许成合" in n["data"]["label"] for n in nodes)
    print("图谱含真实实体 许成合: OK")
    sec = c.get("/api/sections").json()
    print("内容板块 API 返回板块数:", len(sec))
print("=== 全应用真实内容冒烟测试通过 ===")

# tiktoken 参数检测
try:
    OpenAIEmbeddings(model="deepseek-chat", api_key="x", base_url="https://api.deepseek.com/v1", tiktoken_enabled=False)
    print("tiktoken_enabled=False 受支持 -> VPS 部署可规避 tiktoken 下载")
except TypeError:
    print("tiktoken_enabled 参数不存在(版本差异) -> VPS 正常联网会自行下载, 无碍")
