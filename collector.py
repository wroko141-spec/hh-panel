"""Сборщик вакансий hh.ru для панели.

Открывает страницы поиска hh.ru (как обычный браузер), достаёт из них данные
о вакансиях и складывает в data/vacancies.json и data/history.json.

- Плановый сбор: `python3 collector.py` - все подборки из searches.json (таймер раз в 6 часов).
- Ручной сбор: server.py вызывает collect() + merge() для одной подборки из панели.
Только стандартная библиотека Python.
"""
import base64, fcntl, html, json, os, random, re, statistics, subprocess, sys, time, urllib.parse, urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
MSK = timezone(timedelta(hours=3))
KEEP_DAYS = 14          # вакансия пропала из поиска на 14 дней - удаляем
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# навыки, которые ищем в тексте вакансии: имя -> регулярка
SKILLS = {
    "Python": r"python|питон", "Django": r"django", "FastAPI": r"fastapi", "Flask": r"flask",
    "SQL": r"\bsql\b", "PostgreSQL": r"postgre", "MySQL": r"mysql", "Redis": r"redis",
    "Git": r"\bgit\b", "Docker": r"docker", "Linux": r"linux", "REST API": r"\brest\b|\bapi\b",
    "JavaScript": r"javascript|\bjs\b", "TypeScript": r"typescript|\bts\b", "React": r"react",
    "Vue": r"\bvue", "Node.js": r"node", "HTML/CSS": r"\bhtml|\bcss", "Figma": r"figma",
    "PHP": r"\bphp", "Java": r"\bjava\b", "C#": r"c#|\.net", "C++": r"c\+\+", "Go": r"\bgo\b|golang",
    "1С": r"1с|1c\b", "Pandas": r"pandas", "ML": r"machine learning|\bml\b|машинн", "LLM": r"\bllm|gpt|нейросет",
    "Excel": r"excel", "Telegram": r"telegram|телеграм", "Kubernetes": r"kubernetes|k8s",
}
EXP = {"noExperience": "нет", "between1And3": "1–3 года", "between3And6": "3–6 лет", "moreThan6": "6+ лет"}
FMT = {"REMOTE": "удалёнка", "HYBRID": "гибрид", "ON_SITE": "офис", "FIELD_WORK": "разъездная"}
SEARCH_KEYS = ("experience", "work_format", "salary", "only_with_salary")


def log(*a):
    print(datetime.now(MSK).strftime("%H:%M:%S"), *a, flush=True)


def now_stamp():
    return datetime.now(MSK).strftime("%Y-%m-%dT%H:%M")


def fetch(params):
    url = "https://hh.ru/search/vacancy?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', page, re.S)
    if not m:   # вместо выдачи пришла капча или другая страница
        raise RuntimeError("hh.ru не отдал выдачу (капча?)")
    return json.loads(html.unescape(m.group(1)))["vacancySearchResult"]


def clean(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(t or ""))).strip()


def first(lst, key):
    try:
        return lst[0][key]
    except (IndexError, KeyError, TypeError):
        return []


def normalize(v):
    c = v.get("compensation") or {}
    sn = v.get("snippet") or {}
    req, resp = clean(sn.get("req")), clean(sn.get("resp"))
    text = f"{v.get('name', '')} {req} {resp}".lower()
    sal_from, sal_to = c.get("perModeFrom") or c.get("from"), c.get("perModeTo") or c.get("to")
    return {
        "id": v["vacancyId"],
        "name": v.get("name", ""),
        "company": (v.get("company") or {}).get("visibleName") or (v.get("company") or {}).get("name", ""),
        "rating": ((v.get("company") or {}).get("employerReviews") or {}).get("totalRating"),
        "city": (v.get("area") or {}).get("name", ""),
        "salary_from": sal_from, "salary_to": sal_to,      # как в вакансии на hh
        "gross": bool(c.get("gross")),                     # True = «до вычета налогов»
        "currency": c.get("currencyCode"),
        "salary_mode": c.get("mode"),
        "experience": EXP.get(v.get("workExperience"), v.get("workExperience") or ""),
        "formats": [FMT.get(f, f) for f in first(v.get("workFormats"), "workFormatsElement")],
        "published": (v.get("publicationTime") or {}).get("$", "")[:16],
        "url": f"https://hh.ru/vacancy/{v['vacancyId']}",
        "req": req, "resp": resp,
        "skills": [s for s, rx in SKILLS.items() if re.search(rx, text)],
        "responses": v.get("totalResponsesCount"),
    }


def mid(v):
    """Середина вилки - так же, как считает панель. Только месячная зарплата в рублях."""
    a, b = v["salary_from"], v["salary_to"]
    if v["currency"] != "RUR" or v["salary_mode"] not in (None, "MONTH") or not (a or b):
        return None
    return (a + b) / 2 if a and b else a or b


