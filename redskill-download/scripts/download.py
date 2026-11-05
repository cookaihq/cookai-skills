#!/usr/bin/env python3
"""Noninteractive CLI; downloaded Skill instructions are data, never executable input."""
import _runtime_bootstrap

if __name__ == "__main__":
    _runtime_bootstrap.ensure()

import argparse
import json
import sys
import zipfile
from pathlib import Path

from redskill import config, inputs, workflow
from redskill.errors import DownloadError


def main():
    parser = argparse.ArgumentParser(description="Download an official RedSkill ZIP without installing it")
    parser.add_argument("command", choices=("download", "config", "configure"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", help="One identifier or the copied share instruction")
    source.add_argument("--input-file", type=Path, help="UTF-8 file containing the share instruction")
    parser.add_argument("--workspace", type=Path, required=True, help="Agent workspace root, explicitly supplied by the host")
    parser.add_argument("--output-dir", help="One-call override, relative to --workspace")
    globals_group = parser.add_mutually_exclusive_group()
    globals_group.add_argument("--use-global-config", action="store_true")
    globals_group.add_argument("--no-global-config", action="store_true")
    parser.add_argument("--scope", choices=("global", "project"), default="global", help="configure only")
    parser.add_argument("--directory", help="configure only: directory to persist")
    args = parser.parse_args()
    try:
        resolved = config.resolve(args.workspace, use_global=args.use_global_config, output=args.output_dir)
        if args.command == "configure":
            if args.directory is None:
                parser.error("configure requires --directory")
            written = config.configure(args.directory, scope=args.scope)
            after = config.resolve(args.workspace, use_global=args.scope == "global" or args.use_global_config)
            payload = {"status": "configured", "written_file": written, "config": after,
                       "saved_value_effective": after["source"] == written}
        elif args.command == "config":
            payload = {"status": "configuration", "config": resolved}
        else:
            if args.input_file:
                if args.input_file.stat().st_size > 65536:
                    raise DownloadError("input_too_large", "Share instruction exceeds 64 KiB")
                text = args.input_file.read_text(encoding="utf-8-sig")
            else:
                text = args.input or ""
            identifier = inputs.parse_identifier(text)
            payload = workflow.run(identifier, resolved)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except DownloadError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        # OSError text can include paths; never include a raw HTTP exception with a signed URL.
        print(json.dumps({"status": "failed", "code": "local_operation_failed", "error_type": type(exc).__name__,
                          "message": str(exc)[:500]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
