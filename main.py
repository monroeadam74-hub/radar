"""
Радар вирусных роликов: YouTube Shorts + TikTok → фильтры → ИИ-разбор (Gemini) →
база Cloudflare D1 → дайджест в Telegram + отчёт CSV.
Запускается GitHub Actions по расписанию (см. .github/workflows/radar.yml).
"""
import csv, datetime as dt, json, os, re, sqlite3, sys, time
import requests, yaml

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__) or ".", "config.yaml"), encoding="utf-8"))
S = CFG["settings"]
ENV = lambda k, d=None: os.environ.get(k, d)
NOW = dt.datetime.now(dt.timezone.utc)
LOG = []


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.append(msg)


# ------------------------------------------------------------------ БАЗА (Cloudflare D1 или локальный SQLite для теста)
class DB:
    def __init__(self):
        self.local = ENV("LOCAL_DB")
        if self.local:
            self.cx = sqlite3.connect(self.local)
            self.cx.row_factory = sqlite3.Row
            return
        self.acc, self.tok = ENV("CF_ACCOUNT_ID"), ENV("CF_API_TOKEN")
        self.h = {"Authorization": f"Bearer {self.tok}", "Content-Type": "application/json"}
        base = f"https://api.cloudflare.com/client/v4/accounts/{self.acc}/d1/database"
        r = requests.get(base, headers=self.h, params={"name": ENV("D1_NAME", "radar-db")}, timeout=60).json()
        if not r.get("success") or not r.get("result"):
            sys.exit(f"Не нашёл базу D1 'radar-db': {r.get('errors')}")
        self.url = f"{base}/{r['result'][0]['uuid']}/query"

    def q(self, sql, params=()):
        params = [json.dumps(p, ensure_ascii=False) if isinstance(p, (dict, list)) else p for p in params]
        if self.local:
            cur = self.cx.execute(sql, params)
            self.cx.commit()
            return [dict(r) for r in cur.fetchall()]
        for attempt in range(4):
            try:
                r = requests.post(self.url, headers=self.h, json={"sql": sql, "params": params}, timeout=60).json()
                if r.get("success"):
                    self.meta = r["result"][0].get("meta") or {}
                    return r["result"][0].get("results", [])
                err = r.get("errors")
            except Exception as e:
                err = str(e)
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"D1 ошибка: {err} | {sql[:80]}")


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY, platform TEXT, vertical TEXT, url TEXT, author TEXT, author_id TEXT,
        title TEXT, published TEXT, duration INTEGER, lang TEXT, keyword TEXT,
        views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER, followers INTEGER, outlier REAL,
        status TEXT, prefilter TEXT, nutra_score INTEGER, content_type TEXT, analysis TEXT, subtitles TEXT,
        first_seen TEXT, updated TEXT)""",
    "CREATE TABLE IF NOT EXISTS snapshots (video_id TEXT, ts TEXT, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER)",
    """CREATE TABLE IF NOT EXISTS authors (
        id TEXT PRIMARY KEY, platform TEXT, handle TEXT, name TEXT, url TEXT, followers INTEGER,
        shots INTEGER, relevant_shots INTEGER, max_views INTEGER, best_score INTEGER, main_type TEXT,
        lang TEXT, verticals TEXT, first_seen TEXT, last_seen TEXT)""",
    "CREATE TABLE IF NOT EXISTS runs (ts TEXT, found INTEGER, new INTEGER, analyzed INTEGER, cost_usd REAL, log TEXT)",
    "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, v TEXT, note TEXT, hidden INTEGER DEFAULT 0, ts TEXT)",
    "CREATE TABLE IF NOT EXISTS thumbs (id TEXT PRIMARY KEY, mime TEXT, data TEXT, ts TEXT)",
    """CREATE TABLE IF NOT EXISTS profiles (
        id TEXT PRIMARY KEY, platform TEXT, handle TEXT, name TEXT, url TEXT, followers INTEGER,
        reels INTEGER, median_views INTEGER, best_x REAL, shots INTEGER, new_last INTEGER,
        status TEXT, added TEXT, last_run TEXT, active INTEGER DEFAULT 1, ai INTEGER, deep INTEGER)""",
    "CREATE INDEX IF NOT EXISTS i_snap ON snapshots(video_id)",
    "CREATE TABLE IF NOT EXISTS funnels (url TEXT PRIMARY KEY, profile_id TEXT, final_url TEXT, status TEXT, data TEXT, ts TEXT)",
    "CREATE INDEX IF NOT EXISTS i_vid_author ON videos(author_id)",
]


# ------------------------------------------------------------------ СБОР: YouTube Shorts
def iso_dur(s):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def collect_youtube(vertical, keywords):
    key = ENV("YOUTUBE_KEY")
    if not key:
        log("YouTube: нет ключа, пропуск"); return []
    YT = "https://www.googleapis.com/youtube/v3/"
    after = (NOW - dt.timedelta(days=30 * S["months_back"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids, kw_of = [], {}
    for kw in keywords:
        try:
            r = requests.get(YT + "search", params=dict(key=key, part="id", q=kw, type="video", videoDuration="short",
                order="viewCount", publishedAfter=after, regionCode=S["yt_region"],
                relevanceLanguage=S["langs"][0], maxResults=50), timeout=60).json()
        except Exception as e:
            log("YouTube search ошибка:", e); continue
        if "error" in r:
            log("YouTube ошибка:", r["error"].get("message")); break
        for it in r.get("items", []):
            v = it["id"].get("videoId")
            if v and v not in kw_of:
                ids.append(v); kw_of[v] = kw
    out = []
    for i in range(0, len(ids), 50):
        r = requests.get(YT + "videos", params=dict(key=key, part="snippet,statistics,contentDetails",
                         id=",".join(ids[i:i + 50])), timeout=60).json()
        for v in r.get("items", []):
            st, sn = v.get("statistics", {}), v["snippet"]
            out.append(dict(id="yt_" + v["id"], raw_id=v["id"], platform="YouTube", vertical=vertical,
                url=f"https://www.youtube.com/shorts/{v['id']}", author=sn["channelTitle"],
                author_id="yt_" + sn["channelId"], author_url=f"https://www.youtube.com/channel/{sn['channelId']}",
                title=(sn.get("title") or "") + " " + (sn.get("description") or "")[:300],
                published=sn["publishedAt"][:10], duration=iso_dur(v["contentDetails"]["duration"]),
                lang=(sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or "").lower(),
                views=int(st.get("viewCount", 0) or 0), likes=int(st.get("likeCount", 0) or 0),
                comments=int(st.get("commentCount", 0) or 0), shares=None, keyword=kw_of[v["id"]]))
    chans = list({x["author_id"][3:] for x in out})
    subs = {}
    for i in range(0, len(chans), 50):
        r = requests.get(YT + "channels", params=dict(key=key, part="statistics", id=",".join(chans[i:i + 50])),
                         timeout=60).json()
        for c in r.get("items", []):
            subs["yt_" + c["id"]] = int(c["statistics"].get("subscriberCount", 0) or 0)
    for x in out:
        x["followers"] = subs.get(x["author_id"])
    log(f"YouTube [{vertical}]: кандидатов {len(out)}")
    return out


# ------------------------------------------------------------------ СБОР: TikTok (Apify)
def g(d, *paths):
    for p in paths:
        cur = d
        for k in p.split("."):
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            elif isinstance(cur, list) and k.isdigit() and int(k) < len(cur):
                cur = cur[int(k)]
            else:
                cur = None; break
        if cur not in (None, "", []):
            return cur
    return None


def apify():
    from apify_client import ApifyClient
    return ApifyClient(ENV("APIFY_TOKEN"))


def run_actor(actor, run_input, max_items=None):
    ac = apify()
    try:
        run = ac.actor(actor).call(run_input=run_input, **({"max_items": int(max_items)} if max_items else {}))
    except TypeError:   # старый клиент без max_items
        run = ac.actor(actor).call(run_input=run_input)
    ds = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "default_dataset_id", None)
    return list(ac.dataset(ds).iterate_items())


def collect_tiktok(vertical, keywords):
    if not ENV("APIFY_TOKEN"):
        log("TikTok: нет токена Apify, пропуск"); return []
    try:
        items = run_actor(CFG.get("tiktok_search_actor", "paul_44/tiktok-search"), {
            "keywords": keywords, "maxResults": S["tt_results_per_keyword"], "uploadDate": "6months",
            "region": S["tt_region"], "minViews": S["min_views"], "strictKeywordMatch": True,
            "includeSearchKeyword": True})
    except Exception as e:
        log("TikTok Apify ошибка:", str(e)[:200]); return []
    out, seen = [], set()
    for it in items:
        vid = str(g(it, "id") or "")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        user = g(it, "channel.username", "authorMeta.name") or ""
        ts = g(it, "uploadedAt", "createTime")
        out.append(dict(id="tt_" + vid, raw_id=vid, platform="TikTok", vertical=vertical,
            url=g(it, "url", "link", "postPage", "webVideoUrl"), author=user, author_id="tt_" + user,
            author_url=f"https://www.tiktok.com/@{user}", title=g(it, "title", "text") or "",
            published=dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if isinstance(ts, (int, float)) else None,
            duration=g(it, "video.duration", "videoMeta.duration"), lang="",
            views=int(g(it, "views", "playCount") or 0), likes=g(it, "likes", "diggCount"),
            comments=g(it, "comments", "commentCount"), shares=g(it, "shares", "shareCount"),
            keyword=g(it, "searchKeyword"), followers=g(it, "channel.followers", "authorMeta.fans")))
    log(f"TikTok [{vertical}]: кандидатов {len(out)}")
    return out


def tiktok_download(cands):
    """Просим Apify скачать видео (TikTok не отдаёт напрямую). Возвращает {raw_id: (bytes, followers)}"""
    urls = [c["url"] for c in cands if c.get("url")]
    if not urls:
        return {}
    res = {}
    try:
        items = run_actor(CFG.get("tiktok_download_actor", "clockworks/tiktok-scraper"),
                          {"postURLs": urls, "shouldDownloadVideos": True, "shouldDownloadCovers": False,
                           "resultsPerPage": 1})
    except Exception as e:
        log("TikTok скачивание ошибка:", str(e)[:200]); return {}
    for it in items:
        vid, media = str(it.get("id") or ""), it.get("mediaUrls") or []
        if not vid or not media:
            continue
        try:
            r = requests.get(media[0], params={"token": ENV("APIFY_TOKEN")}, timeout=120)
            if r.ok and len(r.content) > 50_000:
                res[vid] = (r.content, g(it, "authorMeta.fans"))
        except Exception:
            pass
    log(f"TikTok: скачано видео {len(res)} из {len(urls)}")
    return res


# ------------------------------------------------------------------ ИИ: Gemini
GEM = {"client": None, "model": None, "tin": 0, "tout": 0}


def gemini():
    if GEM["client"]:
        return GEM["client"]
    from google import genai
    from google.genai import types
    c = genai.Client(api_key=ENV("GEMINI_KEY"), http_options=types.HttpOptions(timeout=120_000))
    names = [m.name.replace("models/", "") for m in c.models.list()]
    ok = [n for n in names if re.fullmatch(r"gemini-(\d+(\.\d+)?-)?flash-lite(-latest)?", n)]
    GEM["model"] = ENV("GEMINI_MODEL") or ("gemini-flash-lite-latest" if "gemini-flash-lite-latest" in ok
                                            else (sorted(ok, reverse=True) or ["gemini-flash-lite-latest"])[0])
    GEM["client"] = c
    # запасные модели на случай перегрузки (503)
    GEM["fallback"] = [m for m in ["gemini-flash-lite-latest", "gemini-flash-latest"] + sorted(ok, reverse=True)
                       if m in names and m != GEM["model"]][:2]
    log("Gemini модель:", GEM["model"], "| запасные:", GEM["fallback"])
    return c


class QuotaDay(Exception):
    """Дневной лимит модели исчерпан."""


def ask_model(contents, label, model):
    """Один вызов конкретной модели (без запасных). 429 «в день» → QuotaDay; 429 «в минуту» и 503 → пауза и повтор."""
    from google.genai import types
    c = gemini()
    for attempt in range(6):
        try:
            r = c.models.generate_content(model=model, contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2))
            u = getattr(r, "usage_metadata", None)
            GEM["tin"] += getattr(u, "prompt_token_count", 0) or 0
            GEM["tout"] += getattr(u, "candidates_token_count", 0) or 0
            txt = (r.text or "").strip().strip("`")
            if txt.startswith("json"):
                txt = txt[4:]
            return json.loads(txt)
        except json.JSONDecodeError:
            if attempt >= 1:
                return None
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                if re.search(r"PerDay|per day|daily", msg, re.I):
                    raise QuotaDay(msg[:300])
                if attempt >= 4:
                    raise QuotaDay("постоянный 429: " + msg[:250])
                time.sleep(20 + 20 * attempt)
                continue
            log(f"  {label}: ошибка {model} ({msg[:90]})")
            if attempt >= 2:
                return None
            time.sleep(8 * (attempt + 1))
    return None


def ask(contents, label):
    from google.genai import types
    c = gemini()
    models = [GEM["model"]] + GEM.get("fallback", [])
    order = (models * 2)[:4]           # основная → запасные → снова основная
    for attempt, model in enumerate(order):
        try:
            r = c.models.generate_content(model=model, contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2))
            u = getattr(r, "usage_metadata", None)
            GEM["tin"] += getattr(u, "prompt_token_count", 0) or 0
            GEM["tout"] += getattr(u, "candidates_token_count", 0) or 0
            txt = (r.text or "").strip().strip("`")
            if txt.startswith("json"):
                txt = txt[4:]
            return json.loads(txt)
        except Exception as e:
            log(f"  {label}: ошибка {model} ({str(e)[:90]})")
            if "429" in str(e) and attempt >= 2:
                break
            time.sleep(8 * (attempt + 1))
    return None


PREFILTER = """Ты фильтруешь ролики для радара вертикали «{vname}».
Для каждого ролика по подписи определи язык и относится ли он к теме вертикали
(боль, здоровье, средства, упражнения по теме). НЕ относятся: юмор, спортивные травмы молодёжи,
развлечение, другая тема. Верни JSON-список объектов: [{{"id": "...", "lang": "en/es/hi/...", "relevant": true/false, "reason": "кратко"}}]
Ролики:
{items}"""

ANALYSIS = """Ты — аналитик вирусного short-form контента для аффилиат-маркетинга (нутра, SS-офферы на США), вертикаль «{vname}».
Разбери видео и верни СТРОГО JSON-объект. ВСЕ поля обязательны (если чего-то нет — "нет").
{{
 "language": "язык речи или надписей",
 "hook_0_3s": "что видно и слышно в первые 3 секунды (по-русски)",
 "hook_type": "вопрос / боль / шок / обещание / история / POV / ASMR / другое",
 "format": "эксперт, упражнение, 3D-анатомия, рецепт, юмор, отзыв, демонстрация товара, слайды и т.п.",
 "content_type": "юмор_травмы / товар / натуральные_средства / упражнения_эксперт / медицина_процедура / другое",
 "cta": "призыв к действию по-русски (дословно, если есть: 'напишите X в директ') или 'нет'",
 "cta_mechanic": "комментарий / директ / ссылка в профиле / подписка / нет",
 "emotion": "эмоциональная дуга",
 "product_mentioned": "продукт/бренд или 'нет'",
 "target_audience": "возраст, пол, проблема",
 "direction": "одно основное направление по симптоматике — ключ из справочника ниже",
 "direction_extra": ["0–2 дополнительных ключа, только если ролик реально про них"],
 "host": "кто в кадре — один ключ из справочника ведущих ниже",
 "visuals": ["1–2 ключа визуального приёма из справочника ниже, первый — главный"],
 "nutra_score": число 1-5 по шкале:
{scale}
 "nutra_reason": "почему такой балл, 1 предложение",
 "why_it_works": "1–2 предложения по-русски",
 "how_to_adapt": "как перенести механику на наш ролик без копирования",
 "tech": {{"cuts": число склеек/смен плана за ролик, "face_share": "доля времени, когда в кадре лицо ведущего, %",
          "voice": "ИИ-голос / живой голос / без голоса", "music": "есть фоновая музыка / нет", "captions": "субтитры на экране: да/нет, где (верх/центр/низ)",
          "first_words_sec": секунда, когда звучит первое слово, "pace": "медленный / средний / быстрый монтаж"}},
 "moderation_risk": "низкий/средний/высокий + почему. ЛЮБЫЕ обещания вылечить, снять или убрать боль, избежать операции или лекарств, обратить болезнь вспять, «за N дней», религиозные «исцелит» = ВЫСОКИЙ",
 "on_screen_text": [{{"t": секунда, "orig": "надпись", "ru": "перевод"}}],
 "subtitles": [{{"start": сек, "end": сек, "orig": "фраза", "ru": "перевод"}}]
}}
Справочник направлений (ключ — название):
{dirs}
{hints}
{vis}
{vhints}
Субтитры: каждая фраза 1–4 сек, не длиннее ~12 слов, таймкоды по моменту речи, все end ≤ {dur}. Речи нет — пустой список."""


DIRECTIONS = [
    ("knees", "Колени и суставы"), ("back", "Спина и шея"), ("potency", "Потенция и либидо"),
    ("testosterone", "Тестостерон и выносливость"), ("prostate", "Простата"), ("sugar", "Сахар в крови"),
    ("weight", "Похудение и метаболизм"), ("heart", "Давление и сердце"), ("brain", "Память и мозг"), ("vision", "Зрение"),
    ("hearing", "Слух и шум в ушах"), ("gut", "ЖКТ и кишечник"), ("liver", "Печень и детокс"), ("sleep", "Сон и стресс"),
    ("women", "Женское здоровье"), ("veins", "Вены и ноги"), ("skin", "Кожа и антиэйдж"), ("hair", "Волосы и ногти"),
    ("energy", "Энергия и усталость"), ("immunity", "Иммунитет и воспаление"), ("kidney", "Почки и мочевой пузырь"),
    ("general", "Общее / долголетие"), ("other", "Не про здоровье"),
]
DIR_KEYS = {k for k, _ in DIRECTIONS}
DIR_VERSION = 2  # поднять при изменении справочника — старая разметка переделается
DIR_LIST = "\n".join(f"{k} — {t}" for k, t in DIRECTIONS)
DIR_HINTS = ("Подсказки: колени и суставы (колени, тазобедренные, плечи, кисти, артрит, артроз, хруст) и спина и шея (поясница, грыжа, "
             "осанка, шея, седалищный нерв) — разные; потенция и либидо (эрекция, желание) и тестостерон и выносливость "
             "(уровень тестостерона, сила, мышцы, мужская энергия) и простата (простатит, мочеиспускание у мужчин) — три разных; "
             "давление и сердце (гипертония, холестерин, сердце, артерии) и вены и ноги (варикоз, отёки, тяжесть в ногах, нейропатия стоп) — разные; "
             "general — здоровье без конкретной проблемы («5 продуктов после 50», привычки долгожителей); "
             "other — ролик не про здоровье.")


HOSTS = [("doctor", "Врач / эксперт"), ("sage", "Мудрец-архетип"), ("elder", "Бабушка / дед / фермер"),
         ("person", "Обычный человек / UGC"), ("none", "Без ведущего")]
VISUALS = [("talking", "Говорящая голова"), ("pointer", "«Указка на тело»"), ("shock", "Шок-крупный план"),
           ("anatomy", "3D-анатомия"), ("beforeafter", "До / после на экране"), ("prop", "Предмет в руке + вопрос"),
           ("experiment", "Наглядный опыт"), ("recipe", "Рецепт / приготовление"), ("exercise", "Упражнение"),
           ("clinic", "Клиника / процедура"), ("rating", "Рейтинг / обзор"), ("product", "Товар / распаковка / ASMR"),
           ("humor", "Юмор / скетч / POV"), ("slides", "Слайды / текст на фоне")]
HOST_KEYS, VIS_KEYS = {k for k, _ in HOSTS}, {k for k, _ in VISUALS}
VIS_VERSION = 1
VIS_LIST = ("Ведущий (кто в кадре), один ключ:\n" + "\n".join(f"{k} — {t}" for k, t in HOSTS) +
            "\nВизуальный приём, 1–2 ключа (первый — главный):\n" + "\n".join(f"{k} — {t}" for k, t in VISUALS))
VIS_HINTS = ("Подсказки: doctor — врач, медик, эксперт в халате/кабинете или представленный как специалист; sage — монах, шаман, "
             "целитель, гуру; elder — пожилой человек из народа: бабушка, дед, фермер, огород, деревенская кухня; person — обычный "
             "человек, отзыв, личная история; none — в кадре нет ведущего (голос за кадром, текст, анимация). "
             "pointer — ведущий показывает на часть тела другого человека/модели и называет проблему («это — забитая печень»); "
             "shock — отталкивающий крупный план (паразиты, гной, гнилые зубы, отёки); prop — ведущий держит предмет (лимон, арбуз, "
             "лёд, модель) и задаёт вопрос/интригу; experiment — наглядный опыт с предметами; talking — ведущий просто говорит в камеру "
             "без особого приёма.")


def norm_vis(host, vis):
    h = str(host or "").strip().lower()
    h = h if h in HOST_KEYS else None
    vs = [str(x).strip().lower() for x in (vis if isinstance(vis, list) else [vis]) if isinstance(x, str)]
    vs = list(dict.fromkeys(x for x in vs if x in VIS_KEYS))[:2]
    return h, (vs[0] if vs else None), (",".join(vs) if vs else None)


VISCLASS = """По описанию разобранного ролика определи, кто в кадре и какой визуальный приём используется.
{vis}
{hints}
Верни JSON-список: [{{"id": "...", "host": "ключ", "visuals": ["ключ", "ключ"]}}]
Ролики:
{items}"""


def classify_visuals(db, limit=4000):
    """Ведущий + визуальный приём для уже разобранных роликов — по тексту разбора, без загрузки видео."""
    db.q("UPDATE videos SET host=NULL, visual=NULL, visuals=NULL WHERE visual IS NOT NULL AND COALESCE(vis_v, 0) < ?", [VIS_VERSION])
    rows = db.q("SELECT id, title, analysis FROM videos WHERE status='analyzed' AND host IS NULL LIMIT ?", [limit])
    log(f"Ведущий/визуал: к разметке {len(rows)} роликов")
    done = 0
    for i in range(0, len(rows), 25):
        chunk = rows[i:i + 25]
        items = []
        for r in chunk:
            a = json.loads(r["analysis"] or "{}")
            ost = " / ".join(str(x.get("ru") or x.get("orig") or "") for x in (a.get("on_screen_text") or [])[:4] if isinstance(x, dict))
            txt = " | ".join(f"{k}: {v}" for k, v in [("хук", a.get("hook_0_3s")), ("формат", a.get("format")),
                             ("тип хука", a.get("hook_type")), ("надписи", ost[:200]), ("почему работает", a.get("why_it_works")),
                             ("подпись", (r.get("title") or "")[:150])] if v and v != "нет")
            items.append(f'{r["id"]}: {txt!r}')
        data = ask([VISCLASS.format(vis=VIS_LIST, hints=VIS_HINTS, items="\n".join(items))], "визуал")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        upd = []
        for d in data or []:
            if isinstance(d, dict) and d.get("id"):
                h, v1, vs = norm_vis(d.get("host"), d.get("visuals"))
                if h or v1:
                    upd.append((d["id"], h, v1, vs))
        if upd:
            ids = ",".join(lit(u[0]) for u in upd)
            case = lambda j: " ".join(f"WHEN {lit(u[0])} THEN {lit(u[j])}" for u in upd)
            db.q(f"UPDATE videos SET vis_v = {VIS_VERSION}, host = CASE id {case(1)} END, visual = CASE id {case(2)} END, "
                 f"visuals = CASE id {case(3)} END WHERE id IN ({ids})")
            done += len(upd)
        log(f"  размечено {done}/{len(rows)}")
    return done


def norm_dirs(main, extra=None):
    """→ (основное, 'основное,доп1,доп2') только из справочника."""
    d = str(main or "").strip().lower()
    d = d if d in DIR_KEYS else None
    ex = [str(x).strip().lower() for x in (extra or []) if isinstance(x, str)]
    ex = [x for x in ex if x in DIR_KEYS and x != d and x != "other"][:2]
    if not d and ex:
        d, ex = ex[0], ex[1:]
    return d, ",".join([d] + ex) if d else None


DIRCLASS = """Отнеси каждый ролик о здоровье к направлению по симптоматике/проблеме, которой он посвящён.
Справочник (используй только ключи слева):
{dirs}
{hints}
Для каждого ролика: "direction" — одно основное направление, "extra" — 0–2 дополнительных (только если ролик реально про них).
Верни JSON-список: [{{"id": "...", "direction": "ключ", "extra": ["ключ"]}}]
Ролики:
{items}"""


def classify_directions(db, limit=4000):
    """Разметка направлений по уже сохранённому тексту (подпись, разбор, субтитры) — без загрузки видео."""
    # всё, что размечено по старому справочнику, — заново
    db.q("UPDATE videos SET direction=NULL, directions=NULL WHERE direction IS NOT NULL AND COALESCE(dir_v, 0) < ?", [DIR_VERSION])
    rows = db.q("SELECT id, title, analysis, subtitles FROM videos WHERE direction IS NULL LIMIT ?", [limit])
    log(f"Направления: к разметке {len(rows)} роликов")
    done = 0
    for i in range(0, len(rows), 30):
        chunk = rows[i:i + 30]
        items = []
        for r in chunk:
            a = json.loads(r["analysis"] or "{}") if r.get("analysis") else {}
            subs = json.loads(r["subtitles"] or "[]") if r.get("subtitles") else []
            speech = " ".join(str(s.get("ru") or s.get("orig") or "") for s in subs if isinstance(s, dict))[:350]
            txt = " | ".join(x for x in [(r.get("title") or "")[:300], a.get("hook_0_3s"), a.get("target_audience"),
                                          a.get("product_mentioned"), speech] if x and x != "нет")
            items.append(f'{r["id"]}: {txt!r}')
        data = ask([DIRCLASS.format(dirs=DIR_LIST, hints=DIR_HINTS, items="\n".join(items))], "направления")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        upd = []
        for d in data or []:
            if not isinstance(d, dict) or not d.get("id"):
                continue
            main, alls = norm_dirs(d.get("direction"), d.get("extra"))
            if main:
                upd.append((d["id"], main, alls))
        if upd:
            ids = ",".join(lit(u[0]) for u in upd)
            db.q("UPDATE videos SET dir_v = " + str(DIR_VERSION) + ", direction = CASE id " + " ".join(f"WHEN {lit(u[0])} THEN {lit(u[1])}" for u in upd) +
                 " END, directions = CASE id " + " ".join(f"WHEN {lit(u[0])} THEN {lit(u[2])}" for u in upd) +
                 f" END WHERE id IN ({ids})")
            done += len(upd)
        log(f"  размечено {done}/{len(rows)}")
    return done


def prefilter(cands, vertical):
    vname = CFG["verticals"][vertical].get("name", vertical)
    res = {}
    for i in range(0, len(cands), 40):
        chunk = cands[i:i + 40]
        items = "\n".join(f'{c["id"]}: {(c.get("title") or "")[:250]!r}' for c in chunk)
        data = ask([PREFILTER.format(vname=vname, items=items)], "префильтр")
        if isinstance(data, dict):
            data = data.get("items") or data.get("videos") or next((v for v in data.values() if isinstance(v, list)), [])
        for d in data or []:
            if isinstance(d, dict) and d.get("id"):
                res[d["id"]] = d
    return res


def clean_subs(subs, dur):
    out = []
    try:
        dur = float(dur or 0)
    except Exception:
        dur = 0
    for x in subs or []:
        if not isinstance(x, dict):
            continue
        try:
            a, b = float(x.get("start", 0)), float(x.get("end", 0))
        except Exception:
            continue
        if dur and a >= dur:
            continue
        if dur:
            b = min(b, dur)
        if b <= a:
            b = a + 2
        out.append({**x, "start": round(a, 1), "end": round(b, 1)})
    return out


def analyze(c, video_bytes=None, model=None):
    from google.genai import types
    v = CFG["verticals"].get(c["vertical"]) or PROFILE_VERTICAL
    prompt = ANALYSIS.format(vname=v.get("name", c["vertical"]), scale=v.get("nutra_scale", ""), dur=c.get("duration") or 60,
                           dirs=DIR_LIST, hints=DIR_HINTS, vis=VIS_LIST, vhints=VIS_HINTS)
    if c["platform"] == "YouTube":
        part = types.Part(file_data=types.FileData(file_uri=f"https://www.youtube.com/watch?v={c['raw_id']}"))
    else:
        part = types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")
    data = ask_model([part, prompt], c["id"], model) if model else ask([part, prompt], c["id"])
    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict)), None)
    if not isinstance(data, dict):
        return None
    data["subtitles"] = clean_subs(data.get("subtitles"), c.get("duration"))
    return data


# ------------------------------------------------------------------ ЗАПИСЬ
def upsert_video(db, c, status, pre=None, a=None):
    ts = NOW.isoformat(timespec="seconds")
    f = c.get("followers")
    outlier = round(c["views"] / f, 1) if f else None
    existing = db.q("SELECT id FROM videos WHERE id=?", [c["id"]])
    if existing:
        db.q("UPDATE videos SET views=?, likes=?, comments=?, shares=?, followers=COALESCE(?, followers), outlier=COALESCE(?, outlier), updated=? WHERE id=?",
             [c["views"], c.get("likes"), c.get("comments"), c.get("shares"), f, outlier, ts, c["id"]])
    else:
        db.q("""INSERT INTO videos (id, platform, vertical, url, author, author_id, title, published, duration, lang, keyword,
                views, likes, comments, shares, followers, outlier, status, prefilter, nutra_score, content_type, analysis,
                subtitles, first_seen, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             [c["id"], c["platform"], c["vertical"], c.get("url"), c.get("author"), c.get("author_id"), (c.get("title") or "")[:500],
              c.get("published"), c.get("duration"), c.get("lang"), c.get("keyword"), c["views"], c.get("likes"),
              c.get("comments"), c.get("shares"), f, outlier, status, pre, None, None, None, None, ts, ts])
        if c.get("source"):
            db.q("UPDATE videos SET source=? WHERE id=?", [c["source"], c["id"]])
    if c.get("vs_median") is not None:
        db.q("UPDATE videos SET vs_median=? WHERE id=?", [c["vs_median"], c["id"]])
    if a is not None:
        a2 = {k: v for k, v in a.items() if k != "subtitles"}
        db.q("UPDATE videos SET status=?, nutra_score=?, content_type=?, analysis=?, subtitles=?, lang=COALESCE(NULLIF(?, ''), lang) WHERE id=?",
             [status, int(a.get("nutra_score") or 0) or None, a.get("content_type"), a2, a.get("subtitles") or [],
              (a.get("language") or "")[:5], c["id"]])
        dm, da = norm_dirs(a.get("direction"), a.get("direction_extra"))
        if dm:
            db.q("UPDATE videos SET direction=?, directions=?, dir_v=? WHERE id=?", [dm, da, DIR_VERSION, c["id"]])
        vh, v1, vs = norm_vis(a.get("host"), a.get("visuals"))
        if vh or v1:
            db.q("UPDATE videos SET host=?, visual=?, visuals=?, vis_v=? WHERE id=?", [vh, v1, vs, VIS_VERSION, c["id"]])
    elif existing and status:
        db.q("UPDATE videos SET status=COALESCE(status, ?) WHERE id=?", [status, c["id"]])
    if a is None:
        db.q("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", [c["id"], ts, c["views"], c.get("likes"), c.get("comments"), c.get("shares")])


