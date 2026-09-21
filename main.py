from collections import defaultdict
from datetime import date
from urllib.parse import quote
import hashlib
import json
import os
import re

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


def _compute_css_version() -> str:
    """
    Короткий хеш содержимого style.css, добавляемый в ссылку как ?v=...
    Раньше при каждом обновлении style.css браузер мог показывать
    закэшированную СТАРУЮ версию файла (тот самый "график залился чёрным" —
    просто новых CSS-правил для него ещё не было в кэше браузера). Так как
    хеш меняется при любом изменении файла, ссылка на файл каждый раз новая,
    и браузер гарантированно подгружает свежий CSS после деплоя — без
    необходимости вручную чистить кэш (Ctrl+F5).
    """
    try:
        path = os.path.join(os.path.dirname(__file__), "static", "style.css")
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()[:8]
    except OSError:
        return "1"


CSS_VERSION = _compute_css_version()


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

# Категории всегда хранятся в базе на украинском (так их определяет бот —
# см. price-bot/classifier.py), а переключатель языка на сайте раньше менял
# только статичные подписи интерфейса (кнопки, заголовки колонок и т.п.), но
# не сами названия категорий, которые приходят из базы как есть. Здесь —
# перевод для отображения на русском; сама база и группировка (grouped/
# category_order) по-прежнему работают по оригинальному украинскому названию,
# меняется только то, что показывается пользователю.
CATEGORY_RU = {
    "Влаштування підлог": "Устройство полов",
    "Облицювання поверхонь": "Облицовка поверхностей",
    "Гідроізоляція": "Гидроизоляция",
    "Теплоізоляція": "Теплоизоляция",
    "Малярні матеріали": "Малярные материалы",
    "Оздоблення": "Отделка",
    "Загальнобуд": "Общестрой",
    "Бетон та ЗБВ": "Бетон и ЖБИ",
    "Покрівля та фасад": "Кровля и фасад",
    "Металопрокат": "Металлопрокат",
    "Сантехніка": "Сантехника",
    "Електрика": "Электрика",
    "Деревина та пиломатеріали": "Древесина и пиломатериалы",
    "Двері, вікна, фурнітура": "Двери, окна, фурнитура",
    "Інструмент та витратники": "Инструмент и расходники",
    "Спецодяг та безпека": "Спецодежда и безопасность",
    "Інше": "Прочее",
}


def translate_category(name: str, lang: str) -> str:
    """Категория, которую видит пользователь — как есть на uk, переведённая на ru.
    Если категория не входит в наш список (например, её самостоятельно
    придумал классификатор для чего-то нестандартного) — показываем как есть,
    переводить просто нечем."""
    if lang == "ru":
        return CATEGORY_RU.get(name, name)
    return name


templates.env.filters["cat"] = translate_category

