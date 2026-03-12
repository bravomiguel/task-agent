"""web_fetch — Fetch and extract readable content from URLs.

Feature-parity with openclaw's web_fetch tool:
- Readability-based content extraction (strips nav, ads, sidebars)
- SSRF protection (blocks private IPs, metadata endpoints, loopback)
- Response size limits (streaming truncation)
- In-memory LRU cache with TTL
- HTML → Markdown conversion with fallback chain
- JSON pretty-printing
- Invisible Unicode stripping
- Cloudflare markdown-for-agents support (Accept: text/markdown)
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from html import unescape
from typing import Literal
from urllib.parse import urlparse

import httpx
from langchain_core.tools import tool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CHARS = 50_000
MAX_RESPONSE_BYTES = 2_000_000  # 2 MB
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 30
CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
MAX_CACHE_ENTRIES = 100
MAX_HTML_SIZE_FOR_READABILITY = 1_000_000  # 1 MB

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "Accept": "text/markdown, text/html;q=0.9, */*;q=0.1",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": USER_AGENT,
}

# ---------------------------------------------------------------------------
# SSRF Guard
# ---------------------------------------------------------------------------

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "169.254.169.254",  # AWS/GCP metadata
    "metadata.internal",
})


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is private, loopback, or otherwise non-public."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # If we can't parse it, block it
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_reserved
        or addr.is_link_local
        or addr.is_multicast
    )


def _ssrf_check(url: str) -> None:
    """Validate URL against SSRF attacks. Raises ValueError if blocked."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}. Must be http or https.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: no hostname.")

    hostname_lower = hostname.lower().rstrip(".")
    if hostname_lower in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Blocked hostname: {hostname}")

    # Resolve DNS and check all IPs
    try:
        results = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"DNS resolution failed for: {hostname}")

    for family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        if _is_private_ip(ip_str):
            raise ValueError(f"Blocked: {hostname} resolves to private IP {ip_str}")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict]] = {}


def _cache_key(url: str, extract_mode: str, max_chars: int) -> str:
    return f"fetch:{url.lower().strip()}:{extract_mode}:{max_chars}"


def _cache_get(key: str) -> dict | None:
    if key in _cache:
        ts, result = _cache[key]
        if time.time() - ts < CACHE_TTL_SECONDS:
            return {**result, "cached": True}
        del _cache[key]
    return None


def _cache_put(key: str, result: dict) -> None:
    # LRU eviction
    if len(_cache) >= MAX_CACHE_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest_key]
    _cache[key] = (time.time(), result)


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

# Invisible Unicode ranges (zero-width, control, formatting)
_INVISIBLE_UNICODE_RE = re.compile(
    "[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff\u00ad\u034f\u061c"
    "\u180e\ufff0-\ufff8\U000e0000-\U000e007f]"
)


def _strip_invisible(text: str) -> str:
    return _INVISIBLE_UNICODE_RE.sub("", text)