def refresh_authors(db, cands):
    ts = NOW.isoformat(timespec="seconds")
    by = {}
    for c in cands:
        if c.get("author_id"):
            by[c["author_id"]] = c
    for aid, c in by.items():
        st = db.q("""SELECT COUNT(*) n, SUM(CASE WHEN nutra_score>=3 THEN 1 ELSE 0 END) rel, MAX(views) mx,
                     MAX(nutra_score) best, MAX(followers) fol FROM videos WHERE author_id=?""", [aid])[0]
        types_ = db.q("SELECT content_type t, COUNT(*) n FROM videos WHERE author_id=? AND content_type IS NOT NULL GROUP BY t ORDER BY n DESC LIMIT 1", [aid])
        langs = db.q("SELECT lang l, COUNT(*) n FROM videos WHERE author_id=? AND lang<>'' GROUP BY l ORDER BY n DESC LIMIT 1", [aid])
        verts = db.q("SELECT DISTINCT vertical v FROM videos WHERE author_id=?", [aid])
        row = [aid, c["platform"], c.get("author"), c.get("author"), c.get("author_url"), st["fol"] or c.get("followers"),
               st["n"], st["rel"] or 0, st["mx"], st["best"], types_[0]["t"] if types_ else None,
               langs[0]["l"] if langs else None, ",".join(v["v"] for v in verts), ts, ts]
        if db.q("SELECT id FROM authors WHERE id=?", [aid]):
            db.q("""UPDATE authors SET followers=?, shots=?, relevant_shots=?, max_views=?, best_score=?, main_type=?, lang=?,
                    verticals=?, last_seen=? WHERE id=?""", row[5:13] + [ts, aid])
        else:
            db.q("INSERT INTO authors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)


# ------------------------------------------------------------------ ВЫВОД
def telegram(text):
    tok = ENV("TELEGRAM_TOKEN")
    if not tok:
        return
    chat = ENV("TELEGRAM_CHAT_ID")
    if not chat:
        try:
            ups = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30).json().get("result", [])
            chat = next((str(u["message"]["chat"]["id"]) for u in reversed(ups) if "message" in u), None)
        except Exception:
            chat = None
        if chat:
            log(f"Telegram chat_id = {chat} (добавьте его в секрет TELEGRAM_CHAT_ID)")
    if not chat:
        log("Telegram: не знаю chat_id — напишите боту любое сообщение и перезапустите"); return
    for i in range(0, len(text), 3900):
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=30,
                      json={"chat_id": chat, "text": text[i:i + 3900], "parse_mode": "HTML", "disable_web_page_preview": True})


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def report(db, new_ids, stats):
    os.makedirs("report", exist_ok=True)
    rows = db.q("SELECT * FROM videos WHERE status='analyzed' ORDER BY nutra_score DESC, views DESC")
    with open("report/videos.csv", "w", newline="", encoding="utf-8-sig") as f:
        cols = ["platform", "vertical", "author", "views", "followers", "outlier", "nutra_score", "content_type", "lang",
                "url", "hook_0_3s", "cta", "cta_mechanic", "why_it_works", "how_to_adapt", "moderation_risk", "first_seen"]
        w = csv.writer(f); w.writerow(cols)
        for r in rows:
            a = json.loads(r["analysis"] or "{}")
            w.writerow([r.get(k) if k in r else a.get(k) for k in cols])
    auth = db.q("SELECT * FROM authors ORDER BY relevant_shots DESC, max_views DESC")
    with open("report/authors.csv", "w", newline="", encoding="utf-8-sig") as f:
        if auth:
            w = csv.DictWriter(f, fieldnames=list(auth[0].keys())); w.writeheader(); w.writerows(auth)
    new = [r for r in rows if r["id"] in new_ids][: S["digest_top"]]
    lines = [f"<b>Радар · {NOW:%d.%m %H:%M} UTC</b>",
             f"Найдено {stats['found']} · новых {stats['new']} · разобрано {stats['analyzed']} · ~${stats['cost']:.3f}"]
    for r in new:
        a = json.loads(r["analysis"] or "{}")
        out = f" · ×{r['outlier']}" if r.get("outlier") else ""
        lines.append(f"\n<b>{r['nutra_score']}/5</b> · {esc(r['platform'])} · @{esc(r['author'])} · {r['views']:,}{out}\n"
                     f"Хук: {esc(a.get('hook_0_3s'))}\nCTA: {esc(a.get('cta'))}\n{esc(r['url'])}")
    top_auth = [a for a in auth if (a.get("relevant_shots") or 0) >= 1][:5]
    if top_auth:
        lines.append("\n<b>Тематические страницы (топ):</b>")
        lines += [f"@{esc(a['handle'])} · {a['platform']} · выстрелов по теме {a['relevant_shots']} · подписчиков {a['followers'] or '?'}"
                  for a in top_auth]
    telegram("\n".join(lines))


