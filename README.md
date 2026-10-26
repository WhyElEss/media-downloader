# media-downloader

A self-hosted media downloader with a web UI for a Raspberry Pi (or any other Docker host).
Paste a link in the browser, including on a phone, and the files are saved to a library on the server.

- **yt-dlp**: video and audio from YouTube, SoundCloud, Instagram Reels, TikTok and [the other sites yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
- **gallery-dl**: images, galleries, Instagram carousels and similar.
- **ffmpeg**: conversion to MP3/M4A, merging video and audio into MP4, previews in the library.

Features:
- link analysis without downloading;
- picking individual items from a carousel or playlist;
- MP4 with a choice of resolution, MP3 with a choice of bitrate, M4A, or the original audio stream;
- a queue with per-file progress and the engine's full error text;
- a library with previews, filters and search; download a single file or a ZIP archive; delete files;
- cookies per platform;
- mandatory password login;
- ready to run behind a reverse proxy / Cloudflare Tunnel.

## How it works

```
Workstation: edit → git commit → git push ─▶ GitHub (this repository)
                                               │
Host: docker compose build --no-cache ◀────────┘  (the image is built straight from GitHub)
      docker compose up -d
```

The host keeps only `docker-compose.yml` and `.env`. No source code is copied there: Compose fetches the repository itself during the build.

| What | Name |
|---|---|
| Compose project | `mediadl` |
| Container | `mediadl` |
| Network | `mediadl_net` |
| Volumes | `mediadl_config` (cookies), `mediadl_data` (queue database, preview cache, temporary files) |
| Port inside the container | `8790` (on the host: `HOST_PORT`) |

The container has two separate Python environments: `/opt/app` for the web app and `/opt/engines` for yt-dlp and gallery-dl. The engines can be upgraded without rebuilding the image.

## Installation

Run all commands on the host, in the service directory (for example `~/media-downloader`).

1. Create the directory and download two files:

   ```bash
   mkdir -p ~/media-downloader && cd ~/media-downloader
   curl -fsSLO https://raw.githubusercontent.com/WhyElEss/media-downloader/main/docker-compose.yml
   curl -fsSL -o .env https://raw.githubusercontent.com/WhyElEss/media-downloader/main/.env.example
   chmod 600 .env
   ```

2. In `.env`, set at least `LIBRARY_PATH` (an existing directory on the host), `PUID`/`PGID` (the output of `id -u` and `id -g`), `TZ` and `APP_USERNAME`.

3. Build the image:

   ```bash
   docker compose build
   ```

4. Generate a password hash and put the printed `APP_PASSWORD_HASH=...` line into `.env`:

   ```bash
   docker compose run --rm mediadl hashpw
   ```

   The hash (scrypt) contains no `$`, so it needs no quotes or escaping in `.env`.

5. Start the service:

   ```bash
   docker compose up -d
   ```

6. Open `http://<host-address>:<HOST_PORT>` and sign in.

On every start the container first upgrades yt-dlp and gallery-dl, so the web UI comes up after a short delay (about 15 seconds on a Raspberry Pi in testing). To follow the start-up:

```bash
docker compose logs -f
```

## `.env` settings

| Variable | Default | Purpose |
|---|---|---|
| `HOST_PORT` | `8790` | Web UI port on the host |
| `LIBRARY_PATH` | — (required) | Library directory on the host, mounted at `/library` |
| `PUID` / `PGID` | `1000` | Owner of the files created on the host |
| `UMASK` | `022` | Permissions of new files |
| `TZ` | `UTC` | Time zone for logs and dates |
| `APP_USERNAME` | `admin` | Login name |
| `APP_PASSWORD_HASH` | — (required) | Password hash from `hashpw`; without it nobody can sign in |
| `SESSION_DAYS` | `7` | Session lifetime |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed logins allowed from one IP before it is locked out |
| `LOGIN_LOCK_MINUTES` | `15` | Counting window and lockout length |
| `COOKIE_SECURE` | `auto` | `Secure` flag on the session cookie: `auto` sets it when the request came over HTTPS (including via `X-Forwarded-Proto`); `true` / `false` force it |
| `FORWARDED_ALLOW_IPS` | `*` | Which addresses are trusted to send `X-Forwarded-*` (uvicorn's `--forwarded-allow-ips`) |
| `MAX_CONCURRENT` | `2` | Number of download processes running at the same time |
| `AUTO_UPDATE_ENGINES` | `true` | Upgrade yt-dlp and gallery-dl on every container start |
| `CPUS` / `MEM_LIMIT` | `2` / `1g` | Container limits |

Brute-force protection:
- after `LOGIN_MAX_ATTEMPTS` failures from one IP, that IP cannot sign in until the oldest of those failures falls out of the `LOGIN_LOCK_MINUTES` window;
- after `4 × LOGIN_MAX_ATTEMPTS` failures in total from all addresses, sign-in is closed for everyone for the same time. Sessions that are already open keep working.

The counters live in memory and reset when the container restarts.

## File layout

```
<LIBRARY_PATH>/<platform>/<author>/<date>_<author>_<post ID>[_<carousel number>].<extension>
```

For example `instagram/natgeo/2026-05-01_natgeo_DAbCdEf_03.jpg` or `youtube/Rick Astley/2009-10-25_Rick Astley_dQw4w9WgXcQ_1080p.mp4`. Video file names end with the frame height, so the same video downloaded in another resolution does not overwrite the first one.

While downloading, yt-dlp keeps intermediate files in the hidden `<LIBRARY_PATH>/.mediadl-tmp/<number>` on the same disk. The directory is removed after the job, and the library does not show hidden directories.

The platform name comes from the link's domain: `x.com` → `twitter`, `youtu.be` → `youtube` and so on. Cookies use the same name.

## Updating

**Service code.** Edit on your workstation → `git commit` → `git push` to `main`. Then on the host:

```bash
cd ~/media-downloader && docker compose build --no-cache && docker compose up -d
```

`--no-cache` fetches the repository again and installs fresh yt-dlp and gallery-dl. Scripts that only run `docker compose pull` will not update this service: it has no image in a registry.

**Engines only** (site changes break yt-dlp and gallery-dl often). Either of these works:
- the **Settings → Update engines** button in the web UI. It runs only when no download is active; new downloads wait until the update finishes;
- a container restart with `AUTO_UPDATE_ENGINES=true`:

  ```bash
  docker compose restart
  ```

An upgrade made without a rebuild lasts until the container is recreated. The next `up -d` with a new image installs fresh versions anyway.

## Cookies (signing in to sites)

Instagram and some other sites show content only to signed-in users. Without cookies such links fail with an engine error, and its text is shown in full.

1. Sign in to the site in a regular browser. A separate account is better than your main one: sites may restrict an account for automated downloads.
2. Export the site's cookies in **Netscape cookies.txt** format with a browser extension. The yt-dlp FAQ recommends "Get cookies.txt LOCALLY" for Chrome and "cookies.txt" for Firefox.
3. In the web UI: **Settings → Cookies**. Platform is the platform name as in the library (`instagram`, `youtube`, `twitter`, `tiktok`…); the file is the exported `cookies.txt`. Upload, then confirm.

### Safari on macOS

Safari cannot export cookies.txt, and yt-dlp's `--cookies-from-browser safari` works only on the Mac itself. The service therefore accepts Safari's own cookie file, `Cookies.binarycookies`, and keeps only the chosen platform's cookies from it.

**In the web UI.** Settings → Cookies → "Safari on a Mac": sign in to the site in Safari, quit Safari (Cmd+Q), pick the platform, press Cmd+Shift+G in the file dialog, paste `~/Library/Containers/com.apple.Safari/Data/Library/Cookies/` and choose `Cookies.binarycookies`. The rest of the file is discarded and never stored.

**From a terminal** (the same, without the browser). The Safari file is streamed to the host over SSH and the `app.safari_cookies` tool in the container keeps only the chosen platform's cookies. The file itself is never written to the host's disk.

1. Sign in to the site in Safari, then quit Safari (Cmd+Q).
2. On the Mac (use your host address and `PUID:PGID`):

   ```bash
   ssh <user>@<host> 'docker exec -i -u 1000:1000 -w /srv mediadl /opt/engines/bin/python -m app.safari_cookies instagram' < ~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies
   ```

   With `--dry-run` after the platform name the tool only lists the names of the cookies it found, without values, and saves nothing.

Cookies are stored in the `mediadl_config` volume (`/config/cookies/<platform>.txt`, mode 600), not in the library. Each download gets a temporary copy, because the engines write cookies back.

If a link stops working again, the site session has most likely expired: export the cookies again and upload them over the old ones.

## Running behind a separate Cloudflare Tunnel

The service is ready to run behind a proxy:
- the web UI has no absolute URLs (all paths are relative);
- uvicorn runs with `--proxy-headers`;
- the session cookie gets `Secure` when the request came over HTTPS.

According to the [Cloudflare documentation](https://developers.cloudflare.com/fundamentals/reference/http-headers/), Cloudflare sets `X-Forwarded-Proto` itself and overwrites any value sent by the client. With `COOKIE_SECURE=auto` the cookie is therefore `Secure` through the tunnel and plain on the local network over `http://`, and sign-in works in both cases.

Things to keep in mind:

1. **A separate tunnel and a separate compose project**: leave existing tunnels alone.
2. **How cloudflared reaches the service.** The simplest way is to attach the cloudflared container to the `mediadl_net` network and set the tunnel's service to `http://mediadl:8790`:

   ```yaml
   services:
     cloudflared:
       image: cloudflare/cloudflared:latest
       command: tunnel --no-autoupdate run
       environment:
         - TUNNEL_TOKEN=${TUNNEL_TOKEN}
       networks: [mediadl_net]
       restart: unless-stopped
   networks:
     mediadl_net:
       external: true
   ```

3. **Visitor IP.** The login rate limit uses `CF-Connecting-IP`, which Cloudflare always overwrites. `X-Forwarded-For` is not suitable: Cloudflare appends to it and keeps whatever the client sent.
4. **Do not expose `HOST_PORT` to the internet.** The tunnel does not need it.
5. Optionally, put Cloudflare Access in front of the tunnel as a second layer. The built-in login stays in place.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Login is not configured" | `APP_PASSWORD_HASH` is missing or broken. Generate it with `docker compose run --rm mediadl hashpw`, put it into `.env`, run `docker compose up -d` |
| "Too many failed attempts" | Brute-force protection kicked in. Wait `LOGIN_LOCK_MINUTES` or restart the container |
| Sign-in succeeds but asks for the password again right away | `Secure` cookie over `http://`. Check `COOKIE_SECURE` (use `auto` or `false` on the local network) |
| `/library is not mounted` in the logs, the container keeps restarting | `LIBRARY_PATH` is not set or the directory does not exist on the host |
| `Permission denied` when writing files | `LIBRARY_PATH` is not writable for `PUID:PGID` |
| Instagram: `login required`, `redirect to login page`, `401` | Cookies are needed, see above |
| YouTube: `Sign in to confirm you're not a bot` | YouTube requires a login for the host's IP. Upload cookies for `youtube` |
| `Unsupported URL` / `No suitable extractor` | The chosen engine does not support the site. Try the "Try gallery-dl / Try yt-dlp" button |
| A site worked yesterday, today extraction fails | Settings → Update engines |
| A video does not play on a phone or TV | The "H.264" box was off: YouTube often offers its best quality only in AV1/VP9 (tested at 720p: without the box the file came in AV1). Turn the box on and download again. Files with the same name are not overwritten, so delete the old one in the library first |
| MP3 from a video: "unable to obtain file audio codec with ffprobe" | The video has no audio track. The analysis now detects this and does not offer audio formats |
| "The server is not reachable right now (HTTP 502)" | The container is restarting, usually after an update. Try again in a few seconds |
| `Your kernel does not support memory limit capabilities` on `up` | The host kernel has the memory cgroup disabled, so `MEM_LIMIT` has no effect (the CPU limit works). On Raspberry Pi OS, according to the [k3s documentation](https://docs.k3s.io/installation/requirements), it is enabled by appending `cgroup_memory=1 cgroup_enable=memory` to the single line in `/boot/firmware/cmdline.txt` and rebooting |
| Healthcheck `unhealthy` | `docker compose logs --tail=100` shows the error |

Service logs:

```bash
docker compose logs -f mediadl
```

## Security

- Login is mandatory. The password is stored only as a scrypt hash; sessions live on the server (the database keeps the token's SHA-256); the cookie is `HttpOnly` and `SameSite=Lax`.
- Every state-changing request checks a CSRF token. Dangerous actions (deleting files, updating engines, uploading cookies) require confirmation in the web UI.
- Library paths are checked: anything that resolves outside `/library` (`../`, absolute paths, symlinks pointing out) is rejected.
- Only `http(s)` links are accepted, and no local-network addresses. Engines run without a shell, and the link is passed after `--`.
- Analysis thumbnails are proxied by the analysis result's ID, so a browser cannot make the server fetch an arbitrary URL.
- The container runs as `PUID:PGID`, without `privileged` or `network_mode: host`, and with `no-new-privileges`.
