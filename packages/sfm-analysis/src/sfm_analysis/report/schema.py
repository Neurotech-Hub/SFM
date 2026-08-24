"""schema.py — report "designs" (JSON) compose "sections" (Python).

Mirrors the experiment system's split (base_station/experiment/schema.py):
JSON declares *what* to include and in what order; Python holds the
behavior. Resolution is dynamic import, exactly like
``experiment.schema.resolve_builder`` — no base class, no registry to keep
in sync by hand.

The one difference from the experiment system: a report section module
exposes a ``SECTIONS`` dict of several related section functions (e.g.
``sections/bandit.py`` holds ``choice_summary``, ``block_curve``,
``wsls``, ...) rather than a single ``build`` factory, since one module
naturally groups several analyses for the same experiment.

Naming note: ``templates/`` is already taken by
``base_station/experiment/templates/`` (the Python behavior for each
experiment, in the parent VFM repo). Report JSON specs therefore live in
this package's ``designs/`` (the noun "design"), not "templates" — and not
"reports", since that's the name of the *output* directory a report is
written into (see build_session_report below).
"""

from __future__ import annotations

import importlib
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def _packaged_designs_dir() -> Path:
    """Filesystem location of the report designs bundled with the package.

    Uses importlib.resources so the path is correct for a wheel installed
    anywhere on sys.path, but deliberately returns a real pathlib.Path
    rather than a Traversable: DEFAULT_REPORTS_DIR is public API and every
    caller does Path things with it (``/``, ``.exists()``, ``.glob()``,
    and passing it back in as ``load_report_defs(directory=...)``).

    pip always unpacks wheels, so files() yields a filesystem-backed path
    in every install mode this package supports (wheel, sdist, editable,
    git subdirectory). The __file__ fallback covers the zipimport case and
    any Python where the resources machinery misbehaves.
    """
    fallback = Path(__file__).resolve().parent / "designs"
    try:
        from importlib.resources import files
    except ImportError:  # pragma: no cover
        return fallback
    try:
        resolved = Path(str(files(f"{__package__}.designs")))
    except (ImportError, ModuleNotFoundError, TypeError, ValueError, OSError):
        return fallback
    return resolved if resolved.is_dir() else fallback


DEFAULT_REPORTS_DIR: Path = _packaged_designs_dir()


@dataclass
class SectionResult:
    """One rendered report section."""

    section_id: str
    title: str
    html: str                                          # a complete <section> body
    summary: Dict[str, Any] = field(default_factory=dict)   # feeds the KPI band + --json
    notes: List[str] = field(default_factory=list)          # data-quality caveats
    page_break_before: bool = False
    empty: bool = False                                 # nothing to say; section is omitted


@dataclass
class SectionContext:
    """Everything a section function may read. Sections must not touch the filesystem."""

    runs: List[Any]                # List[RunData] — 1 for a per-session report, N combined
    metrics: List[Any]             # List[RunMetrics], index-aligned with runs
    combined: bool
    align: str                     # "relative" | "wall" | "trial" | "event:<name>"
    opts: Dict[str, Any] = field(default_factory=dict)   # this section's JSON "options" block
    design: Optional["ReportDef"] = None


SectionFn = Callable[[SectionContext], Optional[SectionResult]]


@dataclass
class SectionSpec:
    ref: str                       # "generic.provenance", "bandit.block_curve"
    title: str = ""
    options: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    page_break_before: bool = False
    beta: bool = False   # appends " (beta)" to the rendered title — see run_section


@dataclass
class ReportDef:
    name: str
    label: str
    description: str = ""
    matches: List[str] = field(default_factory=list)     # experiment names this design serves
    sections: List[SectionSpec] = field(default_factory=list)
    combined_sections: List[SectionSpec] = field(default_factory=list)
    source_path: Optional[Path] = None


