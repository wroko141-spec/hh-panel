"""Сборщик вакансий hh.ru для панели.

Открывает страницы поиска hh.ru (как обычный браузер), достаёт из них данные
о вакансиях и складывает в data/vacancies.json и data/history.json.
Запросы поиска лежат в searches.json. Только стандартная библиотека Python.
"""
import html, json, os, random, re, statistics, sys, time, urllib.parse, urllib.request
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
    "Git": r"\bgit\b", "Docker": r"docker", "Linux": r"linux", "REST API": r"rest|\bapi\b",
    "JavaScript": r"javascript|\bjs\b", "TypeScript": r"typescript|\bts\b", "React": r"react",
    "Vue": r"\bvue", "Node.js": r"node", "HTML/CSS": r"\bhtml|\bcss", "Figma": r"figma",
    "PHP": r"\bphp", "Java": r"\bjava\b", "C#": r"c#|\.net", "C++": r"c\+\+", "Go": r"\bgo\b|golang",
    "1С": r"1с|1c\b", "Pandas": r"pandas", "ML": r"machine learning|\bml\b|машинн", "LLM": r"\bllm|gpt|нейросет",
    "Excel": r"excel", "Telegram": r"telegram|телеграм", "Kubernetes": r"kubernetes|k8s",
}
EXP = {"noExperience": "нет", "between1And3": "1–3 года", "between3And6": "3–6 лет", "moreThan6": "6+ лет"}
FMT = {"REMOTE": "удалёнка", "HYBRID": "гибрид", "ON_SITE": "офис", "FIELD_WORK": "разъездная"}


def log(*a):
    print(datetime.now(MSK).strftime("%H:%M:%S"), *a, flush=True)


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
    if c.get("gross") and c.get("currencyCode") == "RUR":   # «до вычета» -> «на руки» (минус 13%)
        sal_from = sal_from and round(sal_from * .87)
        sal_to = sal_to and round(sal_to * .87)
    return {
        "id": v["vacancyId"],
        "name": v.get("name", ""),
        "company": (v.get("company") or {}).get("visibleName") or (v.get("company") or {}).get("name", ""),
        "rating": ((v.get("company") or {}).get("employerReviews") or {}).get("totalRating"),
        "city": (v.get("area") or {}).get("name", ""),
        "salary_from": sal_from, "salary_to": sal_to,
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


def run_search(s):
    params = {"text": s["text"], "area": s.get("area", 113), "items_on_page": 50,
              "enable_snippets": "true", "order_by": "publication_time"}
    for k in ("experience", "work_format", "salary", "only_with_salary"):
        if s.get(k):
            params[k] = s[k]
    found, total = [], 0
    for page in range(s.get("pages", 2)):
        params["page"] = page
        res = fetch(params)
        total = res.get("totalResults") or total
        raw = res.get("vacancies", [])
        items = [v for v in raw if not v.get("@isAdv")]
        found += [normalize(v) for v in items]
        log(f"  {s['id']} стр.{page + 1}: {len(items)}")
        if len(raw) < 50:
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


def notify(new, searches):
    """Новые вакансии в Telegram, если заданы HH_TG_TOKEN и HH_TG_CHAT."""
    token, chat = os.environ.get("HH_TG_TOKEN"), os.environ.get("HH_TG_CHAT")
    if not (token and chat and new):
        return
    def money(v):
        a, b = v["salary_from"], v["salary_to"]
        if not (a or b): return "з/п не указана"
        return (f"от {a:,}" if a else "") + (f" до {b:,}" if b else "") + " ₽".replace(",", " ")
    lines = [f"🆕 Новых вакансий: {len(new)}"]
    for v in new[:15]:
        lines.append(f"\n<b>{html.escape(v['name'])}</b> - {html.escape(v['company'])}\n"
                     f"{v['city']}, {money(v).replace(',', ' ')}\n{v['url']}")
    if len(new) > 15:
        lines.append(f"\n…и ещё {len(new) - 15} в панели")
    body = urllib.parse.urlencode({"chat_id": chat, "text": "".join(lines), "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)
    except Exception as e:
        log("Telegram:", e)


def main():
    with open(os.path.join(ROOT, "searches.json"), encoding="utf-8") as f:
        searches = json.load(f)
    now = datetime.now(MSK)
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    old = {v["id"]: v for v in load("vacancies.json", {}).get("vacancies", [])}
    first_run = not old
    history = load("history.json", [])
    seen, stats, new = {}, {}, []

    for s in searches:
        log("Поиск:", s["title"])
        try:
            items, total = run_search(s)
        except Exception as e:
            log("  ошибка:", e)
            stats[s["id"]] = {"error": str(e)}
            continue
        sal = [v["salary_from"] or v["salary_to"] for v in items
               if (v["salary_from"] or v["salary_to"]) and v["currency"] == "RUR"]
        stats[s["id"]] = {"total": total, "got": len(items), "median": round(statistics.median(sal)) if sal else None}
        for v in items:
            prev = seen.get(v["id"]) or old.get(v["id"])
            v["searches"] = sorted(set((prev or {}).get("searches", []) if v["id"] in seen else []) | {s["id"]})
            v["first_seen"] = (prev or {}).get("first_seen", stamp)
            v["last_seen"] = stamp
            if v["id"] not in old and v["id"] not in seen:
                new.append(v)
            seen[v["id"]] = v
        time.sleep(random.uniform(3, 6))

    if not any("total" in x for x in stats.values()):
        log("Ни один поиск не прошёл, данные не трогаю")
        sys.exit(1)

    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M")
    for vid, v in old.items():           # не нашли сейчас, но видели недавно - оставляем
        if vid not in seen and v.get("last_seen", "") >= cutoff:
            seen[vid] = v
    vac = sorted(seen.values(), key=lambda v: v.get("published", ""), reverse=True)
    save("vacancies.json", {"updated": stamp, "searches": searches, "stats": stats, "vacancies": vac})
    history.append({"t": stamp, **{k: {"total": x.get("total"), "median": x.get("median")}
                                   for k, x in stats.items() if "total" in x}})
    save("history.json", history[-500:])
    log(f"Готово: всего {len(vac)}, новых {len(new)}")
    if not first_run:
        notify(new, searches)


if __name__ == "__main__":
    main()
