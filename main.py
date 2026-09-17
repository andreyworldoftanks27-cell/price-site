from collections import defaultdict
from urllib.parse import quote
import json

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func

import config
from database import get_session, init_db
from models import Material, Supplier, Category, PriceHistory
from i18n import get_translations, DEFAULT_LANG

app = FastAPI(title="Реєстр цін")
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals["url_quote"] = quote


def _tojson(value) -> Markup:
    """
    JSON-фильтр, безопасный и для <script>, и для HTML-атрибутов.

    Раньше здесь был plain json.dumps(), который Jinja2 (autoescape=True)
    затем HTML-экранировал: кавычки превращались в "&#34;". Внутри onclick="..."
    браузер декодирует эти сущности обратно и всё работает, но внутри <script>
    текст не проходит decode — получается синтаксическая ошибка вида
    "Unexpected token '&'", которая ломает ВЕСЬ script-блок целиком (включая
    вообще не связанные с этим значением функции типа selectCategory()).
    Именно поэтому не работали ни кнопки категорий, ни живой поиск.

    Фикс — как в Flask: экранируем опасные для <script> символы вручную
    (\\u003c и т.п.) и оборачиваем в Markup, чтобы Jinja не экранировала
    результат повторно.
    """
    dumped = json.dumps(value, ensure_ascii=False)
    dumped = (
        dumped.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("'", "\\u0027")
    )
    return Markup(dumped)


templates.env.filters["tojson"] = _tojson

# Тот же фиксированный список категорий, что использует бот при классификации
# (price-bot/classifier.py — при изменении менять в обоих местах). Держим его
# и здесь, чтобы рисовать вкладки-фильтры даже для категорий, в которых пока
# 0 позиций (не только те, что реально есть в базе).
#
# Раньше тут было 12 общих категорий (в основном две — "Загальнобуд" и
# "Оздоблення" — вбирали в себя почти всё, что реально приходит в прайсах).
# Теперь список подробнее и отражает то, как поставщики сами размечают свои
# прайсы (например, Siltek — отдельными разделами "Матеріали для влаштування
# підлог", "Матеріали для облицювання поверхонь" и т.д.) — бот теперь читает
# эту разметку напрямую из файла, а не только угадывает по названию товара.
FIXED_CATEGORIES = [
    "Влаштування підлог",
    "Облицювання поверхонь",
    "Гідроізоляція",
    "Теплоізоляція",
    "Малярні матеріали",
    "Оздоблення",
    "Загальнобуд",
    "Бетон та ЗБВ",
    "Покрівля та фасад",
    "Металопрокат",
    "Сантехніка",
    "Електрика",
    "Деревина та пиломатеріали",
    "Двері, вікна, фурнітура",
    "Інструмент та витратники",
    "Спецодяг та безпека",
    "Інше",
]

# Допустимые значения сортировки каталога (используются и в query-параметре,
# и как ключи переводов sort_*).
SORT_OPTIONS = ("name", "price_asc", "price_desc", "qty_desc", "qty_asc", "supplier")


