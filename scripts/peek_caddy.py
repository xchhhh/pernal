# 看 Caddy 容器日志，定位重启原因
import paramiko, os
KEY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "id_ed25519")
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("114.132.53.126", 22, username="root", key_filename=KEY, timeout=20)
def sh(cmd):
    _, out, err = c.exec_command(cmd)
    t = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    print(f"\n$ {cmd}\n{t}{e}")
sh("echo '=== Caddy 日志 ==='; docker logs --tail 30 portfolio-caddy-1 2>&1")
sh("echo '=== Caddyfile 内容 ==='; cat /opt/portfolio/Caddyfile")
sh("echo '=== 端口占用 ==='; ss -tlnp 2>/dev/null | grep -E ':80|:443'")
c.close()
