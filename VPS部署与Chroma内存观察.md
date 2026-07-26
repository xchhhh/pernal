# VPS 部署 + 盯 Chroma 内存（docker stats）实战手册

> 适用：2 核 2G 的 VPS（腾讯云 TencentOS / CentOS / Ubuntu 均可）。
> 目标：把你这个 AI 应用开发门户真正跑在 VPS 上，并用 `docker stats` 盯住 Chroma 的内存占用。
> 沙箱里没法连外网，所以「云端联调 + docker stats」这两步必须在你的 VPS 上做；本地我们已经把能离线验证的都验证过了。

---

## 一、VPS 前置准备（一次性）

```bash
# 1) 系统更新（TencentOS / CentOS）
sudo yum update -y
# 如果是 Ubuntu，改成：sudo apt update && sudo apt upgrade -y

# 2) 安装 Docker + compose 插件
#    ⚠️ 注意：腾讯云 TencentOS 上 `curl ... get.docker.com | sh` 官方脚本会报
#    "Unsupported distribution 'tencentos'" 直接失败；`download.docker.com` 也被防火墙 reset。
#    最稳的做法是直接用 TencentOS 自带 docker 包（实测 moby 29.3.1 + docker-compose 2.30.3）：
sudo yum install -y docker docker-compose
sudo systemctl enable --now docker
# 验证（看到版本号就 OK）
docker --version
docker compose version

# 2.5) 配置 Docker Hub 镜像加速器（国内 VPS 直连 Docker Hub 常被墙/极慢，拉 python:3.13-slim 必用）
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com", "https://docker.m.daocloud.io"]
}
EOF
sudo systemctl restart docker
docker info | grep -i 'registry mirror'   # 看到上面两个镜像即生效

# 3) 把当前用户加入 docker 组，避免每次都要 sudo（改完重开终端生效）
sudo usermod -aG docker $USER

# 4) 开放 80 / 443 端口（腾讯云控制台也要在安全组里放行 TCP 80、443）
sudo firewall-cmd --permanent --add-port=80/tcp
sudo firewall-cmd --permanent --add-port=443/tcp
sudo firewall-cmd --reload
```

---

## 二、拉代码 + 准备 .env（含密钥）

```bash
# 1) 拉仓库（把 YOUR_GITHUB 换成你的用户名）
git clone https://github.com/YOUR_GITHUB/portfolio.git
cd portfolio

# 2) 创建 .env（本地这份 .env 已 gitignore，不会进仓库；VPS 上请手动建一份同样内容的）
cat > .env <<'EOF'
# ---- LLM：DeepSeek（OpenAI 兼容）----
LLM_API_KEY=sk-xxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# ---- Embedding：火山引擎 Ark 豆包（和 LLM 不是同一家，必须单独配）----
EMBEDDING_API_KEY=ark-xxxxxxxx
EMBEDDING_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
EMBEDDING_MODEL=doubao-embedding-vision-250615

# ---- 其他 ----
SECRET_KEY=随便一段随机长字符串（生产必须改）
SITE_DOMAIN=        # 没域名先留空（走 IP + HTTP）；有 duckdns 子域再填
ACME_EMAIL=         # 申请 HTTPS 证书用的邮箱，填了子域才生效
EOF

# 3) 改一下 SECRET_KEY（随便敲一段 32 位以上的随机字符）
#    例如：python3 -c "import secrets;print(secrets.token_hex(32))"
```

> ⚠️ 重要：compose 现在会读取 `.env` 里的 `LLM_BASE_URL / LLM_MODEL / EMBEDDING_BASE_URL / EMBEDDING_MODEL` 注入容器。
> 漏掉这几个，容器就会用默认占位值（DeepSeek 的 embedding 接口），导致 RAG 检索调用错误接口而失败。
> 我们已把默认占位改成火山 Ark，但**正式部署仍以你 `.env` 里的为准**。

---

## 三、部署启动

```bash
# 第一次：本地构建并后台启动（Caddy + 应用两个容器）
docker compose up -d --build

# 之后更新代码只需拉最新镜像（配合 GitHub Actions CI 时）：
# docker compose pull && docker compose up -d

# 看应用容器日志，确认没报错、看到 app.startup 就说明起来了
docker compose logs -f app
```

---

## 四、验证服务

```bash
# 1) 健康检查（返回 {"status":"ok"} 就正常）
curl -s http://localhost/health

# 2) 用浏览器 / 服务器 IP 访问（没域名时直接 http://你的VPS_IP/ ）
#    能打开首页、看到「许成合」、图谱页有 20 个节点，就说明真实简历内容已生效。

# 3) 端到端联调（在 VPS 上跑这个脚本，验证云端 Embedding+LLM 真能调通）
#    沙箱里这步会失败（没外网），VPS 上应该能看到「Embedding OK / LLM OK」
python3 scripts/e2e_real.py
```

