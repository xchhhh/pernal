# ======================================================================
# 混合检索存储：向量召回(Chroma) + 关键词召回(SQLite FTS5 BM25)
#
# 设计要点（资深视角）：
#   - Chroma 用「进程内 PersistentClient」，不引独立向量服务，契合 2c2g；
#   - BM25 走 SQLite 内置 FTS5 虚拟表，零额外进程；
#   - 两者各取 Top-K 后由 retrieval.py 做 RRF 融合，互补长短（向量懂语义、BM25 懂关键词）；
#   - embed_fn 可注入，方便单测时用确定性的假 embedding，无需云端 key。
# ======================================================================
import chromadb

from app.core.config import get_settings
from app.core.db import engine


class HybridStore:
    """混合检索存储：同时管理 Chroma 向量集合与 FTS5 关键词索引。"""

    def __init__(self, collection_name: str = "portal", embed_fn=None, embed_model_name: str = None):
        s = get_settings()
        # 1) Chroma 进程内持久化客户端（数据落在 chroma_persist_dir 目录）
        self._client = chromadb.PersistentClient(path=s.chroma_persist_dir)
        # 用余弦距离，语义相似度更直观。
        # 版本化：把 embedding 模型名记进 collection 元数据。
        # 若模型/维度变了（比如从假 embedding 切到真实 bge），旧集合维度不匹配会报错，
        # 这里检测到不一致就删掉旧集合重建，避免「Collection expecting embedding with
        # dimension of 32, got 512」这类维度冲突把检索链路卡死（VPS 换模型也同理）。
        try:
            coll = self._client.get_collection(collection_name)
            if embed_model_name and coll.metadata.get("embedding_model") != embed_model_name:
                self._client.delete_collection(collection_name)
                coll = None
        except Exception:
            coll = None
        self._collection = coll or self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine", "embedding_model": embed_model_name},
        )
        # 注入的向量化函数（测试时可传假 embedding）
        self._embed_fn = embed_fn
        # FTS5 表名（BM25 用）
        self._fts_table = "bm25_docs"
        self._ensure_fts()

    # ---------- 写入 ----------
    def add_documents(self, docs: list[dict]) -> None:
        """把切块后的文档写入向量库 + BM25 索引。docs=[{text, metadata}]。"""
        if not docs:
            return
        texts = [d["text"] for d in docs]
        metas = [d["metadata"] for d in docs]
        # id 用 doc_id + 序号，保证同板块多块不冲突且可溯源
        ids = [f"{m['doc_id']}#{i}" for i, m in enumerate(metas)]
        # 向量化（批量）
        embeds = self._embed_fn(texts)
        # 写入 Chroma（upsert：重复 id 覆盖，支持重建索引）
        self._collection.upsert(
            ids=ids, documents=texts, metadatas=metas, embeddings=embeds
        )
        # 写入 FTS5 供关键词召回
        self._index_bm25(ids, texts, metas)

    # ---------- 向量召回 ----------
    def vector_search(self, text: str, k: int) -> list[tuple]:
        """返回 [(id, document, metadata, distance)] 按相似度升序。"""
        emb = self._embed_fn([text])[0]
        res = self._collection.query(query_embeddings=[emb], n_results=k)
        ids = res["ids"][0]
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        dists = res["distances"][0]
        return list(zip(ids, docs, metas, dists))

    # ---------- BM25 关键词召回 ----------
    def _ensure_fts(self) -> None:
        """建 FTS5 虚拟表（已存在就跳过）。"""
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._fts_table} "
                f"USING fts5(id UNINDEXED, text, section_key UNINDEXED, title UNINDEXED)"
            )

    def _index_bm25(self, ids, texts, metas) -> None:
        """把文档写进 FTS5（先删旧再插新，支持重建）。"""
        with engine.begin() as conn:
            for _id, _t, _m in zip(ids, texts, metas):
                conn.exec_driver_sql(
                    f"DELETE FROM {self._fts_table} WHERE id = ?", (_id,)
                )
                conn.exec_driver_sql(
                    f"INSERT INTO {self._fts_table} (id, text, section_key, title) "
                    f"VALUES (?, ?, ?, ?)",
                    (_id, _t, _m.get("section_key", ""), _m.get("title", "")),
                )

    def bm25_search(self, text: str, k: int) -> list[tuple]:
        """FTS5 MATCH 关键词召回，返回 [(id, document, metadata, rank)]。

        注意：SQLite FTS5 默认分词器对中文支持弱（按整句匹配）。
        生产可换 jieba 分词器提升中文召回；当前用 MATCH 兜底，配合向量召回弥补。
        """
        # 把查询按空格/标点拆词，用 OR 连接，提升召回鲁棒性
        terms = " OR ".join([t for t in text.replace("，", " ").split() if t])
        if not terms:
            return []
        rows = []
        with engine.connect() as conn:
            result = conn.exec_driver_sql(
                f"SELECT id, text, section_key, title, rank FROM {self._fts_table} "
                f"WHERE {self._fts_table} MATCH ? ORDER BY rank LIMIT ?",
                (terms, k),
            )
            for _id, _t, _sec, _title, _rank in result:
                rows.append((_id, _t, {"section_key": _sec, "title": _title}, _rank))
        return rows

    # ---------- 清空（重建索引用）----------
    def clear(self) -> None:
        """清空向量集合与 BM25 表，供「重建索引」时先清后写，避免重复。"""
        # 删掉旧集合再重建（collection 名不变，调用方持有的 store 对象仍有效）
        try:
            self._client.delete_collection(self._collection.name)
        except Exception:
            pass
        self._collection = self._client.get_or_create_collection(
            name=self._collection.name, metadata={"hnsw:space": "cosine"}
        )
        with engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM {self._fts_table}")
