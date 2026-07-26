# ======================================================================
# 文档摄取服务（上传/路径 → 分类型解析 → 分类型切分 → 实时写入向量库）
#
# 架构（分类型路由，参考大厂 RAG 摄取层）：
#   parsers.py  负责「文件 → 文本 + kind」（MinerU/Paddle/PyMuPDF/文本读取）
#   chunking.py 负责「文本 + kind → 检索块」（markdown/contract/code/text 各一策略）
#   本文件      负责编排：解析 → 切分 → 入库 → 持久化（IngestedDoc）
#
# 类型 → 入口 → 切分 对照：
#   数字 PDF    MinerU(is_ocr=False)→PyMuPDF        markdown 切分（MinerU 产物带标题）
#   扫描件 PDF  Paddle 文档解析(OCR/VL)→MinerU(OCR)  text 切分
#   Markdown    直接读                               chunk_markdown（标题面包屑）
#   代码        直接读                               chunk_code（函数/类级）
#   合同条款    直接读                               chunk_clauses（按条切）
#   普通文本    直接读                               chunk_text（分段滑窗）
# ======================================================================
from app.core.config import get_settings
from app.core.logging import get_logger
from app.rag.chunking import chunk_by_kind, chunk_text
from app.services.parsers import (  # noqa: F401  （parse_pdf 兼容旧调用方）
    detect_kind,
    parse_by_kind,
    parse_pdf_digital,
    parse_pdf_scanned,
)

log = get_logger("ingest")


def parse_pdf(path: str) -> str:
    """【兼容旧接口】把 PDF 解析成文本。

    新代码请用 parse_by_kind(path)：会自动区分数字 PDF / 扫描件，
    分别走 MinerU / Paddle 入口。这里保留旧名字，内部走新分发。
    """
    text, _ = parse_by_kind(path)
    return text


def _persist_doc(source_name: str, text: str, chunk_count: int) -> None:
    """把解析文本存进 ingested_docs 表（重建索引时重新切块用）。"""
    from app.core.db import SessionLocal
    from app.data.models import IngestedDoc
    with SessionLocal() as db:
        db.add(IngestedDoc(source_name=source_name, text=text, chunk_count=chunk_count))
        db.commit()


def ingest_document(
    source_name: str,
    text: str,
    kind: str,
    store,
    persist: bool = True,
) -> tuple[int, str]:
    """统一入库入口：已解析文本 + kind → 分类型切分 → 写向量库（+持久化）。

    - kind: markdown / contract / code / text（chunk_by_kind 的取值）
    - 返回 (切块数, 文本)。所有上层入口（PDF 上传 / ingest-paths / 代码入库）
      最终都汇到这里，保证「同类型文档永远走同一切分策略」。
    """
    s = get_settings()
    docs = chunk_by_kind(
        kind,
        f"doc::{source_name}",
        source_name,
        text,
        parent_size=s.rag_parent_size,
        child_size=s.rag_child_size,
        child_overlap=s.rag_child_overlap,
    )
    store.add_documents(docs)
    log.info("ingest.document", source=source_name, kind=kind, chunks=len(docs))
    if persist:
        _persist_doc(source_name, text, len(docs))
    return len(docs), text


def ingest_file_to_store(
    path: str,
    source_name: str,
    store,
    kind: str | None = None,
    persist: bool = True,
) -> tuple[int, str, str]:
    """从「文件路径」入库：自动识别类型（或用调用方指定的 kind）→ 解析 → 切分入库。

    kind 可传：pdf_digital / pdf_scanned / markdown / code / contract / text；
    传 None 时按扩展名 + 内容特征自动识别（扫描件用文字密度启发式）。
    返回 (切块数, 解析文本, 实际使用的切分 kind) —— kind 回传给前端展示。
    """
    text, chunk_kind = parse_by_kind(path, kind)
    n, _ = ingest_document(source_name, text, chunk_kind, store, persist)
    return n, text, chunk_kind


def ingest_pdf_to_store(path: str, source_name: str, store, persist: bool = True) -> tuple[int, str]:
    """【兼容旧接口】PDF 上传入库。内部走新分类型管线（自动识别扫描件）。"""
    n, text, _ = ingest_file_to_store(path, source_name, store, kind=None, persist=persist)
    return n, text


def ingest_text_to_store(
    source_name: str,
    text: str,
    store,
    persist: bool = True,
    kind: str | None = None,
) -> tuple[int, str]:
    """【兼容旧接口】纯文本入库。kind 不传时按文件名后缀 + 内容特征自动推断
    （如 main.py → code、README.md → markdown、含多个「第X条」→ contract）。"""
    from app.services.parsers import detect_kind_for_text
    kind = kind or detect_kind_for_text(source_name, text)
    return ingest_document(source_name, text, kind, store, persist)
