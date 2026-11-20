# Радар вирусных роликов — установка

Раз в 2 дня радар сам:
1. ищет Shorts (YouTube) и TikTok по ключам из `config.yaml` с 1 млн+ просмотров;
2. отсекает чужие языки и нерелевантные ролики (дешёвый ИИ-фильтр по подписи);
3. разбирает подходящие ролики через Gemini (хук, формат, CTA, балл под нутру, риск модерации, русские субтитры);
4. собирает тематические страницы (авторов) со статистикой;
5. сохраняет всё в базу Cloudflare D1 и присылает дайджест в Telegram.

---

## 1. Загрузить файлы в GitHub (5 мин)
1. Откройте репозиторий `radar` → **Add file → Upload files**.
2. Перетащите **все файлы и папки** из архива: `main.py`, `config.yaml`, `requirements.txt`, `README.md` и папку `.github`.
   - Если папка `.github` не перетаскивается (скрытая папка): **Add file → Create new file** → в имени файла
     напишите `.github/workflows/radar.yml` → вставьте содержимое этого файла из архива → **Commit**.
3. **Commit changes**.

## 2. Добавить секреты (5 мин)
Репозиторий → **Settings → Secrets and variables → Actions → New repository secret**. Создайте по одному:

| Имя секрета | Что вставить |
|---|---|
| `GEMINI_KEY` | ключ Gemini (AIza…) |
| `YOUTUBE_KEY` | ключ YouTube (AIza…) |
| `APIFY_TOKEN` | токен Apify (apify_api_…) |
| `CF_API_TOKEN` | токен Cloudflare с правом D1 Edit |
| `CF_ACCOUNT_ID` | Account ID Cloudflare |
| `TELEGRAM_TOKEN` | токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | можно не добавлять сразу — см. шаг 4 |

База должна называться **`radar-db`** (Cloudflare → D1). Таблицы радар создаст сам.

## 3. Первый запуск
Репозиторий → **Actions** → слева **radar** → **Run workflow** → **Run workflow**.
Через 5–15 минут запуск станет зелёным ✅.

## 4. Telegram
1. Перед первым запуском напишите своему боту любое сообщение.
2. После запуска откройте лог (Actions → запуск → `run` → шаг `python main.py`) и найдите строку
   `Telegram chat_id = 123456789`.
3. Добавьте это число в секрет `TELEGRAM_CHAT_ID` — тогда дайджест будет приходить всегда.

## 5. Где смотреть результаты
- **Telegram** — дайджест: новые выстрелы + топ тематических страниц.
- **Отчёт CSV** — Actions → запуск → внизу **Artifacts → radar-report** (videos.csv и authors.csv, открываются в Excel).
- **База** — Cloudflare → D1 → `radar-db` → Console. Примеры запросов:
  - `SELECT author, views, nutra_score, url FROM videos WHERE status='analyzed' ORDER BY nutra_score DESC, views DESC LIMIT 50`
  - `SELECT handle, platform, followers, relevant_shots, max_views FROM authors ORDER BY relevant_shots DESC LIMIT 50`

## 6. Настройки (`config.yaml`)
Редактируйте прямо на GitHub (иконка карандаша): ключевые слова, порог просмотров, языки,
лимит разборов за запуск (`max_analyze_per_run` — главный регулятор расходов), новые вертикали.

## Расходы (оценка)
- YouTube API — бесплатно (квота ~10 000 единиц в день, один запуск тратит ~1 800).
- Apify — поиск TikTok ~$0,3–0,6 за запуск + скачивание видео ~$0,004 за ролик.
- Gemini Flash-Lite — ~$0,002 за ролик.
- GitHub Actions, Cloudflare D1 — бесплатно.
Итого ~$5–15 в месяц при запуске раз в 2 дня.

## Статусы роликов в базе
`analyzed` — разобран · `queued` — ждёт разбора (лимит запуска) · `skip_lang` — другой язык · `skip_topic` — не по теме.
