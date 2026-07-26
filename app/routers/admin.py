# ======================================================================
# 管理员后台路由
#
# 功能：登录（签名 Cookie 鉴权）→ 上传 PDF 实时更新向量库 → 查看已索引来源 → 重建索引。
#
# 安全：所有写操作都套了 require_admin 依赖（校验签名 Cookie），未登录直接 403。
# 这是「个人站 + 单管理员」场景下的极简方案，不引用户表/JWT。
# ======================================================================
import os
import pathlib
import tempfile

from fastapi import APIRouter, Body, Depends, Form, Query, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.auth import AUTH_COOKIE, is_authed, make_cookie_value, require_admin
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.data.models import IngestedDoc
from app.rag import api as ai_api          # 复用 _get_store / ensure_index
from app.rag.chunking import chunk_section, chunk_text
from app.services import content
from app.services.ingest import ingest_pdf_to_store, ingest_text_to_store

# 入库白名单：只收这些扩展名的文本文件，避免把二进制/密钥塞进向量库
_INGEST_ALLOWED_EXT = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".html",
    ".css", ".js", ".sh", ".cfg", ".ini", ".dockerfile", ".env.example",
}
# 噪声目录：递归遍历时跳过（调试缓存/依赖/密钥/版本控制）
_INGEST_SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".workbuddy", "node_modules",
    ".venv", "venv", "ai_portal.egg-info", ".github",
}
_INGEST_MAX_BYTES = 256_000  # 单文件上限 ~256KB，跳过超大文件

router = APIRouter()


def _tpl(request: Request):
    """取模板引擎（挂在 app.state 上）。"""
    return request.app.state.templates


# ---------- 登录 ----------
@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: int = Query(default=0)):
    """登录页。已登录就直接进后台；error=1 时展示「口令错误」提示。"""
    if is_authed(request.cookies.get(AUTH_COOKIE)):
        return RedirectResponse(url="/admin", status_code=303)
    return _tpl(request).TemplateResponse(
        request, "admin_login.html", {"request": request, "error": bool(error)}
    )