def _parse_section(raw: dict) -> SectionSpec:
    return SectionSpec(
        ref=str(raw["ref"]),
        title=str(raw.get("title", "")),
        options=dict(raw.get("options", {})),
        enabled=bool(raw.get("enabled", True)),
        page_break_before=bool(raw.get("page_break_before", False)),
        beta=bool(raw.get("beta", False)),
    )


def load_report_def(path: Path) -> ReportDef:
    """Load a single report design JSON file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReportDef(
        name=str(data["name"]),
        label=str(data.get("label", data["name"])),
        description=str(data.get("description", "")),
        matches=[str(m) for m in data.get("matches", [])],
        sections=[_parse_section(s) for s in data.get("sections", [])],
        combined_sections=[_parse_section(s) for s in data.get("combined_sections", [])],
        source_path=path,
    )


def load_report_defs(directory: Optional[Path] = None) -> List[ReportDef]:
    """Load all ``*.json`` report designs from a directory (sorted by name)."""
    root = Path(directory) if directory else DEFAULT_REPORTS_DIR
    if not root.is_dir():
        return []
    return [load_report_def(p) for p in sorted(root.glob("*.json"))]


def resolve_section(ref: str) -> SectionFn:
    """
    Resolve "module.func" to a section callable.

    Dynamically imports ``sfm_analysis.report.sections.<module>`` and
    returns its ``SECTIONS[func]`` entry — mirroring the VFM base
    station's experiment.schema.resolve_builder dynamic import, with a
    per-module dict instead of a single factory since one module holds
    several related sections.

    Uses ``__package__`` rather than a hardcoded string so this keeps
    working if the package is ever renamed or vendored.
    """
    mod_name, sep, func_name = ref.partition(".")
    if not sep:
        raise ValueError(f"Section ref '{ref}' must be 'module.function'")
    mod = importlib.import_module(f"{__package__}.sections.{mod_name}")
    table = getattr(mod, "SECTIONS", None)
    if not isinstance(table, dict) or func_name not in table:
        raise ImportError(f"Section '{ref}' not found in sections.{mod_name}.SECTIONS")
    return table[func_name]


def resolve_design(experiment: str, directory: Optional[Path] = None) -> ReportDef:
    """
    Pick the design for an experiment name.

    1. a design whose ``name`` == experiment
    2. a design listing it in ``matches``
    3. ``default.json``

    Never raises — an unknown experiment (or a missing/corrupt reports/
    directory) silently falls back to the generic design so a report is
    never blocked on a design-file problem.
    """
    defs = load_report_defs(directory)
    for d in defs:
        if d.name == experiment:
            return d
    for d in defs:
        if experiment in d.matches:
            return d
    for d in defs:
        if d.name == "default":
            return d
    # No default.json found on disk either — an empty-but-valid design so
    # callers still get *a* ReportDef rather than None.
    return ReportDef(name="default", label="Session Report")


def run_section(spec: SectionSpec, ctx: SectionContext) -> Optional[SectionResult]:
    """
    Resolve and call one section, isolating failures.

    A section that raises never kills the whole report — it renders a
    visible error block with the traceback instead, and returns a
    SectionResult(empty=False) so it still appears (as an error, not
    silently vanishing).
    """
    try:
        fn = resolve_section(spec.ref)
        result = fn(ctx)
        if result is not None and spec.title:
            result.title = spec.title
        if result is not None:
            result.page_break_before = result.page_break_before or spec.page_break_before
        if result is not None and spec.beta and not result.title.endswith("(beta)"):
            result.title = f"{result.title} (beta)"
        return result
    except Exception:
        tb = traceback.format_exc()
        title = spec.title or spec.ref
        if spec.beta and not title.endswith("(beta)"):
            title = f"{title} (beta)"
        return SectionResult(
            section_id=spec.ref,
            title=title,
            html=(
                f'<details class="error"><summary>Section "{spec.ref}" failed to render'
                f'</summary><pre>{_escape_for_pre(tb)}</pre></details>'
            ),
            page_break_before=spec.page_break_before,
        )


def _escape_for_pre(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
