# 精准修复：上传修正后的 compose + Caddyfile，仅重启 Caddy（不动已 healthy 的 app 容器）
import os, paramiko
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "id_ed25519")
LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = "/opt/portfolio"

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
    return t + e

# 上传修正后的 compose 与 Caddyfile
sftp = c.open_sftp()
for f in ("docker-compose.yml", "Caddyfile"):
    sftp.put(os.path.join(LOCAL, f), f"{REMOTE}/{f}")
    print(f"  put {f}")
sftp.close()

# 仅重启 Caddy（读新 env，重建配置）
sh(f"cd {REMOTE} && docker compose up -d caddy")
import time
time.sleep(8)
sh(f"cd {REMOTE} && docker compose ps")
sh("docker logs --tail 15 portfolio-caddy-1 2>&1")
# 验证 HTTP 是否通（纯 IP 访问）
sh("echo '--- 验证 HTTP ---'")
sh("curl -s -o /dev/null -w 'Caddy /health -> HTTP %{http_code}\\n' http://localhost/health")
sh("curl -s -o /dev/null -w 'Caddy / -> HTTP %{http_code}\\n' http://localhost/")
sh("curl -s -o /dev/null -w 'Caddy /api/sections -> HTTP %{http_code}\\n' http://localhost/api/sections")
c.close()
print("\n✅ Caddy 修复脚本执行完毕。")
