"""Single CLI boundary used by hosts and behavioral tests."""
import argparse
import json
from pathlib import Path
import sys


def report_error(exc):
    error = {"status": "error", "error": str(exc), "kind": type(exc).__name__}
    if hasattr(exc, "retry_at"):
        error["retry_at"] = exc.retry_at
    print(json.dumps(error, ensure_ascii=False))
    return 2


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from _runtime_bootstrap import ensure
    ensure()
    from pdfvision import config, task
    from pdfvision.files import read_json
except Exception as runtime_error:
    raise SystemExit(report_error(runtime_error))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Native host vision PDF workflow")
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start")
    start.add_argument("source")
    start.add_argument("--project", type=Path, required=True)
    start.add_argument("--pages")
    start.add_argument("--owner", required=True)
    start.add_argument("--vision", choices=["available", "unavailable"], required=True)
    start.add_argument("--related-task", type=Path)
    start.add_argument("--track-data", action="store_true")
    start.add_argument("--requirements", type=Path)

    def cleaning_arguments(cmd):
        group = cmd.add_mutually_exclusive_group()
        group.add_argument("--cleaning")
        group.add_argument("--no-cleaning", action="store_true")
        cmd.add_argument("--instructions", type=Path)
        cmd.add_argument("--use-global-config", action="store_true")

    cleaning_arguments(start)
    for name in ("config", "profile-save", "default-profile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", type=Path, required=True)
        cmd.add_argument("--use-global-config", action="store_true")
        if name == "profile-save":
            cmd.add_argument("--name", required=True)
            cmd.add_argument("--input", type=Path, required=True)
            cmd.add_argument("--set-default", action="store_true")
        elif name == "default-profile":
            cmd.add_argument("--profile", required=True)
    for name in ("status", "render", "submit-page", "submit-cleaning", "block-cleaning", "fail-page", "pause", "resume", "adopt", "finish"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--task", type=Path, required=True)
        if name not in {"status", "resume", "adopt"}:
            cmd.add_argument("--writer", required=True)
        if name in {"render", "submit-page", "fail-page"}:
            cmd.add_argument("--page", type=int, required=True)
        if name == "render":
            cmd.add_argument("--dpi", type=int, default=180)
            cmd.add_argument("--crop", type=float, nargs=4)
            cmd.add_argument("--reason")
        if name in {"submit-page", "submit-cleaning"}:
            cmd.add_argument("--input", type=Path, required=True)
        if name in {"fail-page", "pause", "block-cleaning"}:
            cmd.add_argument("--reason", required=True)
        if name in {"resume", "adopt"}:
            cmd.add_argument("--owner", required=True)
        if name == "resume":
            cleaning_arguments(cmd)
            cmd.add_argument("--project", type=Path)
            cmd.add_argument("--writer")
            cmd.add_argument("--takeover", action="store_true")
            cmd.add_argument("--vision", choices=["available", "unavailable"], required=True)
            cmd.add_argument("--pages")
            cmd.add_argument("--redo-pages")
            cmd.add_argument("--requirements", type=Path)
        if name == "adopt":
            cmd.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    try:
        cleaning_options = {key: getattr(args, key, None) for key in ("cleaning", "no_cleaning", "instructions", "use_global")}
        cleaning_options["use_global"] = getattr(args, "use_global_config", False)
        if cleaning_options["no_cleaning"] and cleaning_options["instructions"]:
            raise ValueError("--no-cleaning cannot be combined with --instructions")
        if args.action == "start":
            result = task.start(args.source, args.project.resolve(), args.pages, args.owner, args.vision,
                                related_task=args.related_task, track_data=args.track_data,
                                requirements=args.requirements, **cleaning_options)
        elif args.action == "config":
            result = config.effective(args.project.resolve(), args.use_global_config)
        elif args.action == "profile-save":
            result = config.save_profile(args.project.resolve(), args.name, args.input, args.set_default, args.use_global_config)
        elif args.action == "default-profile":
            result = config.save_default(args.project.resolve(), args.profile, args.use_global_config)
        else:
            root = args.task.resolve()
            with task.locked(root):
                state = task.load(root)
                with task.recording(root, state, args.action, getattr(args, "writer", None)):
                    if args.action == "status":
                        result = task.status(root, state)
                    elif args.action == "resume":
                        result = task.resume(root, state, args.owner, args.vision, writer=args.writer,
                                             takeover=args.takeover, pages=args.pages, redo_pages=args.redo_pages,
                                             project=args.project.resolve() if args.project else None,
                                             requirements=args.requirements, **cleaning_options)
                    elif args.action == "adopt":
                        result = task.adopt(root, state, args.result, args.owner)
                    else:
                        task.require_writer(state, args.writer)
                        if args.action == "render":
                            result = task.render_page(root, state, args.page, args.dpi, args.crop, args.reason)
                        elif args.action == "submit-page":
                            result = task.submit_page(root, state, args.page, read_json(args.input))
                        elif args.action == "submit-cleaning":
                            result = task.submit_cleaning(root, state, read_json(args.input))
                        elif args.action == "block-cleaning":
                            result = task.block_cleaning(root, state, args.reason)
                        elif args.action == "fail-page":
                            result = task.fail_page(root, state, args.page, args.reason)
                        elif args.action == "pause":
                            result = task.pause(root, state, args.reason)
                        else:
                            result = task.finish(root, state)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        return report_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
