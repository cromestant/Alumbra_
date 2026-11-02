"""Manage app tokens on the server (app/tokens.py). Run on the VPS:

    .venv/bin/python backend/tools/tokens.py status
    .venv/bin/python backend/tools/tokens.py list
    .venv/bin/python backend/tools/tokens.py set mint_cap 200
    .venv/bin/python backend/tools/tokens.py set ask_rate 20/300
    .venv/bin/python backend/tools/tokens.py revoke t_042
    .venv/bin/python backend/tools/tokens.py revoke --all
    .venv/bin/python backend/tools/tokens.py unban t_042

Limits (set): mint_cap N; mint_rate, ask_rate, ask_cap as COUNT/SECONDS.
They take effect on the next request, no restart. revoke bans the
token's install id too (the app would otherwise just re-register);
revoke --all bans every install, so new phones still register up to
mint_cap; lower mint_cap to close that as well.

Uses DATA_DIR from the environment or the repo-root .env, like the
service. Live ask counts (ask_rate, ask_cap) are in the server's
memory, so status shows lifetime asks per token instead.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import tokens  # noqa: E402


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def status() -> None:
    rows = tokens.rows()
    lim = tokens.limits()
    live = sum(1 for r in rows if not r["revoked"] and not r["banned"])
    print(f"store:     {tokens.data_dir() / 'tokens.db'}")
    print(f"minted:    {len(rows)} / {lim['mint_cap']}  ({live} live, "
          f"{len(rows) - live} revoked)")
    for key in ("mint_rate", "ask_rate", "ask_cap"):
        print(f"{key + ':':10} {lim[key]}")
    top = sorted(rows, key=lambda r: r["asks"], reverse=True)[:5]
    if top and top[0]["asks"]:
        print("top:       " + ", ".join(
            f"{r['id']} {r['asks']} asks (last {_ago(r['last_used'])})"
            for r in top if r["asks"]))


def list_rows() -> None:
    print(f"{'id':6} {'install':16} {'minted':10} {'last ask':10} "
          f"{'asks':>5}  state")
    for r in tokens.rows():
        state = "banned" if r["banned"] else (
            "revoked" if r["revoked"] else "live")
        print(f"{r['id']:6} {r['install_id'][:16]:16} "
              f"{_ago(r['created']):10} {_ago(r['last_used']):10} "
              f"{r['asks']:>5}  {state}")


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("status", [])
    if cmd == "status" and not args:
        status()
    elif cmd == "list" and not args:
        list_rows()
    elif cmd == "set" and len(args) == 2:
        try:
            tokens.set_limit(args[0], args[1])
        except KeyError:
            print(f"unknown limit {args[0]!r}; one of "
                  f"{', '.join(tokens.DEFAULT_LIMITS)}", file=sys.stderr)
            return 2
        except ValueError:
            print(f"bad value {args[1]!r}: mint_cap takes N, the rates "
                  "COUNT/SECONDS", file=sys.stderr)
            return 2
        print(f"{args[0]} = {tokens.limits()[args[0]]}")
    elif cmd == "revoke" and args == ["--all"]:
        print(f"revoked {tokens.revoke_all()} tokens; every install is "
              "banned. New installs still register up to mint_cap.")
    elif cmd == "revoke" and len(args) == 1:
        install = tokens.revoke(args[0])
        if install is None:
            print(f"no token {args[0]}", file=sys.stderr)
            return 1
        print(f"revoked {args[0]}; install {install} banned")
    elif cmd == "unban" and len(args) == 1:
        if not tokens.unban(args[0]):
            print(f"no token {args[0]}", file=sys.stderr)
            return 1
        print(f"unbanned {args[0]}; its install can register again")
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
