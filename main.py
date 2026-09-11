from collections import defaultdict
from urllib.parse import quote
import json

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select

import config
from database import get_session, init_db
from models import Material, Supplier, Category, PriceHistory
from i18n import get_translations, DEFAULT_LANG

app = FastAPI(title="Реєстр цін")
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["url_quote"] = quote
templates.env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)


def get_lang(request: Request) -> str:
    lang = request.cookies.get("lang", DEFAULT_LANG)
    return lang if lang in ("uk", "ru") else DEFAULT_LANG


def base_ctx(request: Request) -> dict:
    lang = get_lang(request)
    return {"request": request, "lang": lang, "t": get_translations(lang)}


def require_login(request: Request):
    return bool(request.session.get("logged_in"))


@app.on_event("startup")
async def on_startup():
    await init_db()


@app.get("/set-lang/{lang_code}")
async def set_lang(lang_code: str, back: str = "/"):
    resp = RedirectResponse(back if back.startswith("/") else "/")
    if lang_code in ("uk", "ru"):
        resp.set_cookie("lang", lang_code, max_age=60 * 60 * 24 * 365)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    ctx = base_ctx(request)
    ctx["error"] = None
    return templates.TemplateResponse("login.html", ctx)


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if password == config.SITE_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=302)
    ctx = base_ctx(request)
    ctx["error"] = get_translations(get_lang(request))["login_error"]
    return templates.TemplateResponse("login.html", ctx, status_code=401)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def catalog(request: Request, category: str = "", supplier: str = "", q: str = ""):
    if not require_login(request):
        return RedirectResponse("/login", status_code=302)

    async with get_session() as session:
        categories = (await session.execute(select(Category.name).order_by(Category.name))).scalars().all()
        suppliers = (await session.execute(select(Supplier.name).order_by(Supplier.name))).scalars().all()

        stmt = (
            select(Material, Supplier.name.label("supplier_name"), Category.name.label("category_name"))
            .join(Supplier, Material.supplier_id == Supplier.id)
            .join(Category, Material.category_id == Category.id)
            .order_by(Material.name)
        )
        if category:
            stmt = stmt.where(Category.name.ilike(f"%{category}%"))
        if supplier:
            stmt = stmt.where(Supplier.name.ilike(f"%{supplier}%"))
        if q:
            stmt = stmt.where(Material.name.ilike(f"%{q}%"))

        rows = (await session.execute(stmt.limit(500))).all()

    materials = [
        {
            "id": m.id,
            "name": m.name,
            "unit": m.unit,
            "quantity": m.quantity,
            "price": m.current_price,
            "price_date": m.price_date,
            "supplier": supplier_name,
            "category": category_name,
        }
        for m, supplier_name, category_name in rows
    ]

    ctx = base_ctx(request)
    ctx.update({
        "materials": materials,
        "categories": categories,
        "suppliers": suppliers,
        "selected_category": category,
        "selected_supplier": supplier,
        "query": q,
        "total_shown": len(materials),
    })
    return templates.TemplateResponse("catalog.html", ctx)


@app.get("/material/{material_id}/history", response_class=HTMLResponse)
async def material_history(request: Request, material_id: int):
    if not require_login(request):
        return RedirectResponse("/login", status_code=302)

    async with get_session() as session:
        material = await session.get(Material, material_id)
        if material is None:
            return HTMLResponse("Not found", status_code=404)

        supplier = await session.get(Supplier, material.supplier_id)

        history_rows = (
            await session.execute(
                select(PriceHistory)
                .where(PriceHistory.material_id == material_id)
                .order_by(PriceHistory.price_date.asc())
            )
        ).scalars().all()

    prices = [float(h.price) for h in history_rows]
    min_price = min(prices) if prices else 0
    max_price = max(prices) if prices else 1
    price_range = max_price - min_price or 1

    enriched = []
    prev_price = None
    for h, p in zip(history_rows, prices):
        if prev_price is None:
            delta, delta_pct = None, None
        else:
            delta = p - prev_price
            delta_pct = (delta / prev_price * 100) if prev_price else None
        bar_height = 10 + int((p - min_price) / price_range * 90)
        enriched.append({
            "price_date": h.price_date, "price": p, "source_file": h.source_file,
            "delta": delta, "delta_pct": delta_pct, "bar_height": bar_height,
        })
        prev_price = p
    enriched.reverse()

    ctx = base_ctx(request)
    ctx.update({
        "material": material,
        "supplier_name": supplier.name if supplier else "?",
        "history": enriched,
        "chart_points": enriched[::-1],
    })
    return templates.TemplateResponse("history.html", ctx)


@app.get("/duplicates", response_class=HTMLResponse)
async def duplicates(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=302)

    async with get_session() as session:
        rows = (
            await session.execute(
                select(Material, Supplier.name.label("supplier_name"))
                .join(Supplier, Material.supplier_id == Supplier.id)
            )
        ).all()

    groups: dict[tuple[int, str], list] = defaultdict(list)
    for m, supplier_name in rows:
        norm_key = (m.supplier_id, " ".join(m.name.lower().split()))
        groups[norm_key].append({"id": m.id, "name": m.name, "price": m.current_price, "supplier": supplier_name})

    duplicate_groups = [g for g in groups.values() if len(g) > 1]

    ctx = base_ctx(request)
    ctx["groups"] = duplicate_groups
    return templates.TemplateResponse("duplicates.html", ctx)
