# VPS 部署脚本（密钥登录）：装 Docker -> 上传源码 -> 构建镜像 -> 启动 -> 验证 -> docker stats
# 用法：python scripts/vps_deploy.py
import os
import sys
import time
import paramiko

HOST = "114.132.53.126"
PORT = 22
USER = "root"
KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "id_ed25519")
LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # portfolio 根
REMOTE = "/opt/portfolio"

# 只上传这些（绝不传 .venv / 本地 db / 向量库 / 私钥 / 部署脚本）
UPLOAD_DIRS = ["app"]
UPLOAD_FILES = ["Dockerfile", "docker-compose.yml", "Caddyfile",
                "pyproject.toml", ".dockerignore", ".env", ".env.example"]


def ssh_connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, PORT, username=USER, key_filename=KEY_PATH, timeout=20)
    c.get_transport().set_keepalive(30)
    return c


def run(c, cmd, timeout=600):
    """执行命令并打印输出；长命令用 nohup 后台跑的话由调用方处理。"""
    print(f"\n$ {cmd}")
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print("[stderr]", err.rstrip())
    return out + err


def sftp_upload(c):
    sftp = c.open_sftp()
    # 远端目录
    run(c, f"mkdir -p {REMOTE}")
    # 上传目录（递归）
    for d in UPLOAD_DIRS:
        local_d = os.path.join(LOCAL, d)
        remote_d = f"{REMOTE}/{d}"
        sftp.mkdir(remote_d) if not _exists(sftp, remote_d) else None
        _put_tree(sftp, local_d, remote_d)
    # 上传单文件
    for f in UPLOAD_FILES:
        local_f = os.path.join(LOCAL, f)
        if os.path.exists(local_f):
            sftp.put(local_f, f"{REMOTE}/{f}")
            print(f"  put {f}")
    sftp.close()


def _exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def _put_tree(sftp, local_dir, remote_dir):
    for name in os.listdir(local_dir):
        lp = os.path.join(local_dir, name)
        rp = f"{remote_dir}/{name}"
        if os.path.isdir(lp):
            if name in ("__pycache__", ".pytest_cache", ".git", ".venv"):
                continue
            if name == "data" and os.path.exists(os.path.join(lp, "portal.db")):
                # 跳过本地数据库文件，但保留目录与代码
                sftp.mkdir(rp) if not _exists(sftp, rp) else None
                for sub in os.listdir(lp):
                    if sub in ("portal.db", "chroma"):
                        continue
                    sublp = os.path.join(lp, sub)
                    subrp = f"{rp}/{sub}"
                    if os.path.isdir(sublp):
                        sftp.mkdir(subrp) if not _exists(sftp, subrp) else None
                        _put_tree(sftp, sublp, subrp)
                    else:
                        sftp.put(sublp, subrp)
                continue
            sftp.mkdir(rp) if not _exists(sftp, rp) else None
            _put_tree(sftp, lp, rp)
        else:
            if name.endswith(".pyc"):
                continue
            sftp.put(lp, rp)


