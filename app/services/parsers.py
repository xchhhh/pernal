# ======================================================================
# 分类型解析器（parsers）：不同文档类型走不同解析入口
#
# 参考大厂 RAG 摄取层的通用设计：「先识别类型 → 再路由到专用解析器」，
# 而不是所有文件一把梭进同一个解析函数。
#
#   类型          解析入口                                切分策略(chunking)
#   ------------  --------------------------------------  ------------------
#   数字 PDF      MinerU 云端(is_ocr=False) → PyMuPDF      chunk_text/markdown
#   扫描件 PDF    Paddle 文档解析(OCR/VL) → MinerU(OCR)     chunk_text
#                 → PyMuPDF 兜底
#   Markdown      直接读文本                                chunk_markdown
#   代码          直接读文本                                chunk_code
#   合同/条款     直接读文本（或 PDF 先解析）               chunk_clauses
#   普通文本      直接读文本                                chunk_text
#
# 本文件只负责「文件 → 文本 + 类型」，切分与入库由 ingest.py 调 chunk_by_kind。
# ======================================================================
import os
import re
import time

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("parsers")

# 扩展名 → 文档类型 的映射（detect_kind 用）
_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".sql", ".yaml", ".yml",
    ".toml", ".json", ".css", ".vue",
}
_MD_EXTS = {".md", ".markdown"}
# 合同特征：正文里出现多个「第X条」即认定为条款类文本
_CLAUSE_PAT = re.compile(r"第[一二三四五六七八九十百千0-9]+条")


# ----------------------------------------------------------------------
# 底层解析实现（从 ingest.py 迁移至此，ingest 只做「切块+入库」编排）
# ----------------------------------------------------------------------

def _parse_via_pymupdf(path: str) -> str:
    """本地提取：用 PyMuPDF 按页抽文字，拼成纯文本。无需任何密钥，永远可用。"""
    import fitz  # PyMuPDF
    parts = []
    doc = fitz.open(path)
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n\n".join(p for p in parts if p.strip())


def _parse_via_mineru(path: str, s, is_ocr: bool = False) -> str:
    """MinerU 云端 API（官方异步两步式）：签名上传 → 轮询 → 下载 Markdown。

    is_ocr 参数化：数字 PDF 传 False（快），扫描件传 True（走 OCR 管线）。
    失败（提交失败 / 解析失败 / 超时 / 空结果）都抛异常，由上层降级。
    """
    import requests
    base = (s.mineru_api_url or "https://mineru.net/api/v1/agent").rstrip("/")
    auth = {"Authorization": f"Bearer {s.mineru_api_key}"}
    # 1) 提交，拿 task_id + 签名上传 URL
    submit = requests.post(
        f"{base}/parse/file",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "file_name": os.path.basename(path),
            "language": "ch",
            "enable_table": True,
            "is_ocr": is_ocr,        # 数字 PDF=False；扫描件=True
            "enable_formula": True,
        },
        timeout=30,
    )
    submit.raise_for_status()
    sj = submit.json()
    if sj.get("code") != 0:
        raise ValueError(f"MinerU 提交失败: {sj.get('msg')}")
    task_id = sj["data"]["task_id"]
    file_url = sj["data"]["file_url"]
    # 2) 上传文件到签名 URL（OSS，PUT）
    with open(path, "rb") as f:
        up = requests.put(file_url, data=f, timeout=120)
    up.raise_for_status()
    # 3) 轮询任务状态（最长 4 分钟）
    poll_url = f"{base}/parse/{task_id}"
    deadline = time.time() + 240
    md_url = None
    while time.time() < deadline:
        time.sleep(3)
        pr = requests.get(poll_url, headers=auth, timeout=30).json()
        st = (pr.get("data") or {}).get("state")
        if st == "done":
            md_url = (pr.get("data") or {}).get("markdown_url")
            break
        if st == "failed":
            raise ValueError(f"MinerU 解析失败: {(pr.get('data') or {}).get('err_msg')}")
    if not md_url:
        raise TimeoutError("MinerU 解析超时（>240s）")
    # 4) 下载 Markdown
    md = requests.get(md_url, timeout=60).text
    if not md.strip():
        raise ValueError("MinerU 返回的 markdown 为空")
    return md