# Названия товаров, в отличие от категорий, приходят из прайсов поставщиков
# как свободный текст — их тысячи и постоянно добавляются новые, поэтому
# фиксированный словарь "всех значений" тут невозможен (как для категорий).
# Вместо этого переводим ПОСЛОВНО по словарю частой строительной лексики:
# то, чего в словаре нет (артикулы, коды, бренды типа "Ceresit"), остаётся
# как в оригинале. Перевод не идеальный, но заметно читаемее для
# русскоязычного пользователя, чем полностью нетронутый украинский текст,
# и словарь легко пополнять новыми словами по мере необходимости.
MATNAME_WORD_RU = {
    # суміші, розчини, клеї
    "суміш": "смесь", "суміші": "смеси", "розчин": "раствор", "розчину": "раствора",
    "клейовий": "клеевой", "клейова": "клеевая", "клеюча": "клеящая", "клеюче": "клеящее",
    "клеючий": "клеящий",
    # поверхні, підлога, стіни
    "підлоги": "пола", "підлога": "пол", "підлогу": "пол", "стіни": "стены",
    "стіна": "стена", "стін": "стен", "поверхонь": "поверхностей", "поверхні": "поверхности",
    "покриття": "покрытие", "покриттів": "покрытий",
    # властивості (прикметники)
    "високоміцне": "высокопрочное", "високоміцний": "высокопрочный", "високоміцна": "высокопрочная",
    "легковирівнювальна": "легковыравнивающаяся", "легковирівнювальний": "легковыравнивающийся",
    "самовирівнювальна": "самовыравнивающаяся", "самовирівнювальний": "самовыравнивающийся",
    "самовирівнююча": "самовыравнивающая", "універсальний": "универсальный",
    "універсальна": "универсальная", "універсальне": "универсальное",
    "водостійкий": "водостойкий", "водостійка": "водостойкая",
    "морозостійкий": "морозостойкий", "морозостійка": "морозостойкая",
    "декоративна": "декоративная", "декоративне": "декоративное", "декоративний": "декоративный",
    "декоративні": "декоративные", "зовнішніх": "наружных", "зовнішній": "наружный",
    "внутрішніх": "внутренних", "внутрішній": "внутренний", "будівельна": "строительная",
    "будівельні": "строительные", "будівельних": "строительных",
    "реставраційна": "реставрационная", "реставраційний": "реставрационный",
    "швидкотверднучий": "быстротвердеющий", "еластичний": "эластичный", "еластична": "эластичная",
    "полімерний": "полимерный", "полімерна": "полимерная",
    "відновлювальна": "восстановительная", "ремонтна": "ремонтная", "ремонтно": "ремонтно",
    "фінішна": "финишная", "стартова": "стартовая", "глибокопроникна": "глубокопроникающая",
    "сухий": "сухой", "суха": "сухая", "сухе": "сухое",
    # матеріали
    "ґрунтовка": "грунтовка", "грунтовка": "грунтовка", "шпаклівка": "шпаклёвка",
    "фарба": "краска", "фарби": "краски", "штукатурка": "штукатурка", "штукатурки": "штукатурки",
    "плитка": "плитка", "плитки": "плитки", "керамограніт": "керамогранит", "каменю": "камня",
    "камінь": "камень", "гідроізоляція": "гидроизоляция", "теплоізоляція": "теплоизоляция",
    "звукоізоляційний": "звукоизоляционный", "звукоізоляційним": "звукоизоляционным",
    "звукоізоляція": "звукоизоляция", "ефект": "эффект", "ефектом": "эффектом",
    "компонентний": "компонентный", "двокомпонентний": "двухкомпонентный",
    "однокомпонентний": "однокомпонентный", "цемент": "цемент", "гіпс": "гипс", "вапно": "известь",
    "герметик": "герметик", "паркет": "паркет", "піна": "пена", "монтажна": "монтажная",
    "поліуретановий": "полиуретановый", "поліуретан": "полиуретан", "домішка": "добавка",
    "домішки": "добавки", "водно": "водно", "дисперсійний": "дисперсионный",
    "дисперсійна": "дисперсионная",
    # службові слова
    "для": "для", "без": "без", "із": "с", "зі": "со", "та": "и", "від": "от", "клей": "клей",
    "суцільна": "сплошная", "упаковка": "упаковка", "упаковки": "упаковки",

    # Ниже — слова, реально встретившиеся в названиях товаров из загруженных
    # прайсов (Полимин, Siltek, ЦК/Henkel) — собраны разбором готовых
    # названий на отдельные слова и переведены вручную, чтобы покрыть
    # основную массу того, что реально видит пользователь на сайте, а не
    # только "типовые" строительные термины.
    # фарби, кольори, декоративні покриття
    "база": "база", "колір": "цвет", "кольори": "цвета", "кольоровий": "цветной",
    "білий": "белый", "біла": "белая", "сірий": "серый", "сіра": "серая",
    "чорний": "чёрный", "чорна": "чёрная", "червоний": "красный", "червона": "красная",
    "жовтий": "жёлтый", "жовта": "жёлтая", "зелений": "зелёный", "зелена": "зелёная",
    "синій": "синий", "синя": "синяя", "блакитний": "голубой", "блакитна": "голубая",
    "коричневий": "коричневый", "бежевий": "бежевый", "фіолетовий": "фиолетовый",
    "помаранчевий": "оранжевый", "сріблястий": "серебристый", "прозорий": "прозрачный",
    "прозора": "прозрачная", "графіт": "графит", "антрацит": "антрацит", "горіх": "орех",
    "горіховий": "ореховый", "кавовий": "кофейный", "жасмин": "жасмин", "жасмін": "жасмин",
    "карамель": "карамель", "мокко": "мокко", "мигдаль": "миндаль", "оксид": "оксид",
    "натур": "натур", "натура": "натура",
    "еко": "эко", "супер": "супер", "плюс": "плюс", "міні": "мини", "преміум": "премиум",
    "стандарт": "стандарт", "класична": "классическая", "класік": "классик",
    "латексна": "латексная", "акрилова": "акриловая", "акриловий": "акриловый",
    "акрил": "акрил", "силіконова": "силиконовая", "силіконовий": "силиконовый",
    "силікатна": "силикатная", "епоксидна": "эпоксидная",
    "структурна": "структурная", "фактурна": "фактурная", "матова": "матовая",
    "глибокоматова": "глубокоматовая",
    "шовковисто": "шёлковисто", "оксамитово": "бархатисто",
    "камінцева": "камешковая", "баранець": "барашек", "короїд": "короед",
    "мозаїчна": "мозаичная", "мозаїка": "мозаика", "мозаїч": "мозаич",
    "зерно": "зерно", "пігмент": "пигмент", "пігментна": "пигментная",
    "барвник": "краситель", "наповнювач": "наполнитель", "заповнювач": "заполнитель",
    "лак": "лак", "профіль": "профиль", "паста": "паста", "гель": "гель",
    "концентрат": "концентрат", "емульсія": "эмульсия", "мастика": "мастика",
    "премікс": "премикс", "воску": "воска",
    "темно": "тёмно", "світло": "светло",
    # ґрунтовки, клеї, суміші
    "ґрунтівка": "грунтовка", "грунтівка": "грунтовка", "ґрунт": "грунт", "грунт": "грунт",
    "грунтуюча": "грунтующая", "грунтуюче": "грунтующее",
    "глибокопроникаюча": "глубокопроникающая", "гідрофобізатор": "гидрофобизатор",
    "клейова": "клеевая", "мурувальна": "кладочная", "мурування": "кладка",
    "контактна": "контактная", "контакт": "контакт", "бетонконтакт": "бетоноконтакт",
    "однокомпонентна": "однокомпонентная", "двокомпонентна": "двухкомпонентная",
    "цементна": "цементная", "цементно": "цементно", "гіпсова": "гипсовая",
    "вапняна": "известковая", "вапняні": "известковые",
    "полімерцементна": "полимерцементная", "поліуретанова": "полиуретановая",
    "розчин": "раствор", "розчинів": "растворов", "засіб": "средство",
    "очищувач": "очиститель", "секундний": "секундный",
    "лугостійка": "щёлочестойкая", "стійка": "стойкая",
    "гумова": "резиновая", "флізелін": "флизелин", "вініл": "винил",
    "склополотна": "стеклохолста", "шпалер": "обоев",
    "цвяхом": "гвоздём", "цвяхи": "гвозди", "дюбель": "дюбель",
    "стрічка": "лента", "кріплення": "крепление", "сітка": "сетка", "склосітка": "стеклосетка",
    "шурупом": "шурупом",
    # властивості
    "високоеластична": "высокоэластичная", "високоеластичний": "высокоэластичный",
    "армована": "армированная", "армуюча": "армирующая", "армуючий": "армирующий",
    "зміцнююча": "укрепляющая", "швидкотвердіюча": "быстротвердеющая",
    "довговічна": "долговечная", "санітарний": "санитарный", "професійна": "профессиональная",
    "адгезійна": "адгезионная", "антисептичний": "антисептический",
    "теплоізоляційна": "теплоизоляционная", "гідроізоляційна": "гидроизоляционная",
    "гідроізолююча": "гидроизолирующая",
    "проникаюча": "проникающая", "проникаючої": "проникающей",
    "крупнозерниста": "крупнозернистая", "дрібнозерниста": "мелкозернистая",
    "середньозерниста": "среднезернистая", "пористих": "пористых", "щільних": "плотных",
    "легкого": "лёгкого", "легкий": "лёгкий",
    "мінеральна": "минеральная", "мінеральної": "минеральной", "мінеральних": "минеральных",
    "теплоізоляції": "теплоизоляции", "гідроізоляції": "гидроизоляции",
    "пінополістиролу": "пенополистирола", "вати": "ваты", "газобетону": "газобетона",
    "бетону": "бетона", "бетонних": "бетонных", "плит": "плит", "блоків": "блоков",
    "товщиною": "толщиной", "підвищеної": "повышенной", "підвищеною": "повышенной",
    "міцністю": "прочностью", "міцність": "прочность", "текучості": "текучести",
    "зима": "зима", "літо": "лето",
    "цокольний": "цокольный", "фасадна": "фасадная",
    "інтер'єрна": "интерьерная", "облицювання": "облицовка", "миття": "мытья",
    "кладки": "кладки", "керамограніт": "керамогранит", "керамограніту": "керамогранита",
    "керамограніта": "керамогранита", "граніт": "гранит", "мармур": "мрамор", "клінкер": "клинкер",
    "мпа": "МПа", "нанесення": "нанесения", "ручного": "ручного", "машинного": "машинного",
    "штукатурення": "оштукатуривания", "шпаклювання": "шпаклевания",
    "замовлення": "заказ", "всі": "все", "стель": "потолков", "стелі": "потолка",
    "мульті": "мульти", "тріо": "трио", "багатофункц": "многофункц",
    "термо": "термо", "комфорт": "комфорт", "старт": "старт",
    "дерево": "дерево", "гіпсокартону": "гипсокартона", "котедж": "коттедж",
    "дії": "действия", "ущільнююча": "уплотняющая", "полегшена": "облегчённая",
    "швів": "швов", "шов": "шов", "стяжка": "стяжка", "силікон": "силикон",
    "силікат": "силикат", "класу": "класса", "фасад": "фасад", "універсал": "универсал",
    "монтажний": "монтажный", "декор": "декор", "стиків": "стыков", "ппс": "ППС",
    "монтаж": "монтаж", "бетон": "бетон", "анкерна": "анкерная", "гладких": "гладких",
    "основ": "основ", "світлий": "светлый", "адгезії": "адгезии", "нівелір": "нивелир",
    "помаранчева": "оранжевая", "модифікована": "модифицированная",
}