# ------------------------------------------------------------------ ПРОФИЛИ (слежка за аккаунтами)
PROFILE_VERTICAL = {
    "name": "Здоровье (профили)",
    "nutra_scale": "5 = тема и подача легко ведут зрителя 45+ к добавке (суставы, похудение, мужское здоровье, энергия, сон, ЖКТ); "
                   "4 = здоровье/долголетие, мост к добавке очевиден; 3 = общий велнес, мост возможен; 2 = здоровье, но мост натянутый; "
                   "1 = не про здоровье или мост невозможен",
}
UA = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def save_thumb(db, vid, src):
    """Скачать обложку по временной ссылке и сохранить уменьшенную копию в базу."""
    import base64, io
    if not src or db.q("SELECT id FROM thumbs WHERE id=?", [vid]):
        return
    try:
        r = requests.get(src, headers=UA, timeout=30)
        r.raise_for_status()
        data, mime = r.content, r.headers.get("content-type", "image/jpeg")
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            im.thumbnail((360, 640))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=72); data, mime = buf.getvalue(), "image/jpeg"
        except Exception:
            pass
        if len(data) <= 70000:
            db.q("INSERT OR REPLACE INTO thumbs VALUES (?,?,?,?)", [vid, mime, base64.b64encode(data).decode(), NOW.isoformat(timespec="seconds")])
    except Exception as e:
        log(f"  обложка {vid}: {str(e)[:100]}")


def _ts(v):
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%Y-%m-%d")
    return str(v)[:10] if v else None


def _ig_item(it, handle=None):
    code = g(it, "shortCode", "shortcode", "code")
    if not code or g(it, "error"):
        return None
    views = g(it, "videoPlayCount", "playCount", "play_count", "plays", "videoViewCount", "viewCount", "view_count", "views") or 0
    return dict(id="ig_" + code, raw_id=code, platform="Instagram",
        url=g(it, "url") or f"https://www.instagram.com/reel/{code}/",
        author=g(it, "ownerUsername", "authorUsername", "username", "owner.username", "user.username") or handle,
        title=g(it, "caption", "text", "caption.text") or "", published=_ts(g(it, "timestamp", "createdAt", "taken_at", "takenAt")),
        duration=g(it, "videoDuration", "durationSeconds", "video_duration"), lang="", views=int(views),
        likes=g(it, "likesCount", "likeCount", "like_count", "likes"), comments=g(it, "commentsCount", "commentCount", "comment_count", "comments_count"),
        shares=g(it, "sharesCount", "reshareCount", "shareCount", "shares_count"),
        author_bio=g(it, "authorBio", "ownerBiography", "biography"), author_ext=g(it, "authorExternalUrl", "ownerExternalUrl", "externalUrl"),
        followers=g(it, "ownerFollowersCount", "authorFollowerCount", "followersCount"), video_url=g(it, "videoUrl", "video_url"),
        thumb=g(it, "displayUrl", "thumbnailUrl", "thumbnail_url", "images.0"))


def _norm_handle(s):
    s = (s or "").strip().lower()
    m_ = re.search(r"instagram\.com/([a-z0-9._]+)", s)
    return (m_.group(1) if m_ else s).lstrip("@").strip("/")


def fetch_instagram(profs, limit, since=None):
    """Один запуск Apify на все профили сразу → {handle_lower: [ролики]}.
    Только сборщик из настроек (ig_actor). Автоматического перехода на дорогой сборщик нет."""
    handles = [p["handle"] for p in profs]
    imp = ENV("RADAR_IMPORT_RUN")
    if imp:   # повторно использовать уже оплаченный запуск Apify (чтение данных бесплатно)
        items = import_run_items(imp)
        actors = []
    else:
        actors = [S.get("ig_actor", "memo23/instagram-reel-scraper")]
        items = []
    for actor in actors:
        inp = IG_ACTOR_INPUT.get(actor, IG_ACTOR_INPUT["scrapesmith/instagram-reels-scraper"])(handles, int(limit))
        if since and not actor.startswith("apify/"):
            inp["onlyPostsNewerThan"] = since
        try:
            items = run_actor(actor, inp, max_items=len(handles) * int(limit))   # предохранитель: не больше заказанного
        except Exception as e:
            log(f"  Instagram: сборщик {actor} упал: {str(e)[:150]}"); items = []
        ok = [it for it in items if _ig_item(it)]
        if ok:
            log(f"  Instagram: сборщик {actor} → {len(ok)} роликов")
            items = ok; break
        log(f"  Instagram: сборщик {actor} ничего не вернул — сбор пропущен (дорогой сборщик автоматически не включается)")
    by = {h.lower(): [] for h in handles}
    for it in items:
        c = _ig_item(it)
        key = _norm_handle(g(it, "inputUrl", "inputPost", "input", "query"))
        if key not in by:
            key = (c["author"] or "").lower()
        if key in by:
            by[key].append(c)
    return by


def _f(o, *keys):
    """Поле объекта Apify (новые версии клиента отдают объекты, старые — словари)."""
    for k in keys:
        v = o.get(k) if isinstance(o, dict) else getattr(o, k, None)
        if v is not None:
            return v
    return None


def import_run_items(run_id):
    """Данные уже оплаченного запуска Apify. run_id='last' — последний успешный запуск официального сборщика с >= 100 роликами."""
    ac = apify()
    if run_id == "last":
        runs = _f(ac.actor("apify/instagram-reel-scraper").runs().list(desc=True, limit=20), "items") or []
        run_id = None
        for r in runs:
            if str(_f(r, "status")).endswith("SUCCEEDED"):
                ds = ac.dataset(_f(r, "defaultDatasetId", "default_dataset_id")).get()
                if (_f(ds, "itemCount", "item_count") or 0) >= 100:
                    run_id = _f(r, "id"); break
        if not run_id:
            log("  Импорт: подходящий запуск не найден"); return []
    run = ac.run(run_id).get()
    items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
    log(f"  Импорт: запуск {run_id} от {_f(run, 'startedAt', 'started_at')} → {len(items)} строк (повторная оплата сбора не нужна)")
    return items


IG_ACTOR_INPUT = {   # как каждый сборщик принимает список профилей
    "apify/instagram-reel-scraper": lambda hs, n: {"username": hs, "resultsLimit": n},
    "scrapesmith/instagram-reels-scraper": lambda hs, n: {"usernames": hs, "resultsLimit": n, "skipPinnedPosts": False},
    "memo23/instagram-reel-scraper": lambda hs, n: {"usernames": hs, "resultsLimit": n, "skipPinnedPosts": False},
    "zaver.api/instagram-reel-scraper": lambda hs, n: {"directUrls": hs, "resultsLimit": n},
}


def ig_test():
    """Проверка сборщиков: 1 профиль, 5 роликов (~$0.01 каждый). RADAR_IGTEST='актор1,актор2'."""
    ac = apify()
    actors = [a.strip() for a in (ENV("RADAR_IGTEST") or "memo23/instagram-reel-scraper,zaver.api/instagram-reel-scraper").split(",") if a.strip()]
    for actor in actors:
        inp = IG_ACTOR_INPUT.get(actor, IG_ACTOR_INPUT["scrapesmith/instagram-reels-scraper"])(["farmtruths"], 5)
        log(f"\n===== Тест {actor} вход {inp}")
        try:
            run = ac.actor(actor).call(run_input=inp)
        except Exception as e:
            log("  запуск не удался:", str(e)[:300]); continue
        items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
        ok = [c for c in (_ig_item(it) for it in items) if c]
        log(f"  статус {_f(run, 'status')} · строк {len(items)} · распознано роликов {len(ok)}")
        if items:
            it = items[0]
            log("  ВСЕ ПОЛЯ первой строки:")
            for k in sorted(it.keys()):
                v = it[k]
                log(f"    {k}: {str(v)[:90]}")
        if ok:
            c = ok[0]
            log("  как понял радар:", {k: (str(c.get(k))[:60] if c.get(k) is not None else None) for k in
                ("id", "views", "likes", "comments", "followers", "published", "duration", "author", "video_url", "thumb")})
        try:
            log("  журнал Apify (хвост):\n" + (ac.run(_f(run, "id")).log().get() or "")[-1200:])
        except Exception as e:
            log("  журнал недоступен:", e)


def med_enrich():
    """Исследование: свежие данные по списку медицинских профилей (research/med_seed.json) → report/med_enriched.json.
    Instagram — официальный сборщик профилей (~$2.3/1000, лимит = число ников), YouTube — бесплатный API."""
    seed = json.load(open("research/med_seed.json"))
    out = {"ig": [], "yt": [], "tt": seed.get("tt", [])}
    handles = [h for h, _ in seed["ig"]]
    ac = apify()
    rid = ENV("RADAR_IMPORT_RUN")
    run = ac.run(rid).get() if rid else ac.actor("apify/instagram-profile-scraper").call(run_input={"usernames": handles}, max_items=len(handles))
    items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
    log(f"Instagram: запрошено {len(handles)}, получено {len(items)}")
    for it in items:
        out["ig"].append({k: it.get(k) for k in ("username", "fullName", "followersCount", "biography", "externalUrl", "verified",
                                                  "private", "postsCount", "businessCategoryName", "isBusinessAccount", "error", "inputUrl")})
    key = ENV("YOUTUBE_KEY"); YT = "https://www.googleapis.com/youtube/v3/"
    for name, src in seed["yt"]:
        try:
            s_ = requests.get(YT + "search", params=dict(key=key, part="snippet", q=name, type="channel", maxResults=3), timeout=60).json()
            ids = [x["id"]["channelId"] for x in s_.get("items", [])]
            if not ids:
                out["yt"].append({"query": name, "found": False}); continue
            ch = requests.get(YT + "channels", params=dict(key=key, part="snippet,statistics", id=",".join(ids)), timeout=60).json().get("items", [])
            ch.sort(key=lambda c: int(c["statistics"].get("subscriberCount", 0) or 0), reverse=True)
            c = ch[0]
            out["yt"].append({"query": name, "found": True, "id": c["id"], "title": c["snippet"]["title"],
                              "handle": c["snippet"].get("customUrl"), "subs": int(c["statistics"].get("subscriberCount", 0) or 0),
                              "country": c["snippet"].get("country"), "lang": c["snippet"].get("defaultLanguage"),
                              "desc": (c["snippet"].get("description") or "")[:400],
                              "alts": [(x["snippet"]["title"], x["snippet"].get("customUrl"), int(x["statistics"].get("subscriberCount", 0) or 0)) for x in ch[1:]]})
        except Exception as e:
            out["yt"].append({"query": name, "error": str(e)[:150]})
    os.makedirs("report", exist_ok=True)
    json.dump(out, open("report/med_enriched.json", "w"), ensure_ascii=False, indent=1)
    log(f"Готово: IG {len(out['ig'])}, YT {len(out['yt'])}")
    print("MEDJSON_BEGIN"); print(json.dumps(out, ensure_ascii=False)); print("MEDJSON_END")


SNOW_EXCLUDE = ("психиатр, психолог, психотерапевт, коуч; пластический/эстетический хирург; педиатр, неонатолог; "
                "врачи из Индии и контент на хинди/индийских языках")

