# media-downloader

Self-hosted загрузчик медиа с веб-интерфейсом для Raspberry Pi (и любого другого хоста с Docker).
Ссылку вставляешь в браузере, в том числе с телефона, а файлы сохраняются в библиотеку на сервере.

- **yt-dlp** — видео и звук: YouTube, SoundCloud, Instagram Reels, TikTok и [остальные сайты yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
- **gallery-dl** — изображения, галереи, карусели Instagram и т. п.
- **ffmpeg** — конвертация в MP3/M4A, склейка видео и звука в MP4, превью в библиотеке.

Возможности:
- анализ ссылки без скачивания;
- выбор отдельных элементов из карусели или плейлиста;
- MP4 с выбором разрешения, MP3 с выбором битрейта, M4A или исходный звуковой поток;
- очередь с прогрессом по каждому файлу и полным текстом ошибок;
- библиотека с превью, фильтрами и поиском, скачивание одного файла или ZIP-архива, удаление;
- cookies для каждой платформы отдельно;
- обязательный вход по паролю;
- готовность к работе за обратным прокси / Cloudflare Tunnel.

## Как устроено

```
Mac: правка кода → git commit → git push ─▶ GitHub (этот репозиторий)
                                              │
Хост: docker compose build --no-cache ◀───────┘  (образ собирается прямо из GitHub)
      docker compose up -d
```

На хосте лежат только `docker-compose.yml` и `.env`. Исходники не копируются: Compose сам скачивает репозиторий при сборке.

| Что | Имя |
|---|---|
| Проект Compose | `mediadl` |
| Контейнер | `mediadl` |
| Сеть | `mediadl_net` |
| Тома | `mediadl_config` (cookies), `mediadl_data` (база очереди, кэш превью, временные файлы) |
| Порт в контейнере | `8790` (на хосте — `HOST_PORT`) |

Внутри контейнера два отдельных Python-окружения: `/opt/app` для веб-приложения и `/opt/engines` для yt-dlp и gallery-dl. Движки можно обновлять без пересборки образа.

## Установка

Все команды выполняются на хосте, в каталоге сервиса (например, `~/media-downloader`).

1. Создать каталог и скачать два файла:

   ```bash
   mkdir -p ~/media-downloader && cd ~/media-downloader
   curl -fsSLO https://raw.githubusercontent.com/WhyElEss/media-downloader/main/docker-compose.yml
   curl -fsSL -o .env https://raw.githubusercontent.com/WhyElEss/media-downloader/main/.env.example
   chmod 600 .env
   ```

2. Заполнить в `.env` как минимум `LIBRARY_PATH` (существующий каталог на хосте), `PUID`/`PGID` (вывод `id -u` и `id -g`), `TZ` и `APP_USERNAME`.

3. Собрать образ:

   ```bash
   docker compose build
   ```

4. Сгенерировать хеш пароля и вписать выведенную строку `APP_PASSWORD_HASH=...` в `.env`:

   ```bash
   docker compose run --rm mediadl hashpw
   ```

   Хеш (scrypt) не содержит символа `$`, так что кавычки и экранирование в `.env` не нужны.

5. Запустить:

   ```bash
   docker compose up -d
   ```

6. Открыть `http://<адрес-хоста>:<HOST_PORT>` и войти.

Первый старт занимает до пары минут: контейнер обновляет yt-dlp и gallery-dl. Ход запуска виден так:

```bash
docker compose logs -f
```

## Настройки `.env`

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `HOST_PORT` | `8790` | Порт веб-интерфейса на хосте |
| `LIBRARY_PATH` | — (обязательно) | Каталог библиотеки на хосте, монтируется в `/library` |
| `PUID` / `PGID` | `1000` | Владелец создаваемых файлов на хосте |
| `UMASK` | `022` | Права на новые файлы |
| `TZ` | `UTC` | Часовой пояс для логов и дат |
| `APP_USERNAME` | `admin` | Логин |
| `APP_PASSWORD_HASH` | — (обязательно) | Хеш пароля от `hashpw`; без него войти невозможно |
| `SESSION_DAYS` | `7` | Срок жизни сессии |
| `LOGIN_MAX_ATTEMPTS` | `5` | Сколько неудачных входов с одного IP разрешено до блокировки |
| `LOGIN_LOCK_MINUTES` | `15` | Окно подсчёта и длительность блокировки |
| `COOKIE_SECURE` | `auto` | Флаг `Secure` у cookie сессии: `auto` — когда запрос пришёл по HTTPS (в том числе через `X-Forwarded-Proto`); `true` / `false` — принудительно |
| `FORWARDED_ALLOW_IPS` | `*` | От каких адресов доверять `X-Forwarded-*` (параметр uvicorn `--forwarded-allow-ips`) |
| `MAX_CONCURRENT` | `2` | Сколько процессов загрузки работает одновременно |
| `AUTO_UPDATE_ENGINES` | `true` | Обновлять yt-dlp и gallery-dl при каждом старте контейнера |
| `CPUS` / `MEM_LIMIT` | `2` / `1g` | Лимиты контейнера |

Защита от перебора паролей:
- после `LOGIN_MAX_ATTEMPTS` неудач с одного IP вход с этого IP закрывается, пока самая старая из этих неудач не выйдет за окно `LOGIN_LOCK_MINUTES`;
- после `4 × LOGIN_MAX_ATTEMPTS` неудач в сумме со всех адресов вход закрывается для всех на то же время. Уже открытые сессии продолжают работать.

Счётчики хранятся в памяти и сбрасываются при перезапуске контейнера.

## Раскладка файлов

```
<LIBRARY_PATH>/<платформа>/<автор>/<дата>_<автор>_<ID поста>[_<номер в карусели>].<расширение>
```

Например: `instagram/natgeo/2026-05-01_natgeo_DAbCdEf_03.jpg` или `youtube/Rick Astley/2009-10-25_Rick Astley_dQw4w9WgXcQ.mp4`.

Имя платформы получается из домена ссылки: `x.com` → `twitter`, `youtu.be` → `youtube` и т. п. Это же имя используется для cookies.

## Обновление

**Код сервиса.** Правка на Mac → `git commit` → `git push` в `main`. Затем на хосте:

```bash
cd ~/media-downloader && docker compose build --no-cache && docker compose up -d
```

`--no-cache` заново скачивает репозиторий и заново ставит свежие yt-dlp и gallery-dl. Скрипты, которые делают только `docker compose pull`, этот сервис не обновят: у него нет образа в реестре.

**Только движки** (сайты часто ломают yt-dlp и gallery-dl). Подходит любой из способов:
- кнопка **Settings → Update engines** в интерфейсе. Работает, только когда нет активных загрузок; новые загрузки ждут окончания обновления;
- перезапуск контейнера при `AUTO_UPDATE_ENGINES=true`:

  ```bash
  docker compose restart
  ```

Обновление, сделанное без пересборки, живёт до пересоздания контейнера. Следующий `up -d` с новым образом и так поставит свежие версии.

## Cookies (вход на сайты)

Instagram и некоторые другие сайты отдают контент только залогиненным пользователям. Без cookies такие ссылки дают ошибку движка, и её текст показывается полностью.

1. Войти на сайт в обычном браузере. Лучше под отдельным, не основным аккаунтом: сайты могут ограничить аккаунт за автоматические загрузки.
2. Экспортировать cookies этого сайта в формате **Netscape cookies.txt** расширением браузера. В FAQ yt-dlp рекомендуются «Get cookies.txt LOCALLY» для Chrome и «cookies.txt» для Firefox.
3. В интерфейсе: **Settings → Cookies**. Platform — имя платформы, как в библиотеке (`instagram`, `youtube`, `twitter`, `tiktok`…), файл — экспортированный `cookies.txt`. Upload, затем подтвердить.

Файлы хранятся в томе `mediadl_config` (`/config/cookies/<платформа>.txt`, права 600), а не в библиотеке. Каждая загрузка получает временную копию, потому что движки записывают cookies обратно.

Если ссылка снова перестала работать, скорее всего, сессия на сайте истекла: экспортируйте cookies заново и загрузите их поверх старых.

## Подключение отдельного Cloudflare Tunnel

Сервис уже готов работать за прокси:
- в интерфейсе нет абсолютных адресов (все пути относительные);
- uvicorn запускается с `--proxy-headers`;
- cookie сессии получает `Secure`, когда запрос пришёл по HTTPS.

По [документации Cloudflare](https://developers.cloudflare.com/fundamentals/reference/http-headers/) `X-Forwarded-Proto` выставляет сам Cloudflare и перезаписывает значение клиента. Поэтому с `COOKIE_SECURE=auto` через туннель cookie будет `Secure`, а в локальной сети по `http://` — без него, и вход работает в обоих случаях.

Что учесть:

1. **Отдельный туннель и отдельный compose-проект** — уже существующие туннели не трогать.
2. **Как cloudflared достаёт до сервиса.** Проще всего подключить контейнер cloudflared к сети `mediadl_net` и указать в туннеле сервис `http://mediadl:8790`:

   ```yaml
   services:
     cloudflared:
       image: cloudflare/cloudflared:latest
       command: tunnel run
       environment:
         - TUNNEL_TOKEN=${TUNNEL_TOKEN}
       networks: [mediadl_net]
       restart: unless-stopped
   networks:
     mediadl_net:
       external: true
   ```

3. **IP посетителя.** Для лимита попыток входа берётся `CF-Connecting-IP`: Cloudflare всегда перезаписывает этот заголовок. `X-Forwarded-For` для этого не годится — Cloudflare дописывает к нему адрес, а присланное клиентом значение оставляет.
4. **Порт `HOST_PORT` в интернет не пробрасывать.** Туннелю он не нужен.
5. По желанию можно поставить Cloudflare Access поверх туннеля как второй слой защиты. Встроенный вход при этом остаётся.

## Типичные ошибки

| Симптом | Причина и решение |
|---|---|
| «Login is not configured» | Не задан или испорчен `APP_PASSWORD_HASH`. Сгенерировать через `docker compose run --rm mediadl hashpw`, вписать в `.env`, выполнить `docker compose up -d` |
| «Too many failed attempts» | Сработала защита от перебора. Подождать `LOGIN_LOCK_MINUTES` или перезапустить контейнер |
| Вход проходит, но сразу снова просит пароль | Cookie `Secure` при работе по `http://`. Проверить `COOKIE_SECURE` (для локальной сети — `auto` или `false`) |
| `/library is not mounted` в логах, контейнер перезапускается | Не задан `LIBRARY_PATH` или каталога нет на хосте |
| `Permission denied` при записи файлов | Каталог `LIBRARY_PATH` недоступен на запись пользователю `PUID:PGID` |
| Instagram: `login required`, `redirect to login page`, `401` | Нужны cookies, см. раздел выше |
| YouTube: `Sign in to confirm you're not a bot` | YouTube требует вход для IP хоста. Загрузить cookies для `youtube` |
| `Unsupported URL` / `No suitable extractor` | Сайт не поддерживается выбранным движком. Попробовать кнопку «Try gallery-dl / Try yt-dlp» |
| Сайт вчера работал, сегодня ошибка извлечения | Settings → Update engines |
| Видео не играет на телефоне или ТВ | Выключен флажок «H.264»: у YouTube лучшее качество часто только в AV1/VP9 (проверено на 720p — без флажка пришёл AV1). Включить флажок и скачать заново. Файл с тем же именем не перезаписывается, поэтому старый сначала удалите в библиотеке |
| MP3 из видео: «unable to obtain file audio codec with ffprobe» | У видео нет звуковой дорожки. Анализ теперь это определяет и не предлагает аудиоформаты |
| `Your kernel does not support memory limit capabilities` при `up` | В ядре хоста выключен memory cgroup, поэтому `MEM_LIMIT` не действует (лимит CPU работает). На Raspberry Pi OS по [документации k3s](https://docs.k3s.io/installation/requirements) включается добавлением `cgroup_memory=1 cgroup_enable=memory` в конец единственной строки `/boot/firmware/cmdline.txt` и перезагрузкой |
| Healthcheck `unhealthy` | `docker compose logs --tail=100` — ошибка будет там |

Логи сервиса:

```bash
docker compose logs -f mediadl
```

## Безопасность

- Вход обязателен. Пароль хранится только как хеш scrypt, сессии лежат на сервере (в базе — SHA-256 токена), cookie `HttpOnly`, `SameSite=Lax`.
- Каждый изменяющий запрос проверяет CSRF-токен. Опасные действия (удаление, обновление движков, загрузка cookies) требуют подтверждения в интерфейсе.
- Пути в библиотеке проверяются: всё, что после разрешения выходит за пределы `/library` (`../`, абсолютные пути, симлинки наружу), отклоняется.
- Принимаются только ссылки `http(s)`, без адресов локальной сети. Движки запускаются без shell, ссылка передаётся после `--`.
- Миниатюры при анализе проксируются по идентификатору результата анализа: браузер не может заставить сервер скачать произвольный адрес.
- Контейнер работает от `PUID:PGID`, без `privileged` и без `network_mode: host`, с `no-new-privileges`.
