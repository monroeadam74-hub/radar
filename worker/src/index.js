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
  "CREATE TABLE IF NOT EXISTS apikeys (id TEXT PRIMARY KEY, name TEXT, hash TEXT, created TEXT, last_used TEXT, uses INTEGER DEFAULT 0)",
  "CREATE TABLE IF NOT EXISTS mcplog (ts TEXT, key_id TEXT, http TEXT, rpc TEXT, tool TEXT, status INTEGER, ms INTEGER, ua TEXT, accept TEXT, proto TEXT, sess TEXT, err TEXT)",
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


// ===== Публичный API только на чтение (для ChatGPT и других ИИ): ключ в заголовке Authorization: Bearer <ключ> =====
const HIDDEN_TABLES = /\b(users|apikeys|mcplog|thumbs|sqlite_sequence|_cf_kv|d1_migrations)\b/i;
const WRITE_WORDS = /\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|analyze|begin|commit|rollback|savepoint|release|load_extension)\b/i;
const MAX_CHARS = 90000;

function fit(rows, extra = {}) {   // ответ ChatGPT должен быть компактным: режем длинные строки и хвост списка
  const cut = (v) => (typeof v === "string" && v.length > 3000 ? v.slice(0, 3000) + "…[обрезано]" : v);
  let out = rows.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, cut(v)])));
  let body = JSON.stringify({ rows: out, count: out.length, ...extra });
  let truncated = false;
  while (body.length > MAX_CHARS && out.length > 1) {
    out = out.slice(0, Math.floor(out.length * 0.7)); truncated = true;
    body = JSON.stringify({ rows: out, count: out.length, truncated, ...extra });
  }
  return json(truncated ? { rows: out, count: out.length, truncated: true, hint: "Ответ обрезан по размеру: уменьшите limit или выберите меньше колонок", ...extra } : { rows: out, count: out.length, ...extra });
}

async function apiKeyOk(req, env) {
  const m = (req.headers.get("authorization") || "").match(/^Bearer\s+([A-Za-z0-9_-]{20,})$/);
  if (!m) return false;
  await ensure(env);
  const h = await sha(m[1]);
  const k = await env.DB.prepare("SELECT id FROM apikeys WHERE hash=?").bind(h).first();
  if (!k) return false;
  await env.DB.prepare("UPDATE apikeys SET last_used=?, uses=COALESCE(uses,0)+1 WHERE id=?").bind(new Date().toISOString(), k.id).run();
  return true;
}

