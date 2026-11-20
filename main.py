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
    "CREATE INDEX IF NOT EXISTS i_snap ON snapshots(video_id)",
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


def run_actor(actor, run_input):
    ac = apify()
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
 "nutra_score": число 1-5 по шкале:
{scale}
 "nutra_reason": "почему такой балл, 1 предложение",
 "why_it_works": "1–2 предложения по-русски",
 "how_to_adapt": "как перенести механику на наш ролик без копирования",
 "moderation_risk": "низкий/средний/высокий + почему. ЛЮБЫЕ обещания вылечить, избежать операции, обратить болезнь вспять, религиозные «исцелит» = ВЫСОКИЙ",
 "on_screen_text": [{{"t": секунда, "orig": "надпись", "ru": "перевод"}}],
 "subtitles": [{{"start": сек, "end": сек, "orig": "фраза", "ru": "перевод"}}]
}}
Субтитры: каждая фраза 1–4 сек, не длиннее ~12 слов, таймкоды по моменту речи, все end ≤ {dur}. Речи нет — пустой список."""


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


def analyze(c, video_bytes=None):
    from google.genai import types
    v = CFG["verticals"][c["vertical"]]
    prompt = ANALYSIS.format(vname=v.get("name", c["vertical"]), scale=v.get("nutra_scale", ""), dur=c.get("duration") or 60)
    if c["platform"] == "YouTube":
        part = types.Part(file_data=types.FileData(file_uri=f"https://www.youtube.com/watch?v={c['raw_id']}"))
    else:
        part = types.Part.from_bytes(data=video_bytes, mime_type="video/mp4")
    data = ask([part, prompt], c["id"])
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
    if a is not None:
        a2 = {k: v for k, v in a.items() if k != "subtitles"}
        db.q("UPDATE videos SET status=?, nutra_score=?, content_type=?, analysis=?, subtitles=?, lang=COALESCE(NULLIF(?, ''), lang) WHERE id=?",
             [status, int(a.get("nutra_score") or 0) or None, a.get("content_type"), a2, a.get("subtitles") or [],
              (a.get("language") or "")[:5], c["id"]])
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


def main():
    db = DB()
    for s in SCHEMA:
        db.q(s)
    load_settings(db)
    # разобранные ролики не на нужном языке (определено при разборе) — в отсев
    langs = ",".join("'%s'" % l.replace("'", "") for l in S["langs"])
    db.q(f"UPDATE videos SET status='skip_lang' WHERE status='analyzed' AND lang<>'' AND substr(lang,1,2) NOT IN ({langs})")
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

    cost = GEM["tin"] / 1e6 * 0.25 + GEM["tout"] / 1e6 * 1.5
    stats = dict(found=found, new=len(new_ids), analyzed=analyzed, cost=cost)
    db.q("INSERT INTO runs VALUES (?,?,?,?,?,?)", [NOW.isoformat(timespec="seconds"), found, len(new_ids), analyzed, cost, "\n".join(LOG)[-20000:]])
    report(db, new_ids, stats)
    log("Готово:", stats)


if __name__ == "__main__":
    main()
