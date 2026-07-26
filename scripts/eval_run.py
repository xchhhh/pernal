# ======================================================================
# RAG 端到端评测脚本（在应用容器内运行：docker cp 进去后 docker exec 执行）
#
# 用法（容器内）：
#   python /tmp/eval_run.py            # 跑全部：检索层 + 生成层
#   python /tmp/eval_run.py --no-gen   # 只跑检索层（不调 judge，省钱省时）
#
# 金标准（gold set）说明：
#   - relevant_keys 用「section_key / doc 前缀」匹配（宽松版金标准）：
#     ranked_ids 形如 "projects::s0p0" / "doc::README.md::s3p0"，
#     只要 id 以某个 relevant_key 开头即视为相关。
#     这样标注成本低、且不受父块滑窗编号变化影响。
#   - keywords：生成层辅助断言——答案里应出现的关键词（覆盖率参考）。
# ======================================================================
import argparse
import json
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from app.rag.eval_metrics import generation_report, retrieval_report  # noqa: E402

# ---------------- 金标准问答集（按板块 key 标注相关来源） ----------------
GOLD = [
    {
        "q": "他做过哪些 AI 项目？",
        "relevant_keys": ["projects"],
        "keywords": ["AI 应用开发门户", "RAG", "LoRA"],
    },
    {
        "q": "他精通哪些技术栈？",
        "relevant_keys": ["skills"],
        "keywords": ["Python", "FastAPI", "LangChain"],
    },
    {
        "q": "他的教育背景是什么？",
        "relevant_keys": ["education"],
        "keywords": ["学校", "专业"],
    },
    {
        "q": "他的求职方向是什么？",
        "relevant_keys": ["profile", "summary"],
        "keywords": ["AI"],
    },
    {
        "q": "这个门户网站是怎么部署的？",
        "relevant_keys": ["deployment", "projects", "architecture"],
        "keywords": ["Docker", "Caddy"],
    },
    {
        "q": "RAG 检索管线包含哪些步骤？",
        "relevant_keys": ["architecture", "projects", "doc::"],
        "keywords": ["检索", "rerank"],
    },
    {
        "q": "他有哪些个人优势？",
        "relevant_keys": ["summary", "profile"],
        "keywords": ["优势"],
    },
    {
        "q": "微调项目用了什么方法？",
        "relevant_keys": ["projects"],
        "keywords": ["LoRA", "量化"],
    },
]


def _match(ranked_ids: list[str], relevant_keys: list[str]) -> tuple[list[str], set[str]]:
    """把 ranked_ids 映射成「命中的 relevant_key」序列，构造二值相关集合。

    宽松匹配：id 以 key 开头（如 "projects::s0p0" 匹配 "projects"）。
    返回 (映射后的排名列表, 相关集合)——直接喂给 retrieval_report。
    """
    mapped = []
    for rid in ranked_ids:
        hit = next((k for k in relevant_keys if rid.startswith(k)), None)
        mapped.append(hit if hit else rid)  # 命中→归一成 key；未命中→保留原 id
    return mapped, set(relevant_keys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gen", action="store_true", help="跳过生成层 judge（只测检索）")
    args = ap.parse_args()

    # 延迟导入：这些模块要在容器/项目环境里才 import 得动
    from app.rag import api as ai_api
    from app.rag.llm import get_embeddings, get_llm
    from app.rag.retrieval import compress, retrieve_with_trace

    ai_api.ensure_index()
    store = ai_api._get_store()
    emb = get_embeddings()
    llm = get_llm()

    def llm_invoke(p: str) -> str:
        return llm.invoke(p).content

    ret_rows, gen_rows = [], []
    for item in GOLD:
        q = item["q"]
        candidates, trace = retrieve_with_trace(q, store, emb, llm)
        # 新版 trace 直接带 ranked_ids；旧版没有 → 从最终候选块推导（兼容 before 基线评测）
        ranked = trace.get("ranked_ids") or [
            (c.get("metadata") or {}).get("parent_id") or c["id"] for c in candidates
        ]
        mapped, rel = _match(ranked, item["relevant_keys"])
        rr = retrieval_report(mapped, rel, ks=(3, 5))
        rr["q"] = q
        ret_rows.append(rr)
        print(f"[检索] {q}  MRR={rr['mrr']}  R@5={rr['recall@5']}  P@5={rr['precision@5']}  NDCG@5={rr['ndcg@5']}")

        if not args.no_gen:
            ctx = compress(candidates)
            ans = llm_invoke(
                "你是个人门户的 AI 助手，只能依据下方【资料】回答。"
                "忠实、切题、覆盖全相关要点，禁止双引号。\n"
                f"【资料】\n{ctx}\n\n【问题】{q}\n【回答】"
            )
            gr = generation_report(llm_invoke, q, ctx, ans)
            # 关键词覆盖率：辅助观察答案完整性
            kws = item.get("keywords") or []
            gr["keyword_cover"] = round(
                sum(1 for k in kws if k.lower() in ans.lower()) / len(kws), 2
            ) if kws else None
            gr["q"] = q
            gen_rows.append(gr)
            print(f"[生成] {q}  忠实={gr['faithfulness']}  相关={gr['answer_relevance']}  "
                  f"利用={gr['context_utilization']}  安全={gr['safety']}  关键词覆盖={gr['keyword_cover']}")

    # ---------------- 汇总 ----------------
    def avg(rows, key):
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else None

    summary = {
        "retrieval": {k: avg(ret_rows, k) for k in ("mrr", "recall@3", "recall@5", "precision@5", "ndcg@5")},
    }
    if gen_rows:
        summary["generation"] = {
            k: avg(gen_rows, k)
            for k in ("faithfulness", "answer_relevance", "context_utilization", "safety", "keyword_cover")
        }
    print("\n===== 汇总 =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