SNOW_PICK = """Это ники и имена Instagram-аккаунтов из блока «похожие» у популярных врачей-блогеров.
Нужны аккаунты врачей и медиков-блогеров (врач, медсестра/медбрат, фармацевт, физиотерапевт, нутрициолог-медик),
а также ИИ-персонажей, которые рассказывают о здоровье. Язык контента: английский, испанский или португальский.
НЕ нужны: {excl}; бренды, клиники-магазины, знаменитости, спортсмены, фитнес, юмор, мода.
Оцени каждого по имени и нику: 3 — почти точно медик-блогер; 2 — вероятно про здоровье; 1 — сомнительно; 0 — явно не то.
Верни JSON-список: [{{"u": "ник", "s": 0-3}}]
Аккаунты:
{items}"""

SNOW_LABEL = """Для каждого Instagram-аккаунта по имени, категории и описанию профиля (био) определи:
"med": true, если автор — медик или пишет о здоровье как специалист (врач, медсестра, фармацевт, физиотерапевт, нутрициолог-медик, хиропрактик) ИЛИ это ИИ-персонаж о здоровье; иначе false;
"type": "Врач" / "Мед.специалист (не врач)" / "ИИ-персонаж" / "Не медик";
"spec": специализация по-русски, коротко (например «кардиолог», «ортопед, суставы», «нутрициолог»);
"lang": язык контента по био: EN / ES / PT / другое;
"country": страна, если понятно, иначе "";
"excluded": true, если это {excl}.
Верни JSON-список: [{{"u": "ник", "med": true, "type": "...", "spec": "...", "lang": "EN", "country": "", "excluded": false}}]
Аккаунты:
{items}"""


def med_snow():
    """Снежный ком по «похожим профилям» Instagram: thenetaji (похожие) → Gemini-отбор по имени →
    официальный сборщик профилей (подписчики, био) → второй круг от новых врачей. Жёсткий потолок — RADAR_BUDGET."""
    budget = float(ENV("RADAR_BUDGET") or 0)
    seed = json.load(open("research/med_snow_seed.json"))
    known = {u.lower() for u in seed["known"]} | {u.lower() for u in seed["removed"]}
    ac = apify()
    st = {"spent": 0.0}
    profiles, votes, names, labels = {}, {}, {}, {}

    def usage(run, est):
        try:
            info = ac.run(_f(run, "id")).get()
            v = _f(info, "usageTotalUsd", "usage_total_usd")
            st["spent"] += float(v) if v is not None else est
        except Exception:
            st["spent"] += est

    def related(seeds, per):
        est = len(seeds) * per * 0.0025 + 0.03
        if st["spent"] + est > budget:
            log(f"  похожие: не хватает бюджета (нужно ~${est:.2f}, осталось ${budget - st['spent']:.2f})"); return 0
        run = ac.actor("thenetaji/instagram-related-user-scraper").call(
            run_input={"username": seeds, "maxItem": per, "chainDepth": 0}, max_items=len(seeds) * per)
        items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
        usage(run, est)
        n = 0
        for it in items:
            u = (it.get("username") or "").lower()
            if u and u not in known and u not in profiles:
                votes[u] = votes.get(u, 0) + 1
                names[u] = it.get("full_name") or ""
                n += 1
        log(f"  похожие: {len(seeds)} источников × {per} → {len(items)} записей, новых кандидатов {len(votes)}, потрачено ${st['spent']:.2f}")
        return n

    def pick_and_scrape(reserve):
        cands = [u for u in votes if u not in profiles]
        score = {}
        for i in range(0, len(cands), 150):
            chunk = cands[i:i + 150]
            data = ask([SNOW_PICK.format(excl=SNOW_EXCLUDE, items="\n".join(f"{u} | {names.get(u, '')}" for u in chunk))], "отбор")
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            for d in data or []:
                if isinstance(d, dict) and d.get("u"):
                    try:
                        score[str(d["u"]).lower()] = int(d.get("s") or 0)
                    except Exception:
                        pass
        pick = [u for u in cands if score.get(u, 0) >= 2]
        pick.sort(key=lambda u: (score.get(u, 0), votes.get(u, 0)), reverse=True)
        n_max = int((budget - st["spent"] - reserve) / 0.0026)
        log(f"  отбор по имени: из {len(cands)} подходят {len(pick)}, проверяем {min(len(pick), max(0, n_max))}")
        pick = pick[:max(0, n_max)]
        got = []
        for i in range(0, len(pick), 500):
            part = pick[i:i + 500]
            est = len(part) * 0.0026
            if st["spent"] + est > budget:
                break
            run = ac.actor("apify/instagram-profile-scraper").call(run_input={"usernames": part}, max_items=len(part))
            items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
            usage(run, est)
            for it in items:
                u = (it.get("username") or "").lower()
                if u:
                    profiles[u] = it; got.append(it)
            for u in part:
                profiles.setdefault(u, {"username": u, "error": "not_returned"})
            log(f"  профили: {len(items)}/{len(part)}, потрачено ${st['spent']:.2f}")
        return got

    def label(items):
        items = [p for p in items if (p.get("followersCount") or 0) >= 300000 and p["username"].lower() not in labels]
        for i in range(0, len(items), 40):
            chunk = items[i:i + 40]
            txt = "\n".join(f'{p["username"]} | {p.get("fullName") or ""} | {p.get("businessCategoryName") or ""} | '
                            f'{(p.get("biography") or "").replace(chr(10), " ")[:220]}' for p in chunk)
            data = ask([SNOW_LABEL.format(excl=SNOW_EXCLUDE, items=txt)], "разметка")
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            for d in data or []:
                if isinstance(d, dict) and d.get("u"):
                    labels[str(d["u"]).lower()] = d

    def good(u):
        l, p = labels.get(u, {}), profiles.get(u, {})
        return l.get("med") and not l.get("excluded") and l.get("lang") in ("EN", "ES", "PT") and (p.get("followersCount") or 0) >= 500000

    # круг 1: похожие у 65 врачей из списка
    log(f"Бюджет ${budget:.2f}")
    if budget <= 0:
        log("Бюджет 0 — ничего не запускаю"); return
    per1 = max(10, min(40, int((budget * 0.55 - 0.03) / 0.0025 / max(1, len(seed["seed"])))))
    related(seed["seed"], per1)
    label(pick_and_scrape(reserve=budget * 0.3))
    # круг 2: похожие у новых врачей ≥ 500k
    new_docs = [u for u in labels if good(u)]
    left = budget - st["spent"]
    if new_docs and left > 1:
        per2 = max(10, min(40, int((left * 0.55 - 0.03) / 0.0025 / len(new_docs))))
        log(f"Круг 2: новых врачей ≥500k {len(new_docs)}, по {per2} похожих")
        if related(new_docs, per2):
            label(pick_and_scrape(reserve=0.1))
    out = []
    for u, p in profiles.items():
        if u in known or (p.get("followersCount") or 0) < 300000:
            continue
        l = labels.get(u, {})
        out.append({"u": p.get("username"), "name": p.get("fullName"), "fol": p.get("followersCount"), "ver": p.get("verified"),
                    "cat": p.get("businessCategoryName"), "url": p.get("externalUrl"),
                    "bio": (p.get("biography") or "").replace("\n", " · ")[:220], "votes": votes.get(u, 0),
                    **{k: l.get(k) for k in ("med", "type", "spec", "lang", "country", "excluded")}})
    log(f"Готово: проверено профилей {len(profiles)}, новых ≥300k {len(out)}, подходящих медиков ≥500k "
        f"{sum(1 for x in out if good(x['u'].lower()))}, потрачено Apify ${st['spent']:.2f}")
    print("SNOWJSON_BEGIN")
    for x in out:
        print(json.dumps(x, ensure_ascii=True))
    print("SNOWJSON_END")


NAT_PICK = """Это ники и имена Instagram-аккаунтов из блока «похожие» у популярного натуропата.
Нужны аккаунты о натуральной/альтернативной медицине и здоровье: натуропаты, травники, холистика, народные рецепты,
аюрведа/китайская медицина, функциональная медицина, врачи с натуральным подходом, паблики «remedies/remedios/remédios»,
ИИ-персонажи о здоровье (бабушки, фермеры, монахи, «доктора»). Язык: английский, испанский или португальский.
НЕ нужны: Индия и хинди; бренды-магазины; знаменитости; спорт и фитнес без здоровья; юмор; мода; религия без здоровья.
Оцени по имени и нику: 3 — почти точно в нише; 2 — вероятно про здоровье; 1 — сомнительно; 0 — явно не то.
Верни JSON-список: [{{"u": "ник", "s": 0-3}}]
Аккаунты:
{items}"""

NAT_LABEL = """Для каждого Instagram-аккаунта по имени, категории и описанию (био) определи:
"fit": true, если аккаунт о натуральной/альтернативной медицине или здоровье (натуропат, травник, холистика, народные рецепты,
функциональная медицина, врач с натуральным подходом, паблик о здоровье, ИИ-персонаж о здоровье); иначе false;
"type": "Натуропат" / "Травник" / "Врач" / "Практик (не врач)" / "Паблик" / "ИИ-персонаж" / "Не в нише";
"spec": направленность по-русски коротко; "lang": EN / ES / PT / другое; "country": страна или "";
"excluded": true, если Индия/хинди.
Верни JSON-список: [{{"u": "ник", "fit": true, "type": "...", "spec": "...", "lang": "EN", "country": "", "excluded": false}}]
Аккаунты:
{items}"""


def lookalike():
    """Похожие на заданные аккаунты (RADAR_IGTEST — ники через запятую): похожие → отбор по имени → проверка профиля.
    Жёсткий потолок — RADAR_BUDGET."""
    budget = float(ENV("RADAR_BUDGET") or 0)
    seeds = [s.strip().lstrip("@").lower() for s in (ENV("RADAR_IGTEST") or "").split(",") if s.strip()]
    per = int(ENV("RADAR_PER") or 80)
    if budget <= 0 or not seeds:
        log("Нет бюджета или семян — ничего не запускаю"); return
    ac = apify(); spent = 0.0

    def cost(run, est):
        try:
            v = _f(ac.run(_f(run, "id")).get(), "usageTotalUsd", "usage_total_usd")
            return float(v) if v is not None else est
        except Exception:
            return est

    est = len(seeds) * per * 0.0032 + 0.03
    if est > budget:
        per = max(10, int((budget * 0.6 - 0.03) / 0.0032 / len(seeds)))
        est = len(seeds) * per * 0.0032 + 0.03
    run = ac.actor("thenetaji/instagram-related-user-scraper").call(
        run_input={"username": seeds, "maxItem": per, "chainDepth": 0}, max_items=len(seeds) * per)
    rel = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
    spent += cost(run, est)
    votes, names = {}, {}
    for it in rel:
        u = (it.get("username") or "").lower()
        if u and u not in seeds:
            votes[u] = votes.get(u, 0) + 1; names[u] = it.get("full_name") or ""
    log(f"Похожие: {len(rel)} записей, уникальных {len(votes)}, потрачено ${spent:.2f}")
    score = {}
    cands = list(votes)
    for i in range(0, len(cands), 150):
        data = ask([NAT_PICK.format(items="\n".join(f"{u} | {names[u]}" for u in cands[i:i + 150]))], "отбор")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for d in data or []:
            if isinstance(d, dict) and d.get("u"):
                try:
                    score[str(d["u"]).lower()] = int(d.get("s") or 0)
                except Exception:
                    pass
    pick = [u for u in cands if score.get(u, 0) >= 2]
    pick.sort(key=lambda u: (score.get(u, 0), votes[u]), reverse=True)
    n_max = int((budget - spent - 0.05) / 0.0026)
    pick = (seeds + pick)[:max(0, n_max)]
    log(f"Отбор: из {len(cands)} подходят {len([u for u in cands if score.get(u, 0) >= 2])}, проверяем {len(pick)}")
    profs = []
    if pick:
        run = ac.actor("apify/instagram-profile-scraper").call(run_input={"usernames": pick}, max_items=len(pick))
        profs = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
        spent += cost(run, len(pick) * 0.0026)
    lab = {}
    for i in range(0, len(profs), 40):
        txt = "\n".join(f'{p.get("username")} | {p.get("fullName") or ""} | {p.get("businessCategoryName") or ""} | '
                        f'{(p.get("biography") or "").replace(chr(10), " ")[:220]}' for p in profs[i:i + 40])
        data = ask([NAT_LABEL.format(items=txt)], "разметка")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for d in data or []:
            if isinstance(d, dict) and d.get("u"):
                lab[str(d["u"]).lower()] = d
    log(f"Готово: проверено {len(profs)}, потрачено Apify ${spent:.2f}")
    print("SNOWJSON_BEGIN")
    for p in profs:
        u = (p.get("username") or "").lower(); l = lab.get(u, {})
        print(json.dumps({"u": p.get("username"), "name": p.get("fullName"), "fol": p.get("followersCount"), "ver": p.get("verified"),
                          "cat": p.get("businessCategoryName"), "url": p.get("externalUrl"),
                          "bio": (p.get("biography") or "").replace("\n", " · ")[:220], "votes": votes.get(u, 0), "seed": u in seeds,
                          **{k: l.get(k) for k in ("fit", "type", "spec", "lang", "country", "excluded")}}, ensure_ascii=True))
    print("SNOWJSON_END")


