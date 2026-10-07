"""Print `USER:BCRYPT_HASH` for the standalone console: `python -m app.hash_password USER`.

The password is read without echo (or from stdin when piped) and never printed. Put the output in
the file named by ARTIFACTS_CONSOLE_HTPASSWD_FILE (mode 0600), or split it into ARTIFACTS_CONSOLE_USER
and ARTIFACTS_CONSOLE_PASSWORD_HASH. `htpasswd -nBC 12 USER` produces a compatible line.
"""

import getpass
import sys

import bcrypt


def main() -> int:
    if len(sys.argv) != 2 or ":" in sys.argv[1]:
        print("usage: python -m app.hash_password USER", file=sys.stderr)
        return 2
    password = getpass.getpass("Console password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    if not 12 <= len(password.encode()) <= 72:
        print("the password must be 12-72 bytes", file=sys.stderr)
        return 2
    print(f"{sys.argv[1]}:{bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
