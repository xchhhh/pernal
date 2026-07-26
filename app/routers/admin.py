# ======================================================================
# 管理员后台路由
#
# 功能：登录（签名 Cookie 鉴权）→ 上传 PDF 实时更新向量库 → 查看已索引来源 → 重建索引。
#
# 安全：所有写操作都套了 require_admin 依赖（校验签名 Cookie），未登录直接 403。
# 这是「个人站 + 单管理员」场景下的极简方案，不引用户表/JWT。
# ======================================================================
import os
import tempfile

from fastapi import APIRouter, Depends, Form, Query, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.auth import AUTH_COOKIE, is_authed, make_cookie_value, require_admin
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.data.models import IngestedDoc
from app.rag import api as ai_api          # 复用 _get_store / ensure_index
from app.rag.chunking import chunk_section, chunk_text
from app.services import content
from app.services.ingest import ingest_pdf_to_store

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
    await ai_api.ensure_index()  # 确保 store 已初始化
    store = ai_api._get_store()
    store.clear()
    # 1) 9 个板块
    sections = content.get_all_sections()
    s = get_settings()
    docs = []
    for key, sec in sections.items():
        docs.extend(chunk_section(key, sec.get("title", key), sec.get("body"),
                                  chunk_size=s.rag_chunk_size, overlap=s.rag_chunk_overlap))
    # 2) 已上传文档（从 ingested_docs 表取原文重新切）
    with SessionLocal() as db:
        ingested = db.query(IngestedDoc).all()
    for d in ingested:
        docs.extend(chunk_text(f"doc::{d.source_name}", d.source_name, d.text,
                               chunk_size=s.rag_chunk_size, overlap=s.rag_chunk_overlap))
    store.add_documents(docs)
    return JSONResponse({"ok": True, "reindexed_chunks": len(docs)})