function openapi(origin) {
  const q = (name, d, type = "string", extra = {}) => ({ name, in: "query", required: false, description: d, schema: { type, ...extra } });
  return {
    openapi: "3.1.0",
    info: { title: "Радар коротких видео (только чтение)", version: "1.0.0",
      description: "Живая база радара: рилсы Instagram врачей и блогеров о здоровье, их метрики и разбор ИИ. " +
        "«Выстрел» = ролик с ≥100 тыс. просмотров и кратностью vs_median ≥2 к медиане автора (status='analyzed'); остальные ролики — status='profile_rest'. " +
        "Сначала вызовите getSchema, чтобы увидеть таблицы и колонки; для любых срезов используйте runQuery (SQLite, только SELECT)." },
    servers: [{ url: origin }],
    paths: {
      "/api/v1/schema": { get: { operationId: "getSchema", summary: "Таблицы, колонки и пояснения к полям", responses: { "200": { description: "ok" } } } },
      "/api/v1/stats": { get: { operationId: "getStats", summary: "Сводка: ролики по статусам, направления × механизмы, аккаунты", responses: { "200": { description: "ok" } } } },
      "/api/v1/videos": { get: { operationId: "listVideos", summary: "Ролики с фильтрами (компактно, без полного разбора)",
        parameters: [q("status", "shot (выстрелы, по умолчанию) | rest (не выстрелы) | all"), q("direction", "направление, например knees, back, weight"),
          q("mechanism", "механизм решения, например exercise, supplement, selfmassage"), q("author", "ник автора без @"),
          q("q", "поиск по нику и подписи"), q("min_views", "минимум просмотров", "integer"), q("min_x", "минимальная кратность к медиане автора", "number"),
          q("since", "дата публикации от, YYYY-MM-DD"), q("sort", "views | x | published | er (по умолчанию x)"),
          q("limit", "до 200, по умолчанию 50", "integer"), q("offset", "сдвиг для следующей страницы", "integer")],
        responses: { "200": { description: "ok" } } } },
      "/api/v1/video": { get: { operationId: "getVideo", summary: "Один ролик целиком: метрики, полный разбор ИИ, субтитры с переводом",
        parameters: [{ name: "id", in: "query", required: true, description: "id ролика, например ig_DdZ_864kssT", schema: { type: "string" } }],
        responses: { "200": { description: "ok" } } } },
      "/api/v1/query": { post: { operationId: "runQuery", summary: "Произвольный SQL-запрос SELECT к базе (SQLite / Cloudflare D1). Только чтение, до 500 строк",
        requestBody: { required: true, content: { "application/json": { schema: { type: "object", required: ["sql"],
          properties: { sql: { type: "string", description: "Один запрос SELECT или WITH … SELECT. Для полей JSON используйте json_extract(analysis,'$.hook_type')" },
            params: { type: "array", items: {}, description: "значения для ? в запросе" } } } } } },
        responses: { "200": { description: "ok" } } } },
    },
    components: { schemas: {}, securitySchemes: { bearer: { type: "http", scheme: "bearer" } } },
    security: [{ bearer: [] }],
  };
}

const FIELD_NOTES = {
  videos: {
    id: "ig_<shortcode>", url: "ссылка на ролик", author: "ник автора", author_id: "ig:<ник>, связь с profiles.id",
    views: "просмотры", likes: "лайки", comments: "комментарии", shares: "репосты (не всегда есть)",
    vs_median: "кратность к скользящей медиане просмотров автора (×)", status: "analyzed = выстрел с разбором; queued = выстрел в очереди; profile_rest = не выстрел",
    direction: "направление здоровья (knees, back, weight, potency, sleep, …)", mechanism: "основной механизм решения (exercise, supplement, food, drink, topical, selfmassage, habit, drug, clinic, device, visit, folk, info, nothealth, none, other)",
    mechanisms: "все механизмы через запятую", analysis: "JSON полного разбора ИИ: hook_type, format, content_type, host, visuals, cta, cta_mechanic, emotion, why_it_works, how_to_adapt, nutra_score, moderation_risk, target_audience, language, ai_generated, on_screen_text…",
    subtitles: "JSON: [{start,end,orig,ru}] — речь с переводом", published: "дата публикации (UTC)", duration: "длительность, сек", source: "profile = собран с аккаунта",
  },
  profiles: { id: "ig:<ник>", followers: "подписчики", reels: "собрано рилсов", median_views: "медиана просмотров", shots: "выстрелов", wave: "дата волны сбора", ai: "1 = ИИ-аккаунт" },
};

