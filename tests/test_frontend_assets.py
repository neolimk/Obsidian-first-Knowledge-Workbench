from pathlib import Path


def test_index_html_does_not_eval_remote_marked():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert "eval(safeCode)" not in html
    assert "https://cdn.jsdelivr.net" not in html
    assert "vendor/marked.min.js" in html
