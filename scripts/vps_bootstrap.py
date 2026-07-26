# 引导脚本：首次用密码登录，把本机 SSH 公钥装到 VPS，之后改用密钥登录（不再用弱密码）
# 用法：VPS_PW='你的密码' python scripts/vps_bootstrap.py
import os
import sys
import paramiko

HOST = "114.132.53.126"
PORT = 22
USER = "root"
KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "id_ed25519")
PASSWORD = os.environ.get("VPS_PW", "")


def make_key():
    """生成本机 RSA 密钥对（不存在才生成）。返回 paramiko 私钥对象。"""
    if os.path.exists(KEY_PATH):
        return paramiko.RSAKey.from_private_key_file(KEY_PATH)
    key = paramiko.RSAKey.generate(2048)
    # 私钥权限必须 600，否则 ssh 客户端会拒绝使用
    key.write_private_key_file(KEY_PATH)
    os.chmod(KEY_PATH, 0o600)
    return key


def pubkey_line(key):
    """构造 authorized_keys 里的一行：算法 + base64 公钥 + 注释。"""
    return f"{key.get_name()} {key.get_base64()} workbuddy@vps"


def run():
    key = make_key()
    pub = pubkey_line(key)
    print(f"[1] 本机公钥已就绪：{pub[:40]}...")

    # 第一次：用密码登录
    print("[2] 用密码首次连接 VPS ...")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, username=USER, password=PASSWORD, timeout=20)
    print("    密码登录成功")

    # 把公钥写进 /root/.ssh/authorized_keys（去重 + 权限）
    cmd = (
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
        f"grep -qxF '{pub}' /root/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> /root/.ssh/authorized_keys; "
        "chmod 600 /root/.ssh/authorized_keys; echo OK"
    )
    stdin, stdout, stderr = c.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    print("    写公钥结果:", out or err)
    c.close()

    # 第二次：用密钥登录验证
    print("[3] 用密钥重新连接验证 ...")
    c2 = paramiko.SSHClient()
    c2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c2.connect(HOST, PORT, username=USER, key_filename=KEY_PATH, timeout=20)
    stdin, stdout, stderr = c2.exec_command("echo KEY_AUTH_OK; whoami; hostname")
    print("    验证输出:", stdout.read().decode().strip())
    c2.close()
    print("\n✅ 密钥登录已就绪。后续部署将只用密钥，不再使用弱密码。")


if __name__ == "__main__":
    if not PASSWORD:
        print("请通过环境变量 VPS_PW 传入密码：VPS_PW='xxx' python scripts/vps_bootstrap.py")
        sys.exit(1)
    run()