async function v1(req, env, url) {
  const p = url.pathname;
  if (p === "/api/v1/openapi.json") return json(openapi(url.origin));
  if (!(await apiKeyOk(req, env))) return json({ error: "Нужен ключ: заголовок Authorization: Bearer <ключ>" }, 401);

  if (p === "/api/v1/schema") {
    const t = await env.DB.prepare("SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").all();
    const tables = (t.results || []).filter((x) => !HIDDEN_TABLES.test(x.name)).map((x) => ({ name: x.name, sql: x.sql, notes: FIELD_NOTES[x.name] }));
    return json({ tables, tips: ["Выстрелы: WHERE status='analyzed'", "Поля разбора: json_extract(analysis,'$.hook_type')", "Ответ ограничен 500 строками и ~90 тыс. символов — агрегируйте в SQL"] });
  }
  if (p === "/api/v1/stats") {
    const st = await env.DB.prepare("SELECT status, COUNT(*) AS n FROM videos GROUP BY status").all();
    const dm = await env.DB.prepare(`SELECT direction, mechanism, COUNT(*) AS n, CAST(AVG(vs_median) AS REAL) AS avg_x
        FROM videos WHERE status='analyzed' GROUP BY direction, mechanism ORDER BY n DESC LIMIT 300`).all();
    const pr = await env.DB.prepare("SELECT COUNT(*) AS profiles, SUM(COALESCE(ai,0)) AS ai_profiles FROM profiles").first();
    return json({ by_status: st.results, direction_x_mechanism: dm.results, profiles: pr });
  }
  if (p === "/api/v1/videos") {
    const g = (k) => (url.searchParams.get(k) || "").trim();
    const where = [], b = [];
    const status = g("status") || "shot";
    if (status === "shot") where.push("status='analyzed'"); else if (status === "rest") where.push("status='profile_rest'");
    if (g("direction")) { where.push("direction=?"); b.push(g("direction")); }
    if (g("mechanism")) { where.push("(mechanism=? OR ','||mechanisms||',' LIKE ?)"); b.push(g("mechanism"), "%," + g("mechanism") + ",%"); }
    if (g("author")) { where.push("lower(author)=?"); b.push(g("author").replace(/^@/, "").toLowerCase()); }
    if (g("q")) { where.push("(lower(author) LIKE ? OR lower(title) LIKE ?)"); b.push("%" + g("q").toLowerCase() + "%", "%" + g("q").toLowerCase() + "%"); }
    if (g("min_views")) { where.push("views>=?"); b.push(+g("min_views")); }
    if (g("min_x")) { where.push("vs_median>=?"); b.push(+g("min_x")); }
    if (g("since")) { where.push("published>=?"); b.push(g("since")); }
    const order = { views: "views DESC", published: "published DESC", er: "(COALESCE(likes,0)+COALESCE(comments,0))*1.0/NULLIF(views,0) DESC" }[g("sort")] || "vs_median DESC";
    const limit = Math.min(200, Math.max(1, +g("limit") || 50)), offset = Math.max(0, +g("offset") || 0);
    const sql = `SELECT id, url, author, views, likes, comments, shares, vs_median, duration, published, direction, mechanism, status,
        json_extract(analysis,'$.hook_type') AS hook_type, json_extract(analysis,'$.format') AS format, json_extract(analysis,'$.host') AS host,
        json_extract(analysis,'$.cta_mechanic') AS cta, json_extract(analysis,'$.language') AS language, substr(title,1,200) AS caption
        FROM videos ${where.length ? "WHERE " + where.join(" AND ") : ""} ORDER BY ${order} LIMIT ${limit} OFFSET ${offset}`;
    const r = await env.DB.prepare(sql).bind(...b).all();
    return fit(r.results || [], { next_offset: offset + (r.results || []).length });
  }
  if (p === "/api/v1/video") {
    const r = await env.DB.prepare("SELECT * FROM videos WHERE id=?").bind(url.searchParams.get("id") || "").first();
    if (!r) return json({ error: "не найден" }, 404);
    r.analysis = parse(r.analysis, r.analysis); r.subtitles = parse(r.subtitles, r.subtitles);
    return json(r);
  }
  if (p === "/api/v1/query" && req.method === "POST") {
    const body = await req.json().catch(() => ({}));
    let sql = String(body.sql || "").replace(/--[^\n]*/g, " ").replace(/\/\*[\s\S]*?\*\//g, " ").trim().replace(/;\s*$/, "");
    if (!/^(select|with)\b/i.test(sql)) return json({ error: "Разрешён только SELECT (или WITH … SELECT)" }, 400);
    if (sql.includes(";")) return json({ error: "Только один запрос за раз" }, 400);
    if (WRITE_WORDS.test(sql)) return json({ error: "Запрос похож на изменение данных — запрещено, доступ только на чтение" }, 400);
    if (HIDDEN_TABLES.test(sql)) return json({ error: "Эта таблица закрыта" }, 400);
    const wrapped = `SELECT * FROM (${sql}) LIMIT 500`;
    const r = await env.DB.prepare(wrapped).bind(...(Array.isArray(body.params) ? body.params : [])).all();
    return fit(r.results || []);
  }
  return json({ error: "not found" }, 404);
}

// ===== MCP-сервер для ChatGPT (developer mode / плагины): адрес /mcp/<ключ>, только чтение =====
const MCP_TOOLS = [
  { name: "get_schema", title: "Схема базы", description: "Таблицы, колонки и пояснения к полям базы радара. Вызывайте первым.",
    inputSchema: { type: "object", properties: {} } },
  { name: "get_stats", title: "Сводка", description: "Ролики по статусам, направления × механизмы, число аккаунтов.",
    inputSchema: { type: "object", properties: {} } },
  { name: "list_videos", title: "Ролики с фильтрами", description: "Компактный список роликов. «Выстрел» = status shot (≥100 тыс. просмотров и ≥2× медианы автора).",
    inputSchema: { type: "object", properties: {
      status: { type: "string", description: "shot (по умолчанию) | rest | all" }, direction: { type: "string", description: "knees, back, weight, potency…" },
      mechanism: { type: "string", description: "exercise, supplement, selfmassage, food…" }, author: { type: "string" }, q: { type: "string", description: "поиск по нику и подписи" },
      min_views: { type: "integer" }, min_x: { type: "number" }, since: { type: "string", description: "YYYY-MM-DD" },
      sort: { type: "string", description: "x | views | published | er" }, limit: { type: "integer", description: "до 200" }, offset: { type: "integer" } } } },
  { name: "get_video", title: "Ролик целиком", description: "Все поля ролика: метрики, полный разбор ИИ, субтитры с переводом.",
    inputSchema: { type: "object", properties: { id: { type: "string", description: "например ig_DdZ_864kssT" } }, required: ["id"] } },
  { name: "run_query", title: "SQL-запрос", description: "Один запрос SELECT (SQLite / D1) к базе, только чтение, до 500 строк. Поля разбора: json_extract(analysis,'$.hook_type'). Агрегируйте в SQL.",
    inputSchema: { type: "object", properties: { sql: { type: "string" }, params: { type: "array", items: {} } }, required: ["sql"] } },
  { name: "search", title: "Поиск роликов", description: "Поиск выстрелов по нику автора или подписи; возвращает id для fetch.",
    inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } },
  { name: "fetch", title: "Открыть ролик", description: "Полные данные ролика по id из search.",
    inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"] } },
].map((t) => ({ ...t, annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false } }));

