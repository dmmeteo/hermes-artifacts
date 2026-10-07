"""Turn stored bytes into a document that is safe to run in an isolated frame.

Every render is a full HTML document. HTML artifacts run as authored behind a
CSP meta tag that is parsed before any artifact markup. Markdown is rendered
with raw HTML disabled. SVG and raster images are embedded as data URIs inside
<img>, where SVG scripts never execute.
"""

from __future__ import annotations

import base64
import html
from typing import Literal

from markdown_it import MarkdownIt

from .domain import KINDS, Artifact, Kind

Theme = Literal["light", "dark"]

CONTENT_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data: blob:",
        "media-src data: blob:",
        "font-src data:",
        "connect-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "manifest-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
    ]
)
"""Policy for artifact documents. Inline code runs; nothing loads from the network."""

CONTENT_RESPONSE_HEADERS = {
    "Content-Security-Policy": f"{CONTENT_CSP}; sandbox allow-scripts; frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}

_markdown = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(
    ["table", "strikethrough"]
)

_PALETTES: dict[Theme, str] = {
    "light": "--bg:#fbfbf9;--fg:#1d2329;--muted:#5b6670;--rule:#dcdfe2;--code:#f0f1ef;--link:#0b6b6b",
    "dark": "--bg:#0b1f1f;--fg:#f3e9dc;--muted:#bfb3a6;--rule:#28403f;--code:#123030;--link:#7fd6cf",
}

_READING_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg)}
body{font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:clamp(20px,4vw,56px)}
main{max-width:72ch;margin:0 auto}
h1,h2,h3,h4{line-height:1.25;margin:1.6em 0 .6em}
h1{font-size:2rem;margin-top:0}
a{color:var(--link);text-underline-offset:3px}
code,pre{font-family:ui-monospace,"SFMono-Regular",Menlo,monospace;font-size:.92em;background:var(--code)}
code{padding:.1em .3em;border-radius:4px}
pre{padding:14px 16px;overflow:auto;border-radius:6px}
pre code{padding:0;background:none}
blockquote{margin:1em 0;padding-left:1em;border-left:3px solid var(--rule);color:var(--muted)}
table{border-collapse:collapse;width:100%;margin:1em 0;display:block;overflow-x:auto}
th,td{border:1px solid var(--rule);padding:6px 10px;text-align:left}
hr{border:0;border-top:1px solid var(--rule)}
img{max-width:100%}
"""

_IMAGE_CSS = """
html,body{margin:0;height:100%;background:var(--bg)}
body{display:grid;place-items:center}
img{max-width:100%;max-height:100vh;object-fit:contain;display:block}
"""


def _csp_meta() -> str:
    return f'<meta http-equiv="Content-Security-Policy" content="{html.escape(CONTENT_CSP)}">'


def _shell(title: str, theme: Theme, css: str, body: str) -> str:
    return (
        "<!doctype html><html><head>"
        f"{_csp_meta()}"
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{html.escape(title)}</title>"
        f"<style>:root{{{_PALETTES[theme]};color-scheme:{theme}}}{css}</style>"
        f"</head><body>{body}</body></html>"
    )


def render_document(artifact: Artifact, content: bytes, theme: Theme = "light") -> str:
    match artifact.kind:
        case Kind.HTML:
            return (
                "<!doctype html>"
                f"{_csp_meta()}"
                '<meta name="referrer" content="no-referrer">'
                f"{content.decode('utf-8')}"
            )
        case Kind.MARKDOWN:
            body = f"<main>{_markdown.render(content.decode('utf-8'))}</main>"
            return _shell(artifact.title, theme, _READING_CSS, body)
        case Kind.SVG | Kind.PNG | Kind.WEBP:
            media = KINDS[artifact.kind].media_type
            data = base64.b64encode(content).decode("ascii")
            img = f'<img src="data:{media};base64,{data}" alt="{html.escape(artifact.title)}">'
            return _shell(artifact.title, theme, _IMAGE_CSS, img)


NAVIGATION_NOTICE = "hermes-artifacts:navigation-blocked"


def wrapper_document(document: str) -> str:
    """The trusted wrapper around an artifact frame, for a console that loads previews by URL.

    Served with CONTENT_RESPONSE_HEADERS, whose `frame-src 'none'` governs every navigation the inner
    artifact frame attempts. The wrapper restores the artifact and tells the panel when one was blocked.
    The Hermes-hosted panel builds the same wrapper as a srcdoc (web/src/panel/frame.ts).
    """
    return (
        "<!doctype html><style>html,body{margin:0;height:100%;overflow:hidden;background:transparent}"
        "iframe{display:block;border:0;width:100%;height:100%;background:#fff}</style>"
        f'<iframe sandbox="allow-scripts" referrerpolicy="no-referrer" srcdoc="{html.escape(document)}"></iframe>'
        "<script>(function(){var f=document.querySelector('iframe'),n=0;f.addEventListener('load',function(){"
        f'if(++n>1){{n=0;f.srcdoc=f.srcdoc;parent.postMessage("{NAVIGATION_NOTICE}","*");}}}});}})();</script>'
    )
