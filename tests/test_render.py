"""Regression tests for tex_body's handling of literal `$` versus LaTeX math.

A literal dollar sign inside inline code — `$HOME`, `$(pwd)`, a shell snippet —
used to be mis-parsed as the opening of inline math: the math scanner matched
forward to the next `$` and swallowed the prose in between, emitting broken
LaTeX that fails xelatex with::

    ! Paragraph ended before \\text@command was complete.

These tests pin the fix while making sure genuine inline math still renders.
"""
from __future__ import annotations

from tid.render import tex_body


def test_dollar_in_inline_code_is_escaped_not_mathified():
    body = "Every user has the same `$HOME` and the same `$PATH` on the box."
    out = tex_body(body)
    assert r"\texttt{\$HOME}" in out
    assert r"\texttt{\$PATH}" in out
    # The prose between the two code spans must survive intact.
    assert "and the same" in out


def test_two_inline_code_dollars_do_not_form_a_math_span():
    # The classic break: `$HOME` ... `$HOME`. Previously the region between
    # the two dollars became one giant (broken) math expression.
    body = "Both `ubuntu` and `debian` share the same `$HOME` and dotfiles."
    out = tex_body(body)
    assert r"\texttt{\$HOME}" in out
    assert r"\texttt{ubuntu}" in out
    assert r"\texttt{debian}" in out


def test_real_inline_math_still_passes_through():
    out = tex_body("The area scales like $r^2$ as the radius grows.")
    assert "$r^2$" in out


def test_display_math_still_passes_through():
    out = tex_body("Euler:\n\n$$e^{i\\pi} + 1 = 0$$")
    assert r"\[e^{i\pi} + 1 = 0\]" in out


def test_stray_dollar_in_prose_is_escaped_and_does_not_span_paragraphs():
    out = tex_body("It cost $5 yesterday.\n\nToday it is cheaper.")
    assert r"\$5" in out
    assert "Today it is cheaper." in out