---

## 五、盯 Chroma 内存（你的核心诉求）

Chroma 是「进程内」模式，跑在 **app 容器**里，和 Python + LangChain + FastAPI 共用同一块内存。
所以我们盯的其实就是 app 容器的内存占用。

```bash
# 实时看所有容器内存（最直观）
docker stats

# 只看应用容器（容器名一般是 portfolio-app-1）
docker stats portfolio-app-1

# 只看内存这一列，按名字排（更清爽）
# ⚠️ 字段名是 .MemPerc（不是 .MemPct，后者会报 template 解析错误）
docker stats --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" portfolio-app-1
```

**怎么读：**
- `MemUsage` 形如 `412MiB / 1.536GiB` → 当前 Chroma+应用共用了约 412MB，上限 1536MB（即 compose 里的 `mem_limit: 1536m`）。
- 2G 机器里，app 最多吃 1.5G，剩下 ~0.5G 留给系统 + Caddy（128M）。这就是 2c2g 的安全水位。
- 首次冷启动 Chroma 加载 onnxruntime 时内存会涨一截，之后稳定。

**想看更细（可选）：**
```bash
# 进容器看 uvicorn 主进程的常驻内存（RSS，单位 KB）
docker exec portfolio-app-1 sh -c "ps -o pid,rss,comm -p 1; ps aux | grep -i uvicorn | head"
# 向量库落盘大小
docker exec portfolio-app-1 du -sh /app/chroma_data
```

**内存快顶到上限怎么办（2G 吃紧时的兜底）：**
1. 调小 `rag_chunk_size` / `rag_chunk_overlap`（config.py / .env 可覆盖），减少切块数 → 索引更小。
2. 关掉云端 rerank（`rag_enable_rerank=False`，默认已关）。
3. 实在不够：把 VPS 升到 4G，并把 compose 里 app 的 `mem_limit` 提到 `3072m`。
   **不要**在 2G 上把 mem_limit 设超过 ~1.7G，否则系统会被挤爆、容器被 OOM 杀掉。

---

## 六、域名 + 自动 HTTPS（可选，没域名先跳过）

```bash
# 1) 去 duckdns.org 免费申请一个子域，例如 xuchx.duckdns.org
# 2) 在域名服务商/duckdns 把子域 A 记录指向你的 VPS IP
# 3) 改 .env：
#      SITE_DOMAIN=xuchx.duckdns.org
#      ACME_EMAIL=你的邮箱@xx.com
# 4) 重启让 Caddy 自动申请证书并开 443
docker compose up -d
# 之后访问 https://xuchx.duckdns.org 即为 HTTPS（Let's Encrypt 证书，Caddy 自动续期）
```
> 不变更域名时，Caddyfile 里的 `{$SITE_DOMAIN:80}` 会退回到 `:80`，直接用 IP 走 HTTP，完全可用。

---

## 七、CI/CD 自动部署（可选）

- 当前 `docker-compose.yml` 里镜像 tag 是本地标签 `portfolio:latest`（VPS 直接 `build`，不需要 GHCR 前缀）。
- 走 CI 时把 `image:` 改成 `ghcr.io/<你的GitHub用户名>/portfolio:latest`，GitHub Actions 才会 `build → push` 到 GHCR，再 SSH 到 VPS `docker compose pull && up -d`。
- 在仓库 Secrets 配置：`GHCR_TOKEN`（GitHub 容器仓库读写权）+ `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY`（私钥内容，即本地 `scripts/id_ed25519`）。
- 之后 `git push` 触发 GitHub Actions：构建 → 推 GHCR → SSH 到 VPS 拉起。

---

## 八、我们在本地（沙箱）已经验证过的部分

| 项目 | 结果 |
|------|------|
| 全应用真实内容冒烟（首页/about/projects/resume/graph + 图谱 API + 板块 API） | ✅ 全 200，图谱 20 节点，9 板块 |
| 离线 RAG 链路（切块→Chroma+BM25→RRF→压缩） | ✅ 109 块，检索命中真实「物流知识库」项目 |
| 图谱三元组种子 | ✅ 26 条关系，含「许成合→掌握→Python/LangChain…」 |
| compose / Dockerfile / Caddyfile 语法 | ✅ 已校验 |
| **VPS 实际部署（TencentOS + 腾讯云镜像）** | ✅ 已成功（app healthy + Caddy 经 IP 反代 200） |
| **`docker stats` 看 Chroma 内存** | ✅ 已实测：`portfolio-app-1` = **173 MiB / 1.5 GiB（11.26%）** |
| 云端 Embedding + LLM 真实联调（RAG 对话实际调通） | ⏳ 待触发：应用已带密钥运行，需真正访问 `/api/chat` 或跑 `python3 scripts/e2e_real.py` 验证云端调通 |