const MCP_CORS = { "access-control-allow-origin": "*", "access-control-allow-methods": "POST, GET, DELETE, OPTIONS",
  "access-control-allow-headers": "content-type, accept, authorization, mcp-session-id, mcp-protocol-version, last-event-id",
  "access-control-expose-headers": "mcp-session-id" };
async function mcp(req, env, url, key) {
  const t0 = Date.now();
  let rpc = "", tool = "", err = "";
  try { if (req.method === "POST") { const b = await req.clone().json(); const one = Array.isArray(b) ? b[0] : b;
    rpc = (Array.isArray(b) ? "batch:" : "") + String(one && one.method || ""); tool = String(one && one.params && one.params.name || ""); } } catch (e) { err = "bad json"; }
  let r;
  try { r = await mcpInner(req, env, url, key); } catch (e) { err = String(e && e.message || e).slice(0, 200); r = json({ jsonrpc: "2.0", id: null, error: { code: -32603, message: err } }, 500); }
  try {
    await ensure(env);
    if (r.status >= 400 && !err) err = (await r.clone().text()).slice(0, 200);
    await env.DB.prepare("INSERT INTO mcplog VALUES (?,?,?,?,?,?,?,?,?,?,?,?)").bind(new Date().toISOString(), key.slice(4, 12), req.method, rpc, tool, r.status,
      Date.now() - t0, (req.headers.get("user-agent") || "").slice(0, 120), (req.headers.get("accept") || "").slice(0, 80),
      req.headers.get("mcp-protocol-version") || "", req.headers.get("mcp-session-id") ? "yes" : "", err).run();
    await env.DB.prepare("DELETE FROM mcplog WHERE ts < ?").bind(new Date(Date.now() - 7 * 864e5).toISOString()).run();
  } catch {}
  const h = new Headers(r.headers);
  for (const [k, v] of Object.entries(MCP_CORS)) h.set(k, v);
  return new Response(r.body, { status: r.status, headers: h });
}
async function mcpInner(req, env, url, key) {
  if (req.method === "OPTIONS") return new Response(null, { status: 204 });
  if (req.method === "HEAD") return new Response(null, { status: 200 });
  if (req.method === "GET") return new Response("MCP: используйте POST", { status: 405, headers: { allow: "POST" } });
  if (req.method === "DELETE") return new Response(null, { status: 204 });
  if (req.method !== "POST") return new Response(null, { status: 405 });
  const fake = new Request(url.origin + "/api/v1/stats", { headers: { authorization: "Bearer " + key } });
  if (!(await apiKeyOk(fake, env))) return json({ jsonrpc: "2.0", id: null, error: { code: -32001, message: "Неверный или отозванный ключ" } }, 401);
  const call = async (path, params = {}, body) => {
    const u = new URL(url.origin + path);
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, String(v));
    const r = await v1(new Request(u, { method: body ? "POST" : "GET", headers: { authorization: "Bearer " + key, "content-type": "application/json" },
      body: body ? JSON.stringify(body) : undefined }), env, u);
    return r.text();
  };
  const one = async (m) => {
    const id = m.id ?? null;
    if (m.method === "initialize")
      return { jsonrpc: "2.0", id, result: { protocolVersion: (m.params && m.params.protocolVersion) || "2025-06-18",
        capabilities: { tools: { listChanged: false } }, serverInfo: { name: "radar", title: "Радар коротких видео", version: "1.0.0" },
        instructions: "База радара: рилсы врачей и блогеров о здоровье с разбором ИИ. Начните с get_schema. Для подсчётов — run_query (только SELECT), агрегируйте в SQL. Выстрелы: status='analyzed'." } };
    if (m.method === "ping") return { jsonrpc: "2.0", id, result: {} };
    if (m.method === "tools/list") return { jsonrpc: "2.0", id, result: { tools: MCP_TOOLS } };
    if (m.method === "resources/list") return { jsonrpc: "2.0", id, result: { resources: [] } };
    if (m.method === "prompts/list") return { jsonrpc: "2.0", id, result: { prompts: [] } };
    if (m.method === "tools/call") {
      const n = m.params && m.params.name, a = (m.params && m.params.arguments) || {};
      let text;
      try {
        if (n === "get_schema") text = await call("/api/v1/schema");
        else if (n === "get_stats") text = await call("/api/v1/stats");
        else if (n === "list_videos") text = await call("/api/v1/videos", a);
        else if (n === "get_video" || n === "fetch") {
          const raw = await call("/api/v1/video", { id: a.id });
          if (n === "fetch") { const v = parse(raw, {}); text = JSON.stringify({ id: a.id, title: (v.author ? "@" + v.author + ": " : "") + (v.title || a.id), text: raw, url: v.url || "", metadata: { views: v.views, x: v.vs_median } }); }
          else text = raw;
        }
        else if (n === "run_query") text = await call("/api/v1/query", {}, { sql: a.sql, params: a.params });
        else if (n === "search") {
          const raw = parse(await call("/api/v1/videos", { status: "shot", q: a.query, limit: 30 }), {});
          text = JSON.stringify({ results: (raw.rows || []).map((r) => ({ id: r.id, title: "@" + r.author + " — " + (r.caption || "").slice(0, 80) + " (" + r.views + " просм., ×" + r.vs_median + ")", url: r.url })) });
        }
        else return { jsonrpc: "2.0", id, error: { code: -32602, message: "Нет такого инструмента: " + n } };
      } catch (e) { return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: "Ошибка: " + String(e && e.message || e) }], isError: true } }; }
      const err = /^\{"error"/.test(text);
      return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text }], isError: err } };
    }
    if (m.id === undefined || m.id === null) return null;   // уведомления (notifications/*) — без ответа
    return { jsonrpc: "2.0", id, error: { code: -32601, message: "Метод не поддерживается: " + m.method } };
  };
  const body = await req.json().catch(() => null);
  if (!body) return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, 400);
  if (Array.isArray(body)) {
    const out = (await Promise.all(body.map(one))).filter(Boolean);
    return out.length ? json(out) : new Response(null, { status: 202 });
  }
  const r = await one(body);
  if (r && body.method === "initialize") return json(r, 200, { "mcp-session-id": crypto.randomUUID() });
  return r ? json(r) : new Response(null, { status: 202 });
}

