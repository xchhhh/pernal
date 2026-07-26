# ======================================================================
# 内容种子：首次启动把 9 个板块的真实内容写进数据库
#
# 数据来源：用户简历 PDF（ai应用工程师-许成合.pdf）。
#   - 简历里有的（姓名/教育/技能/项目/自评）→ 照实填写。
#   - 简历里缺的（获奖与证书、技术输出）→ 按用户授权生成「示例」条目，
#     值里带「示例」字样，方便你之后替换成真实内容。
#   - seed_if_empty() 只在「表为空」时写入，不会覆盖你之后改的真数据。
# ======================================================================
from app.core.db import SessionLocal
from app.data.models import ContentSection, GraphTriple

# 9 个板块的真实内容（双维度：个人 7 + 系统 2）。body 用 dict/list 存结构化字段。
SEED_SECTIONS: dict = {
    "basics": {
        "title": "个人定位",
        "body": {
            "name": "许成合",
            "headline": "AI 应用开发工程师 · 应届生",
            "target_role": "AI 应用开发 / 大模型应用开发",
            "location": "中国 · 深圳（意向城市）",
            "tagline": "具备大模型应用从方案设计到落地开发的能力，熟悉 RAG / Agent / 微调 / 部署全链路。",
        },
    },
    "education": {
        "title": "教育背景",
        "body": [
            {
                "school": "云南民族大学",
                "major": "计算机科学与技术",
                "degree": "本科",
                "duration": "2022.09 - 2026.06",
                "courses": ["机器学习", "数据结构与算法", "自然语言处理", "数据库原理"],
                # 简历未列荣誉 → 生成示例，请替换为真实荣誉
                "honors": ["示例：校级学业奖学金（请替换为真实荣誉）"],
            }
        ],
    },
    "skills": {
        "title": "技术栈矩阵",
        "body": {
            "languages": ["Python", "SQL"],
            "frameworks": ["LangChain", "LangGraph", "FastAPI", "Streamlit"],
            "ai_stack": ["RAG", "Agent", "Function Calling", "Prompt Engineering", "MCP", "知识图谱"],
            "tools": ["Docker", "Linux", "Git", "GitHub Actions", "Ollama"],
            "cloud": ["DeepSeek", "火山引擎 Ark（豆包）", "硅基流动"],
            "finetune": ["PyTorch", "Transformers", "PEFT", "LoRA", "QLoRA", "DeepSpeed"],
            "vector_db": ["FAISS", "Chroma"],
        },
    },
    "projects": {
        "title": "项目经历",
        "body": [
            {
                "name": "AI 应用开发门户（本项目）",
                "role": "全栈 / AI 工程",
                "stack": ["FastAPI", "LangChain", "LangGraph", "MCP", "知识图谱", "Chroma", "Caddy", "Docker"],
                "highlights": [
                    "内置四大 AI 模块：RAG 问答 + LangGraph Agent + 多 Agent 协作 + 知识图谱可视化",
                    "2 核 2G 生产部署：Caddy 反代 + 容器化 + GitHub Actions 自动部署",
                    "用企业常用 AI 栈（RAG/LangGraph/MCP）作为能力「活证明」",
                ],
                "link": "#",
            },
            {
                "name": "基于 RAG 的物流行业智能知识库问答系统",
                "role": "AI 应用开发",
                "stack": ["Python", "LangChain", "Ollama", "Qwen2.5-7B", "FAISS", "Streamlit"],
                "highlights": [
                    "针对物流资料分散、检索效率低，设计「文档解析-向量检索-检索增强生成」链路",
                    "用 PyMuPDFLoader 解析 PDF + RecursiveCharacterTextSplitter 语义切片",
                    "Embedding 写入 FAISS 做语义召回，LangChain 串 RAG 流程，Ollama 本地部署 Qwen2.5-7B 离线推理",
                    "Streamlit 搭多轮对话交互页面",
                ],
                "link": "#",
            },
            {
                "name": "基于 Qwen 大模型 LoRA 微调实践项目",
                "role": "模型微调 / 训练",
                "stack": ["Python", "PyTorch", "Transformers", "PEFT", "LoRA", "DeepSpeed"],
                "highlights": [
                    "面向垂直业务术语理解不足，基于 Qwen 开源模型做 LoRA 参数高效微调",
                    "领域数据清洗/去噪/指令样本构建，产出可用 SFT 数据集",
                    "引入 4bit 量化降低训练资源，完成权重合并、推理部署与可用性测试",
                ],
                "link": "#",
            },
        ],
    },
    "awards": {
        "title": "获奖与证书",
        # 简历未列 → 生成示例，请替换
        "body": [
            {"name": "示例：全国大学生计算机设计大赛 省级三等奖（2024）", "year": "2024"},
            {"name": "示例：大学英语 CET-6（2023）", "year": "2023"},
        ],
    },
    "outputs": {
        "title": "技术输出",
        # 简历未列 → 生成示例，请替换
        "body": {
            "github": "https://github.com/xuchenghe",
            "blog": "https://blog.example.com（示例，请替换）",
            "opensource": ["示例：参与 LangChain 中文文档翻译"],
        },
    },
    "self_eval": {
        "title": "自我评价",
        "body": {
            "summary": "具备「数据处理—检索增强—模型微调—服务封装—部署验证」的端到端开发能力；能快速理解业务需求并完成原型开发与迭代优化，工程落地意识与协作能力较强。",
            "strengths": ["端到端工程能力", "快速原型", "大模型全链路", "协作落地"],
        },
    },
    "tech_architecture": {
        "title": "系统技术架构",
        "body": {
            "layers": [
                {"layer": "接入层", "tech": "Caddy 2（反向代理 + 自动 HTTPS，无域名时走 HTTP）"},
                {"layer": "应用层", "tech": "FastAPI + Jinja2 SSR（零前端构建）"},
                {"layer": "AI 编排层", "tech": "LangChain(LCEL) + LangGraph + MCP(FastMCP)"},
                {"layer": "存储层", "tech": "SQLite（业务/FTS5 BM25/三元组）+ Chroma（向量，进程内）"},
                {"layer": "可视化层", "tech": "Cytoscape.js（知识图谱可交互）"},
                {"layer": "交付层", "tech": "Docker + compose + GitHub Actions → GHCR"},
            ],
            "note": "本门户本身即 AI 应用开发能力的狗粮式展示。",
        },
    },
    "engineering": {
        "title": "工程实践",
        "body": {
            "cicd": "GitHub Actions 构建 → 推 GHCR → SSH 部署，密钥走 Secrets，支持回滚",
            "observability": "pydantic-settings 配置 + structlog JSON 日志 + /health//ready 探针 + slowapi 限流",
            "security": "密钥不入库、表单/API 限流防刷、HTTPS（域名就绪后自动开启）",
        },
    },
}


