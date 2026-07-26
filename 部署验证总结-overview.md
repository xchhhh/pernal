# 本次会话工作总结

## 一、续做并修复的问题

### 1. 冒烟测试 import 报错（ModuleNotFoundError: app）
- 原因：`scripts/smoke_real.py` 没有把 `portfolio/` 加入模块搜索路径。
- 修复：在文件顶部加 `sys.path.insert(0, portfolio根目录)`，与 `e2e_real.py` 保持一致。

### 2. 图谱 API 返回 0 节点 —— 实为测试脚本解析错误
- 现象：`/api/graph-data` 在测试里拿到 0 个节点，断言失败。
- 排查：直接调 `to_cytoscape()` 正常返回 46 个元素（20 节点 + 26 边，含「许成合」）。
- 根因：API 返回的是 **Cytoscape 格式 `{elements:[...]}`**，而旧测试却去读不存在的 `gd["nodes"]`，于是得到 `[]`。
- 修复：冒烟测试改为按 `elements` 解析（节点=无 `source` 的元素，边=有 `source` 的元素）。**应用本身无 bug。**

### 3. 种子函数半成品风险（防御性修复）
- `seed_if_empty()` 原逻辑：若 `ContentSection` 已存在就 `return`，会**跳过**图谱种子，导致「板块有数据、图谱却为空」。
- 修复：图谱种子始终独立检查，不再被板块状态阻塞。避免 VPS 上出现 0 节点的图谱。

### 4. 部署致命漏洞：容器没注入 Embedding 的 base_url / model
- 现象：compose 只注入了 `LLM_API_KEY` / `EMBEDDING_API_KEY`，没注入 `LLM_BASE_URL/LLM_MODEL/EMBEDDING_BASE_URL/EMBEDDING_MODEL`。
- 后果：容器里 Embedding 会用默认占位（DeepSeek 的 embedding 接口），但你的 Embedding 实际是**火山引擎 Ark 豆包**，RAG 检索会调错接口而失败。
- 修复：
  - `docker-compose.yml`：补注入上述 4 个变量（从 `.env` 读取）。
  - `app/core/config.py`：把误导性的默认占位改成火山 Ark 真实地址与模型名 `doubao-embedding-vision-250615`。

## 二、验证结果（本地沙箱，离线部分）

| 验证项 | 结果 |
|--------|------|
| 全应用真实内容冒烟（首页/about/projects/resume/graph + 图谱API + 板块API） | ✅ 全 200，图谱 20 节点，9 板块 |
| 离线 RAG 链路（切块→Chroma+BM25→RRF→压缩） | ✅ 109 块，检索命中真实「物流知识库」项目 |
| 图谱三元组 | ✅ 26 条关系 |
| compose / Dockerfile / Caddyfile 语法 | ✅ 已校验 |

## 三、仍需在 VPS 上完成（沙箱无外网/无 Docker）

- 云端 Embedding（火山 Ark）+ LLM（DeepSeek）真实联调：`python3 scripts/e2e_real.py`
- `docker stats portfolio-app-1` 盯 Chroma 内存（容器上限 `mem_limit: 1536m`）
- 详见交付物 `VPS部署与Chroma内存观察.md`

## 四、交付物清单

- `portfolio/scripts/smoke_real.py`（已修）
- `portfolio/scripts/e2e_real.py`（内存测量提示已修正为诚实说明）
- `portfolio/app/data/seed.py`（图谱种子解耦）
- `portfolio/app/core/config.py`（Embedding 默认改为火山 Ark）
- `portfolio/docker-compose.yml`（补齐 4 个云端变量注入）
- `portfolio/VPS部署与Chroma内存观察.md`（VPS 部署 + docker stats 手册）

## 五、安全确认

- 真实密钥已从 `.env.example`（被误贴）移出，存于 gitignore 的 `.env`；`.env.example` 仅留占位。
- `.env` 含：DeepSeek LLM Key、火山 Ark Embedding Key、SECRET_KEY 等，均未入库。
