# Студия ингредиентов

Локальный конструктор английских промптов с русским интерфейсом: 751 ингредиент, 1799 форм и 11 исходных пилотных изображений. Новые запросы идут в Cloudflare FLUX-2-KLEIN-9B с размером **512 × 512**.

## Данные перед первым запуском

Этот репозиторий содержит код. Рабочая база, исходные CSV/матрицы и пилотные изображения не публикуются.
Перед запуском восстановите приватные файлы в `inputs/` и `outputs/prompt_pilot/` из своего проекта или резервной копии.
Список файлов указан в README этих каталогов. Для переноса действующей Studio отдельно скопируйте приватный `data/`.
Ключи задаются только в локальном `.env`, пример без значений — `.env.example`.

Тесты используют исходные приватные матрицы и пилотные файлы; для их запуска эти данные также должны быть восстановлены.

## Запуск

Для серверного развёртывания используйте собственный домен. `studio.example.com` далее — только пример: замените его своим адресом в `deploy/compose.yml`, `APP_PUBLIC_URL`, правилах Traefik и настройках MCP-клиента. Пример каталога установки — `/opt/ingredient-studio`; база и изображения сохраняются в его `data/`. Серверная база и локальная копия независимы.

Развёртывание: `docker compose --project-directory /opt/ingredient-studio -f deploy/compose.yml up -d --build`. Пример Compose рассчитан на уже настроенный Traefik: укажите свою внешнюю Docker-сеть вместо `studio_proxy` и свой TLS resolver вместо `myresolver`. Контейнер `ingredient-studio` подключается к этой сети без публикации порта на сервере. Авторизация задаётся отдельным middleware `generator-auth@file`; bcrypt-хеш храните в закрытом файле вашего Traefik, например `/etc/traefik/dynamic/studio.htpasswd`; этот файл создаётся отдельно и не публикуется в Git. `.env` примонтирован только для чтения и не включён в образ. Приложение запускается без root, с файловой системой только для чтения; записывать может в `data/`.

`APP_BASE_PATH=/generator` и `APP_PUBLIC_URL=https://studio.example.com/generator` включают серверный адрес. Без этих переменных сохраняется локальный режим. HTTPS-Origin проверяется для запросов изменения данных. После переноса требуется сверить дневной бюджет, чтобы не использовать устаревший остаток локального приложения.

Python 3.10 или новее, без установки библиотек:

```sh
python3 app.py
```

Открыть http://127.0.0.1:8765/. Можно также дважды нажать `Запустить.command` в Finder. Для завершения — Ctrl+C в окне сервера. Одновременно запускается только один экземпляр студии.

Скопируйте `.env.example` в `.env` и заполните только используемые ключи. Для fal.ai задайте `FAL_KEY`; для Cloudflare в `.env` нужны `Account_ID` и `Your_API_Token`. Секреты читает только сервер; в промпты, историю, ZIP и браузер они не попадают. Для кнопки проверки остатка токену также нужен доступ к аналитике аккаунта. Резервный путь — ручная сверка в Cloudflare.

## MCP для Hermes и других клиентов

Endpoint: `https://studio.example.com/generator/mcp`. Transport: **Streamable HTTP**, JSON responses; совместимые версии протокола `2025-11-25`, `2025-06-18`, `2025-03-26`. MCP использует отдельный Bearer-токен: клиент передаёт `Authorization: Bearer <token>`. В форме Hermes выберите HTTP/SSE и Authentication: Token, вставьте только токен без префикса Bearer. Веб-интерфейс продолжает использовать Basic Auth с заданными вами логином и паролем. Токен не даёт доступ к страницам или REST API конструктора; изображения доступны через MCP ImageContent. Секрет нельзя включать в URL. На сервере сохраняется только SHA-256 хеш высокоэнтропийного случайного токена в закрытом `.mcp-token.sha256`; файл примонтирован только для чтения и не включается в образ. Проверка токена обязательна для POST, GET и DELETE на MCP, сравнение хеша выполняется за постоянное время. При отсутствии хеша доступ закрыт. Для смены токена замените хеш и перезапустите только ingredient-studio. На `initialize` сервер возвращает `Mcp-Session-Id`, который клиент затем передаёт вместе с согласованным `MCP-Protocol-Version`. После перезапуска требуется новая инициализация.

Основной агентский workflow: `get_form(form_id)` → `generate_image(form_id, seed)` → опрашивать `get_generation_history(form_id)` для возвращённого `generation_id` → при `status=succeeded` вызвать `get_generation_image(generation_id)`. Все идентификаторы непрозрачные строки и передаются без изменений. `get_form` возвращает `form_id`; `generate_image` возвращает однозначный `generation_id`, `image_ready` и `next_tool`. История возвращает `generation_id` каждой попытки (старое поле `id` сохранено).