def run_search(s, progress=None):
    """Одна подборка: до s["pages"] страниц по 50 вакансий. progress(page, pages, found, total)."""
    params = {"text": s["text"], "area": s.get("area", 113), "items_on_page": 50,
              "enable_snippets": "true", "order_by": "publication_time"}
    if s.get("search_field", "name") == "name":   # по умолчанию слова ищутся только в названии вакансии
        params["search_field"] = "name"
    for k in SEARCH_KEYS:
        if s.get(k):
            params[k] = s[k]
    found, total, pages = [], 0, s.get("pages", 2)
    for page in range(pages):
        params["page"] = page
        res = fetch(params)
        total = res.get("totalResults") or total
        raw = res.get("vacancies", [])
        items = [v for v in raw if not v.get("@isAdv")]
        found += [normalize(v) for v in items]
        log(f"  {s['id']} стр.{page + 1}: {len(items)}")
        last = len(raw) < 50 or page == pages - 1
        if progress:
            progress(page + 1, pages if not last else page + 1, len(found), total)
        if last:
            break
        time.sleep(random.uniform(3, 6))   # пауза, чтобы не нагружать hh и не словить капчу
    return found, total


def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(DATA, name))


@contextmanager
def data_lock():
    """Плановый и ручной сбор не должны писать в data/ и git одновременно."""
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def saved_searches():
    with open(os.path.join(ROOT, "searches.json"), encoding="utf-8") as f:
        return json.load(f)


def write_saved(searches):
    with open(os.path.join(ROOT, "searches.json"), "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join("  " + json.dumps(s, ensure_ascii=False) for s in searches) + "\n]\n")


def collect(searches, progress=None):
    """Запускает поиски. Возвращает {id: (items, total)} или {id: Exception}."""
    out = {}
    for i, s in enumerate(searches):
        log("Поиск:", s["title"])
        try:
            out[s["id"]] = run_search(s, progress)
        except Exception as e:
            log("  ошибка:", e)
            out[s["id"]] = e
        if i < len(searches) - 1:
            time.sleep(random.uniform(3, 6))
    return out


