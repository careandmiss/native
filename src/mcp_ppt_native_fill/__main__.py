"""Entrypoint: ``python -m mcp_ppt_native_fill`` serves stdio."""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