def nat_snow():
    """Натуропаты: проверка известных имён + похожие от семян из разных кластеров + второй круг (приоритет ES/PT).
    Жёсткий потолок трат — RADAR_BUDGET (по факту из Apify)."""
    budget = float(ENV("RADAR_BUDGET") or 0)
    cfg = json.load(open("research/nat_seed.json"))
    have = set(cfg["have"])
    if budget <= 0:
        log("Бюджет 0 — ничего не запускаю"); return
    ac = apify(); st = {"spent": 0.0}
    profs, labels, votes, names = {}, {}, {}, {}

    def cost(run, est):
        try:
            v = _f(ac.run(_f(run, "id")).get(), "usageTotalUsd", "usage_total_usd")
            st["spent"] += float(v) if v is not None else est
        except Exception:
            st["spent"] += est

    def check(users):
        users = [u for u in dict.fromkeys(users) if u not in profs and u not in have]
        n_max = int((budget - st["spent"] - 0.05) / 0.0026)
        users = users[:max(0, n_max)]
        for i in range(0, len(users), 500):
            part = users[i:i + 500]
            run = ac.actor("apify/instagram-profile-scraper").call(run_input={"usernames": part}, max_items=len(part))
            for it in ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items():
                u = (it.get("username") or "").lower()
                if u:
                    profs[u] = it
            cost(run, len(part) * 0.0026)
            log(f"  профили: {len(part)} проверено, потрачено ${st['spent']:.2f}")
        label([profs[u] for u in users if u in profs])

    def label(items):
        items = [p for p in items if (p.get("followersCount") or 0) >= 50000]
        for i in range(0, len(items), 40):
            txt = "\n".join(f'{p.get("username")} | {p.get("fullName") or ""} | {p.get("businessCategoryName") or ""} | '
                            f'{(p.get("biography") or "").replace(chr(10), " ")[:220]}' for p in items[i:i + 40])
            data = ask([NAT_LABEL.format(items=txt)], "разметка")
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            for d in data or []:
                if isinstance(d, dict) and d.get("u"):
                    labels[str(d["u"]).lower()] = d

    def related(seeds, per):
        est = len(seeds) * per * 0.0032 + 0.03
        if st["spent"] + est > budget:
            log(f"  похожие: не хватает (нужно ~${est:.2f}, осталось ${budget - st['spent']:.2f})"); return []
        run = ac.actor("thenetaji/instagram-related-user-scraper").call(
            run_input={"username": seeds, "maxItem": per, "chainDepth": 0}, max_items=len(seeds) * per)
        rel = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
        cost(run, est)
        for it in rel:
            u = (it.get("username") or "").lower()
            if u and u not in have and u not in profs:
                votes[u] = votes.get(u, 0) + 1; names[u] = it.get("full_name") or ""
        log(f"  похожие: {len(seeds)} семян × {per} → {len(rel)}, кандидатов {len(votes)}, потрачено ${st['spent']:.2f}")
        cands = [u for u in votes if u not in profs]
        score = {}
        for i in range(0, len(cands), 150):
            data = ask([NAT_PICK.format(items="\n".join(f"{u} | {names[u]}" for u in cands[i:i + 150]))], "отбор")
            if isinstance(data, dict):
                data = next((v for v in data.values() if isinstance(v, list)), [])
            for d in data or []:
                if isinstance(d, dict) and d.get("u"):
                    try:
                        score[str(d["u"]).lower()] = int(d.get("s") or 0)
                    except Exception:
                        pass
        pick = [u for u in cands if score.get(u, 0) >= 2]
        pick.sort(key=lambda u: (score.get(u, 0), votes[u]), reverse=True)
        log(f"  отбор по имени: из {len(cands)} подходят {len(pick)}")
        return pick

    def good(u):
        l, p = labels.get(u, {}), profs.get(u, {})
        return l.get("fit") and not l.get("excluded") and l.get("lang") in ("EN", "ES", "PT") and (p.get("followersCount") or 0) >= 100000

    log(f"Бюджет ${budget:.2f}")
    log("Шаг 1: известные имена"); check(cfg["names"])
    log("Шаг 2: похожие от семян"); check(related(cfg["seeds"], 50))
    new = [u for u in profs if good(u) and (profs[u].get("followersCount") or 0) >= 300000]
    new.sort(key=lambda u: (labels[u].get("lang") in ("ES", "PT"), profs[u].get("followersCount") or 0), reverse=True)
    left = budget - st["spent"]
    if new and left > 1:
        seeds2 = new[:25]
        per2 = max(10, min(40, int((left * 0.6 - 0.03) / 0.0032 / len(seeds2))))
        log(f"Шаг 3: второй круг от {len(seeds2)} новых (ES/PT вперёд), по {per2}")
        check(related(seeds2, per2))
    out = [u for u in profs if (profs[u].get("followersCount") or 0) >= 50000]
    log(f"Готово: проверено {len(profs)}, подходящих ≥100k {sum(1 for u in profs if good(u))}, потрачено Apify ${st['spent']:.2f}")
    print("SNOWJSON_BEGIN")
    for u in out:
        p, l = profs[u], labels.get(u, {})
        print(json.dumps({"u": p.get("username"), "name": p.get("fullName"), "fol": p.get("followersCount"), "ver": p.get("verified"),
                          "cat": p.get("businessCategoryName"), "url": p.get("externalUrl"),
                          "bio": (p.get("biography") or "").replace("\n", " · ")[:220], "votes": votes.get(u, 0),
                          "known": u in cfg["names"], **{k: l.get(k) for k in ("fit", "type", "spec", "lang", "country", "excluded")}},
                         ensure_ascii=True))
    print("SNOWJSON_END")


def nat_hash():
    """Натуропаты по хештегам: посты → авторы (без уже известных) → отбор → проверка профилей. Потолок — RADAR_BUDGET."""
    budget = float(ENV("RADAR_BUDGET") or 0)
    cfg = json.load(open("research/nat_hash.json")); have = set(cfg["have"])
    if budget <= 0:
        log("Бюджет 0 — ничего не запускаю"); return
    ac = apify(); spent = 0.0

    def cost(run, est):
        try:
            v = _f(ac.run(_f(run, "id")).get(), "usageTotalUsd", "usage_total_usd")
            return float(v) if v is not None else est
        except Exception:
            return est

    per = max(10, min(50, int(budget * 0.55 / 0.0023 / len(cfg["tags"]))))
    run = ac.actor("apify/instagram-hashtag-scraper").call(
        run_input={"hashtags": cfg["tags"], "resultsLimit": per, "resultsType": "posts"}, max_items=per * len(cfg["tags"]))
    posts = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
    spent += cost(run, len(posts) * 0.0023)
    auth = {}
    for p in posts:
        u = (p.get("ownerUsername") or "").lower()
        if not u or u in have:
            continue
        a = auth.setdefault(u, {"n": 0, "likes": 0, "views": 0, "name": p.get("ownerFullName") or "", "tags": set()})
        a["n"] += 1; a["likes"] = max(a["likes"], p.get("likesCount") or 0)
        a["views"] = max(a["views"], p.get("videoPlayCount") or p.get("videoViewCount") or 0)
        a["tags"].add(str(p.get("inputUrl") or p.get("hashtag") or "")[-30:])
    log(f"Хештеги: {len(cfg['tags'])} × {per} → постов {len(posts)}, новых авторов {len(auth)}, потрачено ${spent:.2f}")
    score = {}
    cands = list(auth)
    for i in range(0, len(cands), 150):
        data = ask([NAT_PICK.format(items="\n".join(f"{u} | {auth[u]['name']}" for u in cands[i:i + 150]))], "отбор")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for d in data or []:
            if isinstance(d, dict) and d.get("u"):
                try:
                    score[str(d["u"]).lower()] = int(d.get("s") or 0)
                except Exception:
                    pass
    pick = [u for u in cands if score.get(u, 0) >= 2]
    pick.sort(key=lambda u: (auth[u]["likes"] + auth[u]["views"] / 20), reverse=True)
    n_max = int((budget - spent - 0.05) / 0.0026)
    log(f"Отбор: из {len(cands)} подходят {len(pick)}, проверяем {min(len(pick), max(0, n_max))}")
    pick = pick[:max(0, n_max)]
    profs = []
    if pick:
        run = ac.actor("apify/instagram-profile-scraper").call(run_input={"usernames": pick}, max_items=len(pick))
        profs = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
        spent += cost(run, len(pick) * 0.0026)
    lab = {}
    big = [p for p in profs if (p.get("followersCount") or 0) >= 50000]
    for i in range(0, len(big), 40):
        txt = "\n".join(f'{p.get("username")} | {p.get("fullName") or ""} | {p.get("businessCategoryName") or ""} | '
                        f'{(p.get("biography") or "").replace(chr(10), " ")[:220]}' for p in big[i:i + 40])
        data = ask([NAT_LABEL.format(items=txt)], "разметка")
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        for d in data or []:
            if isinstance(d, dict) and d.get("u"):
                lab[str(d["u"]).lower()] = d
    log(f"Готово: проверено {len(profs)}, ≥50k {len(big)}, потрачено Apify ${spent:.2f}")
    print("SNOWJSON_BEGIN")
    for p in big:
        u = (p.get("username") or "").lower(); l = lab.get(u, {}); a = auth.get(u, {})
        print(json.dumps({"u": p.get("username"), "name": p.get("fullName"), "fol": p.get("followersCount"), "ver": p.get("verified"),
                          "bio": (p.get("biography") or "").replace("\n", " · ")[:220], "posts": a.get("n"), "likes": a.get("likes"),
                          **{k: l.get(k) for k in ("fit", "type", "spec", "lang", "country", "excluded")}}, ensure_ascii=True))
    print("SNOWJSON_END")


def enrich_from_runs(db):
    """Бесплатно: перечитать уже оплаченные запуски дешёвого сборщика рилсов и дописать репосты, лайки, комментарии,
    шапку и ссылку из шапки авторов (раньше не сохранялись)."""
    ac = apify()
    runs = _f(ac.actor(S.get("ig_actor", "memo23/instagram-reel-scraper")).runs().list(desc=True, limit=30), "items") or []
    items = []
    for r in runs:
        if not str(_f(r, "status")).endswith("SUCCEEDED"):
            continue
        try:
            items += list(ac.dataset(_f(r, "defaultDatasetId", "default_dataset_id")).iterate_items())
        except Exception as e:
            log("  набор данных недоступен:", str(e)[:80])
    log(f"Записей из прошлых запусков: {len(items)} (запусков {len(runs)})")
    vids, prof = {}, {}
    for it in items:
        c = _ig_item(it)
        if not c:
            continue
        vids[c["id"]] = c
        a = (c.get("author") or "").lower()
        if a and (c.get("author_bio") or c.get("author_ext")):
            prof["ig:" + a] = (c.get("author_bio"), c.get("author_ext"))
    have = {r["id"] for r in db.q("SELECT id FROM videos WHERE platform='Instagram'")}
    ch_all = [c for k, c in vids.items() if k in have]
    for i in range(0, len(ch_all), 100):
        ch = ch_all[i:i + 100]
        cs = lambda k: " ".join(f"WHEN {lit(c['id'])} THEN {lit(c.get(k))}" for c in ch)
        db.q(f"UPDATE videos SET shares = COALESCE(CASE id {cs('shares')} END, shares), likes = COALESCE(CASE id {cs('likes')} END, likes), "
             f"comments = COALESCE(CASE id {cs('comments')} END, comments) WHERE id IN ({','.join(lit(c['id']) for c in ch)})")
    for pid, (bio, ext) in prof.items():
        db.q("UPDATE profiles SET bio=COALESCE(?, bio), ext_url=COALESCE(?, ext_url) WHERE id=?", [bio, ext, pid])
    log(f"Готово: роликов обновлено {len(ch_all)}, с репостами {sum(1 for c in ch_all if c.get('shares') is not None)}, "
        f"профилей с шапкой {len(prof)}")


FUNNEL = """Это текст страницы, на которую ведёт ссылка из шапки Instagram-аккаунта о здоровье. Определи воронку монетизации.
Верни JSON: {{"type": "мультиссылка / квиз / видео-продажник (VSL) / лендинг продукта / магазин / Amazon / WhatsApp-Telegram / книга / курс-консультация / подписка-рассылка / другое",
"offer": "что продают или предлагают, по-русски, коротко", "product": "название продукта/бренда или нет",
"price": "цена, если видна, иначе нет", "network": "ClickBank / Digistore24 / BuyGoods / MaxWeb / Amazon / собственный магазин / нет данных",
"lead_magnet": "что дают бесплатно (книга, гайд, квиз) или нет", "geo_lang": "язык и гео страницы",
"claims": "главные обещания продукта, коротко", "summary": "1–2 предложения: как этот аккаунт зарабатывает"}}
Адрес: {url}
Ссылки на странице: {links}
Текст страницы:
{text}"""

LINKHUBS = ("linktr.ee", "beacons.ai", "stan.store", "lnk.bio", "bio.site", "campsite.bio", "linkin.bio", "taplink", "hoo.be",
            "flowcode", "solo.to", "msha.ke", "linkbio", "carrd.co", "bit.ly", "tap.bio", "koji.to")


def _page(url):
    ua = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
          "accept-language": "en-US,en;q=0.9,es;q=0.8,pt;q=0.7"}
    r = requests.get(url, headers=ua, timeout=25, allow_redirects=True)
    html = r.text[:600000]
    links = re.findall(r'href="(https?://[^"#]+)"', html)
    txt = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return r.status_code, r.url, txt, links


def run_funnels(db):
    """Воронки: страница по ссылке из шапки (и первая «денежная» ссылка мультиссылки) → Gemini → таблица funnels. Бесплатно."""
    rows = db.q("SELECT id, ext_url FROM profiles WHERE ext_url IS NOT NULL AND ext_url != ''")
    done = {r["url"] for r in db.q("SELECT url FROM funnels WHERE status='ok'")}
    log(f"Воронки: профилей со ссылкой {len(rows)}, уже разобрано {len(done)}")
    for r in rows:
        url = r["ext_url"]
        if url in done:
            continue
        try:
            st, final, txt, links = _page(url)
            hops = [final]
            if any(h in final for h in LINKHUBS) or len(txt) < 400:
                own = [l for l in links if not re.search(r"instagram|facebook|tiktok|youtube|twitter|x\.com|linktr|beacons|"
                                                          r"stan\.store|apple\.com|google\.com|cdn|fonts|\.(png|jpg|css|js)", l)]
                if own:
                    st2, final2, txt2, links2 = _page(own[0])
                    hops.append(final2)
                    txt = txt[:3000] + " || СЛЕДУЮЩАЯ СТРАНИЦА: " + txt2
                    links = links + links2
            if st >= 400 and len(txt) < 200:
                raise RuntimeError(f"HTTP {st}")
            doms = sorted({re.sub(r"^https?://(www\.)?", "", l).split("/")[0] for l in links})[:40]
            data = ask([FUNNEL.format(url=" → ".join(hops), links=", ".join(doms), text=txt[:12000])], "воронка " + r["id"])
            if isinstance(data, list):
                data = next((x for x in data if isinstance(x, dict)), None)
            db.q("INSERT OR REPLACE INTO funnels VALUES (?,?,?,?,?,?)",
                 [url, r["id"], " → ".join(hops), "ok" if data else "не разобрано", json.dumps(data or {}, ensure_ascii=False),
                  NOW.isoformat(timespec="seconds")])
            log(f"  {r['id']}: {(data or {}).get('type')} — {(data or {}).get('offer')}")
        except Exception as e:
            db.q("INSERT OR REPLACE INTO funnels VALUES (?,?,?,?,?,?)",
                 [url, r["id"], url, "не открылось: " + str(e)[:80], "{}", NOW.isoformat(timespec="seconds")])
            log(f"  {r['id']}: не открылось ({str(e)[:80]})")


def cmp_models(db):
    """Тест моделей: 20 уже разобранных выстрелов Instagram → свежие ссылки на видео (официальный сборщик) →
    один и тот же разбор тремя моделями. Пишет только в лог, базу не трогает."""
    models = [m.strip() for m in (ENV("RADAR_IGTEST") or "gemini-2.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash-lite").split(",")]
    rows = db.q("""SELECT id, url, author, duration, direction, views FROM videos
                   WHERE platform='Instagram' AND status='analyzed' AND url IS NOT NULL ORDER BY views DESC LIMIT 200""")
    seen, pick = set(), []
    for r in rows:                       # разнообразие: по 1–2 на направление и автора
        k = (r.get("direction"), r.get("author"))
        if k in seen:
            continue
        seen.add(k); pick.append(r)
        if len(pick) >= 20:
            break
    ac = apify()
    run = ac.actor("apify/instagram-scraper").call(
        run_input={"directUrls": [r["url"] for r in pick], "resultsType": "posts", "resultsLimit": 1}, max_items=len(pick))
    items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
    try:
        cost = _f(ac.run(_f(run, "id")).get(), "usageTotalUsd", "usage_total_usd")
    except Exception:
        cost = None
    vids = {}
    for it in items:
        sc = it.get("shortCode") or it.get("shortcode")
        if sc and it.get("videoUrl"):
            vids["ig_" + sc] = it["videoUrl"]
    log(f"Ссылки на видео: {len(vids)}/{len(pick)}, Apify ${cost}")
    gemini()
    out = []
    for r in pick:
        if r["id"] not in vids:
            continue
        vb = fetch_bytes(vids[r["id"]])
        if not vb:
            continue
        c = {"id": r["id"], "platform": "Instagram", "vertical": "profile", "duration": r.get("duration"), "raw_id": r["id"][3:]}
        res = {"id": r["id"], "url": r["url"], "author": r["author"], "views": r["views"], "dur": r.get("duration"), "m": {}}
        for mdl in models:
            GEM["model"], GEM["fallback"] = mdl, []
            t0, i0, o0 = time.time(), GEM["tin"], GEM["tout"]
            try:
                a = analyze(c, vb)
            except Exception as e:
                a = {"error": str(e)[:200]}
            res["m"][mdl] = {"a": a, "tin": GEM["tin"] - i0, "tout": GEM["tout"] - o0, "sec": round(time.time() - t0, 1)}
            log(f"  {r['id']} {mdl}: вход {GEM['tin'] - i0}, выход {GEM['tout'] - o0}, {'ок' if isinstance(a, dict) and 'error' not in a else 'сбой'}")
            time.sleep(4)
        out.append(res)
    print("CMPJSON_BEGIN"); print(json.dumps(out, ensure_ascii=True)); print("CMPJSON_END")