Инструменты: `get_ingredient(id)`, `get_form(form_id)`, `get_prompt(form_id)`, `get_generation_history(form_id)`, `generate_image(form_id, seed)`, `get_generation_image(generation_id)`, `update_prompt_fields(form_id, changes)`, `accept_generation(generation_id)`, `reject_generation(generation_id, reason)`.

`id` ингредиента — точная строка `ingredient` / `source` из исходного CSV, например `tomato`. `get_ingredient` возвращает список форм с постоянными `id`. `changes` — только изменяемые блоки на английском: `subject`, `preparation`, `geometry`, `material`, `portion`, `composition`, `view`, `container`. Правка атомарная, остальные поля и история сохраняются.

`generate_image` запускает один запрос 512 × 512 и сразу возвращает `generation_id`. Готовность нужно опрашивать через `get_generation_history`; затем вызвать `get_generation_image`. В ответе `content[0]` — **MCP ImageContent**: `type: image`, `data: base64` исходных байтов, `mimeType: image/png` или `image/jpeg`. Изображение не заменяется ссылкой или текстовым описанием; пилоты возвращаются в исходном размере 1024 × 1024. Метаданные находятся в отдельном текстовом блоке и `structuredContent`.

`generate` сохранён как совместимый псевдоним `generate_image`; для одной попытки нельзя вызывать оба. Схемы результатов (`outputSchema`) явно задают идентификаторы и метаданные, а пиксели остаются отдельным `ImageContent`.

Повтор HTTP-вызова `generate_image` с тем же JSON-RPC ID внутри одной сессии не создаёт новый запрос. Для повторов между сессиями можно передать необязательный `request_id` (8–128 латинских букв, цифр, `_` или `-`). Для намеренно новой попытки нужен новый ID. Очередь, дневной бюджет и запрет автоматических повторов после ошибки общие с интерфейсом. MCP не меняет бюджет и не снимает его ограничения.

`accept_generation` отмечает картинку принятой, но не назначает финальный выбор. `reject_generation` сохраняет причину и снимает выбор, если отклонено выбранное изображение. Ни один инструмент оценки не генерирует следующую попытку автоматически.

Источник формата ImageContent: https://modelcontextprotocol.io/specification/2025-11-25/server/tools

## Работа

1. Найти ингредиент на русском или английском; выбрать форму.
2. При выборе формы английское описание и промпт подготавливаются автоматически из её строки матрицы. Требования RU/EN не редактируются. Восемь полей нужны только для уточнений. Геометрия берётся из конкретной формы и размеров, фактура — из примечаний продукта и отдельных описаний ингредиентов; новые формы по категории не назначаются.
3. Посмотреть готовый промпт; при необходимости уточнить отдельные поля. Критерии отказа остаются отдельными заметками для оценки. Автоматически подготовленный промпт ещё не считается визуально проверенным.
4. В «Бюджете» проверить остаток через Cloudflare либо подтвердить доступный остаток вручную. Бесплатная общая квота — 10 000 neurons в сутки, сброс в 00:00 UTC / 05:00 Екатеринбург. Резерв 1363,64 на запрос сохраняется и для 512 × 512, пока фактическая тарификация не подтверждена.
5. Сгенерировать один вариант или три последовательных seed, сравнить результаты, принять / отклонить / выбрать. Скачать ZIP с исходным изображением и точными метаданными.

Для пакетной работы открыть «Массовая обработка»: выбрать текущий ингредиент или найти формы среди всех 1799, отметить нужные формы и нажать «Генерировать выбранные». Описания готовятся автоматически. Очередь получает только число запросов, помещающееся в бюджет; оставшиеся формы остаются выбранными между перезагрузками страницы. Готовый результат 512 × 512 с тем же промптом и стилем повторно не создаётся; отклонённые варианты не блокируют новую попытку.

Кнопка «Подготовить все 1799 промптов» работает локально, не запускает генерацию и не расходует neurons. Уже внесённые правки и пилотные описания сохраняются. «Скачать промпты JSON» экспортирует ID, блоки, полные промпты, критерии и параметры 512 × 512 для дальнейшей автоматизации.

«Изображение выбрано» и «Промпт проверен» независимы. Проверка требует трёх принятых результатов на разных seed для текущего неизменного промпта, версии стиля и размера 512 × 512. Пилотные 1024 × 1024 для этой проверки не учитываются.

## Хранение и бюджет

