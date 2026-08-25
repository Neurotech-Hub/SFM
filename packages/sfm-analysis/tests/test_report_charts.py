"""Tests for sfm_analysis.report.charts."""

import xml.etree.ElementTree as ET

from sfm_analysis.report import charts as c
from sfm_analysis.report.charts import Frame, Mark, Series, Span


def _parse(svg_body: str, title: str = "t") -> ET.Element:
    frame = Frame()
    full = c.svg(frame, svg_body, title=title)
    return ET.fromstring(full)


class TestPrimitivesParse:
    """Every primitive's output must round-trip through ElementTree.fromstring —
    this is what actually catches an unescaped & or < from real session/event text."""

    def setup_method(self):
        self.f = Frame()
        self.x = c.linear(0, 100, self.f.px0, self.f.px1)
        self.y = c.linear(0, 10, self.f.py1, self.f.py0)

    def test_axes(self):
        body = c.axes(self.f, self.x, self.y, x_ticks=[(0, "0"), (100, "100")], y_ticks=[0, 5, 10])
        _parse(body)

    def test_bars_grouped_and_stacked(self):
        series = [Series("S1", [1, 2, 3], 0), Series("S2", [3, 1, 2], 1)]
        _parse(c.bars(self.f, self.x, self.y, ["a", "b", "c"], series))
        _parse(c.bars(self.f, self.x, self.y, ["a", "b", "c"], series, stacked=True))
        _parse(c.bars(self.f, self.x, self.y, ["a", "b", "c"], series, horizontal=True))

    def test_line_with_markers_and_band(self):
        pts = [(0, 1), (50, 5), (100, 2)]
        band = [(0, 0.5, 1.5), (50, 4, 6), (100, 1, 3)]
        _parse(c.line(self.f, self.x, self.y, pts, key=1, markers=True, band=band))

    def test_line_step(self):
        _parse(c.line(self.f, self.x, self.y, [(0, 1), (50, 3)], step=True))

    def test_error_bars(self):
        pts = [(0, 1), (50, 5)]
        _parse(c.error_bars(self.f, self.x, self.y, pts, lo=[0.5, 4], hi=[1.5, 6]))

    def test_spans_and_raster(self):
        spans_body = c.spans(self.f, self.x, ["n1", "n2"],
                              [Span(0, 0, 50, key=0), Span(1, 20, 80, key=1, hatch=True, open_ended=True)])
        _parse(spans_body)
        raster_body = c.raster(self.f, self.x, ["n1"],
                                [Mark(0, 10, "circle", 0), Mark(0, 50, "cross", 1), Mark(0, 90, "diamond", 2)])
        _parse(raster_body)

    def test_histogram(self):
        _parse(c.histogram(self.f, [1, 2, 2, 3, 3, 3, 4, 4, 5, 6, 7, 1, 2, 3]))

    def test_histogram_empty_is_empty_state_not_exception(self):
        body = c.histogram(self.f, [])
        _parse(body)
        assert "no data" in body

    def test_ecdf(self):
        _parse(c.ecdf(self.f, [Series("A", list(range(1, 11)))]))

    def test_ecdf_empty(self):
        body = c.ecdf(self.f, [Series("A", [])])
        _parse(body)
        assert "no data" in body

    def test_dot_strip(self):
        _parse(c.dot_strip(self.f, [Series("A", [1, 2, 3]), Series("B", [4, 5])]))

    def test_distribution_picks_dot_strip_below_8(self):
        body = c.distribution(self.f, [Series("A", [1, 2, 3])])
        _parse(body)
        # dot_strip draws circle markers; ecdf draws a stepped path — presence
        # of <circle> or <polygon>/<line> marker glyphs indicates dot_strip.
        assert "<path" not in body or "<circle" in body or "<polygon" in body

    def test_distribution_picks_ecdf_at_or_above_8(self):
        body = c.distribution(self.f, [Series("A", list(range(20)))])
        _parse(body)
        assert "<path" in body   # ecdf draws a stepped path

    def test_heatmap(self):
        _parse(c.heatmap(self.f, [[0.1, 0.5], [0.9, None]], ["r1", "r2"], ["c1", "c2"]))

    def test_defs_appears_and_parses(self):
        d = c.defs()
        ET.fromstring(d)
        assert d.count("<pattern") == 8   # one per HATCH_ANGLE slot


class TestEscaping:
    def test_ampersand_and_angle_brackets_escaped_and_still_parses(self):
        body = c.dot_strip(Frame(), [Series("A & B <2>", [1, 2, 3])])
        full = c.svg(Frame(), body, title="A & B <2>")
        ET.fromstring(full)  # would raise ParseError if unescaped
        assert "&amp;" in full
        assert "&lt;" in full
        assert "A & B <2>" not in full  # raw form must not appear unescaped

    def test_escape_text_handles_quotes(self):
        assert c.escape_text('He said "hi"') == "He said &quot;hi&quot;"


class TestNiceTicks:
    def test_degenerate_domains_return_finite_sorted_nonempty(self):
        for d0, d1 in [(0, 1), (0, 0), (-5, 5), (1e-6, 1e-6), (5, 0)]:
            ticks = c.nice_ticks(d0, d1)
            assert len(ticks) > 0
            assert all(isinstance(t, (int, float)) for t in ticks)
            assert ticks == sorted(ticks)

    def test_linear_degenerate_domain_does_not_divide_by_zero(self):
        scale = c.linear(0, 0, 10, 100)
        assert scale(0) == 10.0  # falls back to p0, no exception


class TestNoScriptOrNetwork:
    def test_chart_output_contains_no_script_or_urls(self):
        f = Frame()
        body = c.bars(f, c.linear(0, 10, f.px0, f.px1), c.linear(0, 10, f.py1, f.py0),
                       ["a"], [Series("S", [5], 0)])
        full = c.svg(f, body, title="t")
        # xmlns="http://www.w3.org/2000/svg" is the required SVG namespace
        # URI, not a fetchable resource — only reject href/src pointing
        # off-document, which would break the self-contained-file invariant.
        assert "<script" not in full
        assert "href=" not in full
        assert " src=" not in full