def fetch_tiktok(profs, limit):
    items = run_actor(CFG.get("tiktok_download_actor", "clockworks/tiktok-scraper"),
                      {"profiles": [p["handle"] for p in profs], "resultsPerPage": int(limit),
                       "profileSorting": "latest", "shouldDownloadVideos": False, "shouldDownloadCovers": False})
    by = {p["handle"].lower(): [] for p in profs}
    for it in items:
        vid = str(g(it, "id") or "")
        user = (g(it, "authorMeta.name") or "")
        if not vid.isdigit() or user.lower() not in by:
            continue
        by[user.lower()].append(dict(id="tt_" + vid, raw_id=vid, platform="TikTok",
            url=g(it, "webVideoUrl") or f"https://www.tiktok.com/@{user}/video/{vid}", author=user, title=g(it, "text") or "",
            published=_ts(g(it, "createTime", "createTimeISO")), duration=g(it, "videoMeta.duration"), lang="",
            views=int(g(it, "playCount") or 0), likes=g(it, "diggCount"), comments=g(it, "commentCount"),
            shares=g(it, "shareCount"), followers=g(it, "authorMeta.fans"), thumb=g(it, "videoMeta.coverUrl", "covers.0")))
    return by


def fetch_youtube(p, limit, since):
    key = ENV("YOUTUBE_KEY")
    YT = "https://www.googleapis.com/youtube/v3/"
    h = p["handle"]
    q = dict(id=h) if (h.startswith("UC") and len(h) == 24) else dict(forHandle="@" + h.lstrip("@"))
    r = requests.get(YT + "channels", params=dict(key=key, part="snippet,statistics,contentDetails", **q), timeout=60).json()
    if not r.get("items") and "forHandle" in q:
        s_ = requests.get(YT + "search", params=dict(key=key, part="snippet", q=h, type="channel", maxResults=1), timeout=60).json()
        cid = (s_.get("items") or [{}])[0].get("id", {}).get("channelId")
        if cid:
            r = requests.get(YT + "channels", params=dict(key=key, part="snippet,statistics,contentDetails", id=cid), timeout=60).json()
    if not r.get("items"):
        return []
    ch = r["items"][0]
    p["name"] = ch["snippet"]["title"]
    subs = int(ch["statistics"].get("subscriberCount", 0) or 0)
    up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, tok = [], None
    while len(ids) < limit:
        pl = requests.get(YT + "playlistItems", params=dict(key=key, part="contentDetails", playlistId=up, maxResults=50,
                          **({"pageToken": tok} if tok else {})), timeout=60).json()
        its = pl.get("items", [])
        ids += [x["contentDetails"]["videoId"] for x in its if (x["contentDetails"].get("videoPublishedAt") or "9")[:10] >= since]
        tok = pl.get("nextPageToken")
        if not tok or not its or (its[-1]["contentDetails"].get("videoPublishedAt") or "9")[:10] < since:
            break
    out = []
    for i in range(0, len(ids), 50):
        vr = requests.get(YT + "videos", params=dict(key=key, part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50])), timeout=60).json()
        for v in vr.get("items", []):
            d = iso_dur(v["contentDetails"]["duration"])
            if d > S["max_duration_sec"]:
                continue
            st, sn = v.get("statistics", {}), v["snippet"]
            out.append(dict(id="yt_" + v["id"], raw_id=v["id"], platform="YouTube", url=f"https://www.youtube.com/shorts/{v['id']}",
                author=sn["channelTitle"], title=(sn.get("title") or "") + " " + (sn.get("description") or "")[:300],
                published=sn["publishedAt"][:10], duration=d, lang=(sn.get("defaultAudioLanguage") or "").lower(),
                views=int(st.get("viewCount", 0) or 0), likes=int(st.get("likeCount", 0) or 0),
                comments=int(st.get("commentCount", 0) or 0), shares=None, followers=subs,
                thumb=f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"))
    return out[:limit]


def lit(v):
    """Значение для SQL-литерала (пакетная запись в D1 без лимита на 100 параметров)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\x00", "").replace("'", "''") + "'"


def bulk_save(db, cands, known):
    """Новые ролики — пакетный INSERT, известные — пакетное обновление просмотров + снимки."""
    ts = NOW.isoformat(timespec="seconds")
    cols = ["id", "platform", "vertical", "url", "author", "author_id", "title", "published", "duration", "lang", "keyword",
            "views", "likes", "comments", "shares", "followers", "outlier", "status", "first_seen", "updated", "source"]
    new = [c for c in cands if c["id"] not in known]
    for i in range(0, len(new), 40):
        rows = []
        for c in new[i:i + 40]:
            f = c.get("followers")
            vals = [c["id"], c["platform"], c["vertical"], c.get("url"), c.get("author"), c.get("author_id"), (c.get("title") or "")[:500],
                    c.get("published"), c.get("duration"), c.get("lang"), c.get("keyword"), c["views"], c.get("likes"),
                    c.get("comments"), c.get("shares"), f, round(c["views"] / f, 1) if f else None, "profile_rest", ts, ts, "profile"]
            rows.append("(" + ",".join(lit(x) for x in vals) + ")")
        db.q(f"INSERT OR IGNORE INTO videos ({','.join(cols)}) VALUES " + ",".join(rows))
    old = [c for c in cands if c["id"] in known]
    for i in range(0, len(old), 150):
        ch = old[i:i + 150]
        cs = lambda k: " ".join(f"WHEN {lit(c['id'])} THEN {lit(c.get(k))}" for c in ch)
        db.q(f"UPDATE videos SET views = CASE id {cs('views')} END, likes = COALESCE(CASE id {cs('likes')} END, likes), "
             f"comments = COALESCE(CASE id {cs('comments')} END, comments), shares = COALESCE(CASE id {cs('shares')} END, shares), "
             f"updated={lit(ts)} WHERE id IN ({','.join(lit(c['id']) for c in ch)})")
    for i in range(0, len(cands), 150):
        ch = cands[i:i + 150]
        db.q("INSERT INTO snapshots VALUES " + ",".join(
            "(" + ",".join(lit(x) for x in [c["id"], ts, c["views"], c.get("likes"), c.get("comments"), c.get("shares")]) + ")" for c in ch))


def rolling_median(rows, window, young_days):
    """rows: [{id, views, published}] → {id: (median соседей, возраст в днях)}. Соседи — ±window роликов по дате, без «молодых»."""
    def age(r):
        try:
            return (NOW.date() - dt.date.fromisoformat((r.get("published") or "")[:10])).days
        except Exception:
            return 999
    rows = sorted(rows, key=lambda r: ((r.get("published") or "0000"), r["id"]))
    mature = [i for i, r in enumerate(rows) if age(r) >= young_days]
    out = {}
    for i, r in enumerate(rows):
        neigh = [rows[j]["views"] for j in mature if j != i and abs(j - i) <= window]
        if len(neigh) < 3:   # мало соседей — берём ближайшие зрелые по порядку
            neigh = [rows[j]["views"] for j in sorted(mature, key=lambda j: abs(j - i)) if j != i][:2 * window]
        neigh.sort()
        med = neigh[len(neigh) // 2] if neigh else None
        out[r["id"]] = (med, age(r))
    return out


def fetch_bytes(url):
    try:
        r = requests.get(url, headers=UA, timeout=120)
        if r.ok and len(r.content) > 50_000:
            return r.content
    except Exception:
        pass
    return None


PROFILE_URL = {"Instagram": "https://www.instagram.com/{h}/", "TikTok": "https://www.tiktok.com/@{h}",
               "YouTube": "https://www.youtube.com/@{h}"}


def add_profile(db, platform, handle):
    handle = handle.strip().lstrip("@").strip("/")
    pid = f"{ {'Instagram': 'ig', 'TikTok': 'tt', 'YouTube': 'yt'}[platform]}:{handle.lower()}"
    if db.q("SELECT id FROM profiles WHERE id=?", [pid]):
        return
    url = PROFILE_URL[platform].format(h=handle)
    if platform == "YouTube" and handle.startswith("UC"):
        url = f"https://www.youtube.com/channel/{handle}"
    db.q("INSERT INTO profiles (id, platform, handle, url, added, active) VALUES (?,?,?,?,?,1)",
         [pid, platform, handle, url, NOW.isoformat(timespec="seconds")])


def seed_profiles(db):
    """Стартовый список из config.yaml — один раз, дальше профили правятся в интерфейсе."""
    if db.q("SELECT v FROM kv WHERE k='profiles_seeded'"):
        return
    for line in CFG.get("seed_profiles", []) or []:
        plat, h = line.split(None, 1)
        add_profile(db, {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}[plat.lower()], h)
    db.q("INSERT OR REPLACE INTO kv VALUES ('profiles_seeded', '1')")


def run_profiles(db, only=None, analyze_now=True, deep_n=None):
    """Профили. Первый раз — глубокий скан (до 500 роликов / 18 мес.), дальше раз в 3 дня — ролики за последний месяц.
    Выстрел = просмотры ≥ X × скользящая медиана (±15 соседних роликов, без роликов моложе 3 дней) и ≥ порога просмотров."""
    from concurrent.futures import ThreadPoolExecutor
    seed_profiles(db)
    profs = db.q("SELECT * FROM profiles WHERE COALESCE(active,1)=1 ORDER BY added")
    if only is not None:
        profs = [p for p in profs if p["id"] in only]
    log(f"Профилей на слежке: {len(profs)}")
    deep_n, deep_days = int(deep_n or S.get("profile_deep_reels", 300)), int(S.get("profile_deep_days", 365))
    upd_n, upd_days = int(S.get("profile_update_reels", 50)), int(S.get("profile_update_days", 30))
    x_min, floor = float(S.get("profile_outlier_min", 2)), int(S.get("profile_min_views", 100000))
    window, young = int(S.get("profile_window", 15)), int(S.get("profile_young_days", 3))
    budget = int(S.get("profile_max_analyze", 1000))
    hidden = {r["id"] for r in db.q("SELECT id FROM labels WHERE hidden=1")}
    found, new_ids, analyzed, all_c = 0, set(), 0, []
    deep_since = (NOW - dt.timedelta(days=deep_days)).strftime("%Y-%m-%d")
    upd_since = (NOW - dt.timedelta(days=upd_days)).strftime("%Y-%m-%d")

    # 1) сбор: Instagram/TikTok — один запуск Apify на группу профилей
    strong = int(S.get("profile_deep_min_median", 20000))
    # режим профиля: deep — глубокий скан; sample — первые 50 роликов, чтобы понять, сильный ли профиль; update — обновление
    for p in profs:
        if p.get("deep") == 1:
            p["_mode"] = "update"
        elif p.get("deep") == 0 or (p.get("median_views") or 0) >= strong:
            p["_mode"] = "deep"
        elif p.get("median_views") is not None:
            p["_mode"] = "update"   # слабый профиль: глубокий скан не нужен
        else:
            p["_mode"] = "sample"
    got = {}
    for plat, fn in (("Instagram", fetch_instagram), ("TikTok", fetch_tiktok)):
        for mode in ("deep", "sample", "update"):
            grp = [p for p in profs if p["platform"] == plat and p["_mode"] == mode]
            if not grp:
                continue
            deep = mode == "deep"
            if ENV("RADAR_IMPORT_RUN") and (plat != "Instagram" or mode != "deep"):
                continue
            if not ENV("APIFY_TOKEN"):
                log(f"  {plat}: нет токена Apify, пропуск"); continue
            try:
                n_, since_ = (deep_n, deep_since) if deep else (upd_n, None if mode == "sample" else upd_since)
                res = fn(grp, n_, since_) if plat == "Instagram" else fn(grp, n_)
                log(f"  {plat}: {dict(deep='глубокий скан', sample='первый сбор', update='обновление')[mode]} {len(grp)} профилей → {sum(len(v) for v in res.values())} роликов")
                for p in grp:
                    got[p["id"]] = res.get(p["handle"].lower(), [])
            except Exception as e:
                log(f"  {plat}: ошибка сбора {str(e)[:200]}")
    for p in profs:
        if p["platform"] == "YouTube":
            try:
                got[p["id"]] = fetch_youtube(p, deep_n if p["_mode"] == "deep" else upd_n, deep_since if p["_mode"] != "update" else upd_since)
            except Exception as e:
                log(f"  {p['id']}: ошибка YouTube {str(e)[:200]}")

    queue = []   # (профиль, ролик) на разбор
    for p in profs:
        deep = p["_mode"] in ("deep", "sample")   # в первом сборе тоже разбираем все выстрелы выборки
        cands = got.get(p["id"])
        if cands is None:
            db.q("UPDATE profiles SET status=?, last_run=? WHERE id=?", ["ошибка сбора", NOW.isoformat(timespec="seconds"), p["id"]]); continue
        since = deep_since if deep else upd_since
        cands = [c for c in cands if not c.get("published") or c["published"] >= since][: deep_n if p["_mode"] == "deep" else upd_n]
        # смена ника: все ролики пришли от другого владельца
        owners = {(c.get("author") or "").lower() for c in cands}
        if p["platform"] == "Instagram" and len(owners) == 1 and next(iter(owners)) not in ("", p["handle"].lower()):
            newh = next(iter(owners))
            log(f"  {p['id']}: ник сменился на @{newh}")
            db.q("UPDATE profiles SET name=?, handle=?, url=? WHERE id=?", [newh, newh, f"https://www.instagram.com/{newh}/", p["id"]])
            p["handle"], p["url"] = newh, f"https://www.instagram.com/{newh}/"
        if not cands and deep:
            log(f"  {p['id']}: роликов нет (удалён, закрыт или пуст)")
            db.q("UPDATE profiles SET status=?, last_run=? WHERE id=?", ["нет роликов", NOW.isoformat(timespec="seconds"), p["id"]]); continue
        fol = next((c["followers"] for c in cands if c.get("followers")), None) or p.get("followers")
        bio = next((c["author_bio"] for c in cands if c.get("author_bio")), None)
        ext = next((c["author_ext"] for c in cands if c.get("author_ext")), None)
        if bio or ext:
            db.q("UPDATE profiles SET bio=COALESCE(?, bio), ext_url=COALESCE(?, ext_url) WHERE id=?", [bio, ext, p["id"]])
        for c in cands:
            c.update(vertical="profiles", source="profile", keyword="профиль @" + p["handle"], author_id=p["id"], author_url=p["url"])
            if fol and not c.get("followers"):
                c["followers"] = fol
        known = set()
        ids = [c["id"] for c in cands]
        for i in range(0, len(ids), 90):
            known |= {r["id"] for r in db.q("SELECT id FROM videos WHERE id IN (%s)" % ",".join("?" * len(ids[i:i + 90])), ids[i:i + 90])}
        bulk_save(db, cands, known)
        fresh = [c for c in cands if c["id"] not in known]
        new_ids |= {c["id"] for c in fresh}
        found += len(cands)
        # обложки (временные ссылки → в базу), параллельно
        todo = [c for c in cands if c.get("thumb")]
        have = set()
        for i in range(0, len(todo), 90):
            ch = [c["id"] for c in todo[i:i + 90]]
            have |= {r["id"] for r in db.q("SELECT id FROM thumbs WHERE id IN (%s)" % ",".join("?" * len(ch)), ch)}
        todo = [c for c in todo if c["id"] not in have]
        with ThreadPoolExecutor(1 if ENV("LOCAL_DB") else 8) as ex:
            list(ex.map(lambda c: save_thumb(db, c["id"], c["thumb"]), todo))
        # 2) скользящая медиана по всей истории профиля в базе
        hist = db.q("SELECT id, views, published, status FROM videos WHERE author_id=? AND source='profile'", [p["id"]])
        cur_views = {c["id"]: c["views"] for c in cands}
        for r in hist:
            r["views"] = cur_views.get(r["id"], r["views"] or 0)
        med = rolling_median(hist, window, young)
        x = {}
        for r in hist:
            m_, a_ = med.get(r["id"], (None, 0))
            x[r["id"]] = round(r["views"] / m_, 1) if m_ else None
        rows = [r for r in hist if x.get(r["id"]) is not None]
        for i in range(0, len(rows), 150):
            ch = rows[i:i + 150]
            case = " ".join(f"WHEN {lit(r['id'])} THEN {x[r['id']]}" for r in ch)
            db.q(f"UPDATE videos SET vs_median = CASE id {case} END WHERE id IN ({','.join(lit(r['id']) for r in ch)})")
        # 3) выстрелы: глубокий скан — все за 18 мес.; обновление — новые + поздние за последний месяц
        byid = {c["id"]: c for c in cands}
        shots = []
        for r in hist:
            m_, a_ = med.get(r["id"], (None, 0))
            if r["status"] == "analyzed" or r["id"] in hidden or not x.get(r["id"]) or a_ < young:
                continue
            if not deep and (r.get("published") or "9") < upd_since:
                continue
            if x[r["id"]] >= x_min and r["views"] >= floor:
                shots.append(r["id"])
        if shots:
            db.q(f"UPDATE videos SET status='queued' WHERE id IN ({','.join(lit(i) for i in shots)})")
        n_shots = sum(1 for r in hist if (x.get(r["id"]) or 0) >= x_min and r["views"] >= floor)
        allv = sorted(r["views"] for r in hist if r["views"])
        db.q("""UPDATE profiles SET followers=COALESCE(?, followers), reels=?, median_views=?, best_x=?, shots=?, new_last=?,
                status=?, last_run=?, deep=? WHERE id=?""",
             [fol, len(hist), allv[len(allv) // 2] if allv else None, max([v for v in x.values() if v] or [0]), n_shots, len(fresh),
              f"на разбор {len(shots)}", NOW.isoformat(timespec="seconds"),
              (0 if (allv and allv[len(allv) // 2] >= strong) else 1) if p["_mode"] == "sample" else 1, p["id"]])
        log(f"  {p['id']}: {dict(deep='глубокий скан', sample='первый сбор', update='обновление')[p['_mode']]} — роликов {len(cands)}, новых {len(fresh)}, "
            f"в базе {len(hist)}, выстрелов к разбору {len(shots)}")
        for sid in shots:
            c = byid.get(sid)
            if not c:   # поздний выстрел, которого нет в текущей выдаче
                row = db.q("SELECT * FROM videos WHERE id=?", [sid])[0]
                c = dict(row, raw_id=sid[3:])
            c["vs_median"] = x.get(sid)
            queue.append((p, c))
        all_c += cands

    if not analyze_now:
        if all_c:
            refresh_authors(db, all_c)
        return dict(found=found, new=len(new_ids), analyzed=0, new_ids=new_ids, queue=queue)
    # 4) разбор в 4 потока (запись в базу — в основном потоке)
    queue = queue[:budget]
    log(f"На разбор всего: {len(queue)}")
    tt_files = {}
    tts = [c for p, c in queue if c["platform"] == "TikTok"]
    if tts and S.get("analyze_tiktok", True):
        tt_files = {k: v[0] for k, v in tiktok_download(tts).items()}

    def work(pc):
        p, c = pc
        vb = None
        if c["platform"] == "Instagram":
            vb = fetch_bytes(c["video_url"]) if c.get("video_url") else None
            if not vb:
                return c, None, "видео не скачалось (ссылка устарела — повторим в следующий раз)"
        elif c["platform"] == "TikTok":
            vb = tt_files.get(c["raw_id"])
            if not vb:
                return c, None, "TikTok не скачался"
        try:
            return c, analyze(c, vb), None
        except Exception as e:
            return c, None, str(e)[:150]

    per = {}
    with ThreadPoolExecutor(1 if ENV("LOCAL_DB") else 4) as ex:
        for c, a, err in ex.map(work, queue):
            if a:
                upsert_video(db, c, "analyzed", a=a)
                analyzed += 1; new_ids.add(c["id"]); per[c["author_id"]] = per.get(c["author_id"], 0) + 1
                log(f"  ✓ {c['platform']} @{c['author']} {c['views']:,} (×{c.get('vs_median')} к медиане) → {a.get('nutra_score')}/5")
            else:
                log(f"  ✗ {c['id']}: {err or 'разбор не удался'}")
    for pid, n in per.items():
        db.q("UPDATE profiles SET status=? WHERE id=?", [f"ок: разобрано {n}", pid])
    if all_c:
        refresh_authors(db, all_c)
    return dict(found=found, new=len(new_ids), analyzed=analyzed, new_ids=new_ids)


# ------------------------------------------------------------------ ВОЛНЫ (массовый сбор топ-аккаунтов)
CLS_KEYS = ("direction", "direction_extra", "host", "visuals", "moderation_risk")   # берём у модели-классификатора


def merge_an(base, cls=None, txt=None, cls_m=None, txt_m=None):
    """Склейка разборов: тексты (хук, эмоция, субтитры…) — от txt-модели, классификация — от cls-модели.
    base — уже сохранённый разбор (для дозаполнения второй моделью)."""
    m = dict(txt or base or cls or {})
    if txt and base:   # был разбор классификатора — сохраняем его классификацию
        for k in CLS_KEYS:
            if base.get(k) not in (None, "", []):
                m[k] = base[k]
    if cls:
        for k in CLS_KEYS:
            if cls.get(k) not in (None, "", []):
                m[k] = cls[k]
        if not (txt or base):
            m = dict(cls)
    old = (base or {}).get("_m") or {}
    m["_m"] = {"cls": cls_m or old.get("cls"), "txt": txt_m or old.get("txt")}
    if not m.get("subtitles") and base and base.get("subtitles"):
        m["subtitles"] = base["subtitles"]
    return m


def fresh_links(urls, budget_usd):
    """Свежие ссылки на видео Instagram (официальный сборщик по прямым ссылкам, ~$0.0023 за ролик) с потолком трат."""
    n = min(len(urls), int(budget_usd / 0.0027))
    if n <= 0:
        return {}, 0.0
    ac = apify()
    run = ac.actor("apify/instagram-scraper").call(
        run_input={"directUrls": urls[:n], "resultsType": "posts", "resultsLimit": 1}, max_items=n)
    items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
    try:
        cost = float(_f(ac.run(_f(run, "id")).get(), "usageTotalUsd", "usage_total_usd") or 0)
    except Exception:
        cost = n * 0.0023
    out = {}
    for it in items:
        sc = it.get("shortCode") or it.get("shortcode")
        if sc and it.get("videoUrl"):
            out["ig_" + sc] = it["videoUrl"]
    log(f"  Свежие ссылки: {len(out)}/{n}, Apify ${cost:.2f}")
    return out, cost


def run_wave(db):
    """Волна: следующие N аккаунтов из research/wave_list.json → глубокий скан (до wave_reels роликов) →
    разбор выстрелов двумя моделями, каждая до своего дневного лимита. Остаток — завтра, без повторов."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    t0 = time.time()
    size = int(ENV("RADAR_WAVE") or S.get("wave_size", 25))
    reels = int(S.get("wave_reels", 300))
    budget = float(ENV("RADAR_BUDGET") or 0) or float(S.get("wave_budget", 12))
    m_cls = S.get("wave_cls_model", "gemini-3.1-flash-lite")
    m_txt = S.get("wave_txt_model", "gemini-3.5-flash-lite")
    max_min = float(S.get("wave_max_minutes", 300))
    J = {"date": NOW.strftime("%Y-%m-%d"), "start": NOW.isoformat(timespec="seconds"), "size": size, "budget": budget}
    spent = 0.0

    # 1) новые аккаунты волны
    lst = json.load(open(os.path.join(os.path.dirname(__file__) or ".", "research", "wave_list.json"), encoding="utf-8"))
    have = {r["id"] for r in db.q("SELECT id FROM profiles")}
    todo = [x for x in lst if "ig:" + x["h"].lower() not in have]
    take = todo[:size]
    if ENV("RADAR_IGTEST"):          # ручной список ников для пробы
        take = [{"h": h.strip().lstrip("@")} for h in ENV("RADAR_IGTEST").split(",") if h.strip()]
    est = len(take) * reels * 0.001
    if est > budget:
        take = take[: int(budget / (reels * 0.001))]
        log(f"Потолок ${budget}: аккаунтов в волне {len(take)}")
    ids = set()
    for x in take:
        add_profile(db, "Instagram", x["h"])
        pid = "ig:" + x["h"].lower()
        db.q("UPDATE profiles SET deep=0, active=1, wave=?, followers=COALESCE(followers, ?) WHERE id=?", [J["date"], x.get("f"), pid])
        ids.add(pid)
    J["accounts"] = len(ids); J["left_accounts"] = len(todo) - len([x for x in take if x in todo])
    log(f"Волна {J['date']}: аккаунтов {len(ids)} (осталось в списке после неё {J['left_accounts']}), до {reels} роликов с аккаунта")
    queue = []
    if ids:
        st = run_profiles(db, only=ids, analyze_now=False, deep_n=reels)
        spent += st["found"] * 0.001   # memo23 ≈ $0.8–1 за 1000 роликов (оценка сверху)
        J.update(reels=st["found"], new=st["new"])
        queue = [c for p, c in st["queue"] if c.get("video_url")]
        log(f"Собрано роликов {st['found']} (≈${spent:.2f}), выстрелов к разбору {len(st['queue'])}, со ссылкой на видео {len(queue)}")
    queue.sort(key=lambda c: -(c.get("vs_median") or 0))
    in_q = {c["id"] for c in queue}

    # 2) очередь двух моделей: что ещё не сделано каждой
    done = {}   # id → (base analysis, set of done roles)
    state = {"cls": {"model": m_cls, "off": None, "n": 0}, "txt": {"model": m_txt, "off": None, "n": 0}}
    lock = threading.Lock()
    J["errors"] = 0

    def roles_left(base):
        mm = (base or {}).get("_m") or {}
        return [r for r in ("cls", "txt") if not mm.get(r) and not state[r]["off"]]

    def one(c, base=None):
        if time.time() - t0 > max_min * 60:
            return
        left = roles_left(base)
        if not left:
            return
        vb = fetch_bytes(c["video_url"])
        if not vb:
            with lock:
                J["errors"] += 1
            return
        res = {}
        for role in left:
            if state[role]["off"]:
                continue
            try:
                a = analyze(c, vb, model=state[role]["model"])
            except QuotaDay as e:
                with lock:
                    if not state[role]["off"]:
                        state[role]["off"] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                              "after": state[role]["n"], "msg": str(e)[:200]}
                        log(f"  ⛔ {state[role]['model']}: дневной лимит после {state[role]['n']} разборов ({str(e)[:120]})")
                continue
            if a:
                res[role] = a
                with lock:
                    state[role]["n"] += 1
        if not res:
            return
        m = merge_an(base, cls=res.get("cls"), txt=res.get("txt"),
                     cls_m=state["cls"]["model"] if "cls" in res else None, txt_m=state["txt"]["model"] if "txt" in res else None)
        with lock:
            upsert_video(db, c, "analyzed", a=m)
            log(f"  ✓ @{c.get('author')} {c.get('views') or 0:,} ×{c.get('vs_median')} [{'+'.join(sorted(res))}] → {m.get('nutra_score')}/5")

    threads = int(S.get("wave_threads", 10))
    with ThreadPoolExecutor(1 if ENV("LOCAL_DB") else threads) as ex:
        list(ex.map(one, queue))

    # 3) хвосты прошлых дней: невыполненные выстрелы и разборы одной моделью — со свежими ссылками, пока есть лимит
    tail_n = 0
    tries = json.loads((db.q("SELECT v FROM kv WHERE k='wave_tries'") or [{"v": "{}"}])[0]["v"] or "{}")
    for rnd in range(20):
        if (state["cls"]["off"] and state["txt"]["off"]) or time.time() - t0 > max_min * 60 or budget - spent < 0.3:
            break
        rows = db.q("""SELECT id, url, author, author_id, platform, vertical, duration, views, likes, comments, shares, vs_median, status,
                              analysis, subtitles FROM videos
                       WHERE source='profile' AND platform='Instagram' AND (status='queued'
                          OR (status='analyzed' AND json_extract(analysis,'$._m') IS NOT NULL
                              AND (json_extract(analysis,'$._m.cls') IS NULL OR json_extract(analysis,'$._m.txt') IS NULL)))
                       ORDER BY CASE WHEN status='queued' THEN 0 ELSE 1 END, vs_median DESC LIMIT 400""")
        rows = [r for r in rows if r["id"] not in in_q]
        rows = [r for r in rows if roles_left(json.loads(r["analysis"]) if r["status"] == "analyzed" and r["analysis"] else None)][:100]
        if not rows:
            break
        links, cost = fresh_links([r["url"] for r in rows], min(budget - spent, 0.3))
        spent += cost
        jobs = []
        for r in rows:
            in_q.add(r["id"])
            if r["id"] not in links:
                tries[r["id"]] = tries.get(r["id"], 0) + 1
                if tries[r["id"]] >= 3:   # 3 попытки — дальше не тратим деньги
                    if r["status"] == "queued":
                        db.q("UPDATE videos SET status='profile_rest' WHERE id=?", [r["id"]])
                    else:
                        db.q("""UPDATE videos SET analysis=json_set(analysis, '$._m.cls', COALESCE(json_extract(analysis,'$._m.cls'),'нет'),
                                '$._m.txt', COALESCE(json_extract(analysis,'$._m.txt'),'нет')) WHERE id=?""", [r["id"]])
                    J["gave_up"] = J.get("gave_up", 0) + 1
                continue
            base = None
            if r["status"] == "analyzed":
                base = json.loads(r["analysis"] or "{}")
                base["subtitles"] = json.loads(r["subtitles"] or "[]") if isinstance(r["subtitles"], str) else (r["subtitles"] or [])
            c = dict(r, raw_id=r["id"][3:], video_url=links[r["id"]])
            c.pop("analysis", None); c.pop("subtitles", None)
            jobs.append((c, base))
        with ThreadPoolExecutor(1 if ENV("LOCAL_DB") else threads) as ex:
            list(ex.map(lambda cb: one(*cb), jobs))
        tail_n += len(jobs)

    db.q("INSERT OR REPLACE INTO kv VALUES ('wave_tries', ?)", [json.dumps(tries)])
    left = db.q("""SELECT COUNT(*) n FROM videos WHERE source='profile' AND status='queued'""")[0]["n"]
    part = db.q("""SELECT COUNT(*) n FROM videos WHERE status='analyzed' AND json_extract(analysis,'$._m') IS NOT NULL
                   AND (json_extract(analysis,'$._m.cls') IS NULL OR json_extract(analysis,'$._m.txt') IS NULL)""")[0]["n"]
    J.update(shots_fresh=len(queue), tail=tail_n, done_cls=state["cls"]["n"], done_txt=state["txt"]["n"],
             quota_cls=state["cls"]["off"], quota_txt=state["txt"]["off"], left_queued=left, left_partial=part,
             apify_usd=round(spent, 2), minutes=round((time.time() - t0) / 60))
    jr = json.loads((db.q("SELECT v FROM kv WHERE k='wave_journal'") or [{"v": "[]"}])[0]["v"] or "[]")
    jr.append(J)
    db.q("INSERT OR REPLACE INTO kv VALUES ('wave_journal', ?)", [json.dumps(jr, ensure_ascii=False)])
    log("ЖУРНАЛ ВОЛНЫ: " + json.dumps(J, ensure_ascii=False))
    print("WAVEJSON_BEGIN"); print(json.dumps(J, ensure_ascii=True)); print("WAVEJSON_END")
    return dict(found=J.get("reels", 0), new=J.get("new", 0), analyzed=state["cls"]["n"] + state["txt"]["n"], new_ids=set())


# ------------------------------------------------------------------ ГЛАВНОЕ
def load_settings(db):
    """Настройки живут в базе (их меняют в веб-интерфейсе). Первый раз берём из config.yaml."""
    global CFG, S
    row = db.q("SELECT v FROM kv WHERE k='settings'")
    if row and row[0]["v"]:
        try:
            cfg = json.loads(row[0]["v"])
            if cfg.get("verticals"):
                CFG = {**CFG, **cfg}
                CFG["settings"] = {**S, **cfg.get("settings", {})}
        except Exception as e:
            log("Настройки из базы не прочитались, беру config.yaml:", e)
    else:
        db.q("INSERT OR REPLACE INTO kv VALUES ('settings', ?)",
             [{"settings": CFG["settings"], "verticals": CFG["verticals"]}])
        log("Настройки сохранены в базу из config.yaml")
    S = CFG["settings"]
    for k, d in {"search_youtube": True, "search_tiktok": True, "analyze_tiktok": True}.items():
        S.setdefault(k, d)
    if ENV("RADAR_MODE") == "analyze":
        S["search_youtube"] = S["search_tiktok"] = False
        S["analyze_tiktok"] = True
        log("Режим: только разбор уже найденного (поиск выключен)")
    if (ENV("RADAR_MAX_ANALYZE") or "").strip().isdigit():
        S["max_analyze_per_run"] = int(ENV("RADAR_MAX_ANALYZE"))
        log("Лимит разборов на этот запуск:", S["max_analyze_per_run"])


def tt_cover_url(page, ua):
    """Ссылка на обложку: oEmbed (если вернёт), иначе HTML страницы ролика (og:image / cover в JSON)."""
    try:
        j = requests.get("https://www.tiktok.com/oembed", params={"url": page}, headers=ua, timeout=20).json()
        if j.get("thumbnail_url"):
            return j["thumbnail_url"]
    except Exception:
        pass
    for u in (page, page.replace("www.tiktok.com", "m.tiktok.com")):
        try:
            html = requests.get(u, headers={**ua, "accept-language": "en-US,en;q=0.9"}, timeout=25).text
        except Exception:
            continue
        for pat in (r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', r'"originCover":"([^"]+)"', r'"cover":"(https?:[^"]+)"', r'"dynamicCover":"([^"]+)"'):
            mm = re.search(pat, html)
            if mm and mm.group(1).startswith("http"):
                return mm.group(1).encode().decode("unicode_escape").replace("&amp;", "&")
    return None