`inputs/` — неизменные копии двух матриц и исходных CSV. Матрицы импортированы из JSON, использованных для исходных XLSX. Внутренний ID формы стабилен: хеш точного `source` и английского названия формы. Номер исходной строки и оба языка сохранены. Повторный импорт не перезаписывает правки и результаты.

`data/studio.sqlite3` — правки, версии стиля, бюджет, очередь, история и оценки. `data/images/` — изображения; `data/<ID>.json` — снимки новых попыток. Для резервной копии остановить сервер и скопировать всю папку проекта вместе с `data/`. Папку `.env` хранить приватно.

Каждый отправленный запрос заранее удерживает резерв. Ошибка, тайм-аут и перезапуск не приводят к повторной отправке. После ошибки остальные задания останавливаются. Уже отправленный запрос отменить нельзя. При тайм-ауте расход неизвестен: резерв сохраняется до ручной сверки. При смене дня бюджет нужно подтвердить заново.

Аналитика Cloudflare может запаздывать. Автоматическая сверка дополнительно вычитает сегодняшние резервы этого приложения, даже если они уже вошли в аналитику. Это консервативно; вручную можно установить фактический остаток после сверки. Последующие запросы других приложений не отслеживаются постоянно.

Изменения общего стиля создают новую версию и не меняют старые снимки. Каждый новый файл имеет уникальный ID и не перезаписывает прошлые изображения. Удаление фона, R2 и сборка рецептов не реализованы в этой версии.

## Проверки

```sh
python3 -m unittest -v test_app
```

Тесты создают отдельный временный каталог с фиктивным провайдером; Cloudflare и рабочая база не затрагиваются. 22 теста покрывают импорт, автоматическую подготовку всех 1799 форм, сохранность правок, предметные примеры, экспорт, пакетную очередь и отложенные формы, снимки, размеры, бюджет, одновременные нажатия, тайм-аут, перезапуск, дневной сброс, остановку очереди, проверку трёх seed, выбор и метаданные; дополнительно — серверный подпуть и Origin, девять MCP-инструментов, сессии, идемпотентность, атомарные правки и точный возврат PNG/JPEG ImageContent, Bearer-авторизацию всех HTTP-методов и отказ при отсутствующем хеше.

Живая приёмка новых 512 × 512 пока ожидает восстановления бесплатной квоты: 8 октября API показал 11 запросов и около 15 000 neurons, остаток 0. Конструктор не помечает кандидатов проверенными заранее.

Источник тарифа: https://developers.cloudflare.com/workers-ai/platform/pricing/


## fal.ai / расходы / MCP

Второй сервис — `fal-ai/flux-2/klein/9b` (distilled 4-step), 512×512, PNG, одна картинка на запрос. Сервер читает `FAL_KEY` или существующий `FAI_AI`; ключи не передаются браузеру/истории. Выбор по умолчанию сохраняется в SQLite; каждый запрос хранит собственные сервис/модель. Cloudflare остаётся доступным. Автоматического переключения и повторов нет.

fal.ai: консервативный резерв $0.006 (один полный мегапиксель по опубликованному тарифу), дневной лимит Studio по умолчанию $1, настраивается в «Настройки». Это локальный предел расходов, не кредитный баланс и не бесплатная квота fal.ai. Резервы учитываются в целых миллионных долях USD, ошибки/тайм-ауты удерживают резерв. Резервы, успешные оценки и неизвестный расход показаны отдельно; нейроны Cloudflare не смешиваются с USD. Смена UTC-дня обновляет локальный лимит fal.ai; история не очищается.

Фактические списания: read-only `https://api.fal.ai/v1/models/usage`, пагинация, фильтр этой модели и текущего дня UTC. Требуется Admin key, можно добавить отдельный `FAL_ADMIN_KEY`. Сверка каждые 5 минут и по кнопке; 403 отображается явно, предыдущая успешная сверка сохраняется со временем. Данные охватывают всю рабочую область fal для этой модели, а локальные резервы — только Studio. Фактические списания не подменяют локальные резервы и не кредитуют неизвестные попытки.

MCP `generate_image(form_id, seed, provider?)`: `provider="fal"` или `"cloudflare"`; без параметра — выбранный в UI сервис. Ответ содержит `generation_id`, `provider`, `model`, `cost_currency`, `estimated_reserve`; тот же generation_id передаётся в `get_generation_image`, который возвращает оригинальный MCP ImageContent. Дополнительные tools: `get_generation_settings`, `get_spending`, `refresh_fal_spending`. После обновления сервера переподключить клиент для нового tools/list.

Источники: https://fal.ai/models/fal-ai/flux-2/klein/9b/api и https://fal.ai/docs/platform-apis/v1/models/usage


