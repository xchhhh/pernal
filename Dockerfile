# ======================================================================
# Dockerfile —— 多阶段构建，最终镜像只留运行时，尽量小、尽量稳
# 思路：依赖安装放「deps 阶段」利用层缓存；运行阶段只复制装好的包 + 应用代码
# 注：本项目依赖偏重（Chroma / LangChain / LangGraph / MCP），镜像会比较大（~1.5GB），
#     但 2c2g 只在乎「运行时内存」，不在乎「镜像体积」，所以可接受。
# ======================================================================

# ---- 基础镜像：官方 Python 3.13 精简版（Debian 系，体积小）----
FROM python:3.13-slim AS base

# 环境变量：让 Python 输出不缓冲（日志实时进 docker logs）、不写 __pycache__、pip 不缓存
# 另：pip 改用腾讯云 PyPI 镜像（国内 VPS 直连 pypi.org 常被墙/极慢）
# HF_ENDPOINT 走 hf-mirror 镜像，保证国内能拉到 bge-reranker 等模型权重
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.tencent.com \
    HF_ENDPOINT=https://hf-mirror.com

WORKDIR /app

# 安装系统级运行时库：
#   - libgomp1：Chroma 的 onnxruntime 需要（OpenMP 并行），缺了启动报 libgomp.so.1 not found
#   - libgl1 / libglib2.0-0 等：sentence-transformers / PyMuPDF / 可能的 Paddle 依赖的底层图形库
# 注意：默认 deb.debian.org 在国内 VPS 常被墙，apt-get update 会永久卡死，
# 故先把源换成腾讯云 Debian 镜像，并给 apt 加超时（拉不到就快速报错而非干等）。
RUN sed -i 's|deb.debian.org/debian|mirrors.tencent.com/debian|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null; \
    apt-get update -o Acquire::http::Timeout=20 -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# ---- 依赖安装阶段：只复制 pyproject.toml，依赖不变就不重装（缓存友好）----
FROM base AS deps
COPY pyproject.toml ./
# 升级 pip 后安装项目及其全部依赖（FastAPI/Chroma/LangChain/LangGraph/MCP 全家桶）
# 用 `pip install .` 会读 pyproject.toml 里的 dependencies 一并装好
RUN pip install --upgrade pip && pip install .

# ---- 运行阶段：复制装好的包 + 应用代码 ----
FROM base AS runtime
# 把 deps 阶段装好的 site-packages 和命令脚本整目录搬过来（比重新装快、且一致）
COPY --from=deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# 复制应用源码（app/ 包）
COPY app ./app

# 预创建持久化目录：业务库（SQLite）和 Chroma 向量库，由 compose 挂载卷保数据
RUN mkdir -p /app/data /app/chroma_data

# 健康检查：探 /health 端点，Caddy / compose 据此判断容器是否存活并自动重启
# start_period 给 Chroma 冷启动留时间（首次加载 onnxruntime 可能稍慢）
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health'); sys.exit(0)" || exit 1

# 容器内只暴露应用端口（Caddy 在另一个容器里反代它，外部不直接暴露 8000）
EXPOSE 8000

# 启动命令：单 worker（2c2g 下足够；多 worker 会让 Chroma/SQLite 各起一份，反而费内存且写锁冲突）
# 用 sh -c 包一层，方便以后在命令里插入环境变量展开
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
