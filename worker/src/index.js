// Радар — API + раздача интерфейса (Cloudflare Worker + D1 + static assets)

const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, v TEXT, note TEXT, hidden INTEGER DEFAULT 0, ts TEXT)",
  "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)",
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

async function ensure(env) {
  for (const s of SCHEMA) await env.DB.prepare(s).run();
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
                v.content_type, v.analysis, v.subtitles, v.first_seen, l.v AS label, l.note AS note, COALESCE(l.hidden, 0) AS hidden
         FROM videos v LEFT JOIN labels l ON l.id = v.id ORDER BY v.views DESC LIMIT 3000`).all();
      rows = r.results || [];
    } catch (e) { return json({ videos: [], note: "Таблица videos ещё не создана — запустите сбор" }); }
    for (const x of rows) { x.analysis = parse(x.analysis, null); x.subtitles = parse(x.subtitles, []); }
    return json({ videos: rows });
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
    const { id, v, note } = await req.json();
    if (!id) return json({ error: "id" }, 400);
    await env.DB.prepare(
      `INSERT INTO labels (id, v, note, ts) VALUES (?1, ?2, ?3, ?4)
       ON CONFLICT(id) DO UPDATE SET v = COALESCE(?2, labels.v), note = COALESCE(?3, labels.note), ts = ?4`)
      .bind(id, v ?? null, note ?? null, new Date().toISOString()).run();
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

  if (p === "/api/run" && req.method === "POST") {
    if (!env.GH_TOKEN || !env.GH_REPO) return json({ ok: false, error: "Не настроен GH_TOKEN / GH_REPO" }, 400);
    const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/radar.yml/dispatches`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "radar-worker" },
      body: JSON.stringify({ ref: env.GH_BRANCH || "main" }),
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