## Immutable prompt versions / rollback (MCP 1.3.0)

`prompt_versions(version_id,form_id,blocks,prompt,style_id,style_text,created_at,complete,parent_version_id,origin)` stores full immutable snapshots; SQLite triggers reject UPDATE/DELETE. `forms.current_prompt_version_id` selects current; `jobs.prompt_version_id` identifies the frozen version used by the generation. A restore updates the pointer and compatibility `forms.blocks` only; it does not rewrite the version or queued jobs. Snapshots include historical style, even when it differs from the current global style. Explicit global style changes create versions for forms. No-op field edits preserve the version.

Migration runs transactionally and idempotently. Existing rows, image files, selections and reviews are retained. Historical generation versions are created only for complete known snapshots; 11 imported pilots with `blocks={}` remain unlinked and report `snapshot_complete=false` / `exact_rollback_possible=false`. Current existing form snapshots are recorded without inventing historical fields. Initial preparation of an incomplete current form creates a new version; a complete version is never autofilled during generation.

MCP retains existing names and adds:
- `get_generation_prompt(generation_id)` — exact snapshot and completeness flags.
- `restore_prompt_version(form_id,version_id,expected_current_version_id)` — atomic compare-and-swap.
- `restore_prompt_from_generation(generation_id,expected_current_version_id)` — restore candidate; incomplete legacy rejected.

`get_form` / `get_prompt` expose `current_prompt_version_id`. `generate_image` returns `prompt_version_id`. `update_prompt_fields` returns previous/new version IDs and `changed_fields`; optional `expected_current_version_id` protects edits too. Conflict is MCP isError with `http_status=409` and `error_code=conflict`; reread current before deciding. UI edits also send expected current version, and clean open cards refresh after MCP updates. Historical jobs/download metadata expose their version ID.

Workflow: read current A → patch B → patch C → inspect candidate's generation snapshot → restore B with expected C → NEW generate request → generation.prompt_version_id=B. Reusing an old request_id returns the old generation, not a fresh attempt. Rollback does not accept/reject/select images. Tests and real Hermes SDK verification use synthetic pixels; no paid image call is needed.


## Batch navigation / fal settings (MCP 1.4.0)

`list_forms(status="all", limit=10, cursor=None)` is strictly read-only. Statuses: `missing_image` (zero succeeded jobs with existing saved PNG/JPEG), `has_image` (one or more), `pending_acceptance` (has image and zero accepted reviews), `accepted` (one or more `jobs.review="accepted"`), `all`. Pilots count regardless of image dimensions. Existing selection also sets review to accepted; `selected_generation_id` remains separate. Latest generation ID means latest successful saved image; latest accepted ID follows the review field. No new acceptance flag or migration.

Order is `(ingredient_id, form_id)`; opaque status-bound keyset cursor avoids offset skips when processed forms disappear. Limit 1–100. `total_matching` is the whole current status selection, independent of cursor. Start each primary batch with missing_image without cursor; moderate pending_acceptance without regenerating it. An item re-entering a status behind the cursor appears in a fresh traversal.

`set_fal_budget(daily_limit_usd)` / `POST /api/settings/fal-budget` edits the persisted daily cap without changing provider or clearing spent/uncertain reserves. Settings uses the same method. This is a Studio spending cap, not an account balance top-up. Reconnect Hermes to refresh the 18-tool catalog.


## DNS failures (MCP 1.4.1)

A proven pre-send `socket.gaierror` (direct or wrapped in `URLError`) marks only that attempt failed, releases its reservation and records `not_sent_dns`; remaining jobs continue. There is no automatic retry of the failed attempt. Timeouts and other ambiguous transport failures still retain reservations and stop the queue. Historical uncertain jobs are not reclassified or refunded because their precise cause was not stored. No data migration.


The proven direct fal adapter has been restored (MCP 1.4.2): one POST to `https://fal.run/fal-ai/flux-2/klein/9b`, `sync_mode=true`, original PNG data URI. No submission retries. Provider request ID is saved as soon as response headers are available. DNS errors before sending release reserve; ambiguous transport failures continue to retain it.


## Interrupted fal responses (MCP 1.4.3)

Direct `fal.run` submissions remain unchanged. JSON transport accepts gzip. If reading the response fails after a request ID was received, Studio retrieves the existing result with GET `queue.fal.run/fal-ai/flux-2/requests/{request_id}`. Only these GET reads retry (up to 3), never a new generation POST. The same prompt, seed, version and one reservation are retained. `IncompleteRead` without a recoverable ID is uncertain, not a misleading local save error. Server diagnostics record stage, exception class and code locations, without credentials or response payloads.