def _parse_via_paddle(path: str, s) -> str:
    """百度 Paddle 文档解析 API（扫描件 OCR/VL 主力）。

    百度文档解析需先换 access_token，再调解析接口；失败抛异常由上层降级。
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
    # 2) 调文档解析接口（端点以选用的百度产品为准）
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


def _read_text_file(path: str) -> str:
    """读取文本文件（utf-8 优先，gbk 兜底——Windows 中文环境常见）。"""
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    # 最后兜底：忽略非法字节，保证不崩
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


# ----------------------------------------------------------------------
# 类型识别
# ----------------------------------------------------------------------

def is_scanned_pdf(path: str, sample_pages: int = 3, min_chars_per_page: int = 25) -> bool:
    """启发式判断 PDF 是否为扫描件：抽样前几页看「可提取文字密度」。

    原理：数字 PDF 内嵌文字层，PyMuPDF 能抽出大量文字；
    扫描件本质是图片，几乎抽不出文字（每页 < 25 字符视为无文字层）。
    判断失败时保守返回 False（按数字 PDF 处理，MinerU 也能兜住）。
    """
    try:
        import fitz
        doc = fitz.open(path)
        n = min(sample_pages, doc.page_count)
        if n == 0:
            doc.close()
            return False
        total = sum(len(doc[i].get_text().strip()) for i in range(n))
        doc.close()
        return (total / n) < min_chars_per_page
    except Exception as e:
        log.warning("parsers.scan_detect_failed", error=str(e))
        return False


def detect_kind_for_text(name: str, text: str) -> str:
    """对「已在内存里的文本」按名字后缀 + 内容特征识别切分 kind。

    与 detect_kind 的区别：不读磁盘、不判 PDF（文本已解析好），
    供 ingest_text_to_store / ingest-paths 端点复用同一套判断逻辑。
    """
    ext = os.path.splitext(name)[1].lower()
    if ext in _MD_EXTS:
        return "markdown"
    if ext in _CODE_EXTS:
        return "code"
    if len(_CLAUSE_PAT.findall((text or "")[:5000])) >= 3:
        return "contract"
    return "text"


def detect_kind(path: str, content_sample: str | None = None) -> str:
    """按扩展名 + 内容特征识别文档类型，返回切分策略用的 kind。

    返回值：markdown / code / contract / pdf_digital / pdf_scanned / text
    （pdf_* 是解析入口的区分；切分时两者都映射到 text/markdown。）
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return "pdf_scanned" if is_scanned_pdf(path) else "pdf_digital"
    if ext in _MD_EXTS:
        return "markdown"
    if ext in _CODE_EXTS:
        return "code"
    # 文本类：读一段内容看是否合同条款（出现 >=3 个「第X条」）
    sample = content_sample
    if sample is None:
        try:
            sample = _read_text_file(path)[:5000]
        except Exception:
            sample = ""
    if len(_CLAUSE_PAT.findall(sample or "")) >= 3:
        return "contract"
    return "text"


# ----------------------------------------------------------------------
# 分类型解析入口（每种类型一个函数 + 统一分发）
# ----------------------------------------------------------------------

def parse_pdf_digital(path: str) -> str:
    """数字 PDF 入口：MinerU（is_ocr=False，版式解析强）→ PyMuPDF 兜底。"""
    s = get_settings()
    if s.mineru_api_key:
        try:
            return _parse_via_mineru(path, s, is_ocr=False)
        except Exception as e:
            log.warning("parsers.mineru_digital_failed", error=str(e))
    return _parse_via_pymupdf(path)


def parse_pdf_scanned(path: str) -> str:
    """扫描件 PDF 入口：Paddle 文档解析（OCR/VL 主力）→ MinerU(OCR) → PyMuPDF。

    扫描件没有文字层，PyMuPDF 兜底基本抽不到内容，但保证流程不崩、
    日志里能看到降级链路，方便运营时补配密钥。
    """
    s = get_settings()
    if s.paddle_api_key:
        try:
            return _parse_via_paddle(path, s)
        except Exception as e:
            log.warning("parsers.paddle_scanned_failed", error=str(e))
    if s.mineru_api_key:
        try:
            return _parse_via_mineru(path, s, is_ocr=True)  # OCR 管线
        except Exception as e:
            log.warning("parsers.mineru_ocr_failed", error=str(e))
    return _parse_via_pymupdf(path)


def parse_by_kind(path: str, kind: str | None = None) -> tuple[str, str]:
    """统一分发入口：文件 → (解析文本, 切分用 kind)。

    kind 传 None 时自动识别（detect_kind）；返回的 kind 是「切分策略」维度：
      - pdf_digital → 解析产物是 Markdown，切分按 markdown 处理（保留标题层级）
      - pdf_scanned → OCR 纯文本，按 text 切
      - markdown / code / contract / text → 原样
    """
    kind = kind or detect_kind(path)
    if kind == "pdf_digital":
        text = parse_pdf_digital(path)
        # MinerU 产物是 Markdown（带 # 标题）→ 用 markdown 切分策略更优；
        # PyMuPDF 兜底产物无标题结构，chunk_markdown 内部会自动退回 chunk_text。
        return text, "markdown"
    if kind == "pdf_scanned":
        return parse_pdf_scanned(path), "text"
    # 文本类文件：直接读
    text = _read_text_file(path)
    if kind in ("markdown", "code", "contract"):
        return text, kind
    # text 类再做一次内容级合同识别（上传时用户没指定 kind 的场景）
    if len(_CLAUSE_PAT.findall(text[:5000])) >= 3:
        return text, "contract"
    return text, "text"
