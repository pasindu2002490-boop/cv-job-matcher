from cv_job_matcher.cv_parser import _repair_spaced_pdf_text, parse_cv


def test_repair_spaced_pdf_text_restores_words():
    text = "A I / M L  E n g i n e e r  w i t h  P y t h o n\nE D U C A T I O N"
    repaired = _repair_spaced_pdf_text(text)
    assert "AI/ML Engineer with Python" in repaired
    assert "EDUCATION" in repaired


def test_parse_cv_after_pdf_repair_detects_ai_ml_title():
    text = _repair_spaced_pdf_text("A I / M L  E n g i n e e r  w i t h  P y t h o n  a n d  M L O p s")
    profile = parse_cv(text)
    assert "ai/ml engineer" in profile.likely_titles
    assert "python" in profile.skills
    assert "mlops" in profile.skills