def translate_material_name(name: str, lang: str) -> str:
    if lang != "ru" or not name:
        return name

    def repl(match: "re.Match[str]") -> str:
        word = match.group(0)
        translated = MATNAME_WORD_RU.get(word.lower())
        if translated is None:
            return word
        if word[0].isupper():
            translated = translated[0].upper() + translated[1:]
        return translated

    # Апостроф — обычная буква украинской орфографии внутри слова (як у
    # "інтер'єрна"), а не разделитель — раньше здесь такое слово резалось на
    # два токена ("інтер" + "єрна"), и оно не находилось в словаре целиком.
    normalized = name.replace("’", "'")
    return re.sub(r"[^\W\d_]+(?:'[^\W\d_]+)*", repl, normalized)


templates.env.filters["matname"] = translate_material_name

# Частые сокращения поставщиков в названиях товарів — "д/підлоги" вместо
# "для підлоги", "2компл." вместо "двокомпонентний" и т.п. Разворачиваем их
# для строки "Повна назва" на странице товара, чтобы название можно было
# прочитать полностью, без сокращений. Порядок важен: более длинные и
# специфичные сокращения проверяем раньше более коротких, чтобы они не
# "срабатывали" по кусочку общего шаблона (например "звукоіз.еф." — раньше
# отдельных "еф." и "компл.").
_ABBREVIATIONS = [
    (r"звукоіз\.\s*еф\.", "звукоізоляційним ефектом"),
    (r"\b2\s*компл?\.", "двокомпонентний"),
    (r"\b1\s*компл?\.", "однокомпонентний"),
    (r"\bд/", "для "),
    (r"\bб/", "без "),
    (r"\bз/", "із "),
    (r"\bеф\.", "ефект"),
    (r"\bкомпл?\.", "компонентний"),
    (r"\bморозост\.", "морозостійкий"),
    (r"\bводост\.", "водостійкий"),
    (r"\bуніверс\.", "універсальний"),
]
_ABBREV_RE = [(re.compile(pat, re.IGNORECASE), repl) for pat, repl in _ABBREVIATIONS]


