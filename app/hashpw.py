"""Prints an APP_PASSWORD_HASH line for .env: docker compose run --rm mediadl hashpw"""
import getpass
import sys

from .auth import hash_password


def main() -> int:
    first = getpass.getpass("New password: ")
    if len(first) < 10:
        print("Use at least 10 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat password: ") != first:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    print(f"APP_PASSWORD_HASH={hash_password(first)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
