# VPS 实际部署实战总结（2026-07-26 深夜）

## 一、做了什么
用 SSH 把「AI 应用开发门户」真正部署到了你的 VPS（`114.132.53.126`，腾讯云 TencentOS，2 核 2G），并实测了 `docker stats` 的 Chroma 内存占用。

## 二、关键修复（每个都是部署中真实踩到的坑）
| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | `get.docker.com` 报错 "Unsupported distribution 'tencentos'"；`download.docker.com` 被防火墙 reset | 官方脚本不支持 TencentOS + Docker Hub 国内被墙 | 改用 **TencentOS 自带 docker 包**（moby 29.3.1 + compose 2.30.3）兜底；配腾讯云镜像加速器拉 Docker Hub |
| 2 | compose 直接报错 interrupt | `ghcr.io/OWNER/portfolio:latest` 中 `OWNER` 大写非法 | 镜像 tag 改成合法的本地标签 `portfolio:latest` |
| 3 | Caddy 容器反复 Restarting | compose 把 `SITE_DOMAIN` 传成空串 → Caddyfile 展开成无 key 块报错 | `SITE_DOMAIN=${SITE_DOMAIN:-:80}`（默认 `:80` 纯 HTTP） |
| 4 | `apt-get update` 在构建容器里永久卡死（无镜像连接） | Debian trixie 官方源被墙 | Dockerfile 硬编码腾讯云镜像：apt 源换 `mirrors.tencent.com/debian`，pip 设 `PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/` |
| 5 | `docker stats` 模板解析失败 | 字段名误用 `.MemPct`（应为 `.MemPerc`） | 改为正确字段名 |

额外安全项：你给的弱密码 `test.123` 已废弃，改为 **RSA 密钥登录**（公钥写入 VPS `authorized_keys`，私钥留本地 `scripts/id_ed25519` 且已加 `.gitignore`）。

## 三、验证结果（全绿）
- `portfolio-app-1`：**Up + healthy**；`portfolio-caddy-1`：**Up**
- 经 Caddy 反代：`http://114.132.53.126/` → 200、`/health` → 200、`/api/sections` → 200
- 首页含「许成合」、知识图谱 **20 节点 / 26 边**、内容板块 **9 个**（全部真实简历内容）

## 四、Chroma 内存（你的核心诉求 `docker stats`）
```
NAME              MEM USAGE / LIMIT     MEM %     CPU %
portfolio-app-1   173MiB / 1.5GiB       11.26%    0.11%   ← Chroma+Python+LangChain+FastAPI
portfolio-caddy-1 15.38MiB / 128MiB     12.01%    0.00%
```
Chroma 当前仅 ~173MB，离 1.5G 上限很远，2c2g 完全扛得住。

## 五、待你拍板/执行
1. **云端 AI 联调还需真正触发一次**：应用已带密钥运行，但还没真正访问 `/api/chat`（RAG 对话）验证火山 Ark Embedding + DeepSeek LLM 真能调通。在 VPS 上跑 `python3 scripts/e2e_real.py` 或浏览器访问首页点「AI 对话」即可验证。
2. **改掉弱密码**（你已改用密钥，但 root 密码仍是 `test.123`）：`passwd root` 改复杂密码；可选在 `/etc/ssh/sshd_config` 设 `PasswordAuthentication no` 仅允许密钥。
3. **替换示例数据**：简历里 awards / outputs / 教育荣誉 标了「示例」，有真实信息就替换 `app/data/seed.py` 后重新 `docker compose up -d --build`。
4. **可选 duckdns 子域 → 自动 HTTPS**（手册第六节）。

## 六、交付/产出文件
- `portfolio/VPS部署与Chroma内存观察.md`：修正后的实战手册（含 TencentOS 专属坑）
- `portfolio/scripts/vps_bootstrap.py`：SSH 密钥引导
- `portfolio/scripts/vps_deploy.py`：一键部署（装 Docker+上传+构建+启动+验证+stats，幂等、含国内镜像、清理残留构建）
- `portfolio/scripts/fix_caddy.py` / `verify_final.py` / `peek*.py`：排查与验证工具
- `portfolio/docker-compose.yml`、`Dockerfile`、`Caddyfile`：均已修正为可在国内 TencentOS 跑通的版本
