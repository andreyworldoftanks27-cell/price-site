from collections import defaultdict
from datetime import date

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func as sa_func

import config
from database import get_session, init_db
from models import Material, Supplier, Category, PriceHistory

app = FastAPI(title="Реестр цен")
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def require_login(request: Request):
    if not request.session.get("logged_in"):
        return False
    return True


@app.on_event("startup")
async def on_startup():
    await init_db()


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if password == config.SITE_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Неверный пароль"}, status_code=401
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def catalog(
    request: Request,
    category: str = "",
    supplier: str = "",
    q: str = "",
):
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
            stmt = stmt.where(Category.name == category)
        if supplier:
            stmt = stmt.where(Supplier.name == supplier)
        if q:
            stmt = stmt.where(Material.name.ilike(f"%{q}%"))

        rows = (await session.execute(stmt.limit(500))).all()

    materials = [
        {
            "id": m.id,
            "name": m.name,
            "unit": m.unit,
            "price": m.current_price,
            "price_date": m.price_date,
            "supplier": supplier_name,
            "category": category_name,
        }
        for m, supplier_name, category_name in rows
    ]

    return templates.TemplateResponse(
        "catalog.html",
        {
            "request": request,
            "materials": materials,
            "categories": categories,
            "suppliers": suppliers,
            "selected_category": category,
            "selected_supplier": supplier,
            "query": q,
            "total_shown": len(materials),
        },
    )


@app.get("/material/{material_id}/history", response_class=HTMLResponse)
async def material_history(request: Request, material_id: int):
    if not require_login(request):
        return RedirectResponse("/login", status_code=302)

    async with get_session() as session:
        material = await session.get(Material, material_id)
        if material is None:
            return HTMLResponse("Материал не найден", status_code=404)

        supplier = await session.get(Supplier, material.supplier_id)

        history = (
            await session.execute(
                select(PriceHistory)
                .where(PriceHistory.material_id == material_id)
                .order_by(PriceHistory.price_date.desc())
            )
        ).scalars().all()

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "material": material,
            "supplier_name": supplier.name if supplier else "?",
            "history": history,
        },
    )


@app.get("/duplicates", response_class=HTMLResponse)
async def duplicates(request: Request):
    """
    Показывает позиции, которые ВЫГЛЯДЯТ как один и тот же материал
    у одного поставщика, но записаны чуть по-разному (регистр, пробелы) —
    и поэтому не схлопнулись в одну запись при дедупликации в боте.
    """
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

    return templates.TemplateResponse(
        "duplicates.html",
        {"request": request, "groups": duplicate_groups},
    )
