# 诊断构建是否真卡死：看 pip/apt 进程 + 网络连接在拉什么
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
sh("echo '=== pip/apt 进程 ==='; ps aux | grep -E 'pip|apt-get|apt ' | grep -v grep")
sh("echo '=== 构建容器 runc  alive? ==='; ps aux | grep runc | grep -v grep | head")
sh("echo '=== 网络连接（看在拉 pypi/deb）==='; ss -tnp 2>/dev/null | grep -E 'ESTAB|SYN' | head -20")
sh("echo '=== build.log 文件大小/时间 ==='; ls -la /opt/portfolio/build.log; wc -l /opt/portfolio/build.log")
c.close()
