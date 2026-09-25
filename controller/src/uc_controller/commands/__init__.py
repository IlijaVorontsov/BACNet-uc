"""uc-ctl command modules. Each defines ``register(subparsers)`` and sets a
handler with ``parser.set_defaults(func=handler)``; the handler takes the
parsed arguments and returns an exit code (DESIGN.md §19.3)."""
