# ======================================================================
# 切块（chunking）：把结构化的板块内容拍平成可检索的文本块
#
# 为什么单独成文件：切块策略直接影响 RAG 召回质量，是独立可测的纯逻辑。
# 这里用「先按字段拍平，再做滑动窗口」的简单稳健策略，
# 适合个人站这种体量小、内容不超长的数据。
# ======================================================================
import re


def _flatten_body(body) -> list[str]:
    """把板块的 JSON 正文拍平成若干段文本。

    支持三种形态：
      - 字符串：直接一段
      - 列表：每个元素再递归拍平
      - 字典：把「键: 值」拼成一个可读句（如 "技术栈: Python, FastAPI"）
    """
    if body is None:
        return []
    if isinstance(body, str):
        return [body] if body.strip() else []
    if isinstance(body, list):
        out = []
        for item in body:
            out.extend(_flatten_body(item))
        return out
    if isinstance(body, dict):
        out = []
        for key, val in body.items():
            flat = _flatten_body(val)
            for f in flat:
                out.append(f"{key}：{f}")
        return out
    # 其它类型（数字等）转字符串
    return [str(body)]


def _sliding_window(text: str, chunk_size: int, overlap: int) -> list[str]:
    """对一段文本做带重叠的滑动窗口切块。"""
    if len(text) <= chunk_size:
        return [text]
    step = max(1, chunk_size - overlap)  # 每次前进的步长
    chunks = []
    for i in range(0, len(text), step):
        chunk = text[i : i + chunk_size]
        if chunk:
            chunks.append(chunk)
    return chunks


def chunk_section(
    section_key: str,
    title: str,
    body,
    chunk_size: int = 500,
    overlap: int = 80,
) -> list[dict]:
    """把一个板块切成若干检索块，返回 [{text, metadata}]。

    metadata 带 section_key / title / doc_id，供召回后溯源与去重。
    """
    docs = []
    for idx, segment in enumerate(_flatten_body(body)):
        for chunk in _sliding_window(segment, chunk_size, overlap):
            docs.append({
                "text": chunk,
                "metadata": {
                    "section_key": section_key,
                    "title": title,
                    "doc_id": f"{section_key}::{idx}",  # 稳定 id，RRF 去重用
                },
            })
    return docs


def chunk_text(
    source_key: str,
    title: str,
    text: str,
    chunk_size: int = 500,
    overlap: int = 80,
) -> list[dict]:
    """把「一段自由文本」（如 PDF 解析出的正文）切成检索块，返回 [{text, metadata}]。

    与 chunk_section 不同：这里输入是纯文本，先按换行分段，每段再滑窗切块。
    供管理员上传 PDF 后实时入库用（doc_id 用 source_key 溯源）。
    """
    docs = []
    # 按空行/换行把长文拆成若干段，避免一整段滑窗把不同主题混在一起
    segments = [seg for seg in re.split(r"\n+", text) if seg.strip()]
    if not segments:
        segments = [text]
    for idx, seg in enumerate(segments):
        for chunk in _sliding_window(seg, chunk_size, overlap):
            docs.append({
                "text": chunk,
                "metadata": {
                    "section_key": source_key,
                    "title": title,
                    "doc_id": f"{source_key}::{idx}",
                },
            })
    return docs