def db_size(db):
    try:
        db.q("SELECT 1")
        b = (getattr(db, "meta", {}) or {}).get("size_after")
        if b:
            log(f"Размер базы: {b/1e6:.1f} МБ из 5000 МБ бесплатных")
    except Exception:
        pass


def tiktok_thumbs(db, limit=400):
    """Обложки TikTok: oEmbed с сервера GitHub (Cloudflare TikTok не пускает) → уменьшенная JPEG в таблицу thumbs."""
    import base64, io
    rows = db.q("SELECT id, url, author FROM videos WHERE platform='TikTok' AND id NOT IN (SELECT id FROM thumbs) LIMIT ?", [limit])
    if not rows:
        return
    ua = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
    ok = 0
    for r in rows:
        vid = r["id"][3:]
        page = (r.get("url") or "").split("?")[0]
        if "/video/" not in page:
            page = f"https://www.tiktok.com/@{r.get('author') or 'x'}/video/{vid}"
        try:
            src = tt_cover_url(page, ua)
            if not src:
                if ok == 0 and rows.index(r) < 3:
                    log(f"  обложка {r['id']}: не нашёл ссылку на странице {page}")
                continue
            img = requests.get(src, headers=ua, timeout=30)
            img.raise_for_status()
            data, mime = img.content, img.headers.get("content-type", "image/jpeg")
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data)).convert("RGB")
                im.thumbnail((360, 640))
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=72); data, mime = buf.getvalue(), "image/jpeg"
            except Exception:
                pass
            if len(data) > 70000:
                log(f"  обложка {r['id']} слишком большая: {len(data)} байт")
                continue
            db.q("INSERT OR REPLACE INTO thumbs VALUES (?,?,?,?)", [r["id"], mime, base64.b64encode(data).decode(), NOW.isoformat(timespec="seconds")])
            ok += 1
        except Exception as e:
            log(f"  обложка {r['id']}: {str(e)[:120]}")
        time.sleep(0.5)
    log(f"Обложки TikTok: сохранено {ok} из {len(rows)}")