def _normalize_name(name: str) -> str:
    """Та же нормализация, что и на странице дублей — по ней ищем совпадения между поставщиками."""
    return " ".join(name.lower().split())


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
async def catalog(
    request: Request,
    category: str = "",
    supplier: str = "",
    q: str = "",
    sort: str = "name",
    compare: str = "",
):
    if not require_login(request):
        return RedirectResponse("/login", status_code=302)

    if sort not in SORT_OPTIONS:
        sort = "name"
    compare_on = compare == "1"

    async with get_session() as session:
        suppliers = (await session.execute(select(Supplier.name).order_by(Supplier.name))).scalars().all()

        # Подзапрос: цена ДО текущей (предыдущая запись в истории) — чтобы показать тренд.
        prev_price_subq = (
            select(PriceHistory.price)
            .where(PriceHistory.material_id == Material.id)
            .where(PriceHistory.price_date < Material.price_date)
            .order_by(PriceHistory.price_date.desc())
            .limit(1)
            .correlate(Material)
            .scalar_subquery()
        )

        stmt = (
            select(
                Material,
                Supplier.name.label("supplier_name"),
                Category.name.label("category_name"),
                prev_price_subq.label("prev_price"),
            )
            .join(Supplier, Material.supplier_id == Supplier.id)
            .join(Category, Material.category_id == Category.id)
        )
        if supplier:
            stmt = stmt.where(Supplier.name.ilike(f"%{supplier}%"))
        # "q" (текстовый поиск) сюда намеренно НЕ подмешиваем — см. пояснение
        # у ctx["query"] ниже: он теперь работает только на клиенте.

        # Счётчик по категориям — учитывает текущий фильтр по поставщику,
        # но НЕ саму категорию, чтобы можно было видеть, сколько позиций в каждой вкладке.
        count_stmt = (
            select(Category.name, func.count(Material.id))
            .join(Material, Material.category_id == Category.id)
            .join(Supplier, Material.supplier_id == Supplier.id)
            .group_by(Category.name)
        )
        if supplier:
            count_stmt = count_stmt.where(Supplier.name.ilike(f"%{supplier}%"))
        counts_raw = dict((await session.execute(count_stmt)).all())

        if category:
            stmt = stmt.where(Category.name == category)

        # Сортировка. По умолчанию ("name") — по категории и названию, как раньше
        # (список рисуется секциями по категориям). Любая другая сортировка —
        # это сквозной список по всем выбранным категориям, поэтому в шаблоне
        # для неё используется отдельный "плоский" режим отображения.
        if sort == "price_asc":
            stmt = stmt.order_by(Material.current_price.asc(), Material.name.asc())
        elif sort == "price_desc":
            stmt = stmt.order_by(Material.current_price.desc(), Material.name.asc())
        elif sort == "qty_desc":
            stmt = stmt.order_by(Material.quantity.desc().nulls_last(), Material.name.asc())
        elif sort == "qty_asc":
            stmt = stmt.order_by(Material.quantity.asc().nulls_last(), Material.name.asc())
        elif sort == "supplier":
            stmt = stmt.order_by(Supplier.name.asc(), Material.name.asc())
        else:
            stmt = stmt.order_by(Category.name.asc(), Material.name.asc())

        rows = (await session.execute(stmt.limit(1000))).all()

        # Режим "порівняти ціни": для тих самих позицій (однакова назва) від
        # РІЗНИХ постачальників — показуємо їх поруч, найдешевша зверху.
        # Рахуємо окремим (нефільтрованим по категорії) запитом, щоб порівняння
        # не ламалось, коли обрана лише одна категорія.
        compare_groups = []
        if compare_on:
            cmp_stmt = (
                select(Material, Supplier.name.label("supplier_name"), Category.name.label("category_name"))
                .join(Supplier, Material.supplier_id == Supplier.id)
                .join(Category, Material.category_id == Category.id)
            )
            if supplier:
                cmp_stmt = cmp_stmt.where(Supplier.name.ilike(f"%{supplier}%"))
            if category:
                cmp_stmt = cmp_stmt.where(Category.name == category)
            cmp_rows = (await session.execute(cmp_stmt.limit(5000))).all()

            groups_by_key: dict[str, list] = defaultdict(list)
            for m, supplier_name, category_name in cmp_rows:
                key = _normalize_name(m.name)
                groups_by_key[key].append({
                    "id": m.id,
                    "name": m.name,
                    "unit": m.unit or "шт",
                    "quantity": m.quantity,
                    "price": float(m.current_price),
                    "supplier": supplier_name,
                    "category": category_name,
                })

            for items in groups_by_key.values():
                distinct_suppliers = {it["supplier"] for it in items}
                if len(distinct_suppliers) < 2:
                    continue
                items.sort(key=lambda it: it["price"])
                compare_groups.append({
                    "name": items[0]["name"],
                    "supplier_count": len(distinct_suppliers),
                    # НЕ называть ключ "items" — это дальше словарь, и в Jinja
                    # group.items вызовет dict.items() вместо чтения ключа.
                    "offers": items,
                })
            compare_groups.sort(key=lambda g: g["name"])

    total_all = sum(counts_raw.values())

    materials = []
    for m, supplier_name, category_name, prev_price in rows:
        prev = float(prev_price) if prev_price is not None else None
        cur = float(m.current_price)
        materials.append({
            "id": m.id,
            "name": m.name,
            "unit": m.unit or "шт",
            "quantity": m.quantity,
            "price": cur,
            "prev_price": prev,
            "price_date": m.price_date,
            "supplier": supplier_name,
            "category": category_name,
        })

    flat_mode = sort != "name" and not compare_on

    # Группируем по категории для отображения секциями (как в референсе).
    # В "плоском" режиме (сортировка не по умолчанию) секции не используются —
    # список рисуется одним сквозным блоком в исходном порядке материалов.
    grouped = defaultdict(list)
    if not flat_mode:
        for m in materials:
            grouped[m["category"]].append(m)

    ctx = base_ctx(request)
    ctx.update({
        "grouped": grouped,
        "category_order": [c for c in FIXED_CATEGORIES if c in grouped] + [c for c in grouped if c not in FIXED_CATEGORIES],
        "suppliers": suppliers,
        "sort": sort,
        "compare": compare_on,
        "compare_groups": compare_groups,
        "flat_mode": flat_mode,
        "flat_materials": materials if flat_mode else [],
        "selected_category": category,
        "selected_supplier": supplier,
        "query": q,
        "total_shown": len(materials),
        "total_all": total_all,
    })
    return templates.TemplateResponse("catalog.html", ctx)


/* Line chart, plain inline SVG (points computed server-side) — no JS charting lib needed */
.trend-chart-wrap { margin-bottom: 26px; }
.trend-svg {
    width: 100%;
    height: 180px;
    display: block;
    overflow: visible;
}
.trend-area {
    fill: var(--accent);
    fill-opacity: 0.14;
    stroke: none;
}
.trend-line {
    fill: none;
    stroke: var(--accent);
    stroke-width: 3;
    stroke-linejoin: round;
    stroke-linecap: round;
    vector-effect: non-scaling-stroke;
}
.trend-dot {
    fill: var(--surface);
    stroke: var(--accent);
    stroke-width: 2.5;
    vector-effect: non-scaling-stroke;
    cursor: pointer;
    transition: fill 0.1s ease;
}
.trend-dot:hover { fill: var(--accent); }
.trend-axis {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
}

    groups: dict[tuple[int, str], list] = defaultdict(list)
    for m, supplier_name in rows:
        norm_key = (m.supplier_id, " ".join(m.name.lower().split()))
        groups[norm_key].append({"id": m.id, "name": m.name, "price": m.current_price, "supplier": supplier_name})

    duplicate_groups = [g for g in groups.values() if len(g) > 1]

    ctx = base_ctx(request)
    ctx["groups"] = duplicate_groups
    return templates.TemplateResponse("duplicates.html", ctx)
