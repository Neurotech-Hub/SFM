"""
cli/report.py — generate an HTML behavior report from VFM session logs.

Point it at a session name (or a shell glob, or an explicit .csv path) and
it writes a single self-contained, printable HTML file next to the source
CSV (or wherever ``--out`` says). Pass more than one target with
``--combine`` for a comparative report across sessions.

Installed as the ``sfm-report`` console script. The VFM base station's own
``run_report.py`` is a thin wrapper around this module that additionally
injects the base station's own (writable-probing) log-directory resolver
— see that file's docstring for why the two resolvers are kept separate.

Examples::

    # Try it with no rig, no log directory, and no real data at all:
    sfm-report --demo --open

    # List sessions found in the default log directory:
    sfm-report --list

    # One session, opened in the default browser when done:
    sfm-report EXP-Test-02 --open

    # Every session matching a glob, combined into one report:
    sfm-report "cohortA_*" --combine -o /tmp/cohort.html

    # Check how session names parse into subject/cohort/day:
    sfm-report --check-names
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from ..report import build_combined_report, build_session_report
from ..report.loader import LogSchema, discover_sessions, sniff_schema
from ..report.naming import parse_session_name
from ..report.schema import DEFAULT_REPORTS_DIR, load_report_defs
from ..storage import default_log_dir as _sdk_default_log_dir
from ..targeting import resolve_targets as _resolve_targets


def _print_list(log_dir: Path) -> None:
    refs = discover_sessions(log_dir)
    if not refs:
        print(f"No sessions found in {log_dir}")
        return
    print(f"Sessions in {log_dir}:")
    print(f"  {'session':30s} {'schema':10s} {'size':>10s}")
    for ref in refs:
        usable = "" if ref.schema == LogSchema.UNIFIED else "  (unsupported schema, will be skipped)"
        print(f"  {ref.session:30s} {ref.schema.value:10s} {ref.size_bytes:>10d}{usable}")


def _print_designs() -> None:
    defs = load_report_defs(DEFAULT_REPORTS_DIR)
    if not defs:
        print(f"No report designs found in {DEFAULT_REPORTS_DIR}")
        return
    print("Available report designs:")
    for d in defs:
        matches = ", ".join(d.matches) if d.matches else "(fallback for any experiment)"
        print(f"  {d.name:22s} {d.label}")
        print(f"  {'':22s} matches: {matches}")


def _print_check_names(log_dir: Path) -> None:
    refs = discover_sessions(log_dir)
    if not refs:
        print(f"No sessions found in {log_dir}")
        return
    print(f"{'session':30s} {'subject':10s} {'cohort':10s} {'day':5s} parsed?")
    for ref in refs:
        ident = parse_session_name(ref.session)
        print(f"{ref.session:30s} {ident.subject or '—':10s} {ident.cohort or '—':10s} "
              f"{str(ident.day) if ident.day is not None else '—':5s} {'yes' if ident.parsed else 'no'}")


def _filter_by_date(paths: List[Path], refs_by_path: dict, since: Optional[str], until: Optional[str]) -> List[Path]:
    """Filter by file modification time as a proxy for session start date."""
    if not since and not until:
        return paths
    since_dt = datetime.strptime(since, "%Y-%m-%d") if since else None
    until_dt = datetime.strptime(until, "%Y-%m-%d") if until else None
    out = []
    for p in paths:
        ref = refs_by_path.get(p)
        if ref is None:
            out.append(p)
            continue
        mtime = datetime.fromtimestamp(ref.mtime)
        if since_dt and mtime < since_dt:
            continue
        if until_dt and mtime > until_dt:
            continue
        out.append(p)
    return out


def _prompt_picker(log_dir: Path) -> tuple:
    """Interactive session picker (TTY only, no target given). Returns (paths, combine)."""
    refs = discover_sessions(log_dir)
    if not refs:
        print(f"No sessions found in {log_dir}")
        return [], False
    print(f"Sessions in {log_dir}:")
    for i, ref in enumerate(refs, start=1):
        ident = parse_session_name(ref.session)
        label = ident.subject or ref.session
        print(f"  {i:>3d}  {ref.session:30s} {label:12s} {ref.schema.value}")

    selection = input("Select sessions (e.g. '3', '1,4,7', '2-6', 'all', or blank to cancel): ").strip()
    if not selection:
        return [], False
    indices = set()
    if selection.lower() == "all":
        indices = set(range(1, len(refs) + 1))
    else:
        for part in selection.split(","):
            part = part.strip()
            if "-" in part:
                lo, _, hi = part.partition("-")
                if lo.strip().isdigit() and hi.strip().isdigit():
                    indices.update(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                indices.add(int(part))
    paths = [refs[i - 1].path for i in sorted(indices) if 1 <= i <= len(refs)]
    combine = False
    if len(paths) > 1:
        answer = input("Combine into one comparative report? [y/N]: ").strip().lower()
        combine = answer.startswith("y")
    return paths, combine


def main(argv: Optional[List[str]] = None, *, default_log_dir: Optional[Callable[[], Path]] = None) -> int:
    """
    Entry point. ``default_log_dir`` is injectable so a caller can supply
    its own resolver instead of the SDK's read-only one — the VFM base
    station's run_report.py shim does this to keep using its writable-
    probing directory resolver (base_station.storage.default_log_dir),
    so that default log-dir resolution on the base station is unchanged
    by this CLI having moved into the SDK.
    """
    resolve_log_dir = default_log_dir or _sdk_default_log_dir

    parser = argparse.ArgumentParser(
        prog="sfm-report",
        description="Generate an HTML behavior report from VFM session logs.",
    )
    parser.add_argument("target", nargs="*",
                        help="Session name, shell glob, or path to a .csv (repeatable)")
    parser.add_argument("--log-dir", default=None, help="Default: auto-detected external drive / ~/sfm_logs")
    parser.add_argument("--list", action="store_true", help="List discovered sessions and exit")
    parser.add_argument("--list-designs", action="store_true", help="List report designs and exit")
    parser.add_argument("--check-names", action="store_true",
                        help="Show how each session name parses (subject/cohort/day) and exit")
    parser.add_argument("--combine", action="store_true", help="One comparative report over all targets")
    parser.add_argument("--all", action="store_true", help="Every session in --log-dir")
    parser.add_argument("--demo", action="store_true",
                        help="Render the bundled demo session (no rig or log dir needed)")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="Only sessions modified on/after this date")
    parser.add_argument("--until", default=None, metavar="YYYY-MM-DD",
                        help="Only sessions modified on/before this date")
    parser.add_argument("--run", type=int, default=None, dest="run_id",
                        help="Only this run_id (default: all runs in the file)")
    parser.add_argument("--design", default=None, help="Force a report design by name (default: auto by experiment)")
    parser.add_argument("--align", default="relative",
                        help="Combined-report time alignment: relative | wall | trial | event:<name> "
                             "(default: relative)")
    parser.add_argument("--out", "-o", default=None, help="Output file path")
    parser.add_argument("--title", default=None, help="Override the document title")
    parser.add_argument("--no-explorer", action="store_true",
                        help="Skip the embedded interactive timeline (guaranteed script-free, leaner output)")
    parser.add_argument("--open", action="store_true", help="Open the result in the default browser when done")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.demo and (args.target or args.all):
        parser.error("--demo cannot be combined with explicit targets or --all")
    include_explorer = not args.no_explorer

    log_dir = Path(args.log_dir).expanduser() if args.log_dir else Path(resolve_log_dir())

    if args.list:
        _print_list(log_dir)
        return 0
    if args.list_designs:
        _print_designs()
        return 0
    if args.check_names:
        _print_check_names(log_dir)
        return 0

    align = args.align
    if align not in ("relative", "wall", "trial") and not align.startswith("event:"):
        parser.error("--align must be one of relative | wall | trial | event:<name>")

    refs = discover_sessions(log_dir)
    refs_by_path = {r.path: r for r in refs}

    targets = args.target
    prompted_combine = False
    if args.demo:
        from ..report.demo import DEMO_SESSION_PATH
        paths = [DEMO_SESSION_PATH]
        if not args.out:
            # DEMO_SESSION_PATH lives inside the installed package itself;
            # every other build_session_report default writes next to its
            # source CSV, which here means the package's own site-packages
            # directory -- not writable on a normal (non-editable) install.
            # Default to the current directory instead, same filename
            # build_session_report would otherwise have chosen.
            args.out = str(Path.cwd() / f"{DEMO_SESSION_PATH.stem}_report.html")
    elif args.all:
        if targets:
            parser.error("--all cannot be combined with explicit targets")
        paths = [r.path for r in refs]
    elif not targets:
        if sys.stdin.isatty():
            paths, prompted_combine = _prompt_picker(log_dir)
            if not paths:
                return 1
        else:
            parser.error("a target (session name / glob / .csv path) is required, or use --list / --all")
    else:
        paths = _resolve_targets(targets, log_dir)

    paths = _filter_by_date(paths, refs_by_path, args.since, args.until)

    if not paths:
        print(f"No session matched: {' '.join(targets) or '(--all with --since/--until filters)'}", file=sys.stderr)
        return 1

    combine = args.combine or prompted_combine

    usable, skipped = [], []
    for p in paths:
        if sniff_schema(p) == LogSchema.UNIFIED:
            usable.append(p)
        else:
            skipped.append(p)
    for p in skipped:
        print(f"skipping {p.name}: unsupported schema (not the unified 15-column log format)", file=sys.stderr)
    if not usable:
        print("No usable sessions (all targets had an unsupported schema).", file=sys.stderr)
        return 2

    # Whether --out is a single output file or a directory of reports is
    # decided by how many targets were *requested*, not how many survived
    # schema filtering — so "session_a session_b --out X" always means "X
    # is a directory", even if one of the two turns out to be unusable.
    multi_target = len(paths) > 1

    outputs: List[Path] = []
    try:
        if combine:
            outputs.append(build_combined_report(
                usable, Path(args.out) if args.out else None,
                design=args.design, align=align, title=args.title,
            ))
        elif not multi_target:
            outputs.append(build_session_report(
                usable[0], Path(args.out) if args.out else None,
                run_id=args.run_id, design=args.design, align=align,
                title=args.title, include_explorer=include_explorer,
            ))
        else:
            # Multiple targets without --combine: one report per session,
            # written into --out as a directory (default: each session's
            # own reports/ folder next to its CSV).
            out_dir = Path(args.out) if args.out else None
            if out_dir:
                out_dir.mkdir(parents=True, exist_ok=True)
            for p in usable:
                report_dest = out_dir / f"{p.stem}_report.html" if out_dir else None
                outputs.append(build_session_report(
                    p, report_dest, run_id=args.run_id, design=args.design, align=align,
                    include_explorer=include_explorer,
                ))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        for out in outputs:
            print(f"Report written: {out}")

    if args.open:
        for out in outputs:
            if not webbrowser.open(out.resolve().as_uri()):
                print(f"warning: could not open {out}", file=sys.stderr)

    return 0


def cli_main() -> None:
    """Console-script entry point (``sfm-report``)."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
