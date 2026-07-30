from __future__ import annotations

import html
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

MAX_CARD_WIDGETS = 25
_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _render_inline_tokens(tokens: list[Token]) -> str:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        tt = t.type
        if tt == "text":
            out.append(_escape(t.content))
        elif tt == "code_inline":
            out.append(f'<font face="monospace">{_escape(t.content)}</font>')
        elif tt == "hardbreak":
            out.append("<br>")
        elif tt == "softbreak":
            out.append(" ")
        elif tt == "strong_open":
            out.append("<b>")
        elif tt == "strong_close":
            out.append("</b>")
        elif tt == "em_open":
            out.append("<i>")
        elif tt == "em_close":
            out.append("</i>")
        elif tt == "s_open":
            out.append("<s>")
        elif tt == "s_close":
            out.append("</s>")
        elif tt == "link_open":
            href = ""
            if t.attrs:
                href = dict(t.attrs).get("href", "")
            out.append(f'<a href="{_escape(href)}">')
        elif tt == "link_close":
            out.append("</a>")
        i += 1
    return "".join(out)


def _inline_html(token: Token) -> str:
    if token.type != "inline" or token.children is None:
        return _escape(token.content)
    return _render_inline_tokens(token.children)


def _paragraph_widget(text_html: str) -> dict[str, Any]:
    return {"textParagraph": {"text": text_html}}


def _table_to_widgets(tokens: list[Token], start: int) -> tuple[list[dict[str, Any]], int]:
    widgets: list[dict[str, Any]] = []
    i = start
    headers: list[str] = []
    row_cells: list[str] = []
    in_header = False

    while i < len(tokens):
        t = tokens[i]
        if t.type == "thead_open":
            in_header = True
        elif t.type == "thead_close":
            in_header = False
        elif t.type in {"th_open", "td_open"}:
            j = i + 1
            cell_html = ""
            while j < len(tokens) and tokens[j].type not in {"th_close", "td_close"}:
                if tokens[j].type == "inline":
                    cell_html = _inline_html(tokens[j])
                j += 1
            if in_header:
                headers.append(cell_html)
            else:
                row_cells.append(cell_html)
            i = j
        elif t.type == "tr_close" and row_cells:
            first = row_cells[0] if row_cells else ""
            rest = " | ".join(row_cells[1:]) if len(row_cells) > 1 else ""
            bottom = ""
            if len(row_cells) > 2:
                rest = row_cells[1]
                bottom = " | ".join(row_cells[2:])
            w = {
                "decoratedText": {
                    "topLabel": html.unescape(first)[:80],
                    "text": rest or first,
                    "wrapText": True,
                }
            }
            if bottom:
                w["decoratedText"]["bottomLabel"] = html.unescape(bottom)[:150]
            widgets.append(w)
            row_cells = []
        elif t.type == "table_close":
            break
        i += 1

    if headers:
        widgets.insert(0, _paragraph_widget(" | ".join([f"<b>{h}</b>" for h in headers])))
    return widgets, i


def markdown_to_gchat_widgets(md_text: str) -> list[dict[str, Any]]:
    tokens = _MD.parse(md_text)
    widgets: list[dict[str, Any]] = []
    i = 0
    list_stack: list[str] = []
    pending_list_items: list[str] = []

    def flush_list_items() -> None:
        nonlocal pending_list_items
        if pending_list_items:
            widgets.append(_paragraph_widget("<br>".join(pending_list_items)))
            pending_list_items = []

    while i < len(tokens):
        t = tokens[i]
        tt = t.type
        if tt in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append(tt)
        elif tt in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
            if not list_stack:
                flush_list_items()
        elif tt == "inline":
            html_text = _inline_html(t)
            if list_stack:
                pending_list_items.append(f"• {html_text}")
            else:
                if html_text.strip():
                    widgets.append(_paragraph_widget(html_text))
        elif tt in {"fence", "code_block"}:
            flush_list_items()
            code_html = _escape(t.content).replace("\n", "<br>")
            widgets.append(
                _paragraph_widget(f'<font face="monospace">{code_html}</font>')
            )
        elif tt == "table_open":
            flush_list_items()
            table_widgets, end_idx = _table_to_widgets(tokens, i)
            widgets.extend(table_widgets)
            i = end_idx
        i += 1

    flush_list_items()
    return widgets[:MAX_CARD_WIDGETS]


def markdown_to_card(md_text: str, title: str = "Kimi K3") -> dict[str, Any]:
    return {
        "cardsV2": [
            {
                "cardId": "md",
                "card": {
                    "header": {"title": title[:60]},
                    "sections": [{"widgets": markdown_to_gchat_widgets(md_text)}],
                },
            }
        ]
    }
