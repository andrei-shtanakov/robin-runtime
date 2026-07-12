"""§6.7 output escaping and Telegram chunking — includes the spec's `<name>` regression."""

from __future__ import annotations

from robin.agent import Answer
from robin.fmt import TELEGRAM_LIMIT, chunk, escape_html, render_answer
from robin.kb import Hit


def test_escape_html_spec_regression() -> None:
    # The spec's production lesson: a digest quoting "Hi <name>" broke Telegram's parser.
    assert escape_html("Hi <name> & <b>friends</b>") == (
        "Hi &lt;name&gt; &amp; &lt;b&gt;friends&lt;/b&gt;"
    )


def test_render_answer_escapes_before_adding_markup() -> None:
    answer = Answer(
        question="q",
        sources=[Hit("a/<file>.md", 3, "text")],
        text='See config["a"] < config["b"] & more',
    )
    html = render_answer(answer)
    assert "&lt;" in html and "&amp;" in html
    assert (
        "<code>a/&lt;file&gt;.md:3</code>" in html
    )  # our markup survives, payload escaped
    assert "<b>Sources</b>" in html


def test_render_answer_without_sources() -> None:
    assert render_answer(Answer(question="q", sources=[], text="plain")) == "plain"


def test_chunk_short_passthrough() -> None:
    assert chunk("hello") == ["hello"]
    assert chunk("") == []


def test_chunk_respects_limit_and_boundaries() -> None:
    paragraphs = [f"para {i} " + "x" * 200 for i in range(40)]
    html = "\n\n".join(paragraphs)
    parts = chunk(html)
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert "".join(parts).replace("\n", "") == html.replace("\n", "")  # nothing lost
    for part in parts:  # never splits a paragraph across chunks in this regime
        assert part.startswith("para")


def test_chunk_never_splits_a_cite_tag() -> None:
    lines = [f"• <code>path/to/file-{i}.md:12</code>" for i in range(300)]
    parts = chunk("\n".join(lines))
    for part in parts:
        assert part.count("<code>") == part.count("</code>")


def test_chunk_hard_splits_pathological_line() -> None:
    parts = chunk("y" * (TELEGRAM_LIMIT * 2 + 5))
    assert [len(p) for p in parts] == [TELEGRAM_LIMIT, TELEGRAM_LIMIT, 5]
