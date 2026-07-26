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


# ======================================================================
# 分类型切分策略（参考大厂 RAG 方案：不同文档类型用不同切分器）
#
#   - markdown：按标题层级切（LangChain MarkdownHeaderTextSplitter 思路），
#     每块携带「H1 > H2 > H3」面包屑做语义前缀，检索时标题语义能帮命中；
#   - contract（合同/条款）：以「条」为原子单元（法律语义的最小完整单位），
#     绝不把一条切成两半；块前缀带「第X章 章名」保留层级归属；
#   - code：以函数/类为原子单元（Python 走 AST 精确切，其他语言用正则兜底），
#     保留签名 + docstring，块前缀带文件名与符号路径便于溯源；
#   - text（默认）：退回通用 chunk_text（按空行分段 + 父子切分）。
#
# 所有策略最终都汇入 _parent_child_chunks，保持「子块检索、父块喂 LLM」
# 的统一收口，检索层无需感知文档类型。
# ======================================================================

# markdown 标题行：捕获 # 的个数（层级）与标题文本
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)

# 合同结构行：第X章 / 第X条（中文数字或阿拉伯数字均可）
_CLAUSE_CHAPTER = re.compile(r"^\s*(第[一二三四五六七八九十百千0-9]+章)\s*(.*)$")
_CLAUSE_ARTICLE = re.compile(r"^\s*(第[一二三四五六七八九十百千0-9]+条)\s*(.*)$")

# 代码符号行（非 Python 语言的正则兜底）：function/class/def/func 等定义开头
_CODE_SYMBOL = re.compile(
    r"^(?:export\s+)?(?:public|private|protected|static|async\s+)?\s*"
    r"(?:function|class|def|func|fn|interface|struct|impl)\s+([A-Za-z_][\w$]*)",
)


def chunk_markdown(
    source_key: str,
    title: str,
    text: str,
    parent_size: int = 1000,
    child_size: int = 350,
    child_overlap: int = 50,
) -> list[dict]:
    """Markdown 专用切分：按标题层级切段，每段带「标题面包屑」语义前缀。

    做法（大厂通用）：
      1. 扫描所有 `#`~`######` 标题行，把全文切成「标题 → 下一个标题」的小节；
      2. 维护一个层级栈（H1 > H2 > H3 …），每个小节的前缀是它完整的祖先路径，
         例如「部署指南 > Docker > 国内镜像」—— 检索 query 里出现任何一层
         标题词都能命中该块；
      3. 每个小节（前缀 + 正文）作为一段送入父子切分，超长自动滑窗。
    """
    docs = []
    matches = list(_MD_HEADING.finditer(text))
    if not matches:
        # 没有任何标题：整篇当普通文本处理
        return chunk_text(source_key, title, text, parent_size, child_size, child_overlap)

    # 标题栈：元素为 (层级, 标题文本)，用于构造面包屑
    stack: list[tuple[int, str]] = []
    seg_idx = 0

    # 文首在第一个标题之前的引言部分（若非空也要入库，别丢）
    preamble = text[: matches[0].start()].strip()
    if preamble:
        seg = f"{title}\n{preamble}" if title else preamble
        docs.extend(_parent_child_chunks(
            seg, parent_size, child_size, child_overlap, source_key, title, seg_idx))
        seg_idx += 1

    for i, m in enumerate(matches):
        level = len(m.group(1))          # '#' 个数即层级
        heading = m.group(2).strip()     # 标题文本
        # 弹出层级 >= 当前的旧标题（进入了新的同级/上级小节）
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        # 小节正文：当前标题行结束 → 下一个标题行开始（或文末）
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        # 面包屑前缀：文档名 + 祖先标题路径，作为该块的语义锚点
        crumb = " > ".join(h for _, h in stack)
        prefix = f"{title} > {crumb}" if title else crumb
        # 空小节（纯标题无正文）也保留一行标题，让目录型查询可命中
        seg = f"{prefix}\n{body}" if body else prefix
        docs.extend(_parent_child_chunks(
            seg, parent_size, child_size, child_overlap, source_key, title, seg_idx))
        seg_idx += 1
    return docs