# 知识图谱三元组（M4 数据源）：用真实简历实体，方便查询与可视化。
SEED_GRAPH_TRIPLES: list[dict] = [
    {"subject": "许成合", "relation": "求职于", "obj": "AI 应用开发", "source_section": "basics"},
    {"subject": "许成合", "relation": "掌握", "obj": "Python", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "FastAPI", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "LangChain", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "LangGraph", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "MCP", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "RAG", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "Agent", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "知识图谱", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "LoRA", "source_section": "skills"},
    {"subject": "许成合", "relation": "掌握", "obj": "Docker", "source_section": "skills"},
    {"subject": "许成合", "relation": "毕业于", "obj": "云南民族大学", "source_section": "education"},
    {"subject": "AI 应用开发门户（本项目）", "relation": "使用", "obj": "FastAPI", "source_section": "projects"},
    {"subject": "AI 应用开发门户（本项目）", "relation": "使用", "obj": "LangChain", "source_section": "projects"},
    {"subject": "AI 应用开发门户（本项目）", "relation": "使用", "obj": "LangGraph", "source_section": "projects"},
    {"subject": "AI 应用开发门户（本项目）", "relation": "使用", "obj": "MCP", "source_section": "projects"},
    {"subject": "AI 应用开发门户（本项目）", "relation": "使用", "obj": "知识图谱", "source_section": "projects"},
    {"subject": "基于 RAG 的物流行业智能知识库问答系统", "relation": "使用", "obj": "RAG", "source_section": "projects"},
    {"subject": "基于 RAG 的物流行业智能知识库问答系统", "relation": "使用", "obj": "LangChain", "source_section": "projects"},
    {"subject": "基于 RAG 的物流行业智能知识库问答系统", "relation": "使用", "obj": "FAISS", "source_section": "projects"},
    {"subject": "基于 Qwen 大模型 LoRA 微调实践项目", "relation": "使用", "obj": "LoRA", "source_section": "projects"},
    {"subject": "基于 Qwen 大模型 LoRA 微调实践项目", "relation": "使用", "obj": "PyTorch", "source_section": "projects"},
    {"subject": "RAG", "relation": "属于", "obj": "AI 应用开发", "source_section": "skills"},
    {"subject": "LangGraph", "relation": "用于", "obj": "多Agent协作", "source_section": "skills"},
    {"subject": "MCP", "relation": "用于", "obj": "工具调用", "source_section": "skills"},
    {"subject": "Python", "relation": "用于", "obj": "AI 应用开发门户（本项目）", "source_section": "projects"},
]


def seed_graph_if_empty() -> None:
    """仅当图谱三元组表为空时写入真实关系，避免覆盖已填数据。"""
    with SessionLocal() as db:
        if db.query(GraphTriple).count() > 0:
            return
        for t in SEED_GRAPH_TRIPLES:
            db.add(GraphTriple(**t))
        db.commit()


def seed_if_empty() -> None:
    """板块表为空才写内容；图谱三元组独立判断，两者互不影响、互不阻塞。

    注意：图谱种子必须「始终检查」，不能因为板块已存在就跳过——
    否则会出现「板块有数据、图谱却为空」的半成品状态（图谱页面 0 节点）。
    """
    with SessionLocal() as db:
        if db.query(ContentSection).count() == 0:
            for key, val in SEED_SECTIONS.items():
                db.add(
                    ContentSection(
                        section_key=key,
                        title=val["title"],
                        body=val["body"],
                    )
                )
            db.commit()
    # 图谱三元组独立种子：无论板块是否已存在，都确保图谱补齐（防半成品状态）
    seed_graph_if_empty()
