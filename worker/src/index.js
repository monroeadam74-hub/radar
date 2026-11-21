// Радар — API + раздача интерфейса (Cloudflare Worker + D1 + static assets)

const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, v TEXT, note TEXT, hidden INTEGER DEFAULT 0, ts TEXT)",
  "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
  "CREATE TABLE IF NOT EXISTS thumbs (id TEXT PRIMARY KEY, mime TEXT, data TEXT, ts TEXT)",
  `CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, platform TEXT, handle TEXT, name TEXT, url TEXT, followers INTEGER,
     reels INTEGER, median_views INTEGER, best_x REAL, shots INTEGER, new_last INTEGER,
     status TEXT, added TEXT, last_run TEXT, active INTEGER DEFAULT 1)`,
];

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", ...headers } });

async function sha(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

async function authed(req, env) {
  if (!env.APP_PASSWORD) return true; // пароль не задан — открыто (только для первой проверки)
  const cookie = req.headers.get("cookie") || "";
  const m = cookie.match(/radar_auth=([a-f0-9]{64})/);
  return !!m && m[1] === (await sha(env.APP_PASSWORD + "|radar"));
}

let ensured = false;
async function ensure(env) {
  if (ensured) return;
  for (const s of SCHEMA) await env.DB.prepare(s).run();
  for (const a of ["ALTER TABLE labels ADD COLUMN reason TEXT", "ALTER TABLE videos ADD COLUMN source TEXT",
                   "ALTER TABLE videos ADD COLUMN vs_median REAL", "ALTER TABLE profiles ADD COLUMN ai INTEGER",
                   "ALTER TABLE videos ADD COLUMN direction TEXT", "ALTER TABLE videos ADD COLUMN directions TEXT"]) {
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
    const { password } = await req.json().catch(() => ({}));
    if (!env.APP_PASSWORD || password === env.APP_PASSWORD) {
      const tok = await sha((env.APP_PASSWORD || "") + "|radar");
      return json({ ok: true }, 200, {
        "set-cookie": `radar_auth=${tok}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`,
      });
    }
    return json({ ok: false, error: "Неверный пароль" }, 401);
  }
  if (!(await authed(req, env))) return json({ error: "auth" }, 401);
  await ensure(env);

  if (p === "/api/videos") {
    let rows = [];
    try {
      const r = await env.DB.prepare(
        `SELECT v.id, v.platform, v.vertical, v.url, v.author, v.author_id, v.title, v.published, v.duration, v.lang, v.keyword,
                v.views, v.likes, v.comments, v.shares, v.followers, v.outlier, v.status, v.prefilter, v.nutra_score,
                v.content_type, v.analysis, v.subtitles, v.first_seen, v.source, v.vs_median, v.direction, v.directions, COALESCE(pr.ai, 0) AS ai, l.v AS label, l.note AS note, l.reason AS reason, COALESCE(l.hidden, 0) AS hidden
         FROM videos v LEFT JOIN labels l ON l.id = v.id LEFT JOIN profiles pr ON pr.id = v.author_id ORDER BY v.views DESC LIMIT 3000`).all();
      rows = r.results || [];
    } catch (e) { return json({ videos: [], note: "Таблица videos ещё не создана — запустите сбор" }); }
    for (const x of rows) { x.analysis = parse(x.analysis, null); x.subtitles = parse(x.subtitles, []); }
    return json({ videos: rows });
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
    return json({ profiles: r.results || [] });
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
    const mode = ["full", "profiles"].includes(opts.mode) ? opts.mode : "analyze";
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
