# ======================================================================
# 切块（chunking）：把结构化的板块内容转成「连贯可读文本」再做父子切分
#
# 为什么单独成文件：切块策略直接影响 RAG 召回质量，是独立可测的纯逻辑。
#
# 关键设计（踩坑后修订）：
#   旧版先 `_flatten_body` 把结构化数据拍成「一个键:值一行」的原子叶子
#   （如 "role：AI 应用开发"），再对每个叶子单独做父子切分 —— 因为每片都
#   远小于父块尺寸，父块==叶子本身，父子切分完全失效，且检索只能捞到
#   零散碎片，拼进 prompt 时引号散架、还漏掉其他项目。
#   新版改为：先把整个板块序列化成「连贯段落」（保留层级、不拆散），
#   再对这段连贯文本做父子切分 —— 子块做精细检索，命中后回退到「包含
#   整个板块/项目」的父块给 LLM，召回连贯、不再丢信息。
# ======================================================================
import re

# 键名→中文标签：序列化板块时把原始字段名翻成可读标签，
# 让 LLM 读到的上下文更自然（如 "name：…" → "项目名：…"）。
_KEY_LABELS = {
    "name": "项目名", "role": "角色", "stack": "技术栈", "highlights": "亮点",
    "languages": "语言", "frameworks": "框架", "ai_stack": "AI 栈",
    "tools": "工具", "cloud": "云", "finetune": "微调", "vector_db": "向量库",
    "school": "学校", "major": "专业", "degree": "学历", "duration": "时间",
    "courses": "课程", "honors": "荣誉",
    "summary": "总结", "strengths": "优势",
    "github": "GitHub", "blog": "博客", "opensource": "开源",
    "layers": "层级", "note": "备注",
    "cicd": "CI/CD", "observability": "可观测性", "security": "安全",
    "headline": "标语", "target_role": "求职方向", "location": "地点", "tagline": "简介",
    "layer": "层", "tech": "技术", "subject": "主体", "relation": "关系", "obj": "客体",
}


def _inline(v) -> str:
    """把列表/字典压成一行可读文本（用于板块序列化，不拆成独立叶子）。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "、".join(_inline(x) for x in v)
    if isinstance(v, dict):
        return "；".join(f"{_KEY_LABELS.get(k, k)}：{_inline(val)}" for k, val in v.items())
    return str(v)


def _section_to_text(body) -> str:
    """把板块正文序列化成一段连贯可读文本（保留层级，但不拍散成原子叶子）。

    支持三种形态：
      - 字符串：直接一段
      - 列表：每个元素序列化后换行拼接（如多个项目各自成段）
      - 字典：把「键: 值」拼成可读句（值为列表/字典时压成一行，不拆散）
    """
    if body is None:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, list):
        parts = [_section_to_text(item) for item in body]
        return "\n".join(p for p in parts if p)
    if isinstance(body, dict):
        lines = []
        for k, v in body.items():
            label = _KEY_LABELS.get(k, k)
            if isinstance(v, (list, dict)):
                lines.append(f"{label}：{_inline(v)}")
            else:
                lines.append(f"{label}：{v}")
        return "\n".join(lines)
    # 其它类型（数字等）转字符串
    return str(body)


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

    设计：先把整个板块序列化成「连贯段落」（保留层级不拆散），
    再对这段连贯文本做父子切分 —— 父块即包含整个板块的大块，
    命中子块后回退到父块，LLM 拿到的是连贯完整上下文，不再丢信息。
    metadata 带 section_key / title / doc_id（子块）/ parent_id / parent_text（父块）。
    """
    text = _section_to_text(body)
    if not text:
        return []
    # 标题作为整段语义前缀，帮助检索定位板块
    full = f"{title}\n{text}" if title else text
    # 整段作为「父块」切分；> parent_size 时滑窗成多个父块（每个仍是连贯大块）
    return _parent_child_chunks(
        full, parent_size, child_size, child_overlap, section_key, title, 0
    )


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
