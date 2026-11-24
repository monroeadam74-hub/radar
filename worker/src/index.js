// Радар — API + раздача интерфейса (Cloudflare Worker + D1 + static assets)

const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, v TEXT, note TEXT, hidden INTEGER DEFAULT 0, ts TEXT)",
  "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
  "CREATE TABLE IF NOT EXISTS thumbs (id TEXT PRIMARY KEY, mime TEXT, data TEXT, ts TEXT)",
  `CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, platform TEXT, handle TEXT, name TEXT, url TEXT, followers INTEGER,
     reels INTEGER, median_views INTEGER, best_x REAL, shots INTEGER, new_last INTEGER,
     status TEXT, added TEXT, last_run TEXT, active INTEGER DEFAULT 1)`,
  "CREATE TABLE IF NOT EXISTS funnels (url TEXT PRIMARY KEY, profile_id TEXT, final_url TEXT, status TEXT, data TEXT, ts TEXT)",
  "CREATE TABLE IF NOT EXISTS users (login TEXT PRIMARY KEY, salt TEXT, hash TEXT, role TEXT, note TEXT, created TEXT, last_login TEXT)",
];

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", ...headers } });

async function sha(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

// Пользователи: секрет APP_USERS = "логин:пароль;логин2:пароль2" (или старый APP_PASSWORD — один пароль без логина)
function users(env) {
  const out = {};
  for (const pair of String(env.APP_USERS || "").trim().replace(/^["'`]+|["'`]+$/g, "").split(/[;\n]/)) {
    const i = pair.indexOf(":");
    if (i > 0) out[pair.slice(0, i).trim().toLowerCase()] = pair.slice(i + 1).trim();
  }
  if (!Object.keys(out).length && env.APP_PASSWORD) out[""] = env.APP_PASSWORD;
  return out;
}
const tokenFor = (login, pass) => sha(login + "|" + pass + "|radar");

// Пароль пользователя из раздела «Доступ»: PBKDF2-SHA256, 100 000 итераций, своя соль у каждого
const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
async function pbkdf2(pass, salt) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(pass), "PBKDF2", false, ["deriveBits"]);
  return hex(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: new TextEncoder().encode(salt), iterations: 100000 }, k, 256));
}

// Кто вошёл: {login, admin} или null. Админы — логины из секрета APP_USERS; остальные — из таблицы users.
async function who(req, env) {
  const U = users(env);
  if (!Object.keys(U).length) return { login: "", admin: true, open: true }; // доступ не настроен — открыто
  const m = (req.headers.get("cookie") || "").match(/radar_auth=([a-z0-9._@-]*)\.([a-f0-9]{64})/i);
  if (!m) return null;
  const login = m[1].toLowerCase();
  if (login in U) return m[2] === (await tokenFor(login, U[login])) ? { login, admin: true } : null;
  await ensure(env);
  const u = await env.DB.prepare("SELECT login, hash, role FROM users WHERE login=?").bind(login).first();
  if (!u || m[2] !== (await tokenFor(login, u.hash))) return null;
  return { login, admin: u.role === "admin" };
}
const authed = async (req, env) => !!(await who(req, env));

