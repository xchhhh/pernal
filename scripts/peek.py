# 一次性诊断远端部署状态：build.log 末尾 + 已构建镜像 + compose 容器状态 + 构建进程
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
sh("echo '=== build.log 末尾 40 行 ==='; tail -n 40 /opt/portfolio/build.log")
sh("echo '=== docker images ==='; docker images 2>/dev/null")
sh("echo '=== compose 容器 ==='; cd /opt/portfolio && docker compose ps 2>/dev/null")
sh("echo '=== 构建进程 ==='; ps aux | grep -E 'buildkit|docker compose|containerd-shim' | grep -v grep | head")
c.close()