@router.post("/api/admin/login")
async def admin_login(request: Request, token: str = Form(...)):
    """校验管理员口令；正确则下发签名 Cookie 并跳到后台；错误回登录页并带提示。"""
    s = get_settings()
    if token != s.admin_token:
        # 303 重定向回登录页（带 error 标记），比裸 HTML 报错友好
        return RedirectResponse(url="/admin/login?error=1", status_code=303)
    resp = RedirectResponse(url="/admin", status_code=303)
    # httpOnly + SameSite=Lax：防 XSS 读 Cookie、防 CSRF 简单攻击
    resp.set_cookie(AUTH_COOKIE, make_cookie_value(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


# ---------- 后台主页（页面：未登录先跳登录页，不再吐裸 JSON）----------
@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    """后台主页：上传表单 + 已索引来源列表。

    页面访问场景：未登录 303 跳 /admin/login（浏览器地址栏直达 /admin 时不再看到
    {"detail":"未登录..."} 的裸 JSON）。API 场景（/api/admin/*）仍用 require_admin 返 403。
    """
    if not is_authed(request.cookies.get(AUTH_COOKIE)):
        return RedirectResponse(url="/admin/login", status_code=303)
    with SessionLocal() as db:
        docs = db.query(IngestedDoc).order_by(IngestedDoc.created_at.desc()).all()
        sources = [
            {"source_name": d.source_name, "chunk_count": d.chunk_count, "created_at": str(d.created_at)}
            for d in docs
        ]
    return _tpl(request).TemplateResponse(
        request, "admin.html", {"request": request, "sources": sources}
    )


# ---------- 上传 PDF（需登录）：实时更新向量库 ----------
@router.post("/api/admin/upload")
async def admin_upload(
    request: Request,
    _: bool = Depends(require_admin),
    file: UploadFile = File(...),
):
    """接收 PDF → 解析（MinerU/Paddle/PyMuPDF）→ 切块 → 实时写入向量库。"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"ok": False, "error": "只接受 PDF 文件"}, status_code=400)
    # 先落到临时文件（Chroma/PyMuPDF 都更爱吃文件路径）
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.write(await file.read())
    tmp.close()
    try:
        store = ai_api._get_store()
        source_name = file.filename
        chunk_count, _ = ingest_pdf_to_store(tmp.name, source_name, store)
        return JSONResponse({"ok": True, "source": source_name, "chunks": chunk_count})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass


# ---------- 入库「项目代码 / 项目资料」（需登录）：直接向量化进网站 ----------
@router.post("/api/admin/ingest-paths")
async def admin_ingest_paths(
    request: Request,
    _: bool = Depends(require_admin),
    body: dict = Body(default={}),
):
    """把指定路径（容器内文件/目录）的「项目代码 / 项目资料」向量化入库。

    请求体：{"paths": ["/app/app", {"path": "/app/contract.txt", "kind": "contract"}, ...]}
      - 每项可以是「字符串路径」（自动识别类型）或 {"path", "kind"}（显式指定切分策略）；
        kind 取值：markdown / code / contract / text；
      - 目录：递归遍历，只收白名单扩展名、排除噪声目录、跳过超大文件；
      - 文件：直接读；
    每个文件作为一个 IngestedDoc 持久化，并实时写入向量库（Chroma+BM25），
    立即可被问答检索。所有路径必须位于 /app 内（防越权读系统文件）。
    """
    paths = body.get("paths") or []
    root = pathlib.Path("/app")
    store = ai_api._get_store()
    ingested, errors = [], []
    total_chunks = 0
    for p in paths:
        # 每项支持两种形态：纯字符串（自动识别）或 {"path","kind"}（显式指定）
        forced_kind = None
        if isinstance(p, dict):
            forced_kind = p.get("kind") or None
            p = p.get("path") or ""
        pp = pathlib.Path(p)
        try:
            pp_resolved = pp.resolve()
        except Exception:
            errors.append(f"无法解析路径: {p}")
            continue
        # 越权保护：只允许 /app 内部
        if root not in pp_resolved.parents and pp_resolved != root:
            errors.append(f"跳过越权路径（必须在 /app 内）: {p}")
            continue
        targets = []
        if pp_resolved.is_dir():
            for f in sorted(pp_resolved.rglob("*")):
                if not f.is_file():
                    continue
                if any(part in _INGEST_SKIP_DIRS for part in f.parts):
                    continue
                if f.suffix.lower() not in _INGEST_ALLOWED_EXT:
                    continue
                try:
                    if f.stat().st_size > _INGEST_MAX_BYTES:
                        continue
                except Exception:
                    continue
                targets.append(f)
        elif pp_resolved.is_file():
            targets.append(pp_resolved)
        else:
            errors.append(f"路径不存在: {p}")
            continue
        for f in targets:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                errors.append(f"{f}: 读取失败 {e}")
                continue
            if not text.strip():
                continue
            rel = str(f.relative_to(root))
            source = f"code::{rel}"
            try:
                # kind：显式指定 > 按扩展名/内容自动识别（在 ingest 层完成）
                n, _ = ingest_text_to_store(
                    source, f"# 文件：{rel}\n\n{text}", store, kind=forced_kind
                )
                total_chunks += n
                ingested.append(rel)
            except Exception as e:
                errors.append(f"{rel}: 入库失败 {e}")
    return JSONResponse({
        "ok": True,
        "ingested_files": ingested,
        "file_count": len(ingested),
        "chunk_count": total_chunks,
        "errors": errors,
    })


# ---------- 已索引来源列表（需登录）----------
@router.get("/api/admin/sources")
async def admin_sources(_: bool = Depends(require_admin)):
    """返回所有已上传并入库的文档来源。"""
    with SessionLocal() as db:
        docs = db.query(IngestedDoc).order_by(IngestedDoc.created_at.desc()).all()
        return JSONResponse([
            {"source_name": d.source_name, "chunk_count": d.chunk_count, "created_at": str(d.created_at)}
            for d in docs
        ])


# ---------- 重建索引（需登录）----------
@router.post("/api/admin/reindex")
async def admin_reindex(request: Request, _: bool = Depends(require_admin)):
    """清空向量库+BM25，从「数据库板块 + 已上传文档」重新切块入库。

    适用场景：改了简历内容、或上传了新文档想整体刷新时一键重建。
    """
    ai_api.ensure_index()  # 确保 store 已初始化（同步函数，勿 await）
    store = ai_api._get_store()
    store.clear()
    # 1) 9 个板块
    sections = content.get_all_sections()
    s = get_settings()
    docs = []
    for key, sec in sections.items():
        docs.extend(chunk_section(key, sec.get("title", key), sec.get("body"),
                                  parent_size=s.rag_parent_size,
                                  child_size=s.rag_child_size,
                                  child_overlap=s.rag_child_overlap))
    # 2) 已上传文档（从 ingested_docs 表取原文，按类型重新切）
    from app.rag.chunking import chunk_by_kind
    from app.services.parsers import detect_kind_for_text
    with SessionLocal() as db:
        ingested = db.query(IngestedDoc).all()
    for d in ingested:
        kind = detect_kind_for_text(d.source_name, d.text)
        docs.extend(chunk_by_kind(kind, f"doc::{d.source_name}", d.source_name, d.text,
                                  parent_size=s.rag_parent_size,
                                  child_size=s.rag_child_size,
                                  child_overlap=s.rag_child_overlap))
    store.add_documents(docs)
    return JSONResponse({"ok": True, "reindexed_chunks": len(docs)})