def chunk_clauses(
    source_key: str,
    title: str,
    text: str,
    parent_size: int = 1000,
    child_size: int = 350,
    child_overlap: int = 50,
) -> list[dict]:
    """合同/条款专用切分：以「条」为原子单元，块前缀带所属章。

    法律文本的语义最小完整单位是「条」——把一条拦腰切断会让
    「违约责任是什么」这类问题只召回半句话。做法：
      1. 逐行扫描，遇「第X章」更新当前章上下文，遇「第X条」开启新条块；
      2. 每个条块 = 「文档名 · 第X章 章名 · 第X条」前缀 + 条全文；
      3. 单条超过 parent_size 才滑窗（罕见），否则整条即父块。
    """
    docs = []
    lines = text.splitlines()
    chapter = ""            # 当前所属章（如「第三章 违约责任」）
    cur: list[str] = []     # 当前条的累积行
    cur_label = ""          # 当前条号（如「第十二条」）
    seg_idx = 0
    preamble: list[str] = []  # 第一条之前的开头部分（当事人、鉴于条款等）

    def flush():
        """把累积的当前条落成检索块。"""
        nonlocal seg_idx
        body = "\n".join(cur).strip()
        if not body:
            return
        # 前缀 = 文档名 · 章 · 条号，检索「第X条」「违约」都能锚定
        parts = [p for p in (title, chapter, cur_label) if p]
        seg = "・".join(parts) + "\n" + body if parts else body
        docs.extend(_parent_child_chunks(
            seg, parent_size, child_size, child_overlap, source_key, title, seg_idx))
        seg_idx += 1

    for line in lines:
        ch = _CLAUSE_CHAPTER.match(line)
        art = _CLAUSE_ARTICLE.match(line)
        if ch:
            flush(); cur, cur_label = [], ""            # 章边界：先落上一条
            if preamble and any(l.strip() for l in preamble):
                # 开头部分（当事人、鉴于条款）在进入第一章前落库，不沾章前缀
                cur, cur_label = preamble, ""
                flush()
                cur, preamble = [], []
            chapter = f"{ch.group(1)} {ch.group(2)}".strip()
        elif art:
            if cur:
                flush()                                  # 条边界：落上一条
            elif preamble:
                # 第一条出现前，先把开头部分（当事人信息等）落库
                cur, cur_label = preamble[:], ""
                flush()
                preamble = []
            cur = [line]                                 # 新条从条号行开始
            cur_label = art.group(1)
        elif cur:
            cur.append(line)                             # 条内正文行
        else:
            preamble.append(line)                        # 尚未遇到第一条
    flush()  # 收尾：最后一条

    if not docs:
        # 完全没有「第X条」结构：退回通用文本切分，别让内容丢失
        return chunk_text(source_key, title, text, parent_size, child_size, child_overlap)
    if preamble and any(l.strip() for l in preamble):
        # 理论上 preamble 已在第一条前落库；此处兜底防丢
        cur, cur_label = preamble, ""
        flush()
    return docs


def chunk_code(
    source_key: str,
    title: str,
    text: str,
    parent_size: int = 1500,
    child_size: int = 400,
    child_overlap: int = 50,
) -> list[dict]:
    """代码专用切分：以函数/类为原子单元（Python 走 AST，其他语言正则兜底）。

    代码块比散文更「贵」：一个函数被拦腰切断后既不可读也不可运行。做法：
      1. Python：ast 解析出顶层 函数/类 的起止行，精确按符号切；
         模块级 import / 常量归入「模块头」块；
      2. 非 Python / 解析失败：按 function/class/def 等定义行的正则切；
      3. 每块前缀「文件名 · 符号名」，父块尺寸放大到 1500（代码行更长）。
    """
    docs = []
    seg_idx = 0

    def emit(label: str, body: str):
        """把一个代码符号落成检索块（前缀带文件与符号名便于溯源）。"""
        nonlocal seg_idx
        body = body.strip("\n")
        if not body.strip():
            return
        prefix = f"{title}・{label}" if label else title
        seg = f"{prefix}\n{body}" if prefix else body
        docs.extend(_parent_child_chunks(
            seg, parent_size, child_size, child_overlap, source_key, title, seg_idx))
        seg_idx += 1

    # ---- 路径 1：Python AST 精确切分 ----
    try:
        import ast
        tree = ast.parse(text)
        lines = text.splitlines()
        spans: list[tuple[int, int, str]] = []  # (起行, 止行, 符号名)，行号 1-based
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # decorator 行也算进符号（部署/路由语义常在装饰器上）
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                spans.append((start, node.end_lineno or node.lineno, node.name))
        if spans:
            # 模块头：第一个符号之前的 import / 常量 / 模块 docstring
            head_end = spans[0][0] - 1
            if head_end > 0:
                emit("模块头", "\n".join(lines[:head_end]))
            for start, end, name in spans:
                emit(name, "\n".join(lines[start - 1 : end]))
            return docs
    except SyntaxError:
        pass  # 不是合法 Python：走正则兜底

    # ---- 路径 2：正则兜底（JS/TS/Go/Rust/Java 等）----
    lines = text.splitlines()
    cut_points = [i for i, l in enumerate(lines) if _CODE_SYMBOL.match(l.strip())]
    if not cut_points:
        # 连符号定义都没有（配置片段等）：整篇滑窗，但保留代码尺寸参数
        emit("", text)
        return docs
    if cut_points[0] > 0:
        emit("文件头", "\n".join(lines[: cut_points[0]]))
    for j, start in enumerate(cut_points):
        end = cut_points[j + 1] if j + 1 < len(cut_points) else len(lines)
        m = _CODE_SYMBOL.match(lines[start].strip())
        emit(m.group(1) if m else "", "\n".join(lines[start:end]))
    return docs


def chunk_by_kind(
    kind: str,
    source_key: str,
    title: str,
    text: str,
    **kw,
) -> list[dict]:
    """按文档类型分发到对应切分策略（入库层唯一入口）。

    kind 取值：
      - "markdown"：MD 文档（标题层级感知）
      - "contract"：合同/条款（按条切）
      - "code"    ：源代码（函数/类级）
      - 其他/"text"：通用文本（含 PDF 解析产物）
    """
    if kind == "markdown":
        return chunk_markdown(source_key, title, text, **kw)
    if kind == "contract":
        return chunk_clauses(source_key, title, text, **kw)
    if kind == "code":
        return chunk_code(source_key, title, text, **kw)
    return chunk_text(source_key, title, text, **kw)


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
