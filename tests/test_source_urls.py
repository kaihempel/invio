"""Tests for ``normalize_url``: the canonical URL behind a candidate's ``url_hash``."""

import pytest

from invio.domain import url_hash
from invio.sources.urls import normalize_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),  # the path keeps its case
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("http://example.com:443/a", "http://example.com:443/a"),  # not the default for http
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("http://[2001:DB8::1]:80/a", "http://[2001:db8::1]/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:/a", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("https://user:pw@Example.com/a", "https://user:pw@example.com/a"),
        ("mailto:Someone@Example.com", "mailto:Someone@Example.com"),
    ],
)
def test_normalize_url(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://e.com/a?utm_source=rss&utm_medium=feed", "https://e.com/a"),
        ("https://e.com/a?id=7&UTM_Campaign=x&page=2", "https://e.com/a?id=7&page=2"),
        ("https://e.com/a?%75tm_source=x&id=7", "https://e.com/a?id=7"),  # encoded name
        ("https://e.com/a?fbclid=1&gclid=2&msclkid=3", "https://e.com/a"),
        ("https://e.com/a?mc_cid=1&mc_eid=2&q=x", "https://e.com/a?q=x"),
        ("https://e.com/a?b=2&a=1&utm_x=0", "https://e.com/a?b=2&a=1"),  # order kept
        ("https://e.com/a?q=a%20b&r=c+d&flag", "https://e.com/a?q=a%20b&r=c+d&flag"),
        ("https://e.com/a?q=1&&utm_term=x&", "https://e.com/a?q=1"),
        ("https://e.com/a?utmost=1", "https://e.com/a?utmost=1"),  # only the utm_ prefix
    ],
)
def test_tracking_parameters_are_dropped(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


def test_utm_variants_share_one_hash() -> None:
    plain = "https://blog.example.com/posts/1"
    variants = [
        "https://blog.example.com/posts/1?utm_source=rss",
        "https://Blog.Example.com:443/posts/1?utm_source=newsletter&utm_medium=email",
        "https://blog.example.com/posts/1?utm_campaign=x#top",
    ]

    hashes = {url_hash(normalize_url(url)) for url in [plain, *variants]}

    assert hashes == {url_hash(plain)}


def test_different_pages_keep_different_hashes() -> None:
    first = normalize_url("https://e.com/a?id=1&utm_source=x")
    second = normalize_url("https://e.com/a?id=2&utm_source=x")

    assert url_hash(first) != url_hash(second)


def test_normalize_is_idempotent() -> None:
    once = normalize_url("HTTP://E.com:80?b=1&utm_source=x#f")

    assert normalize_url(once) == once == "http://e.com/?b=1"


def test_non_numeric_port_is_left_alone() -> None:
    assert normalize_url("http://E.com:abc/x") == "http://e.com:abc/x"


def test_unsplittable_url_raises_value_error() -> None:
    with pytest.raises(ValueError):
        normalize_url("http://[::1/x")