def merge(results, run_searches, fresh=False):
    """Вливает результаты в data/. Подборки, которые сейчас не запускались, не трогает.
    fresh=True - выбросить вакансии запущенных подборок, которых нет в текущей выдаче.
    Возвращает новые (впервые увиденные) вакансии."""
    stamp = now_stamp()
    with data_lock():
        data = load("vacancies.json", {})
        old = {}                                   # любой id склеенной карточки -> карточка
        for v in data.get("vacancies", []):
            for i in v.get("ids") or [v["id"]]:
                old[i] = v
        stats = data.get("stats", {})
        known = {s["id"]: s for s in data.get("searches", [])}
        ran = {s["id"] for s in run_searches}
        ok_ids, seen = set(), {}
        for s in run_searches:
            known[s["id"]] = s
            res = results.get(s["id"])
            if not isinstance(res, tuple):
                stats[s["id"]] = {**stats.get(s["id"], {}), "error": str(res or "не запускался")}
                continue
            items, total = res
            ok_ids.add(s["id"])
            sal = [m for m in map(mid, items) if m]
            stats[s["id"]] = {"total": total, "got": len(items), "t": stamp,
                              "median": round(statistics.median(sal)) if sal else None}
            for v in items:
                if v["id"] in seen:                # уже нашли в другой подборке этого запуска
                    base = set(seen[v["id"]]["searches"])
                    prev = seen[v["id"]]
                else:                              # подборки, которые сейчас не запускались, сохраняем
                    prev = old.get(v["id"]) or {}
                    base = set(prev.get("searches", [])) - ran
                v["searches"] = sorted(base | {s["id"]})
                v["first_seen"] = prev.get("first_seen", stamp)
                v["last_seen"] = stamp
                seen[v["id"]] = v

        cutoff = (datetime.now(MSK) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M")
        for v in {id(x): x for x in old.values()}.values():   # каждая старая карточка один раз
            if any(i in seen for i in v.get("ids") or [v["id"]]):
                continue
            vs = set(v.get("searches", []))
            if vs & (ran - ok_ids):                # её поиск упал (капча) - не считаем пропавшей
                v["last_seen"] = max(v.get("last_seen", ""), stamp)
            elif fresh and vs & ran:
                continue
            if v.get("last_seen", "") < cutoff:
                continue
            seen[v["id"]] = v

        # одна и та же вакансия в нескольких городах -> одна карточка со списком городов
        groups = {}
        for v in sorted(seen.values(), key=lambda v: v.get("published", ""), reverse=True):
            k = (v["name"].strip().lower(), v["company"], v["salary_from"], v["salary_to"])
            ids = v.get("ids") or [v["id"]]
            if k in groups:
                g = groups[k]
                g["ids"] += [i for i in ids if i not in g["ids"]]
                for c in v.get("cities") or [v["city"]]:
                    if c not in g["cities"]:
                        g["cities"].append(c)
                g["searches"] = sorted(set(g["searches"]) | set(v["searches"]))
                g["first_seen"] = min(g["first_seen"], v["first_seen"])
                g["last_seen"] = max(g["last_seen"], v["last_seen"])
            else:
                v["ids"] = list(dict.fromkeys([v["id"]] + list(ids)))
                v["cities"] = list(v.get("cities") or [v["city"]])
                groups[k] = v
        vac = list(groups.values())
        for v in vac:
            v["copies"] = len(v["ids"])
        new = [g for g in vac if g["first_seen"] == stamp]

        # подборки: сохранённые + разовые, у которых ещё есть вакансии (свежие сверху)
        saved = saved_searches()
        saved_ids = {s["id"] for s in saved}
        used = {sid for v in vac for sid in v["searches"]}
        adhoc = [s for sid, s in known.items() if sid not in saved_ids and sid in used]
        all_s = saved + sorted(adhoc, key=lambda s: stats.get(s["id"], {}).get("t", ""), reverse=True)
        live = {s["id"] for s in all_s}
        stats = {k: x for k, x in stats.items() if k in live}
        save("vacancies.json", {"updated": stamp, "searches": all_s, "stats": stats, "vacancies": vac})
        history = load("history.json", [])
        history.append({"t": stamp, **{k: {"total": stats[k]["total"], "median": stats[k]["median"]} for k in ok_ids}})
        save("history.json", history[-500:])
    log(f"Готово: всего {len(vac)}, новых {len(new)}")
    return new


def publish(msg):
    """git commit + push данных, чтобы обновилась копия на GitHub Pages."""
    with data_lock():
        subprocess.run(["git", "add", "data", "searches.json"], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "-c", "user.name=wroko141-spec", "-c", "user.email=wroko141-spec@users.noreply.github.com",
                        "commit", "-qm", msg], cwd=ROOT, check=True)
        with open(os.path.expanduser("~/.github_token")) as f:
            h = base64.b64encode(f"x-access-token:{f.read().strip()}".encode()).decode()
        subprocess.run(["git", "-c", f"http.extraHeader=Authorization: Basic {h}", "push", "-q",
                        "https://github.com/wroko141-spec/hh-panel.git", "main"], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def notify(new):
    """Новые вакансии в Telegram, если заданы HH_TG_TOKEN и HH_TG_CHAT."""
    token, chat = os.environ.get("HH_TG_TOKEN"), os.environ.get("HH_TG_CHAT")
    if not (token and chat and new):
        return
    def money(v):
        a, b = v["salary_from"], v["salary_to"]
        if not (a or b):
            return "з/п не указана"
        n = lambda x: f"{x:,}".replace(",", " ")
        cur = {"RUR": "₽", "USD": "$", "EUR": "€"}.get(v["currency"], v["currency"] or "")
        per = {"HOUR": "/час", "SHIFT": "/смена", "SERVICE": " за услугу"}.get(v["salary_mode"], "")
        s = f"{n(a)}–{n(b)}" if a and b else (f"от {n(a)}" if a else f"до {n(b)}")
        return f"{s} {cur}{per}" + (" до вычета" if v.get("gross") else "")
    lines = [f"🆕 Новых вакансий: {len(new)}"]
    for v in new[:15]:
        lines.append(f"\n<b>{html.escape(v['name'])}</b> - {html.escape(v['company'])}\n"
                     f"{html.escape(v['city'])}, {money(v)}\n{v['url']}")
    if len(new) > 15:
        lines.append(f"\n…и ещё {len(new) - 15} в панели")
    body = urllib.parse.urlencode({"chat_id": chat, "text": "".join(lines), "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)
    except Exception as e:
        log("Telegram:", e)


def main():
    searches = saved_searches()
    first_run = not load("vacancies.json", {}).get("vacancies")
    results = collect(searches)
    if not any(isinstance(r, tuple) for r in results.values()):
        log("Ни один поиск не прошёл, данные не трогаю")
        sys.exit(1)
    new = merge(results, searches, fresh=os.environ.get("HH_FRESH") == "1")
    if not first_run:
        notify(new)
    if "--publish" in sys.argv:
        publish(f"Данные {now_stamp().replace('T', ' ')}")


if __name__ == "__main__":
    main()
