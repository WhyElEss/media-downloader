"""Import one platform's cookies from Safari's Cookies.binarycookies.

Safari has no cookies.txt export and yt-dlp reads its store only on macOS,
so the Mac streams the file over SSH and this keeps the platform's cookies:

    ssh <host> 'docker exec -i -u <PUID>:<PGID> -w /srv mediadl \
        /opt/engines/bin/python -m app.safari_cookies instagram' \
        < ~/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies

The file arrives on stdin and is never written to disk; only the matching
cookies are saved, the same way as an upload from Settings. --dry-run prints
cookie names (never values) and saves nothing.
"""
import argparse
import os
import sys
import tempfile

from yt_dlp.cookies import YoutubeDLCookieJar, parse_safari_cookies

from . import cookies
from .config import TMP
from .engines import PLATFORM_ALIASES


def domains_for(platform: str) -> list[str]:
    names = [host for host, name in PLATFORM_ALIASES.items() if name == platform]
    return names or [f"{platform}.com"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("platform", help="platform name as in the library, e.g. instagram")
    parser.add_argument("--dry-run", action="store_true", help="list matching cookie names only")
    args = parser.parse_args()

    data = sys.stdin.buffer.read()
    if not data.startswith(b"cook"):
        print("stdin is not a Safari Cookies.binarycookies file.", file=sys.stderr)
        return 1
    domains = domains_for(args.platform)
    jar = YoutubeDLCookieJar()
    for cookie in parse_safari_cookies(data):
        host = cookie.domain.lstrip(".").lower()
        if any(host == d or host.endswith("." + d) for d in domains):
            jar.set_cookie(cookie)

    names = sorted({c.name for c in jar})
    print(f"{len(jar)} cookie(s) for {', '.join(domains)}: {', '.join(names) or '-'}")
    if not len(jar):
        print("Nothing to save. Sign in to the site in Safari, quit Safari, try again.", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0

    TMP.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=TMP, prefix="safari-")
    os.close(fd)
    try:
        jar.save(tmp)
        with open(tmp, "rb") as fh:
            count = cookies.save(args.platform, fh.read())
    finally:
        os.unlink(tmp)
    print(f"Saved {count} cookie(s) as {args.platform}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
