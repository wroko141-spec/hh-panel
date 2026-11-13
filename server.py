"""Сервер сбора для HH панели.

Слушает 127.0.0.1:8790, наружу выходит через Tailscale Funnel (HTTPS).
Читать данные может кто угодно (это то же, что лежит на GitHub Pages),
запускать сбор и менять подборки - только с паролем HH_PANEL_PASS из ~/.config/hh-panel.env.
"""
import hmac, json, os, re, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import collector as C

PORT = 8790
ORIGINS = {"https://wroko141-spec.github.io", "http://localhost:8765"}
PASS = os.environ.get("HH_PANEL_PASS", "")
MAX_PAGES = 10
AREAS = []                       # [[id, name, region], ...] - справочник hh
try:
    with open(os.path.join(C.ROOT, "areas.json"), encoding="utf-8") as f:
        AREAS = json.load(f)
except FileNotFoundError:
    pass
AREA_NAME = {a[0]: a[1] for a in AREAS}
# крупные города - выше в подсказках (больше число - выше)
_BIG = ("Вся Россия,Москва,Санкт-Петербург,Новосибирск,Екатеринбург,Казань,Нижний Новгород,Красноярск,Челябинск,Самара,Уфа,"
        "Ростов-на-Дону,Краснодар,Омск,Воронеж,Пермь,Волгоград,Саратов,Тюмень,Тольятти,Махачкала,Барнаул,Ижевск,Хабаровск,"
        "Ульяновск,Иркутск,Владивосток,Ярославль,Севастополь,Ставрополь,Томск,Кемерово,Набережные Челны,Оренбург,Новокузнецк,"
        "Балашиха,Рязань,Чебоксары,Пенза,Липецк,Калининград,Астрахань,Тула,Киров,Сочи,Курск,Улан-Удэ,Тверь,Магнитогорск,"
        "Сургут,Брянск,Якутск,Иваново,Владимир,Симферополь,Белгород,Нижний Тагил,Калуга,Чита,Грозный,Волжский,Смоленск,"
        "Подольск,Саранск,Вологда,Курган,Череповец,Архангельск,Орел,Владикавказ,Нижневартовск,Йошкар-Ола,Стерлитамак,"
        "Мурманск,Кострома,Новороссийск,Тамбов,Химки,Мытищи,Нальчик,Таганрог,Нижнекамск,Благовещенск,Комсомольск-на-Амуре,"
        "Петрозаводск,Королев,Шахты,Энгельс,Великий Новгород,Люберцы,Братск,Старый Оскол,Ангарск,Сыктывкар,Дзержинск,"
        "Псков,Орск,Красногорск,Армавир,Абакан,Балаково,Бийск,Южно-Сахалинск,Одинцово,Ногинск,Альметьевск,Елабуга").split(",")
_NAMES = {}
for a in AREAS:
    _NAMES.setdefault(a[1], a[0])          # первое совпадение имени - сам город, а не одноимённое село
BIG = {_NAMES[n]: len(_BIG) - i for i, n in enumerate(_BIG) if n in _NAMES}

JOBS = {}                        # id -> состояние сбора
JOB_LOCK = threading.Lock()      # одновременно идёт только один ручной сбор
FAILS = []                       # время неудачных вводов пароля


def now():
    return time.time()


def check_pass(given):
    global FAILS
    FAILS = [t for t in FAILS if now() - t < 3600]
    if len(FAILS) >= 10:         # 10 ошибок за час - пауза, чтобы пароль не подбирали
        return "Слишком много неверных паролей, попробуйте через час"
    if not PASS or not hmac.compare_digest(str(given or "").encode(), PASS.encode()):   # байты: в пароле может быть кириллица
        FAILS.append(now())
        return "Неверный пароль"
    return None


def clean_search(p):
    """Проверяет параметры из панели. Возвращает подборку или текст ошибки."""
    text = str(p.get("text", "")).strip()
    if not 1 <= len(text) <= 200:
        return "Укажите слова для поиска (до 200 символов)"
    try:
        area = int(p.get("area", 113))
    except (TypeError, ValueError):
        return "Неверный город"
    if area not in AREA_NAME:
        return "Неизвестный город"
    try:
        pages = max(1, min(MAX_PAGES, int(p.get("pages") or 2)))
    except (TypeError, ValueError):
        return "Неверное число страниц"
    s = {"text": text, "area": area, "pages": pages,
         "search_field": "name" if p.get("search_field", "name") == "name" else "everywhere"}
    if p.get("experience") in C.EXP:
        s["experience"] = p["experience"]
    if p.get("work_format") in C.FMT:
        s["work_format"] = p["work_format"]
    try:
        sal = int(p.get("salary") or 0)
    except (TypeError, ValueError):
        sal = 0
    if sal > 0:
        s["salary"] = min(sal, 10_000_000)
    if p.get("only_with_salary"):
        s["only_with_salary"] = "true"
    exp = {"noExperience": "без опыта", "between1And3": "опыт 1–3", "between3And6": "опыт 3–6", "moreThan6": "опыт 6+"}
    parts = [text, AREA_NAME[area]] + ([exp[s["experience"]]] if "experience" in s else []) \
        + (["удалёнка" if s["work_format"] == "REMOTE" else C.FMT[s["work_format"]]] if "work_format" in s else []) \
        + ([f"от {sal // 1000}k"] if sal else [])
    s["title"] = str(p.get("title") or "").strip()[:80] or ", ".join(parts)
    s["area_name"] = AREA_NAME[area]
    return s


