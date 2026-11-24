"""Service-startup marker (real wiring lands with T5.2).

Two jobs, one file:

1. Sentinel for the ``Makefile``'s ``config-check`` target: the guard is
   ``python -c "import app.__main__"``, so importing this module cleanly
   (no side effects at import time!) flips config-check from SKIP to the
   real ``python -m app.config check`` — the T0.4 promise, landing in T1.6.
2. Honest failure of ``python -m app`` until the API entry exists.
"""

import sys


def main(argv: list[str] | None = None) -> int:
    del argv  # nothing to parse before T5.2
    print(
        "service startup is wired in T5.2 (`make dev` / uvicorn "
        "app.main:app); for config tooling run "
        "`python -m app.config check|dump`.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