def _html_to_markdown(html: str) -> str:
    """Regex-based HTML to markdown conversion (fallback)."""
    text = html
    # Remove script, style, noscript
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert links
    text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert headings
    for i in range(1, 7):
        text = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", rf"{'#' * i} \1\n", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert list items
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert paragraphs and divs to newlines
    text = re.sub(r"<(p|div|br|tr)[^>]*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode entities
    text = unescape(text)
    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_readability(html: str) -> tuple[str | None, str | None]:
    """Extract readable content using readability-lxml. Returns (title, content_markdown) or (None, None)."""
    try:
        from readability import Document
    except ImportError:
        return None, None

    if len(html) > MAX_HTML_SIZE_FOR_READABILITY:
        return None, None

    try:
        doc = Document(html)
        title = doc.title()
        summary_html = doc.summary()
        content = _html_to_markdown(summary_html)
        if content and len(content.strip()) > 50:
            return title, content
    except Exception:
        pass
    return None, None


def _markdown_to_text(md: str) -> str:
    """Convert markdown to plain text."""
    text = md
    # Remove code blocks
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    # Remove markdown links, keep text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove headings markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
    # Remove images
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate text to max_chars. Returns (text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


# ---------------------------------------------------------------------------
# Fetch with size limit
# ---------------------------------------------------------------------------


def _fetch_url(url: str, timeout: int = TIMEOUT_SECONDS) -> httpx.Response:
    """Fetch URL with SSRF guard, redirect handling, and size limit."""
    _ssrf_check(url)

    with httpx.Client(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=timeout,
        headers=REQUEST_HEADERS,
    ) as client:
        with client.stream("GET", url) as response:
            # Read up to MAX_RESPONSE_BYTES
            chunks = []
            bytes_read = 0
            for chunk in response.iter_bytes(chunk_size=8192):
                chunks.append(chunk)
                bytes_read += len(chunk)
                if bytes_read >= MAX_RESPONSE_BYTES:
                    break

            # Build a regular response with the body we read
            body = b"".join(chunks)

    # Re-create a simple response-like result
    return _FetchResult(
        status_code=response.status_code,
        headers=dict(response.headers),
        url=str(response.url),
        body=body,
        truncated=bytes_read >= MAX_RESPONSE_BYTES,
    )


class _FetchResult:
    """Lightweight result from size-limited fetch."""

    def __init__(self, status_code: int, headers: dict, url: str, body: bytes, truncated: bool):
        self.status_code = status_code
        self.headers = headers
        self.url = url
        self.truncated = truncated
        self._body = body

    @property
    def text(self) -> str:
        # Detect encoding from content-type header, fall back to utf-8
        ct = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].split(";")[0].strip()
        try:
            return self._body.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return self._body.decode("utf-8", errors="replace")

    @property
    def content_type(self) -> str:
        ct = self.headers.get("content-type", "")
        return ct.split(";")[0].strip().lower()


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@tool
def web_fetch(
    url: str,
    extract_mode: Literal["markdown", "text"] = "markdown",
    max_chars: int = MAX_CHARS,
) -> dict:
    """Fetch and extract readable content from a URL (HTML → markdown/text). Use for lightweight page access without browser automation.

    Args:
        url: HTTP or HTTPS URL to fetch.
        extract_mode: Extraction mode ("markdown" or "text").
        max_chars: Maximum characters to return (truncates when exceeded).
    """
    start = time.time()

    # Check cache
    ck = _cache_key(url, extract_mode, max_chars)
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    try:
        resp = _fetch_url(url)
    except ValueError as e:
        return {"error": str(e), "url": url}
    except httpx.TimeoutException:
        return {"error": f"Request timed out after {TIMEOUT_SECONDS}s", "url": url}
    except httpx.HTTPError as e:
        return {"error": f"HTTP error: {e}", "url": url}

    ct = resp.content_type
    title = None
    extractor = "raw"
    content = resp.text

    if resp.status_code >= 400:
        # For error responses, still extract readable text
        if "html" in ct:
            content = _html_to_markdown(content)
        content, truncated = _truncate(content, min(max_chars, 4000))
        return {
            "error": f"HTTP {resp.status_code}",
            "url": url,
            "final_url": resp.url,
            "content": _strip_invisible(content),
        }

    # Route by content type
    if ct == "text/markdown" or "markdown" in resp.headers.get("content-type", ""):
        # Cloudflare markdown-for-agents or any server returning markdown
        extractor = "cf-markdown"
        if extract_mode == "text":
            content = _markdown_to_text(content)

    elif "html" in ct:
        # Try readability first, fall back to regex
        r_title, r_content = _extract_readability(content)
        if r_content:
            title = r_title
            content = r_content
            extractor = "readability"
        else:
            content = _html_to_markdown(content)
            extractor = "html-to-markdown"

        if extract_mode == "text":
            content = _markdown_to_text(content)

    elif ct in ("application/json", "text/json") or ct.endswith("+json"):
        extractor = "json"
        try:
            content = json.dumps(json.loads(content), indent=2)
        except (json.JSONDecodeError, ValueError):
            pass  # Keep raw text

    else:
        # Plain text or other — pass through
        extractor = "raw"

    # Strip invisible unicode
    content = _strip_invisible(content)

    # Truncate
    content, truncated = _truncate(content, max_chars)

    elapsed_ms = int((time.time() - start) * 1000)

    result = {
        "url": url,
        "final_url": resp.url,
        "status": resp.status_code,
        "content_type": ct,
        "extract_mode": extract_mode,
        "extractor": extractor,
        "text": content,
        "truncated": truncated or resp.truncated,
        "length": len(content),
        "took_ms": elapsed_ms,
    }
    if title:
        result["title"] = title

    _cache_put(ck, result)
    return result
