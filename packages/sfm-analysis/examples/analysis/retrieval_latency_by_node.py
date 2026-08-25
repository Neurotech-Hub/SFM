#!/usr/bin/env python3
"""retrieval_latency_by_node.py — tidy-table recipe: per-node summary
stats with nothing beyond the standard library, or a pandas one-liner if
you have it installed.

Shows both paths deliberately: cycles_table() returns a plain list[dict]
so stdlib-only code always works, and to_dataframe() is there the moment
you want pandas/seaborn instead of hand-rolled grouping.

    python examples/analysis/retrieval_latency_by_node.py
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from sfm_analysis.analysis import load_session, to_dataframe
from sfm_analysis.report.demo import DEMO_SESSION_PATH


def main() -> None:
    s = load_session(str(DEMO_SESSION_PATH))
    cycles = s.cycles_table()

    print(f"{s.name}: {len(cycles)} dispense cycles in {s.run().run_label}")

    # Stdlib-only path: group by node, drop the Nones (a cycle whose dome
    # never opened, or the mouse never took the pellet, has no
    # retrieval_latency — that's meaningful, not missing data to impute).
    by_node = defaultdict(list)
    for row in cycles:
        if row["retrieval_latency"] is not None:
            by_node[row["node"]].append(row["retrieval_latency"])

    print("\nRetrieval latency by node (stdlib):")
    for node, values in sorted(by_node.items()):
        print(f"  node {node}: n={len(values):2d}  median={statistics.median(values):6.2f}s"
              f"  min={min(values):6.2f}s  max={max(values):6.2f}s")

    # pandas path, if available.
    try:
        df = to_dataframe(cycles)
    except ImportError:
        print("\n(install sfm-analysis[pandas] to see the pandas groupby path too)")
        return

    print("\nSame thing via pandas:")
    print(df.groupby("node")["retrieval_latency"].median())


if __name__ == "__main__":
    main()
