# 验证助手问答：经公网 IP 触发一次真实问答，解析 SSE 的 trace + 回答
# 这会真正调用：火山 Ark Embedding + DeepSeek LLM + bge-reranker（首次会下载模型）
import json
import urllib.request

BASE = "http://114.132.53.126"

def post_sse(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=600)
    event = "message"; buf = ""
    trace = None; answer = ""
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            buf += line[5:].strip()
        elif line == "":
            if buf:
                try:
                    obj = json.loads(buf)
                except Exception:
                    obj = None
                if event == "trace" and obj:
                    trace = obj
                elif event == "token" and obj is not None:
                    answer += obj
            event = "message"; buf = ""
    return trace, answer

print("=== 健康检查 ===")
print(urllib.request.urlopen(BASE + "/health", timeout=30).read().decode()[:120])

for path, name in [("/assistant", "助手页"), ("/admin/login", "后台登录页")]:
    code = urllib.request.urlopen(BASE + path, timeout=30).getcode()
    print(f"{name} {path} -> HTTP {code}")

print("\n=== 触发一次真实问答（会调 Embedding+LLM+rerank）===")
trace, answer = post_sse("/api/assistant/chat", {"message": "许成合做过哪些 AI 相关的项目？"})
print("\n--- 思考过程(trace) ---")
print("查询改写:", trace.get("rewritten") if trace else None)
print("主管分派:", trace.get("plan") if trace else None)
if trace:
    for s in trace.get("agent_trace", []):
        print(f"  · {s['agent']}: {s['detail']}")
    r = trace.get("retrieval", {})
    print("向量召回数:", len(r.get("vector_top", [])), " BM25召回数:", len(r.get("bm25_top", [])))
    print("rerank 前:", r.get("rerank_before"))
    print("rerank 后:", r.get("rerank_after"))
print("\n--- 回答 ---")
print(answer[:1200])