def main():
    c = ssh_connect()
    print("=== [1] 安装 Docker ===")
    # 幂等：重跑脚本时若 Docker 已装，跳过安装步骤（避免重复装/冲突）
    if "Docker version" in run(c, "docker --version 2>/dev/null"):
        print("Docker 已安装，跳过安装步骤")
    else:
        # 注：TencentOS 不被 get.docker.com 官方脚本支持；且 download.docker.com 在本机被防火墙 reset，
        # 故改用腾讯云 Docker CE 镜像（TencentOS 二进制兼容 CentOS）。
        run(c, "command -v curl >/dev/null 2>&1 || yum install -y curl")
        # 探测 RHEL 主版本（TencentOS 3≈RHEL8，4≈RHEL9），用于选对 Docker 仓库路径
        rhel_out = run(c, "rpm -E %rhel 2>/dev/null || echo 8")
        rhel_major = "".join(ch for ch in rhel_out if ch.isdigit()) or "8"
        print(f"检测到 RHEL 主版本: {rhel_major}")
        # 方案 A：腾讯云 Docker CE 镜像
        mirror_base = "https://mirrors.cloud.tencent.com/docker-ce/linux/centos"
        run(c, f"""cat > /etc/yum.repos.d/docker-ce.repo <<'EOF'
[docker-ce-stable]
name=Docker CE Stable (Tencent Mirror)
baseurl={mirror_base}/{rhel_major}/x86_64/stable
enabled=1
gpgcheck=1
gpgkey={mirror_base}/gpg
EOF
""")
        run(c, f"rpm --import {mirror_base}/gpg")
        run(c, "yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin", timeout=900)
        # 若 Docker CE 没装上，回退到 TencentOS 自带 docker 包
        if "Docker version" not in run(c, "docker --version 2>/dev/null"):
            print("⚠️ Docker CE 镜像安装失败，回退到 TencentOS 自带 docker 包")
            run(c, "rm -f /etc/yum.repos.d/docker-ce.repo")
            run(c, "yum install -y docker docker-compose 2>/dev/null || yum install -y docker")
            run(c, "docker compose version 2>/dev/null || yum install -y docker-compose-plugin 2>/dev/null || true")
        run(c, "systemctl enable --now docker")
        # 校验 Docker 真的装上了，否则明确报错中断（避免后面空转轮询）
        ver = run(c, "docker --version && docker compose version")
        if "Docker version" not in ver:
            raise SystemExit("❌ Docker 安装失败，已中断。请查看上面的 yum 报错。")

    # 建 2G swap：2c2g 上要跑交叉编码器 rerank 模型（~1.2GB），物理内存不够，
    # 用 swap 兜底避免 OOM；若内存充足则 swap 几乎不会被用到。
    print("\n=== [1.5] 确保有 swap（2c2g 跑 rerank 模型需要）===")
    run(c, """SWAP=/swapfile
if [ -z "$(swapon --show)" ]; then
  fallocate -l 2G $SWAP 2>/dev/null || dd if=/dev/zero of=$SWAP bs=1M count=2048
  chmod 600 $SWAP
  mkswap $SWAP
  swapon $SWAP
  grep -q "$SWAP" /etc/fstab || echo "$SWAP none swap sw 0 0" >> /etc/fstab
  echo "swap 已创建并启用"
else
  echo "swap 已存在，跳过"
fi
free -h | head -2""")

    # 配置 Docker Hub 镜像加速器（国内 VPS 直连 Docker Hub 常被墙/极慢，必须用镜像）
    run(c, """mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com", "https://docker.m.daocloud.io"]
}
EOF
""")
    run(c, "systemctl restart docker")
    run(c, "docker info 2>/dev/null | grep -i 'registry mirror' || true")

    print("\n=== [2] 上传源码到", REMOTE, "===")
    sftp_upload(c)
    run(c, f"ls -la {REMOTE}")

    print("\n=== [3] 构建并后台启动（日志写 build.log）===")
    # 先清理上一次可能残留的构建/容器，避免冲突（尤其 apt 卡死导致的僵尸构建）
    run(c, f"cd {REMOTE} && docker compose down 2>/dev/null; pkill -f 'docker compose' 2>/dev/null; pkill -f buildkit 2>/dev/null; sleep 2; echo cleaned")
    # 用 nohup 后台跑，避免 SSH 会话长时间阻塞
    run(c, f"cd {REMOTE} && nohup docker compose up -d --build > {REMOTE}/build.log 2>&1 &")
    # 轮询构建/启动进度
    for i in range(60):  # 最多等 30 分钟（每次 30s）
        time.sleep(30)
        log = run(c, f"tail -n 15 {REMOTE}/build.log; echo '---PS---'; docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' 2>/dev/null")
        if "healthy" in log or "Up" in log:
            # 再看 app 容器是否起来
            ps = run(c, "docker ps --filter name=portfolio-app -q")
            if ps.strip():
                print(f"[轮询 {i+1}] app 容器已启动，停止轮询")
                break
        print(f"[轮询 {i+1}/60] 仍在构建/启动中 ...")
    else:
        print("⚠️ 超过等待上限，请手动查看 build.log")

    print("\n=== [4] 健康检查 ===")
    run(c, "sleep 5; curl -s http://localhost/health; echo")
    run(c, "curl -s -o /dev/null -w '首页 HTTP %{http_code}\\n' http://localhost/")

    print("\n=== [5] docker stats 看 Chroma 内存（你的核心诉求）===")
    run(c, "docker stats --no-stream")

    print("\n=== [6] 容器状态 ===")
    run(c, "docker ps --format 'table {{{{.Names}}}}\\t{{{{.Status}}}}\\t{{{{.Ports}}}}'")
    c.close()
    print("\n✅ 部署脚本执行完毕。")


if __name__ == "__main__":
    main()
