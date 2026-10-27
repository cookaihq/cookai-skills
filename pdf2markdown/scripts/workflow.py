#!/usr/bin/env python3
"""Claude Code, Codex and WorkBuddy share this entry point."""
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    import _runtime_bootstrap
    _runtime_bootstrap.ensure()
    from pdf2markdown.cli import main
    raise SystemExit(main())
