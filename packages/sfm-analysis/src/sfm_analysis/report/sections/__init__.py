"""sections — report rendering, split from sfm_analysis.report.analyses.

Each module exposes a ``SECTIONS: Dict[str, SectionFn]`` registry at its
tail (see schema.resolve_section). Modules here turn already-computed
metrics into HTML; they must not recompute anything analyses/*.py already
provides, and must not touch the filesystem (schema.SectionContext is
built once by report/__init__.py and handed to every section read-only).
"""