## Isolated fal failures (MCP 1.4.4)

A timeout or transport failure marks only that fal attempt uncertain, retains its reservation and leaves other queued forms eligible. Invalid provider results mark the individual attempt failed. Neither causes a new submission of that attempt. HTTP 401/402/403/429 or a missing key stop pending fal requests; local storage and unexpected processing failures still stop all pending requests. Cloudflare retains its existing stop policy. Daily budget checks remain unchanged. Previously blocked or interrupted jobs are not silently resumed.

## Visual review by form

`/generator/review` shows one form without a selected final asset, with ALL its saved successful generations side by side (including accepted and rejected candidates). Selecting one calls the existing select action: it becomes accepted and the form's single selected_job. The page advances to the next form without a selected result. Reject/comment affect only that candidate and stay on the current form; other candidates are preserved. Skip defers the form in sessionStorage, with an explicit return button. Concurrent selections are protected by only_unselected, so an existing selection is not overwritten. Studio and Food MCP see the same saved choice. No generation requests are made.

## MCP final selection (1.4.5)

`select_generation(form_id, generation_id)` chooses the single final asset, marks that candidate accepted and preserves its comment. Ownership, succeeded state and saved file are checked atomically. Choosing a different candidate replaces selected_job while retaining all previous images/reviews. Repeating a choice is safe. No generation, budget change or prompt restore occurs. Output includes form_id, generation_id, selected_generation_id, review and prompt_verified. accept_generation remains an acceptance-only action.

## Telegram form reviewer

`telegram_review_bot.py` runs in the separate `review-bot` Compose service (no Studio workers or generation submissions). TELEGRAM_REVIEW_BOT_TOKEN is server-only in .env. Private chats only; usernames axelder and egolfeld bind once to stable Telegram user IDs. Durable permissions, update cursor, current form/button nonce and per-user skips are in data/telegram-review-state.json (0600). /start and /next show form requirements, all saved variants in photo albums, numbered selection buttons and global counters. Selection uses the same Studio transaction with form ownership and only_unselected checks; old/duplicate buttons cannot overwrite a completed selection. /stats shows counts, /skipped resets that user's deferrals. Files upload directly; Basic Auth stays enabled on the web page. A user must start a private chat to receive reviews. Bot state is separate from prompt/generation data. No Cloudflare/fal spending occurs.

Telegram review now sends one numbered photo per message, with Select and Comment inline buttons directly under that exact variant. Comment requests use ForceReply and bind the reply to the user, current form nonce, candidate index and prompt message ID; comments persist as jobs.reason and do not accept/select or advance the form. /cancel abandons comment entry. Closing a form clears all its photo keyboards. Older album-style sessions remain usable until /next refreshes them. No images are modified or generated.

## Completed AI review (MCP 1.4.6)

forms.ai_reviewed_at is nullable UTC ISO 8601 text in SQLite. No automatic backfill or marking on selection/start. mark_reviewed(form_id) atomically preserves the first timestamp and changes no prompts, images, selection or budgets. New optional list_forms filters: ai_reviewed (bool), successful_generation_count (exact nonnegative count), selected (bool). Omitting them preserves old behavior, including missing_image meaning zero saved successful images. Optimizer batch query: list_forms(status="all", successful_generation_count=1, selected=false, ai_reviewed=false, limit=30). Cursors must be reused with the same filters; unfiltered legacy cursors remain supported. API GET /api/list/forms exposes the filters; POST /api/mark-reviewed accepts form_id. Timestamp also appears in get_form/Web/Telegram form data; manual review queues are unaffected. Mark only after a final outcome, including unresolved human/provider/vision outcomes; never at start or after a mid-form crash. Explicit re-review by form ID remains possible without resetting the first timestamp.

## Transparent selected images

Install ImageMagick on the host and enable deploy/ingredient-background-removal.service. The host service runs background_removal.py as UID/GID 10001, sharing data/studio.sqlite3 and data/images with Studio; it never initializes the Studio generation worker. image_variants links each transparent PNG to its generation, source SHA256 and algorithm version. Originals, selections and generation records remain unchanged; variants from earlier selections are retained. Only selected successful generations are processed, including new Web/MCP/Telegram selections. Edge-connected near-white background is flood-filled to transparency, preserving enclosed white details and original dimensions. No provider API calls or model costs. Studio displays original and transparent checkerboard previews; downloads include both. Failed processing is recorded separately. Service installation requires a registered host UID 10001 and execute ACL for that UID on the private project directory; retain data ownership 10001. ImageMagick 6 uses matte flood-fill, version 7 uses alpha flood-fill. Existing selected images backfill in the background.
