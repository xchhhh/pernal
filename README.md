# 个人 AI 应用开发门户（AI Application Portal）

> 许成合 · AI 应用开发工程师（应届生）的作品集 + 可交互 AI 演示站点。
> 一套把 **RAG / 多智能体（LangGraph）/ MCP / 知识图谱** 串起来的全栈演示，
> 访客可以用自然语言问答关于「我」的一切，管理员能随时把新资料/代码向量化进站点。

---

## 一、这个项目能做什么

- **ChatGPT 式问答主页（`/`）**：左侧栏 + 主对话区，访客提问后实时看到
  「🧠 思考过程」（查询改写 → 多 agent 协同 → 混合检索 → RRF → rerank）。
- **RAG 问答（M1）**：`/api/chat` —— 检索增强生成，严格依据站内资料回答，避免幻觉。
- **单 Agent + MCP（M2）**：`/api/agent` —— 绑定 10 个 MCP 工具，模型自己决定调哪个。
- **多智能体协作（M3）**：`/api/multi-agent` —— 主管分派「检索员 / 图谱员」协作后生成回答。
- **知识图谱可视化（M4）**：`/api/graph-data` —— 能力与项目的关系网，前端 Cytoscape 渲染。
- **管理员后台（`/admin`）**：登录后上传 PDF 实时更新向量库；还能把**项目代码 / 资料**
  直接向量化入库（见下文「把项目代码/资料导入站点」），之后即可在问答中查询项目本身。

---

## 二、技术架构

```
┌──────────────────────────────────────────────────────────────┐
│                        访客 / 管理员                          │
└───────────────────────────┬──────────────────────────────────┘
                            │  HTTP (Caddy 反代 :80)
┌───────────────────────────▼──────────────────────────────────┐
│                    FastAPI (app/main.py)                      │
│  pages(SSR) · assistant(流式问答) · admin(后台) · ai(api 路由)  │
└───────┬───────────────┬────────────────┬─────────────────────┘
        │               │                │
┌───────▼──────┐ ┌──────▼───────┐ ┌──────▼────────┐
│  RAG 管线    │ │ LangGraph    │ │ 知识图谱       │
│ (retrieval) │ │ Agent/MCP   │ │ (networkx)    │
└───────┬──────┘ └──────┬───────┘ └──────┬────────┘
        │               │                │
┌───────▼───────────────▼────────────────▼────────┐
│           混合检索 HybridStore                    │
│  向量检索(Chroma)  +  BM25(FTS5)  →  RRF 融合     │
│  → rerank(bge-reranker 交叉编码器) → 父子切块回退  │
└───────┬───────────────────────────┬─────────────┘
        │                           │
┌───────▼─────────┐         ┌───────▼────────────┐
│ 火山多模态 Embedding │       │  DeepSeek LLM       │
│ (/embeddings/     │         │ (OpenAI 兼容接口)   │
│  multimodal 文本) │         │                     │
└──────────────────┘         └─────────────────────┘
```

### RAG 检索链路（M1）
1. **查询改写**：LLM 把短问句扩写成利于检索的查询（可选）。
2. **混合召回**：Chroma 向量检索 + SQLite FTS5 的 BM25 关键词检索，两路并行。
3. **RRF 融合**：`score = Σ 1/(k+rank)` 把两路排名合成一个去重排名。
4. **rerank 重排**：`bge-reranker-v2-m3` 交叉编码器对候选精排（本地，离线可用）。
5. **父子切块回退**：命中「子块」（精细检索）后回退到「父块」（完整上下文）喂给 LLM，
   并按父块去重——根治「碎片拼接引号散架 / 漏信息」问题。

---

## 三、技术栈

| 层 | 技术 |
|---|---|
| Web 框架 | FastAPI + Jinja2（SSR）+ SlowAPI（限流）|
| AI 编排 | LangChain（LCEL）+ LangGraph（Agent）+ MCP（官方 SDK）|
| 向量库 | Chroma（进程内 `PersistentClient`，无需独立服务）|
| 关键词检索 | SQLite FTS5（BM25）|
| Embedding | 火山引擎 `doubao-embedding-vision`（走 `/embeddings/multimodal` 文本端点）|
| LLM | DeepSeek（`deepseek-chat`，OpenAI 兼容接口）|
| rerank | `bge-reranker-v2-m3`（sentence-transformers，本地交叉编码器）|
| 图谱 | NetworkX（内存遍历三元组）|
| 持久化 | SQLAlchemy + SQLite（板块内容 / 留言 / 图谱三元组）|
| 部署 | Docker + docker-compose + Caddy（反代，可选 HTTPS）|

---

## 四、目录结构

```
portfolio/
├── app/
│   ├── main.py              # 应用装配根（create_app / lifespan）
│   ├── core/                # config / db / auth / limiter / logging
│   ├── data/                # ORM 模型 + 9 板块种子内容(seed.py)
│   ├── rag/                 # RAG 全链路：chunking / store / embed /
│   │                        #   retrieval / rerank / graph / agent / llm / api
│   ├── routers/             # health / pages / assistant / admin
│   ├── services/            # content / ingest(PDF 解析+入库)
│   ├── static/              # CSS / JS（ChatGPT 式前端）
│   └── templates/           # Jinja2 页面模板
├── tests/                   # 单元测试
├── scripts/                 # 部署/验证/调试脚本（含 VPS 部署）
├── docker-compose.yml       # 容器编排（app + caddy）
├── Dockerfile              # 镜像构建（国内 apt/pip 镜像加速）
├── Caddyfile              # 反代配置
├── pyproject.toml          # 依赖清单
└── .env.example            # 配置样例（真实密钥放 .env，已 gitignore）
```

