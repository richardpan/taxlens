"""Smoke tests for the v0.34 visualizations: bracket-fill heatmap and
carryforward vintage composition. These guard against the chart code
silently being deleted or wired to the wrong DOM ids."""
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / "src" / "taxlens" / "web"
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")


def test_bracket_heatmap_container_exists():
    assert 'id="trendsBracketHeat"' in INDEX_HTML, (
        "Bracket-fill heatmap container missing from index.html"
    )


def test_carryforward_vintage_card_exists():
    assert 'id="trendsCarryCard"' in INDEX_HTML
    assert 'id="trendsCarryVintages"' in INDEX_HTML


def test_bracket_heatmap_renderer_wired():
    assert "drawBracketHeatmap" in APP_JS
    assert "drawBracketHeatmap('trendsBracketHeat'" in APP_JS


def test_carryforward_vintage_renderer_wired():
    assert "drawCarryforwardVintages" in APP_JS
    assert "drawCarryforwardVintages('trendsCarryVintages'" in APP_JS


def test_heatmap_reads_correct_bracket_fields():
    """The chart uses BracketFill.lower / .upper — not the older invented
    bracket_low / bracket_high names. Catch the regression I just fixed."""
    assert "Number(b.lower)" in APP_JS
    assert "b.upper" in APP_JS
    assert "bracket_low" not in APP_JS
    assert "bracket_high" not in APP_JS


def test_vintage_chart_reads_lots_fields():
    """Must read the same field names the engine emits on TaxResult."""
    assert "ftc_carryforward_lots_out" in APP_JS
    assert "nol_carryforward_lots_out" in APP_JS
    assert "ftc_expired_this_year" in APP_JS
    assert "nol_expired_this_year" in APP_JS