async function api(req, env, url) {
  const p = url.pathname;
  if (p.startsWith("/api/v1/")) return v1(req, env, url);

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

  // «Доступ»: ключи API для ИИ (только админы). Ключ показывается один раз; в базе — только хеш
  if (p === "/api/apikeys") {
    if (!me.admin) return json({ error: "Только для администратора" }, 403);
    await ensure(env);
    if (req.method === "GET") {
      const r = await env.DB.prepare("SELECT id, name, created, last_used, uses FROM apikeys ORDER BY created").all();
      return json({ keys: r.results || [] });
    }
    if (req.method === "POST") {
      const b = await req.json().catch(() => ({}));
      const name = String(b.name || "ИИ").slice(0, 60);
      const raw = [...crypto.getRandomValues(new Uint8Array(24))].map((x) => x.toString(16).padStart(2, "0")).join("");
      const key = "rdr_" + raw, id = raw.slice(0, 8);
      await env.DB.prepare("INSERT INTO apikeys (id, name, hash, created, uses) VALUES (?,?,?,?,0)").bind(id, name, await sha(key), new Date().toISOString()).run();
      return json({ ok: true, id, key });
    }
    if (req.method === "DELETE") {
      await env.DB.prepare("DELETE FROM apikeys WHERE id=?").bind(url.searchParams.get("id") || "").run();
      return json({ ok: true });
    }
  }

  if (p === "/api/mcplog") {
    if (!me.admin) return json({ error: "Только для администратора" }, 403);
    await ensure(env);
    const r = await env.DB.prepare("SELECT * FROM mcplog ORDER BY ts DESC LIMIT 100").all();
    return json({ log: r.results || [] });
  }

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
            ORDER BY v.views DESC LIMIT 3000 OFFSET ?`).bind(...binds, Math.max(0, parseInt(url.searchParams.get("offset") || "0") || 0)).all();
        const tot = await env.DB.prepare(`SELECT COUNT(*) AS n FROM videos v WHERE v.status='profile_rest' AND (?='' OR lower(v.author) LIKE ? OR lower(v.title) LIKE ?)${dsql}`).bind(...binds).first();
        rows = r.results || [];
        counts.rest_total = tot ? tot.n : rows.length;
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
    const mk = url.pathname.match(/^\/mcp\/(rdr_[a-f0-9]{48})\/?$/);
    if (mk) {
      try { return await mcp(req, env, url, mk[1]); }
      catch (e) { return json({ jsonrpc: "2.0", id: null, error: { code: -32603, message: String(e && e.message || e) } }, 500); }
    }
    if (url.pathname.startsWith("/api/")) {
      try { return await api(req, env, url); }
      catch (e) { return json({ error: String(e && e.message || e) }, 500); }
    }
    return env.ASSETS.fetch(req);
  },
};
