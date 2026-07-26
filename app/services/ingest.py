# ======================================================================
# PDF 摄取服务（管理员上传 → 解析 → 切块 → 实时写入向量库）
#
# 解析优先级（按你��的走云端 API，不引本地重模型）：
#   1) MinerU 云端 API  —— 版式解析强，保留标题/表格结构（需要 MINERU_API_KEY）
#   2) 百度 Paddle 文档解析 API —— OCR 兜底（需要 PADDLE_API_KEY）
#   3) PyMuPDF 本地提取 —— 零额外依赖、最稳；没配密钥时自动走这条（功能可用）
#
# 设计要点：解析与「切块+入库」解耦。parse_pdf 只负责「PDF→文本」，
# 切块/写入由调用方（admin 路由）决定，方便以后接别的来源（网页/Word）。
# ======================================================================
import re

from app.core.config import get_settings
from app.core.logging import get_logger
from app.rag.chunking import chunk_text

log = get_logger("ingest")


def parse_pdf(path: str) -> str:
    """把 PDF 解析成文本/Markdown，按可用方案自动降级。"""
    s = get_settings()
    # 1) MinerU 云端 API（版式解析最强）
    if s.mineru_api_key:
        try:
            return _parse_via_mineru(path, s)
        except Exception as e:
            log.warning("ingest.mineru_failed", error=str(e))
    # 2) 百度 Paddle 文档解析 API（OCR 兜底）
    if s.paddle_api_key:
        try:
            return _parse_via_paddle(path, s)
        except Exception as e:
            log.warning("ingest.paddle_failed", error=str(e))
    # 3) 降级：PyMuPDF 本地提取（一定可用）
    return _parse_via_pymupdf(path)


def _parse_via_pymupdf(path: str) -> str:
    """本地提取：用 PyMuPDF 按页抽文字，拼成纯文本。无需任何密钥。"""
    import fitz  # PyMuPDF
    parts = []
    doc = fitz.open(path)
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n\n".join(p for p in parts if p.strip())


def _parse_via_mineru(path: str, s) -> str:
    """MinerU 云端 API：上传文件，取回 Markdown。

    注意：具体请求/响应字段以 MinerU 官方 API 文档为准；这里做的是「通用形态」，
    若你的账户返回结构不同，改下面的字段提取即可（失败会自动降级 PyMuPDF）。
    """
    import requests
    url = s.mineru_api_url or "https://api.mineru.net/v1/file_parse"
    with open(path, "rb") as f:
        resp = requests.post(
            url,
            files={"file": f},
            headers={"Authorization": f"Bearer {s.mineru_api_key}"},
            timeout=180,
        )
    resp.raise_for_status()
    data = resp.json()
    # 兼容多种返回形态：{"markdown":...} / {"data":{"markdown":...}} / {"result":...}
    md = (
        data.get("markdown")
        or (data.get("data") or {}).get("markdown")
        or data.get("result")
        or ""
    )
    if not md:
        raise ValueError("MinerU 返回中没有找到 markdown 字段，请检查 API 形态")
    return md


def _parse_via_paddle(path: str, s) -> str:
    """百度 Paddle / 百度智能云文档解析 API（OCR 兜底）。

    百度文档解析需要先换 access_token，再调解析接口；不同产品端点不同，
    这里实现「通用流程骨架」，具体端点/字段以百度官方为准；失败自动降级。
    """
    import requests
    # 1) 用 API Key/Secret 换 access_token（标准百度鉴权流程）
    token_url = "https://aip.baidubce.com/oauth/2.0/token"
    tok = requests.get(
        token_url,
        params={
            "grant_type": "client_credentials",
            "client_id": s.paddle_api_key,
            "client_secret": s.paddle_api_secret or "",
        },
        timeout=30,
    ).json().get("access_token")
    if not tok:
        raise ValueError("百度 access_token 获取失败")
    # 2) 调文档解析接口（端点以你选用的百度产品为准）
    url = s.paddle_api_url or "https://aip.baidubce.com/rest/2.0/ocr/v1/doc_analysis"
    with open(path, "rb") as f:
        resp = requests.post(
            url,
            params={"access_token": tok},
            files={"pdf_file": f},
            timeout=180,
        )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text") or (data.get("result") or {}).get("text") or ""
    if not text:
        raise ValueError("百度文档解析返回中没有 text 字段")
    return text


def ingest_pdf_to_store(path: str, source_name: str, store, persist: bool = True) -> tuple[int, str]:
    """解析 PDF → 切块 → 实时写入向量库（Chroma + BM25），返回 (切块数, 解析文本)。

    source_name 用作 doc_id 前缀，方便在向量库里区分「这条来自哪个上传文件」。
    persist=True 时同时把解析文本存进 ingested_docs 表，供「重建索引」时重新切块。
    """
    text = parse_pdf(path)
    docs = chunk_text(
        f"doc::{source_name}",
        source_name,
        text,
        chunk_size=get_settings().rag_chunk_size,
        overlap=get_settings().rag_chunk_overlap,
    )
    store.add_documents(docs)
    if persist:
        from app.core.db import SessionLocal
        from app.data.models import IngestedDoc
        with SessionLocal() as db:
            db.add(IngestedDoc(source_name=source_name, text=text, chunk_count=len(docs)))
            db.commit()
    return len(docs), text
