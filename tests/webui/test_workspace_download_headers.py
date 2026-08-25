"""The Content-Disposition one workspace download answers with."""

from __future__ import annotations

from nanoinfra.webui.ws_http import _content_disposition  # pyright: ignore[reportPrivateUsage]


def test_an_ascii_name_is_quoted_and_repeated_utf8() -> None:
    value = _content_disposition("notes.md")

    assert value == "attachment; filename=\"notes.md\"; filename*=UTF-8''notes.md"


def test_a_non_ascii_name_survives_in_the_utf8_form() -> None:
    value = _content_disposition("diseño.md")

    # The ASCII fallback is stripped rather than dropped, so a client that ignores
    # filename* still gets a usable name.
    assert 'filename="diseo.md"' in value
    assert "filename*=UTF-8''dise%C3%B1o.md" in value


def test_a_quote_or_newline_cannot_close_the_header() -> None:
    """A raw quote or CRLF in a header value is how one header becomes two.

    A newline is a legal character in a POSIX filename, so this is reachable from a
    file on disk rather than only from a crafted request. What matters is that the
    value stays one header with one quoted string: the leftover text is then just
    part of a filename, which is inert.
    """
    value = _content_disposition('ev"il\r\nX-Injected: 1.txt')

    assert "\r" not in value
    assert "\n" not in value
    assert value.count('"') == 2
    assert value.startswith('attachment; filename="')
    # And the UTF-8 form is percent-encoded, so it carries no raw separator either.
    assert "filename*=UTF-8''ev%22il%0D%0AX-Injected%3A%201.txt" in value


def test_a_name_with_nothing_usable_left_still_names_something() -> None:
    value = _content_disposition("😀")

    assert 'filename="download"' in value
    assert "filename*=UTF-8''%F0%9F%98%80" in value