def main():
    db = DB()
    for s in SCHEMA:
        db.q(s)
    for alter in ("ALTER TABLE labels ADD COLUMN reason TEXT", "ALTER TABLE videos ADD COLUMN source TEXT",
                  "ALTER TABLE videos ADD COLUMN vs_median REAL", "ALTER TABLE profiles ADD COLUMN ai INTEGER",
                  "ALTER TABLE profiles ADD COLUMN deep INTEGER", "ALTER TABLE videos ADD COLUMN direction TEXT",
                  "ALTER TABLE videos ADD COLUMN directions TEXT", "ALTER TABLE videos ADD COLUMN dir_v INTEGER",
                  "ALTER TABLE videos ADD COLUMN host TEXT", "ALTER TABLE videos ADD COLUMN visual TEXT",
                  "ALTER TABLE videos ADD COLUMN visuals TEXT", "ALTER TABLE videos ADD COLUMN vis_v INTEGER",
                  "ALTER TABLE profiles ADD COLUMN bio TEXT", "ALTER TABLE profiles ADD COLUMN ext_url TEXT",
                  "ALTER TABLE profiles ADD COLUMN wave TEXT"):
        try:
            db.q(alter)
        except Exception:
            pass
    load_settings(db)
    if ENV("RADAR_MODE") == "thumbs":
        tiktok_thumbs(db)
        db_size(db)
        return
    if ENV("RADAR_MODE") == "visuals":
        n = classify_visuals(db)
        log(f"Готово: размечено {n}, Gemini ~${GEM['tin'] / 1e6 * 0.1 + GEM['tout'] / 1e6 * 0.4:.3f} (оценка)")
        return
    if ENV("RADAR_MODE") == "directions":
        n = classify_directions(db)
        log(f"Готово: размечено {n}, Gemini ~${GEM['tin'] / 1e6 * 0.1 + GEM['tout'] / 1e6 * 0.4:.3f} (оценка)")
        return
    if ENV("RADAR_MODE") == "formats":
        rows = db.q("SELECT id, platform, source, views, nutra_score, direction, host, visual, visuals, analysis FROM videos WHERE status='analyzed'")
        print("FMT_BEGIN")
        for r in rows:
            a = json.loads(r["analysis"] or "{}")
            print(json.dumps({"id": r["id"], "p": r["platform"], "src": r["source"], "v": r["views"], "s": r["nutra_score"],
                              "d": r["direction"], "host": r.get("host"), "vis": r.get("visuals"), "f": a.get("format"), "h": a.get("hook_type"), "hook": (a.get("hook_0_3s") or "")[:160]},
                             ensure_ascii=True))
        print("FMT_END")
        return
    if ENV("RADAR_MODE") == "reltest":
        ac = apify()
        info = ac.actor("thenetaji/instagram-related-user-scraper").last_run().get()   # уже оплаченный пробный запуск
        items = list(ac.dataset(_f(info, "defaultDatasetId", "default_dataset_id")).iterate_items())
        log(f"reltest: элементов {len(items)}, статус {_f(info, 'status')}, стоимость ${_f(info, 'usageTotalUsd', 'usage_total_usd')}, "
            f"события {_f(info, 'chargedEventCounts', 'charged_event_counts')} цена {str(_f(info, 'pricingInfo', 'pricing_info'))[:400]}")
        for it in items[:25]:
            log("  ", json.dumps({k: it.get(k) for k in ("username", "full_name", "is_verified", "discovered_from", "depth")}, ensure_ascii=False))
        return
    if ENV("RADAR_MODE") == "enrich":
        enrich_from_runs(db)
        return
    if ENV("RADAR_MODE") == "funnels":
        run_funnels(db)
        return
    if ENV("RADAR_MODE") == "cmpmodels":
        cmp_models(db)
        return
    if ENV("RADAR_MODE") == "profcheck":
        users = [s.strip().lstrip("@") for s in (ENV("RADAR_IGTEST") or "").split(",") if s.strip()]
        budget = float(ENV("RADAR_BUDGET") or 0)
        if not users or len(users) * 0.0026 > budget:
            log(f"Не запускаю: ников {len(users)}, бюджет ${budget}"); return
        ac = apify()
        run = ac.actor("apify/instagram-profile-scraper").call(run_input={"usernames": users}, max_items=len(users))
        items = list(ac.dataset(_f(run, "defaultDatasetId", "default_dataset_id")).iterate_items())
        try:
            spent = _f(ac.run(_f(run, "id")).get(), "usageTotalUsd", "usage_total_usd")
        except Exception:
            spent = None
        log(f"Проверено {len(items)}/{len(users)}, потрачено ${spent}")
        print("SNOWJSON_BEGIN")
        for p in items:
            print(json.dumps({"u": p.get("username"), "name": p.get("fullName"), "fol": p.get("followersCount"), "ver": p.get("verified"),
                              "url": p.get("externalUrl"), "cat": p.get("businessCategoryName"), "err": p.get("error"),
                              "bio": (p.get("biography") or "").replace("\n", " · ")[:300]}, ensure_ascii=True))
        print("SNOWJSON_END")
        return
    if ENV("RADAR_MODE") == "nathash":
        nat_hash()
        return
    if ENV("RADAR_MODE") == "natsnow":
        nat_snow()
        return
    if ENV("RADAR_MODE") == "lookalike":
        lookalike()
        return
    if ENV("RADAR_MODE") == "medsnow":
        med_snow()
        return
    if ENV("RADAR_MODE") == "medenrich":
        med_enrich()
        return
    if ENV("RADAR_MODE") == "igtest":
        n = db.q("SELECT COUNT(*) n FROM videos WHERE source='profile'")[0]["n"]
        log(f"В базе роликов из профилей: {n}")
        log("Профили:", [(p["id"], p.get("reels"), p.get("deep"), p.get("status")) for p in db.q("SELECT * FROM profiles")])
        ig_test()
        return
    if ENV("RADAR_MODE") == "wave":
        stats = run_wave(db)
        db.q("INSERT INTO runs VALUES (?,?,?,?,?,?)", [NOW.isoformat(timespec="seconds"), stats["found"], stats["new"],
             stats["analyzed"], 0, "\n".join(LOG)[-20000:]])
        db_size(db)
        return
    if ENV("RADAR_MODE") == "profiles":
        stats = run_profiles(db)
        try:
            tiktok_thumbs(db)
        except Exception as e:
            log("Обложки TikTok: ошибка", str(e)[:200])
        try:
            classify_directions(db)
            classify_visuals(db)
        except Exception as e:
            log("Направления/визуал: ошибка", str(e)[:200])
        cost = GEM["tin"] / 1e6 * 0.25 + GEM["tout"] / 1e6 * 1.5
        stats["cost"] = cost
        db.q("INSERT INTO runs VALUES (?,?,?,?,?,?)", [NOW.isoformat(timespec="seconds"), stats["found"], stats["new"],
             stats["analyzed"], cost, "\n".join(LOG)[-20000:]])
        report(db, stats.pop("new_ids"), stats)
        db_size(db)
        log("Готово (профили):", stats)
        return
    # разобранные ролики не на нужном языке (определено при разборе) — в отсев
    langs = ",".join("'%s'" % l.replace("'", "") for l in S["langs"])
    db.q(f"UPDATE videos SET status='skip_lang' WHERE status='analyzed' AND COALESCE(source,'search')<>'profile' AND lang<>'' AND substr(lang,1,2) NOT IN ({langs})")
    found, new_ids, analyzed = 0, set(), 0
    budget = S["max_analyze_per_run"]
    for vertical, v in CFG["verticals"].items():
        cands = []
        if S.get("search_youtube", True):
            cands += collect_youtube(vertical, v["keywords"])
        if S.get("search_tiktok", True):
            cands += collect_tiktok(vertical, v["keywords"])
        if not cands:
            log(f"[{vertical}] поиск новых выключен — только разбор уже найденного")
        cands = [c for c in cands if c["views"] >= S["min_views"]
                 and (not c.get("duration") or c["duration"] <= S["max_duration_sec"])]
        found += len(cands)
        known = {r["id"]: r["status"] for r in db.q(
            "SELECT id, status FROM videos WHERE vertical=?", [vertical])}
        fresh = [c for c in cands if c["id"] not in known]
        for c in cands:
            if c["id"] in known:
                upsert_video(db, c, None)
        log(f"[{vertical}] подходящих {len(cands)}, новых {len(fresh)}")

        keep = []
        # ролики, которые префильтр не успел проверить в прошлый раз
        fresh += [dict(r, raw_id=r["id"][3:]) for r in db.q("SELECT * FROM videos WHERE vertical=? AND status='prefilter'", [vertical])
                  if r["id"] not in {c["id"] for c in fresh}]
        pre = prefilter(fresh, vertical) if fresh else {}
        for c in fresh:
            p = pre.get(c["id"])
            if not p:   # фильтр не ответил (перегрузка) — не пускаем вслепую, проверим в следующий запуск
                upsert_video(db, c, "prefilter"); db.q("UPDATE videos SET status='prefilter' WHERE id=?", [c["id"]])
                continue
            lang = (p.get("lang") or c.get("lang") or "").lower()[:2]
            c["lang"] = lang
            ok_lang = lang in S["langs"]
            ok_topic = p.get("relevant", True)
            status = "queued" if (ok_lang and ok_topic) else ("skip_lang" if not ok_lang else "skip_topic")
            upsert_video(db, c, status, pre=p.get("reason"))
            db.q("UPDATE videos SET status=?, prefilter=?, lang=? WHERE id=?", [status, p.get("reason"), lang, c["id"]])
            new_ids.add(c["id"])
            if status == "queued":
                keep.append(c)
        # недоразобранные с прошлых запусков (лимит или ошибка)
        ids_now = {c["id"] for c in keep}
        for r in db.q("SELECT * FROM videos WHERE vertical=? AND status='queued'", [vertical]):
            if r["id"] not in ids_now:
                keep.append(dict(r, raw_id=r["id"][3:]))
        hidden = {r["id"] for r in db.q("SELECT id FROM labels WHERE hidden=1")}
        keep = [c for c in keep if c["id"] not in hidden]
        keep.sort(key=lambda c: c["views"], reverse=True)
        keep = keep[:max(0, budget - analyzed)]
        log(f"[{vertical}] на разбор {len(keep)} (языки {S['langs']}, тема)")

        if not S.get("analyze_tiktok", True):
            keep = [c for c in keep if c["platform"] != "TikTok"]
        tt_files = tiktok_download([c for c in keep if c["platform"] == "TikTok"])
        for c in keep:
            vb = None
            if c["platform"] == "TikTok":
                got = tt_files.get(c["raw_id"])
                if not got:
                    continue
                vb, fans = got
                if fans and not c.get("followers"):
                    c["followers"] = fans
            a = analyze(c, vb)
            if a:
                upsert_video(db, c, "analyzed", a=a)
                new_ids.add(c["id"]); analyzed += 1
                log(f"  ✓ {c['platform']} @{c['author']} {c['views']:,} → {a.get('nutra_score')}/5")
            time.sleep(2)
        refresh_authors(db, cands)

    try:
        tiktok_thumbs(db)
    except Exception as e:
        log("Обложки TikTok: ошибка", str(e)[:200])
    try:
        classify_directions(db)
        classify_visuals(db)
    except Exception as e:
        log("Направления/визуал: ошибка", str(e)[:200])
    cost = GEM["tin"] / 1e6 * 0.25 + GEM["tout"] / 1e6 * 1.5
    stats = dict(found=found, new=len(new_ids), analyzed=analyzed, cost=cost)
    db.q("INSERT INTO runs VALUES (?,?,?,?,?,?)", [NOW.isoformat(timespec="seconds"), found, len(new_ids), analyzed, cost, "\n".join(LOG)[-20000:]])
    report(db, new_ids, stats)
    db_size(db)
    log("Готово:", stats)


if __name__ == "__main__":
    main()
