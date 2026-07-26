# ======================================================================
# 页面路由（SSR 真实版）
#
# 职责（资深视角）：路由只做三件事——收请求、取数据、渲模板。
#   业务逻辑（读板块内容）下沉到 app/services/content.py，写库下沉到模型层。
#   本文件保持「瘦」，便于以后加鉴权、埋点、缓存时只动一处。
#
# 注意（版本契约）：Starlette 1.3.1 的 TemplateResponse 签名是
#   TemplateResponse(request, name, context)，request 为第一个必填参数。
#   这与旧版 (name, context, request=...) 不同，调用时务必把 request 放最前。
# ======================================================================
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

# 内容服务：统一从这里读 9 个板块（和后面 RAG / MCP 读同一张表）
from app.services import content

router = APIRouter()


def _tpl(request: Request):
    """取模板引擎的快捷方式：引擎挂在 app.state 上，路由不自己 new。"""
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """首页：Hero + 能力亮点 + 技术栈 + 项目速览。"""
    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,                       # Starlette 1.x：request 必须放第一个
        "home.html",
        {
            "request": request,
            "basics": sections.get("basics"),
            "skills": sections.get("skills"),
            "projects": sections.get("projects", {}).get("body") or [],
            "sections": sections,
        },
    )


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request) -> HTMLResponse:
    """关于我：教育 / 技能 / 获奖 / 输出 / 自评。"""
    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,
        "about.html",
        {
            "request": request,
            "education": sections.get("education"),
            "skills": sections.get("skills"),
            "awards": sections.get("awards"),
            "outputs": sections.get("outputs"),
            "self_eval": sections.get("self_eval"),
        },
    )


@router.get("/projects", response_class=HTMLResponse)
async def projects(request: Request) -> HTMLResponse:
    """项目经历：逐条卡片。"""
    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,
        "projects.html",
        {
            "request": request,
            "projects": sections.get("projects", {}).get("body") or [],
        },
    )


@router.get("/resume", response_class=HTMLResponse)
async def resume(request: Request) -> HTMLResponse:
    """简历：多板块拼成可打印视图。"""
    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,
        "resume.html",
        {
            "request": request,
            "basics": sections.get("basics"),
            "education": sections.get("education"),
            "skills": sections.get("skills"),
            "projects": sections.get("projects", {}).get("body") or [],
            "awards": sections.get("awards"),
        },
    )


@router.get("/contact", response_class=HTMLResponse)
async def contact_get(request: Request) -> HTMLResponse:
    """联系页（GET）：展示表单。"""
    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,
        "contact.html",
        {
            "request": request,
            "outputs": sections.get("outputs"),
            "submitted": False,
        },
    )


@router.post("/contact", response_class=HTMLResponse)
async def contact_post(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
) -> HTMLResponse:
    """联系页（POST）：把留言落库（SMTP 配了再发邮件）。

    这里直接写 ContactMessage，体现「表单 → 数据库」这条最朴素的链路；
    限流由 #21 阶段的 slowapi 装饰器在路由上补，不在这里硬编码。
    """
    from app.core.db import SessionLocal
    from app.data.models import ContactMessage

    with SessionLocal() as db:
        db.add(ContactMessage(name=name, email=email, message=message))
        db.commit()

    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,
        "contact.html",
        {
            "request": request,
            "outputs": sections.get("outputs"),
            "submitted": True,
        },
    )


@router.get("/graph", response_class=HTMLResponse)
async def graph(request: Request) -> HTMLResponse:
    """知识图谱可视化页（Cytoscape.js 在前端渲染，真实数据 #21 接入）。"""
    sections = content.get_all_sections()
    return _tpl(request).TemplateResponse(
        request,
        "graph.html",
        {"request": request, "sections": sections},
    )
