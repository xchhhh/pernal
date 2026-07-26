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


def _parent_child_chunks(
    segment: str,
    parent_size: int,
    child_size: int,
    child_overlap: int,
    source_key: str,
    title: str,
    seg_idx: int,
) -> list[dict]:
    """把一段文本切成「父子」结构，返回 [{text, metadata}]。

    设计（父子切分 / parent-child chunking）：
      - 父块：对整段做滑窗（父块之间重叠 child_size，保证切换处语义连续），
        作为「完整上下文」最终喂给 LLM；
      - 子块：在父块内再做小滑窗，作为「检索单元」被向量化 / 进 BM25，
        检索精度高、不会把一句话切散；
      - metadata 里带上 parent_text（父块全文）与 parent_id（父块稳定 id），
        检索命中子块后，由 retrieval 层回退到父块并按 parent_id 去重。

    短文本（< child_size）时父块==子块==整段，退化为单块，不会变碎。
    """
    docs = []
    # 父块：整段滑窗，步长 = parent_size - child_size（父块间重叠一个子块宽度）
    parents = _sliding_window(segment, parent_size, max(1, parent_size - child_size))
    for p_idx, parent in enumerate(parents):
        parent_id = f"{source_key}::s{seg_idx}p{p_idx}"   # 父块稳定 id（去重键）
        # 子块：父块内细切，作为检索单元
        children = _sliding_window(parent, child_size, child_overlap)
        for c_idx, child in enumerate(children):
            docs.append({
                "text": child,                              # 子块：被向量化 / 进 BM25
                "metadata": {
                    "section_key": source_key,
                    "title": title,
                    "doc_id": f"{parent_id}c{c_idx}",       # 子块稳定 id（向量/B25 主键）
                    "parent_id": parent_id,                 # 检索后按它去重、回退父块
                    "parent_text": parent,                  # 命中后回退给 LLM 的完整上下文
                    "chunk_type": "child",
                },
            })
    return docs


def chunk_section(
    section_key: str,
    title: str,
    body,
    parent_size: int = 1000,
    child_size: int = 350,
    child_overlap: int = 50,
) -> list[dict]:
    """把一个板块切成「父子」检索块，返回 [{text, metadata}]。

    metadata 带 section_key / title / doc_id（子块）/ parent_id / parent_text（父块）。
    """
    docs = []
    for idx, segment in enumerate(_flatten_body(body)):
        docs.extend(_parent_child_chunks(
            segment, parent_size, child_size, child_overlap, section_key, title, idx
        ))
    return docs


def chunk_text(
    source_key: str,
    title: str,
    text: str,
    parent_size: int = 1000,
    child_size: int = 350,
    child_overlap: int = 50,
) -> list[dict]:
    """把「一段自由文本」（如 PDF 解析出的正文）切成「父子」检索块，返回 [{text, metadata}]。

    与 chunk_section 不同：这里输入是纯文本，先按换行分段，每段再父子切分。
    供管理员上传 PDF 后实时入库用（doc_id 用 source_key 溯源）。
    """
    docs = []
    # 按空行/换行把长文拆成若干段，避免一整段滑窗把不同主题混在一起
    segments = [seg for seg in re.split(r"\n+", text) if seg.strip()]
    if not segments:
        segments = [text]
    for idx, seg in enumerate(segments):
        docs.extend(_parent_child_chunks(
            seg, parent_size, child_size, child_overlap, source_key, title, idx
        ))
    return docs


def collapse_to_parents(candidates: list[dict]) -> list[dict]:
    """父子切分收口（纯逻辑，可被检索层与单测复用，无重依赖）。

    检索命中的是「子块」（精细但碎），这里回退到「父块」并按下父块去重：
      - 若候选 metadata 带 parent_id + parent_text，则 text 替换为 parent_text，
        同一 parent_id 只保留第一次命中的子块（去重，防止重复进上下文）；
      - 若没有 parent 信息（老数据 / 仅 BM25 命中的短片段），原样保留，不退化。
    """
    out, seen = [], set()
    for c in candidates:
        meta = dict(c.get("metadata") or {})
        pid = meta.get("parent_id")
        ptext = meta.get("parent_text")
        if pid and ptext:
            if pid in seen:
                continue
            seen.add(pid)
            c = {**c, "text": ptext, "metadata": meta}
        out.append(c)
    return out
