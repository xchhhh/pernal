# ======================================================================
# 配置中心：用 pydantic-settings 从环境变量 / .env 读取所有配置
# 好处：配置和代码分离（12-factor 原则），密码/密钥绝不写死在代码里
# ======================================================================
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有运行配置集中在这里，启动时一次性加载，全局复用同一份。"""

    # ---- 应用基础信息 ----
    app_name: str = "AI 应用开发门户"                 # 站点名称，显示在页面标题/日志里
    environment: str = "production"                  # 环境：development / production
    host: str = "0.0.0.0"                            # 监听地址（容器内监听所有网卡）
    port: int = 8000                                 # 容器内端口，Caddy 会把 443 反代到这里
    site_domain: str = ""                            # 站点域名（Caddy 自动 HTTPS 用，部署时填）

    # ---- 数据与向量库 ----
    database_url: str = "sqlite:///./app/data/portal.db"  # SQLite 业务库（板块内容/留言/图谱三元组）
    chroma_persist_dir: str = "./app/data/chroma"         # Chroma 向量库持久化目录（进程内）

    # ---- 安全 ----
    secret_key: str = "change-me-in-prod"            # 会话签名等用途，生产必须换成随机长字符串

    # ---- 限流（防刷）----
    rate_limit_per_minute: int = 60                  # 单 IP 每分钟最多请求数

    # ---- 云端 LLM（DeepSeek，OpenAI 兼容）----
    llm_api_key: str = ""                            # DeepSeek API Key（生产放环境变量，不进代码）
    llm_base_url: str = "https://api.deepseek.com/v1"  # DeepSeek 的 OpenAI 兼容接口地址
    llm_model: str = "deepseek-chat"                 # 用的模型名

    # ---- Embedding（文本向量化）----
    # 实测结论：本项目的火山 Ark 账号只有「视觉多模态 embedding」模型
    # （doubao-embedding-vision-*），它不支持 OpenAI 兼容 /embeddings 的纯字符串
    # （会 400）。但它支持 /embeddings/multimodal 端点：input 用
    # [{type:text,text:...}] 对象即可做「文本-only」嵌入（整段 input 合成一个向量，
    # 故批量需逐条调用）。这样文本 RAG 就能直接用云端 embedding，呼应「云端 AI 联调」。
    #   - embedding_backend="cloud" ：默认，走火山 /embeddings/multimodal（文本-only）
    #   - embedding_backend="local" ：离线兜底，用 local_embedding_model 本地向量化
    embedding_backend: str = "cloud"                 # cloud / local
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"  # 本地中文向量模型（离线兜底用）
    # 云端 embedding 配置（backend=cloud 时启用）
    embedding_api_key: str = ""                      # 火山引擎 Ark 的 API Key
    embedding_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"  # 火山 Ark 接口（multimodal 端点在其下）
    embedding_model: str = "doubao-embedding-vision-251215"  # 视觉多模态模型（用 multimodal 端点做文本嵌入；250615 亦可）

    # ---- RAG 检索管线参数（A7：查询改写→混合检索→RRF→rerank→压缩）----
    rag_chunk_size: int = 500                        # 切块最大字符数（板块内容拍平后滑动窗口）
    rag_chunk_overlap: int = 80                      # 相邻块重叠字符数（避免切断语义）
    rag_top_k_vector: int = 5                        # 向量召回 Top-K
    rag_top_k_bm25: int = 5                          # BM25 关键词召回 Top-K
    rag_rrf_k: int = 60                              # RRF 倒数排名常数 k（值越大越弱化排名差异）
    rag_rerank_top_n: int = 6                        # rerank 后保留的条数（父块更大，多留点候选选优）

    # ---- 父子切分（parent-child chunking）----
    # 原 500 字滑窗把句子切散，召回的是碎片，拼进 prompt 引号都错位。
    # 改为：子块(小)做向量/BM25 精细检索，命中后回退父块(大、完整上下文)给 LLM，
    # 并按下父块去重——既保检索精度，又保回答连贯。
    rag_parent_size: int = 1000     # 父块最大字符数（给 LLM 看，承载完整语义）
    rag_child_size: int = 350      # 子块最大字符数（做 embedding / BM25 检索单元）
    rag_child_overlap: int = 50    # 子块之间重叠字符数（避免切断词意）
    rag_enable_query_rewrite: bool = True            # 是否开启查询改写（扩写/子问题，提升召回）
    rag_enable_rerank: bool = True                   # 是否开启重排（默认开：rerank 是 RAG 质量关键一环）
    rerank_backend: str = "cross-encoder"            # 重排后端：cross-encoder（本地 bge-reranker）/ llm（用 DeepSeek 打分降级）
    rerank_cross_encoder_model: str = "BAAI/bge-reranker-base"  # 交叉编码器模型名（base 版约 440MB，契合 2c2g；v2-m3 太大易 OOM）
    rag_compress: bool = True                        # 是否压缩上下文（截断/选块，控长度与成本）

    # ---- HuggingFace 镜像（国内 VPS 拉模型用，避免 hf.co 被墙）----
    hf_endpoint: str = "https://hf-mirror.com"       # sentence-transformers 下载 bge-reranker 时走这个镜像

    # ---- 管理员后台（PDF 上传 / 实时更新向量库）----
    # 警告：生产务必改成随机长字符串！这里只是方便你本地/演示直接试。
    admin_token: str = "admin123"                    # 管理员口令（登录后签发签名 Cookie）

    # ---- PDF 云端解析（MinerU + 百度 Paddle，按你说的走 API 而非本地重模型）----
    # 都不填时自动降级为 PyMuPDF 本地提取（功能可用，只是版式/表格还原弱一些）。
    mineru_api_key: str = ""                         # MinerU 云端 API Key（版式解析强）
    mineru_api_url: str = ""                         # MinerU API 地址（不填用官方默认）
    paddle_api_key: str = ""                         # 百度 Paddle / 百度智能云文档解析 Key（OCR 兜底）
    paddle_api_secret: str = ""                      # 百度文档解析 Secret Key（与 Key 配套换 access_token，必填 Paddle 才可用）
    paddle_api_url: str = ""                         # 百度文档解析 API 地址（不填用官方默认）

    # ---- 联系表单（可选 SMTP，不配则只存库不发送）----
    smtp_host: str = ""                              # SMTP 服务器（如 smtp.qq.com）
    smtp_port: int = 465                             # SMTP 端口
    smtp_user: str = ""                              # SMTP 用户名（邮箱）
    smtp_pass: str = ""                              # SMTP 授权码
    contact_to_email: str = ""                       # 表单提交后发到这个邮箱

    # ---- 日志 ----
    log_level: str = "INFO"                          # 日志级别

    # pydantic-settings 的配置：本地读 .env，生产由真实环境变量覆盖
    model_config = SettingsConfigDict(
        env_file=".env",                 # 本地开发读 .env
        env_file_encoding="utf-8",
        extra="ignore",                  # 多余的环境变量忽略，不报错
    )


@lru_cache
def get_settings() -> Settings:
    """带缓存地取配置单例，全局复用，避免重复解析环境变量。"""
    return Settings()
