# 最终验证：docker stats 看 Chroma 内存 + 公网 IP 服务真实简历内容
import os, paramiko, json
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "id_ed25519")
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("114.132.53.126", 22, username="root", key_filename=KEY, timeout=20)

def sh(cmd):
    print(f"\n$ {cmd}")
    _, out, err = c.exec_command(cmd)
    t = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    if t.strip(): print(t.rstrip())
    if e.strip(): print("[stderr]", e.rstrip())
    return t

print("========== [A] docker stats（Chroma 内存核心指标）==========")
sh("docker stats --no-stream --format 'table {{.Name}}\\t{{.MemUsage}}\\t{{.MemPct}}\\t{{.CPUPerc}}'")

print("\n========== [B] 公网 IP 服务真实简历内容 ==========")
home = sh("curl -s http://localhost/ | grep -o '许成合' | head -1")
print("首页含'许成合':", "是 ✅" if home.strip() else "否 ❌")
# 图谱节点数
gd = sh("curl -s http://localhost/api/graph-data")
try:
    els = json.loads(gd)["elements"]
    nodes = sum(1 for e in els if "source" not in e.get("data", {}))
    print(f"知识图谱节点数: {nodes} ✅" if nodes else "知识图谱为空 ❌")
except Exception as ex:
    print("图谱解析失败:", ex)
# 板块数
sec = sh("curl -s http://localhost/api/sections")
try:
    cnt = len(json.loads(sec))
    print(f"内容板块数: {cnt} ✅" if cnt == 9 else f"板块数={cnt}")
except Exception as ex:
    print("板块解析失败:", ex)

print("\n========== [C] 从公网 IP 直接访问（模拟浏览器）==========")
sh("curl -s -o /dev/null -w 'http://114.132.53.126/ -> HTTP %{http_code}\\n' http://114.132.53.126/")
sh("curl -s -o /dev/null -w 'http://114.132.53.126/health -> HTTP %{http_code}\\n' http://114.132.53.126/health")
c.close()
print("\n✅ 最终验证完成。")