---

## 五、快速开始（本地）

> 前置：Python 3.11+。云端 Embedding/LLM 需要对应密钥；纯本地体验可切 `local` 后端（需联网下载 bge 模型）。

```bash
# 1) 安装依赖
pip install -e ".[dev]"

# 2) 准备配置
cp .env.example .env
#   编辑 .env，填入 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL
#   以及 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL

# 3) 启动
uvicorn app.main:app --reload --port 8000
# 打开 http://localhost:8000 即可问答
```

本地无密钥时，把 `.env` 里 `EMBEDDING_BACKEND=local`、`LLM` 也指向本地/兼容模型，
即可离线跑通 RAG 检索链路（仅 embedding/rerank 用本地模型）。

---

## 六、配置说明（`.env`）

真实密钥**只在 `.env`**（已被 gitignore），仓库只保留 `.env.example` 占位。

| 变量 | 说明 |
|---|---|
| `EMBEDDING_BACKEND` | `cloud`（默认，火山）或 `local`（sentence-transformers 兜底）|
| `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` | 火山多模态 embedding 凭证 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek 等 OpenAI 兼容 LLM |
| `ADMIN_TOKEN` | 管理员后台登录口令 |
| `MINERU_API_KEY` | MinerU 云端 PDF 解析（版式最强）|
| `PADDLE_API_KEY` / `PADDLE_API_SECRET` | 百度文档解析 OCR 兜底 |
| `RAG_*` | 切块尺寸 / 召回数 / RRF-k / rerank 开关等 |

---

## 七、部署（VPS / Docker）

```bash
# 在服务器上
git clone <你的仓库> /opt/portfolio && cd /opt/portfolio
cp .env.example .env && vim .env   # 填好密钥
docker compose up -d --build       # 构建 app + caddy 两个服务
```

- 无域名：Caddy 监听 `:80` 走 HTTP。
- 有域名：在 `Caddyfile` 把 `:80` 改成你的域名，Caddy 自动申请 ACME HTTPS。
- 容器内存建议 ≥ 1.5GB（含 Python + LangChain + Chroma + rerank 模型）。

---

## 八、把项目代码 / 资料导入站点（可问答）

站点默认只索引「个人资料 9 板块」。如果想让访客也能**查询这个项目本身的代码与文档**，
管理员可以把它们向量化进同一个向量库：

**方式 A：管理员接口（在容器内遍历，免去大文件传输）**

```bash
# 登录拿 Cookie
curl -c /tmp/cj.txt -X POST http://<站点>/api/admin/login \
  --data-urlencode 'token=<你的ADMIN_TOKEN>' \
  -H 'Content-Type: application/x-www-form-urlencoded'

# 把项目代码/资料入库（路径必须位于容器内的 /app 内）
curl -b /tmp/cj.txt -X POST http://<站点>/api/admin/ingest-paths \
  -H 'Content-Type: application/json' \
  -d '{"paths":["/app/app","/app/tests","/app/requirements_plan.md","/app/Dockerfile"]}'
```

- 仅收白名单扩展名（`.py/.md/.json/.yaml/.toml/...`），自动跳过 `__pycache__`、`.git`、超大文件。
- 每个文件作为一个文档持久化（`ingested_docs` 表），之后点「重建索引」也会重新切块，不会丢。

**方式 B：上传 PDF**

后台 `/admin` 上传 PDF，自动走 `MinerU → 百度 Paddle → PyMuPDF` 三级解析兜底，实时入库。

导入后直接问，例如：「这个项目的 RAG 检索是怎么实现的？」「store.py 做了什么？」即可召回对应代码块。

---

## 九、API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | ChatGPT 式问答主页 |
| GET | `/admin` | 管理员后台（未登录跳登录页）|
| POST | `/api/admin/login` | 管理员登录（下发签名 Cookie）|
| POST | `/api/admin/upload` | 上传 PDF 实时入库 |
| POST | `/api/admin/ingest-paths` | 把指定路径的代码/资料向量化入库 |
| POST | `/api/admin/reindex` | 清空并重建全部索引 |
| POST | `/api/chat` | M1 RAG 问答（流式）|
| POST | `/api/agent` | M2 单 Agent + MCP（流式）|
| POST | `/api/multi-agent` | M3 多智能体协作（流式）|
| GET | `/api/sections` | 取全部板块内容 |
| GET | `/api/graph-data` | 知识图谱（Cytoscape 格式）|

---

## 十、备注

- 知识库内容来自简历 PDF，`seed.py` 中部分缺失字段（获奖/技术输出）为带「示例」字样的占位，
  请替换为真实内容；`seed_if_empty()` 只在表空时写入，不会覆盖你后续改的数据。
- 本项目为个人作品集演示，AI 回答严格依据站内资料，资料之外的问题会如实说「不知道」。
- 仓库地址：`git@github.com:xchhhh/pernal.git`