let ensured = false;
async function ensure(env) {
  if (ensured) return;
  for (const s of SCHEMA) await env.DB.prepare(s).run();
  for (const a of ["ALTER TABLE labels ADD COLUMN reason TEXT", "ALTER TABLE videos ADD COLUMN source TEXT",
                   "ALTER TABLE videos ADD COLUMN vs_median REAL", "ALTER TABLE profiles ADD COLUMN ai INTEGER",
                   "ALTER TABLE videos ADD COLUMN direction TEXT", "ALTER TABLE videos ADD COLUMN directions TEXT", "ALTER TABLE videos ADD COLUMN dir_v INTEGER",
                   "ALTER TABLE videos ADD COLUMN host TEXT", "ALTER TABLE videos ADD COLUMN mechanism TEXT", "ALTER TABLE videos ADD COLUMN mechanisms TEXT", "ALTER TABLE videos ADD COLUMN mech_note TEXT", "ALTER TABLE videos ADD COLUMN mech_v INTEGER", "ALTER TABLE videos ADD COLUMN visual TEXT", "ALTER TABLE videos ADD COLUMN visuals TEXT",
                   "ALTER TABLE profiles ADD COLUMN bio TEXT", "ALTER TABLE profiles ADD COLUMN ext_url TEXT"]) {
    try { await env.DB.prepare(a).run(); } catch {}
  }
  // стартовые профили — ИИ-персонажи (метка ставится один раз, дальше переключается во вкладке «Профили»)
  try {
    if (!(await env.DB.prepare("SELECT v FROM kv WHERE k='ai_seeded'").first())) {
      await env.DB.prepare(`UPDATE profiles SET ai=1 WHERE id IN ('ig:farmtruths','ig:mingsanhealing','ig:vega.naturalmed','ig:amish.house1',
        'ig:nonnasofiahealth','ig:shantivaidya.ayurveda','ig:wellnesswithjacob1','ig:mayalin_wellness','ig:grandmatruth','ig:herbalance_lab')`).run();
      await env.DB.prepare("INSERT OR REPLACE INTO kv VALUES ('ai_seeded','1')").run();
    }
  } catch {}
  ensured = true;
}

const parse = (s, d) => { try { return s ? JSON.parse(s) : d; } catch { return d; } };

