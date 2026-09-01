from modelmux.workers.qwen3_tts import hard_split, sections


def test_sections_keep_short_paragraphs_together() -> None:
    text = "第一段。\n\n第二段。\n\n第三段。"
    assert sections(text, maximum=100) == ["第一段。\n\n第二段。\n\n第三段。"]


def test_hard_split_prefers_sentence_boundary() -> None:
    text = "甲" * 40 + "。" + "乙" * 40
    assert hard_split(text, maximum=50) == ["甲" * 40 + "。", "乙" * 40]