def expand_material_name(name: str) -> str:
    """Разворачивает частые сокращения в названии товара в полные слова —
    см. _ABBREVIATIONS выше. Используется для строки "Повна назва" на
    странице истории товара."""
    if not name:
        return name
    result = name
    for pattern, repl in _ABBREV_RE:
        result = pattern.sub(repl, result)
    return re.sub(r"\s{2,}", " ", result).strip()


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
    return {"request": request, "lang": lang, "t": get_translations(lang), "css_version": CSS_VERSION}


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

        # Небольшая шапка-сводка над каталогом (дизайн со "статистикой сверху") —
        # три дешёвых агрегата, не зависящие от текущих фильтров: сколько всего
        # категорий реально используется, и сколько позиций поменяли цену
        # именно СЕГОДНЯ. Второе опирается на то, что price_date у материала
        # обновляется только когда цена правда изменилась (см. фикс в bot.py —
        # раньше date всегда была "сегодня" при любой перезагрузке прайса, и
        # эта цифра была бы бессмысленной).
        total_categories = (
            await session.execute(select(func.count(func.distinct(Material.category_id))))
        ).scalar() or 0
        updated_today = (
            await session.execute(
                select(func.count(Material.id)).where(Material.price_date == date.today())
            )
        ).scalar() or 0

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
        "total_suppliers": len(suppliers),
        "total_categories": total_categories,
        "updated_today": updated_today,
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
        category = await session.get(Category, material.category_id)

        history_rows = (
            await session.execute(
                select(PriceHistory)
                .where(PriceHistory.material_id == material_id)
                .order_by(PriceHistory.price_date.asc())
            )
        ).scalars().all()

    prices = [float(h.price) for h in history_rows]

    # Обогащаем каждую запись историей действия цены: с какой даты она была
    # установлена (price_date) и до какой действовала (дата СЛЕДУЮЩЕГО
    # изменения — changed_on), либо None для самой свежей/текущей цены
    # (шаблон покажет "дотепер"/"по настоящее время"). Ничего в price_history
    # никогда не удаляется и не перезаписывается — бот (bot.py) только
    # добавляет новую запись и переставляет current_price/price_date на
    # самом Material, поэтому вся история по каждой позиции доступна целиком,
    # с точностью до даты каждого изменения цены.
    enriched = []
    prev_price = None
    for i, (h, p) in enumerate(zip(history_rows, prices)):
        if prev_price is None:
            delta, delta_pct = None, None
        else:
            delta = p - prev_price
            delta_pct = (delta / prev_price * 100) if prev_price else None
        changed_on = history_rows[i + 1].price_date if i + 1 < len(history_rows) else None
        enriched.append({
            "price_date": h.price_date, "price": p, "source_file": h.source_file,
            "delta": delta, "delta_pct": delta_pct, "changed_on": changed_on,
        })
        prev_price = p

    # Точки графика — в хронологическом порядке (старые слева, новые справа).
    # Координаты SVG-ломаной, сетки и подписей осей считаем один раз здесь,
    # на сервере: обычный line-chart в духе Excel/Google Sheets, без внешних
    # JS-библиотек графиков — только разметка + CSS в цветах сайта.
    chart_points = list(enriched)
    chart_width, chart_height = 1000, 380
    pad_left, pad_right = 68, 20
    # pad_bottom раньше был 64 — под подписи дат под осью X, повёрнутые
    # наискосок. При большом числе точек (сотни записей истории) они всё
    # равно налезали друг на друга и выглядели неряшливо, поэтому дату теперь
    # показываем только при наведении на точку (см. <title> у .trend-dot в
    # history.html) — постоянных подписей под осью больше нет, и весь
    # освободившийся отступ снизу отдаём самому графику.
    pad_top, pad_bottom = 20, 20
    plot_width = chart_width - pad_left - pad_right
    plot_height = chart_height - pad_top - pad_bottom
    chart_min_price = min(prices) if prices else 0
    chart_max_price = max(prices) if prices else 0
    price_range = (chart_max_price - chart_min_price) or 1
    n = len(chart_points)

    for i, pt in enumerate(chart_points):
        pt["svg_x"] = round(pad_left + (i / (n - 1) * plot_width if n > 1 else plot_width / 2), 1)
        pt["svg_y"] = round(pad_top + (1 - (pt["price"] - chart_min_price) / price_range) * plot_height, 1)
    points_attr = " ".join(f"{pt['svg_x']},{pt['svg_y']}" for pt in chart_points)
    baseline_y = pad_top + plot_height
    area_attr = (
        f"{pad_left},{baseline_y} {points_attr} {chart_width - pad_right},{baseline_y}"
        if points_attr else ""
    )

    # Горизонтальные линии сетки с подписями цены слева (0%, 25%, 50%, 75%, 100%
    # диапазона цены) — как в Excel/Google Sheets.
    grid_lines = []
    n_grid = 4
    for k in range(n_grid + 1):
        frac = k / n_grid
        grid_lines.append({
            "y": round(pad_top + (1 - frac) * plot_height, 1),
            "label": f"{chart_min_price + frac * price_range:.2f}",
        })

    enriched.reverse()  # для таблицы — новые записи сверху

    full_name = expand_material_name(material.name)

    ctx = base_ctx(request)
    ctx.update({
        "material": material,
        "full_name": full_name,
        "supplier_name": supplier.name if supplier else "?",
        "category_name": category.name if category else "?",
        "history": enriched,
        "chart_points": chart_points,
        "chart_width": chart_width,
        "chart_height": chart_height,
        "chart_plot_left": pad_left,
        "chart_plot_right": chart_width - pad_right,
        "chart_baseline_y": baseline_y,
        "chart_min_price": chart_min_price,
        "chart_max_price": chart_max_price,
        "points_attr": points_attr,
        "area_attr": area_attr,
        "grid_lines": grid_lines,
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