async function api(req, env, url) {
  const p = url.pathname;

  if (p === "/api/login" && req.method === "POST") {
    const b = await req.json().catch(() => ({}));
    const U = users(env), login = String(b.login || "").trim().toLowerCase(), pass = String(b.password || "");
    const ok = (key, secret) => tokenFor(key, secret).then((tok) => json({ ok: true }, 200, {
      "set-cookie": `radar_auth=${key}.${tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`,
    }));
    if (!Object.keys(U).length) return ok("", "");
    const key = "" in U ? "" : login;
    if (key in U && U[key] === pass) return ok(key, U[key]);
    if (login && /^[a-z0-9._@-]{2,40}$/.test(login)) {
      await ensure(env);
      const u = await env.DB.prepare("SELECT login, salt, hash FROM users WHERE login=?").bind(login).first();
      if (u && (await pbkdf2(pass, u.salt)) === u.hash) {
        await env.DB.prepare("UPDATE users SET last_login=? WHERE login=?").bind(new Date().toISOString(), login).run();
        return ok(login, u.hash);
      }
    }
    await new Promise((r) => setTimeout(r, 1200)); // замедление подбора
    return json({ ok: false, error: "Неверный логин или пароль" }, 401);
  }
  if (p === "/api/logout") {
    return json({ ok: true }, 200, { "set-cookie": "radar_auth=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0" });
  }
  const me = await who(req, env);
  if (!me) return json({ error: "auth" }, 401);
  if (p === "/api/me") return json({ login: me.login, admin: me.admin, open: !!me.open });

  // «Доступ»: управление пользователями (только админы)
  if (p === "/api/users") {
    if (!me.admin) return json({ error: "Только для администратора" }, 403);
    await ensure(env);
    if (req.method === "GET") {
      const r = await env.DB.prepare("SELECT login, role, note, created, last_login FROM users ORDER BY created").all();
      return json({ users: r.results || [], admins: Object.keys(users(env)).filter(Boolean) });
    }
    if (req.method === "POST") {
      const b = await req.json().catch(() => ({}));
      const login = String(b.login || "").trim().toLowerCase(), pass = String(b.password || "");
      const role = b.role === "admin" ? "admin" : "user", note = String(b.note || "").slice(0, 200);
      if (!/^[a-z0-9._@-]{2,40}$/.test(login)) return json({ ok: false, error: "Логин: 2–40 символов, латиница, цифры, . _ - @" }, 400);
      if (login in users(env)) return json({ ok: false, error: "Этот логин задан в секрете GitHub — меняйте его там" }, 400);
      const ex = await env.DB.prepare("SELECT login FROM users WHERE login=?").bind(login).first();
      if (!ex && pass.length < 8) return json({ ok: false, error: "Пароль — минимум 8 символов" }, 400);
      if (pass && pass.length < 8) return json({ ok: false, error: "Пароль — минимум 8 символов" }, 400);
      if (ex) {
        if (pass) {
          const salt = hex(crypto.getRandomValues(new Uint8Array(16)));
          await env.DB.prepare("UPDATE users SET salt=?, hash=?, role=?, note=? WHERE login=?").bind(salt, await pbkdf2(pass, salt), role, note, login).run();
        } else {
          await env.DB.prepare("UPDATE users SET role=?, note=? WHERE login=?").bind(role, note, login).run();
        }
      } else {
        const salt = hex(crypto.getRandomValues(new Uint8Array(16)));
        await env.DB.prepare("INSERT INTO users VALUES (?,?,?,?,?,?,NULL)").bind(login, salt, await pbkdf2(pass, salt), role, note, new Date().toISOString()).run();
      }
      return json({ ok: true });
    }
    if (req.method === "DELETE") {
      const login = String(url.searchParams.get("login") || "").toLowerCase();
      if (login === me.login) return json({ ok: false, error: "Нельзя удалить самого себя" }, 400);
      await env.DB.prepare("DELETE FROM users WHERE login=?").bind(login).run();
      return json({ ok: true });
    }
  }
  await ensure(env);

  // обычный пользователь — только смотрит и размечает; запуски, настройки и профили — админ (это траты денег)
  const adminOnly = (p === "/api/run") || (p === "/api/settings" && req.method === "POST") ||
    (p.startsWith("/api/profiles") && req.method === "POST");
  if (adminOnly && !me.admin) return json({ ok: false, error: "Только для администратора" }, 403);

  if (p === "/api/videos") {
    // scope=main — всё, кроме «не выстрелов» профилей (облегчённый разбор: только поля для списка и фильтров);
    // scope=rest — «не выстрелы» профилей по поиску (q) — догружаются по запросу
    const scope = url.searchParams.get("scope") || "main";
    const q = (url.searchParams.get("q") || "").trim().toLowerCase().replace(/^@+/, "");
    const slim = `json_object('hook_0_3s', json_extract(v.analysis,'$.hook_0_3s'), 'hook_type', json_extract(v.analysis,'$.hook_type'),
        'cta', json_extract(v.analysis,'$.cta'), 'cta_mechanic', json_extract(v.analysis,'$.cta_mechanic'), 'ai_generated', json_extract(v.analysis,'$.ai_generated'),
        'tech', json_extract(v.analysis,'$.tech'))`;
    const cols = `v.id, v.platform, v.vertical, v.url, v.author, v.author_id, substr(v.title,1,600) AS title, v.published, v.duration, v.lang, v.keyword,
        v.views, v.likes, v.comments, v.shares, v.followers, v.outlier, v.status, v.prefilter, v.nutra_score, v.content_type,
        CASE WHEN v.analysis IS NULL THEN NULL ELSE ${slim} END AS analysis, v.first_seen, v.source, v.vs_median, v.direction, v.directions,
        v.host, v.visual, v.visuals, v.mechanism, v.mechanisms, v.mech_note, COALESCE(pr.ai, 0) AS ai, l.v AS label, l.note AS note, l.reason AS reason, COALESCE(l.hidden, 0) AS hidden`;
    const from = "FROM videos v LEFT JOIN labels l ON l.id = v.id LEFT JOIN profiles pr ON pr.id = v.author_id";
    let rows = [], counts = {};
    try {
      if (scope === "rest") {
        const like = "%" + q + "%";
        // фильтр направлений на сервере: иначе редкие темы (суставы) теряются за топ-3000 по просмотрам
        const dirs = String(url.searchParams.get("dirs") || "").split(",").map((x) => x.trim()).filter((x) => /^[a-z_]+$/.test(x)).slice(0, 30);
        const main = url.searchParams.get("main") === "1";
        let dsql = "", binds = [q, like, like];
        if (dirs.length) {
          if (main) { dsql = ` AND v.direction IN (${dirs.map(() => "?").join(",")})`; binds.push(...dirs); }
          else { dsql = " AND (" + dirs.map(() => "(',' || COALESCE(v.directions, v.direction, '') || ',') LIKE ?").join(" OR ") + ")"; binds.push(...dirs.map((d) => `%,${d},%`)); }
        }
        const r = await env.DB.prepare(`SELECT ${cols} ${from} WHERE v.status='profile_rest' AND (?='' OR lower(v.author) LIKE ? OR lower(v.title) LIKE ?)${dsql}
            ORDER BY v.views DESC LIMIT 3000`).bind(...binds).all();
        rows = r.results || [];
      } else {
        const r = await env.DB.prepare(`SELECT ${cols} ${from} WHERE COALESCE(v.status,'') != 'profile_rest' ORDER BY v.views DESC LIMIT 30000`).all();
        rows = r.results || [];
        const c = await env.DB.prepare("SELECT status, COUNT(*) AS n FROM videos GROUP BY status").all();
        for (const x of c.results || []) counts[x.status || ""] = x.n;
      }
    } catch (e) { return json({ videos: [], counts, note: "Таблица videos ещё не создана — запустите сбор" }); }
    for (const x of rows) { x.analysis = parse(x.analysis, null); x.slim = 1; }
    return json({ videos: rows, counts });
  }

  if (p === "/api/video") {
    const id = url.searchParams.get("id") || "";
    const r = await env.DB.prepare("SELECT analysis, subtitles FROM videos WHERE id = ?").bind(id).first();
    return json({ analysis: parse(r && r.analysis, null), subtitles: parse(r && r.subtitles, []) });
  }

  if (p === "/api/snaps") {
    const id = url.searchParams.get("id") || "";
    try {
      const r = await env.DB.prepare("SELECT ts, views, likes, comments, shares FROM snapshots WHERE video_id = ? ORDER BY ts").bind(id).all();
      return json({ snaps: r.results || [] });
    } catch { return json({ snaps: [] }); }
  }

  if (p === "/api/thumb") {
    // обложка TikTok: oEmbed по полной ссылке ролика -> сама картинка, кэш на неделю
    const vid = url.searchParams.get("id") || "";
    const isIG = /^ig_[A-Za-z0-9_-]+$/.test(vid);
    const id = vid.replace(/^(tt|ig)_/, "");
    if (!isIG && !/^tt_\d+$/.test(vid)) return new Response("bad id", { status: 400 });
    const cache = caches.default, key = new Request(`https://thumb.cache/${isIG ? "ig" : "tt2"}/${id}`);
    const hit = await cache.match(key);
    if (hit) return hit;
    try {
      const t = await env.DB.prepare("SELECT mime, data FROM thumbs WHERE id=?").bind(vid).first();
      if (t && t.data) {
        const bin = Uint8Array.from(atob(t.data), (ch) => ch.charCodeAt(0));
        const res = new Response(bin, { headers: { "content-type": t.mime || "image/jpeg", "cache-control": "public, max-age=604800" } });
        await cache.put(key, res.clone());
        return res;
      }
    } catch {}
    if (isIG) return new Response("no thumb", { status: 404, headers: { "cache-control": "no-store" } });
    let pageUrl = `https://www.tiktok.com/video/${id}`;
    try {
      const row = await env.DB.prepare("SELECT url, author FROM videos WHERE id=?").bind(vid).first();
      if (row && row.url && row.url.includes("/video/")) pageUrl = row.url.split("?")[0];
      else if (row && row.author) pageUrl = `https://www.tiktok.com/@${row.author}/video/${id}`;
    } catch {}
    const ua = { "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36" };
    const r = await fetch(`https://www.tiktok.com/oembed?url=${encodeURIComponent(pageUrl)}`, { headers: ua });
    const j = r.ok ? await r.json().catch(() => null) : null;
    if (!j || !j.thumbnail_url) return new Response("no thumb", { status: 404, headers: { "cache-control": "no-store" } });
    const img = await fetch(j.thumbnail_url, { headers: ua });
    if (!img.ok) return new Response(null, { status: 302, headers: { location: j.thumbnail_url, "cache-control": "no-store" } });
    const res = new Response(img.body, { headers: { "content-type": img.headers.get("content-type") || "image/jpeg", "cache-control": "public, max-age=604800" } });
    await cache.put(key, res.clone());
    return res;
  }

  if (p === "/api/stats") {
    const r = await env.DB.prepare("SELECT (SELECT COUNT(*) FROM videos) AS videos, (SELECT COUNT(*) FROM thumbs) AS thumbs").first().catch(() => ({}));
    const m = await env.DB.prepare("SELECT 1").run();
    return json({ size: m.meta && m.meta.size_after, ...r });
  }

  if (p === "/api/authors") {
    try {
      const r = await env.DB.prepare("SELECT * FROM authors ORDER BY relevant_shots DESC, max_views DESC LIMIT 1000").all();
      return json({ authors: r.results || [] });
    } catch { return json({ authors: [] }); }
  }

  if (p === "/api/runs") {
    try {
      const r = await env.DB.prepare("SELECT ts, found, new, analyzed, cost_usd, substr(log, -3000) AS log FROM runs ORDER BY ts DESC LIMIT 20").all();
      return json({ runs: r.results || [] });
    } catch { return json({ runs: [] }); }
  }

  if (p === "/api/label" && req.method === "POST") {
    const { id, v, note, reason } = await req.json();
    if (!id) return json({ error: "id" }, 400);
    await env.DB.prepare(
      `INSERT INTO labels (id, v, note, reason, ts) VALUES (?1, ?2, ?3, ?5, ?4)
       ON CONFLICT(id) DO UPDATE SET v = COALESCE(?2, labels.v), note = COALESCE(?3, labels.note), reason = COALESCE(?5, labels.reason), ts = ?4`)
      .bind(id, v ?? null, note ?? null, new Date().toISOString(), reason ?? null).run();
    return json({ ok: true });
  }

  if (p === "/api/hide" && req.method === "POST") {
    const { id, hidden } = await req.json();
    await env.DB.prepare(
      `INSERT INTO labels (id, hidden, ts) VALUES (?1, ?2, ?3)
       ON CONFLICT(id) DO UPDATE SET hidden = ?2, ts = ?3`)
      .bind(id, hidden ? 1 : 0, new Date().toISOString()).run();
    return json({ ok: true });
  }

  if (p === "/api/settings") {
    if (req.method === "POST") {
      const body = await req.json();
      if (!body || !body.verticals) return json({ error: "Нет вертикалей" }, 400);
      await env.DB.prepare("INSERT OR REPLACE INTO kv (k, v) VALUES ('settings', ?1)").bind(JSON.stringify(body)).run();
      return json({ ok: true });
    }
    const r = await env.DB.prepare("SELECT v FROM kv WHERE k = 'settings'").first();
    return json({ settings: parse(r && r.v, null) });
  }

  if (p === "/api/profiles" && req.method === "GET") {
    const r = await env.DB.prepare("SELECT * FROM profiles ORDER BY added").all();
    const prof = r.results || [];
    // частота постинга: роликов в неделю за последние 90 дней (по дате публикации)
    try {
      const f = await env.DB.prepare(`SELECT author_id, COUNT(*) AS n, MIN(published) AS first FROM videos
          WHERE source='profile' AND published >= date('now','-90 day') GROUP BY author_id`).all();
      const m = Object.fromEntries((f.results || []).map((x) => [x.author_id, x]));
      for (const p of prof) { const x = m[p.id]; p.per_week = x ? Math.round((x.n / (90 / 7)) * 10) / 10 : 0; }
    } catch {}
    try {
      const fu = await env.DB.prepare("SELECT profile_id, final_url, status, data, ts FROM funnels").all();
      const m = Object.fromEntries((fu.results || []).map((x) => [x.profile_id, { ...x, data: parse(x.data, {}) }]));
      for (const p of prof) p.funnel = m[p.id] || null;
    } catch {}
    return json({ profiles: prof });
  }

  if (p === "/api/profiles" && req.method === "POST") {
    // добавить: { input: "ссылка или ник" } или { platform, handle }
    const b = await req.json().catch(() => ({}));
    let platform = b.platform, handle = (b.handle || "").trim();
    const s = (b.input || "").trim();
    if (s) {
      let m;
      if ((m = s.match(/instagram\.com\/([A-Za-z0-9._]+)/i))) { platform = "Instagram"; handle = m[1]; }
      else if ((m = s.match(/tiktok\.com\/@([A-Za-z0-9._]+)/i))) { platform = "TikTok"; handle = m[1]; }
      else if ((m = s.match(/youtube\.com\/(?:channel\/)?(UC[A-Za-z0-9_-]{22})/i))) { platform = "YouTube"; handle = m[1]; }
      else if ((m = s.match(/youtube\.com\/@([^/?#\s]+)/i))) { platform = "YouTube"; handle = decodeURIComponent(m[1]); }
      else if ((m = s.match(/^(instagram|insta|ig|tiktok|tt|youtube|yt)\s+@?(.+)$/i))) {
        platform = { instagram: "Instagram", insta: "Instagram", ig: "Instagram", tiktok: "TikTok", tt: "TikTok", youtube: "YouTube", yt: "YouTube" }[m[1].toLowerCase()];
        handle = m[2].trim();
      } else { platform = "Instagram"; handle = s; }
    }
    handle = handle.replace(/^@/, "").replace(/\/+$/, "");
    if (!["Instagram", "TikTok", "YouTube"].includes(platform) || !handle || /\s/.test(handle) && platform !== "YouTube")
      return json({ ok: false, error: "Не понял ссылку. Пример: instagram.com/farmtruths или tiktok.com/@user или youtube.com/@channel" }, 400);
    const pid = `${{ Instagram: "ig", TikTok: "tt", YouTube: "yt" }[platform]}:${handle.toLowerCase()}`;
    const purl = platform === "Instagram" ? `https://www.instagram.com/${handle}/` : platform === "TikTok" ? `https://www.tiktok.com/@${handle}`
      : handle.startsWith("UC") ? `https://www.youtube.com/channel/${handle}` : `https://www.youtube.com/@${encodeURIComponent(handle)}`;
    const ex = await env.DB.prepare("SELECT id, active FROM profiles WHERE id=?").bind(pid).first();
    if (ex) {
      await env.DB.prepare("UPDATE profiles SET active=1 WHERE id=?").bind(pid).run();
      return json({ ok: true, id: pid, existed: true });
    }
    await env.DB.prepare("INSERT INTO profiles (id, platform, handle, name, url, added, active, status) VALUES (?,?,?,?,?,?,1,?)")
      .bind(pid, platform, handle, b.name || null, purl, new Date().toISOString().slice(0, 19), "ждёт первого сбора").run();
    return json({ ok: true, id: pid });
  }

  if (p === "/api/profiles/ai" && req.method === "POST") {
    const { id, ai } = await req.json().catch(() => ({}));
    await env.DB.prepare("UPDATE profiles SET ai=? WHERE id=?").bind(ai ? 1 : 0, id).run();
    return json({ ok: true });
  }

  if (p === "/api/profiles/delete" && req.method === "POST") {
    // удалить из слежки; ролики остаются в базе (если не попросили удалить и их)
    const { id, withVideos } = await req.json().catch(() => ({}));
    if (!id) return json({ ok: false, error: "id" }, 400);
    await env.DB.prepare("DELETE FROM profiles WHERE id=?").bind(id).run();
    if (withVideos) {
      await env.DB.prepare("DELETE FROM thumbs WHERE id IN (SELECT id FROM videos WHERE author_id=? AND source='profile')").bind(id).run();
      await env.DB.prepare("DELETE FROM videos WHERE author_id=? AND source='profile'").bind(id).run();
    }
    return json({ ok: true });
  }

  if (p === "/api/run" && req.method === "POST") {
    if (!env.GH_TOKEN || !env.GH_REPO) return json({ ok: false, error: "Не настроен GH_TOKEN / GH_REPO" }, 400);
    const opts = await req.json().catch(() => ({}));
    const mode = ["full", "profiles", "funnels"].includes(opts.mode) ? opts.mode : "analyze";
    const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/radar.yml/dispatches`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "radar-worker" },
      body: JSON.stringify({ ref: env.GH_BRANCH || "main", inputs: { mode, max_analyze: String(opts.max_analyze || "") } }),
    });
    if (r.status === 204) return json({ ok: true });
    return json({ ok: false, error: `GitHub ответил ${r.status}: ${(await r.text()).slice(0, 200)}` }, 400);
  }

  return json({ error: "not found" }, 404);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname.startsWith("/api/")) {
      try { return await api(req, env, url); }
      catch (e) { return json({ error: String(e && e.message || e) }, 500); }
    }
    return env.ASSETS.fetch(req);
  },
};
