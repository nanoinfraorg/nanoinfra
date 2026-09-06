"""Who may scrape `/metrics` (#235).

The endpoint publishes model names, spend volumes and queue depths. Three questions decide
whether a request gets them, and each one has a wrong answer that would have shipped quietly:

* **Disabled** must be a 404 and not an empty 200. An empty body reads as "nothing is happening".
* **A configured token** must be required, and compared in constant time.
* **No token on a routable bind** must fail closed. "The bind is the authentication" is the
  Prometheus convention and it holds exactly while the bind is local -- and the demo sets
  `gateway.host` to `0.0.0.0` so a reverse proxy can front the port.

That last case is the one worth a test: it is a deployment that exports its own telemetry publicly
the moment somebody flips one boolean.
"""

from __future__ import annotations

import pytest

from nanoinfra.cli.gateway_runtime import (
    _metrics_scrape_allowed,  # pyright: ignore[reportPrivateUsage]
    _presented_bearer_token,  # pyright: ignore[reportPrivateUsage]
)
from nanoinfra.config.schema import Config

# --- the credential a request presented --------------------------------------------------


def test_a_bearer_header_is_read() -> None:
    raw = b"GET /metrics HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer s3cret\r\n\r\n"

    assert _presented_bearer_token(raw) == "s3cret"


def test_the_header_name_is_matched_case_insensitively() -> None:
    """Prometheus, curl and a reverse proxy do not agree on the casing."""
    raw = b"GET /metrics HTTP/1.1\r\nauthorization: bearer s3cret\r\n\r\n"

    assert _presented_bearer_token(raw) == "s3cret"


def test_a_request_with_no_authorization_presents_nothing() -> None:
    assert _presented_bearer_token(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n") == ""


def test_a_non_bearer_scheme_presents_nothing() -> None:
    """Basic auth is not a bearer token, and treating it as one would compare a base64 blob."""
    raw = b"GET /metrics HTTP/1.1\r\nAuthorization: Basic dXNlcjpwYXNz\r\n\r\n"

    assert _presented_bearer_token(raw) == ""


def test_a_header_after_the_blank_line_is_body_and_not_a_header() -> None:
    raw = b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\nAuthorization: Bearer smuggled\r\n"

    assert _presented_bearer_token(raw) == ""


def test_a_truncated_request_does_not_raise() -> None:
    """The socket read is 4096 bytes with a timeout, so a partial request is normal."""
    assert _presented_bearer_token(b"GET /metrics HTTP/1.1\r\nAuthoriz") == ""
    assert _presented_bearer_token(b"") == ""


# --- whether the scrape is allowed -------------------------------------------------------


def test_a_matching_token_is_allowed_from_anywhere() -> None:
    """A token is what makes a scrape from another host possible at all."""
    assert _metrics_scrape_allowed(host="0.0.0.0", expected="s3cret", presented="s3cret") is True


def test_a_wrong_token_is_refused_even_on_loopback() -> None:
    """Once a token is configured it is the rule, and the bind stops being one."""
    assert _metrics_scrape_allowed(host="127.0.0.1", expected="s3cret", presented="nope") is False
    assert _metrics_scrape_allowed(host="127.0.0.1", expected="s3cret", presented="") is False


def test_a_loopback_bind_with_no_token_is_allowed() -> None:
    """The Prometheus convention, and it is true exactly here."""
    assert _metrics_scrape_allowed(host="127.0.0.1", expected="", presented="") is True
    assert _metrics_scrape_allowed(host="localhost", expected="", presented="") is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "gateway.example.org"])
def test_a_routable_bind_with_no_token_fails_closed(host: str) -> None:
    """The case that would have shipped a public `/metrics` on the demo.

    `gateway.host` is `0.0.0.0` there so Caddy can front the port, and `metricsEnabled` is one
    boolean away. Refusing is the only answer that does not depend on a firewall nobody checked.
    """
    assert _metrics_scrape_allowed(host=host, expected="", presented="") is False
    # And a token presented to a deployment that configured none does not help: there is nothing
    # to compare it against, so the bind is still the only rule and it still says no.
    assert _metrics_scrape_allowed(host=host, expected="", presented="guess") is False


# --- the config surface ------------------------------------------------------------------


def test_metrics_are_off_and_untokened_by_default() -> None:
    """No deployment gains a scrape surface by upgrading."""
    config = Config()

    assert config.gateway.metrics_enabled is False
    assert config.gateway.metrics_token == ""


def test_both_fields_are_settable_in_camel_case_like_the_rest_of_the_file() -> None:
    config = Config.model_validate(
        {"gateway": {"metricsEnabled": True, "metricsToken": "s3cret"}}
    )

    assert config.gateway.metrics_enabled is True
    assert config.gateway.metrics_token == "s3cret"


def test_the_default_bind_is_loopback_which_is_what_makes_the_untokened_case_safe() -> None:
    assert Config().gateway.host == "127.0.0.1"