def run_job(job_id, s, save):
    job = JOBS[job_id]
    def progress(page, pages, found, total):
        job.update(page=page, pages=pages, found=found, total=total)
    try:
        job["state"] = "running"
        res = C.collect([s], progress)
        r = res[s["id"]]
        if not isinstance(r, tuple):
            raise RuntimeError(str(r))
        if save:                                   # сохранить как подборку - её будет обновлять таймер
            with C.data_lock():
                saved = [x for x in C.saved_searches() if x["id"] != s["id"]]
                C.write_saved(saved + [s])
        C.merge(res, [s])
        job.update(state="done", found=len(r[0]), total=r[1])
        try:
            C.publish(f"Сбор из панели: {s['title']}")
        except Exception as e:
            C.log("publish:", e)
    except Exception as e:
        job.update(state="error", error=str(e))
    finally:
        job["finished"] = now()
        JOB_LOCK.release()


class H(BaseHTTPRequestHandler):
    server_version = "hh-panel"

    def log_message(self, fmt, *a):
        pass

    def cors(self):
        o = self.headers.get("Origin")
        if o in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pass")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def reply(self, code, obj=None, raw=None, ctype="application/json; charset=utf-8"):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/ping":
            return self.reply(200, {"ok": True, "busy": JOB_LOCK.locked()})
        if u.path in ("/api/data", "/api/history"):
            name = "vacancies.json" if u.path == "/api/data" else "history.json"
            try:
                with open(os.path.join(C.DATA, name), "rb") as f:
                    return self.reply(200, raw=f.read())
            except FileNotFoundError:
                return self.reply(404, {"error": "нет данных"})
        if u.path == "/api/areas":
            q = (parse_qs(u.query).get("q") or [""])[0].strip().lower().replace("ё", "е")
            if len(q) < 2:
                return self.reply(200, [])
            starts = [a for a in AREAS if a[1].lower().replace("ё", "е").startswith(q)]
            inside = [a for a in AREAS if q in a[1].lower().replace("ё", "е") and a not in starts]
            # сначала «вся Россия» и крупные города, потом остальные города, регионы - в конце
            rank = lambda a: (0 if a[0] in BIG else 2 if a[2] == "регион" else 1, BIG.get(a[0], 0) * -1, len(a[1]))
            res = sorted(starts, key=rank) + sorted(inside, key=rank)
            return self.reply(200, res[:15])
        m = re.fullmatch(r"/api/job/([0-9a-f]{12})", u.path)
        if m and m.group(1) in JOBS:
            return self.reply(200, JOBS[m.group(1)])
        self.reply(404, {"error": "не найдено"})

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_POST(self):
        try:
            self.post()
        except Exception as e:
            C.log("POST error:", repr(e))
            self.reply(500, {"error": "Внутренняя ошибка сервера"})

    def post(self):
        u = urlparse(self.path)
        p = self.body()
        if p is None:
            return self.reply(400, {"error": "неверный запрос"})
        err = check_pass(unquote(self.headers.get("X-Pass") or ""))   # панель кодирует пароль (encodeURIComponent)
        if err:
            return self.reply(403, {"error": err})
        if u.path == "/api/login":
            return self.reply(200, {"ok": True})
        if u.path == "/api/run":
            s = clean_search(p)
            if isinstance(s, str):
                return self.reply(400, {"error": s})
            if not JOB_LOCK.acquire(blocking=False):
                return self.reply(409, {"error": "Уже идёт сбор, дождитесь окончания"})
            job_id = uuid.uuid4().hex[:12]
            s["id"] = ("s-" if p.get("save") else "r-") + job_id[:8]
            JOBS[job_id] = {"id": job_id, "search": s, "state": "queued", "page": 0, "pages": s["pages"],
                            "found": 0, "total": None, "started": now()}
            for k in [k for k, j in JOBS.items() if now() - j.get("finished", now()) > 3600]:
                JOBS.pop(k, None)                 # старые задания не копим
            threading.Thread(target=run_job, args=(job_id, s, bool(p.get("save"))), daemon=True).start()
            return self.reply(200, JOBS[job_id])
        if u.path == "/api/search/delete":
            sid = str(p.get("id", ""))
            with C.data_lock():
                saved = C.saved_searches()
                C.write_saved([x for x in saved if x["id"] != sid])
                data = C.load("vacancies.json", {})
                data["searches"] = [x for x in data.get("searches", []) if x["id"] != sid]
                data.get("stats", {}).pop(sid, None)
                for v in data.get("vacancies", []):
                    v["searches"] = [x for x in v["searches"] if x != sid]
                data["vacancies"] = [v for v in data.get("vacancies", []) if v["searches"]]
                C.save("vacancies.json", data)
            threading.Thread(target=lambda: C.publish("Удалена подборка"), daemon=True).start()
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "не найдено"})


if __name__ == "__main__":
    if not PASS:
        raise SystemExit("Не задан HH_PANEL_PASS в ~/.config/hh-panel.env")
    C.log(f"Сервер панели на 127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
