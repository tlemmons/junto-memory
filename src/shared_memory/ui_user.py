"""Host CLI for human web logins (ui_auth.py). Accounts are never created over the web.

    docker exec -it mcp-rag-arch python -m shared_memory.ui_user add tom [--role owner] [--projects a,b]
    docker exec -it mcp-rag-arch python -m shared_memory.ui_user passwd tom
    docker exec -it mcp-rag-arch python -m shared_memory.ui_user list
    docker exec -it mcp-rag-arch python -m shared_memory.ui_user disable tom   (also ends its sessions)
    docker exec -it mcp-rag-arch python -m shared_memory.ui_user enable tom

Passwords are read with getpass (twice), never from argv, so they stay out of
shell history and `ps`.
"""

import argparse
import getpass
import sys

from shared_memory import ui_auth


def _ask() -> str:
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Again: "):
        sys.exit("Passwords don't match.")
    return pw


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m shared_memory.ui_user", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--role", default="owner")
    a.add_argument("--projects", default="", help="comma list; empty = unscoped")
    for c in ("passwd", "disable", "enable"):
        sub.add_parser(c).add_argument("username")
    sub.add_parser("list")
    args = p.parse_args(argv)

    from shared_memory.clients import get_mongo
    db = get_mongo()
    if db is None:
        print("MongoDB unavailable", file=sys.stderr)
        return 1
    if args.cmd == "add":
        out = ui_auth.create_user(db, args.username, _ask(), args.role,
                                  [x.strip() for x in args.projects.split(",") if x.strip()])
    elif args.cmd == "passwd":
        out = ui_auth.set_password(db, args.username, _ask())
    elif args.cmd in ("disable", "enable"):
        out = ui_auth.set_disabled(db, args.username, args.cmd == "disable")
    else:
        for u in ui_auth.list_users(db):
            print(f"{u['username']:20} {u['role']:8} {','.join(u['projects']) or '(unscoped)':20}"
                  f"{'  DISABLED' if u['disabled'] else ''}")
        return 0
    if out.get("error"):
        print(out["error"], file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
