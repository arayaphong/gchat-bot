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
    def render_token(t: Token) -> str:
        tt = t.type
        if tt == "text":
            return _escape(t.content)
        if tt == "code_inline":
            return f'<font face="monospace">{_escape(t.content)}</font>'
        if tt == "hardbreak":
            return "<br>"
        if tt == "softbreak":
            return " "
        if tt == "strong_open":
            return "<b>"
        if tt == "strong_close":
            return "</b>"
        if tt == "em_open":
            return "<i>"
        if tt == "em_close":
            return "</i>"
        if tt == "s_open":
            return "<s>"
        if tt == "s_close":
            return "</s>"
        if tt == "link_open":
            href = dict(t.attrs).get("href", "") if t.attrs else ""
            return f'<a href="{_escape(href)}">'
        if tt == "link_close":
            return "</a>"
        return ""

    def render_from(i: int) -> list[str]:
        return [] if i >= len(tokens) else [render_token(tokens[i]), *render_from(i + 1)]

    return "".join(render_from(0))


def _inline_html(token: Token) -> str:
    if token.type != "inline" or token.children is None:
        return _escape(token.content)
    return _render_inline_tokens(token.children)


def _paragraph_widget(text_html: str) -> dict[str, Any]:
    return {"textParagraph": {"text": text_html}}


def _table_to_widgets(tokens: list[Token], start: int) -> tuple[list[dict[str, Any]], int]:
    def collect_cell(j: int, cell_html: str = "") -> tuple[str, int]:
        if j >= len(tokens) or tokens[j].type in {"th_close", "td_close"}:
            return cell_html, j
        next_html = _inline_html(tokens[j]) if tokens[j].type == "inline" else cell_html
        return collect_cell(j + 1, next_html)

    def row_widget(row_cells: list[str]) -> dict[str, Any]:
        first = row_cells[0] if row_cells else ""
        rest = " | ".join(row_cells[1:]) if len(row_cells) > 1 else ""
        bottom = ""
        if len(row_cells) > 2:
            rest = row_cells[1]
            bottom = " | ".join(row_cells[2:])
        widget = {
            "decoratedText": {
                "topLabel": html.unescape(first)[:80],
                "text": rest or first,
                "wrapText": True,
            }
        }
        if bottom:
            widget["decoratedText"]["bottomLabel"] = html.unescape(bottom)[:150]
        return widget

    def scan(
        i: int,
        in_header: bool,
        headers: list[str],
        row_cells: list[str],
        widgets: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]], int]:
        if i >= len(tokens):
            return headers, widgets, i

        t = tokens[i]
        if t.type == "thead_open":
            return scan(i + 1, True, headers, row_cells, widgets)
        if t.type == "thead_close":
            return scan(i + 1, False, headers, row_cells, widgets)
        if t.type in {"th_open", "td_open"}:
            cell_html, next_idx = collect_cell(i + 1)
            return (
                scan(
                    next_idx,
                    in_header,
                    [*headers, cell_html] if in_header else headers,
                    row_cells if in_header else [*row_cells, cell_html],
                    widgets,
                )
            )
        if t.type == "tr_close" and row_cells:
            return scan(i + 1, in_header, headers, [], [*widgets, row_widget(row_cells)])
        if t.type == "table_close":
            return headers, widgets, i
        return scan(i + 1, in_header, headers, row_cells, widgets)

    headers, widgets, end_idx = scan(start, False, [], [], [])
    header_widget = (
        []
        if not headers
        else [_paragraph_widget(" | ".join(f"<b>{h}</b>" for h in headers))]
    )
    return [*header_widget, *widgets], end_idx


def markdown_to_gchat_widgets(md_text: str) -> list[dict[str, Any]]:
    tokens = _MD.parse(md_text)
    def flush_list_items(
        widgets: list[dict[str, Any]], pending_list_items: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        return (
            ([*widgets, _paragraph_widget("<br>".join(pending_list_items))], [])
            if pending_list_items
            else (widgets, pending_list_items)
        )

    def walk(
        i: int,
        widgets: list[dict[str, Any]],
        list_stack: list[str],
        pending_list_items: list[str],
    ) -> list[dict[str, Any]]:
        if i >= len(tokens):
            final_widgets, _ = flush_list_items(widgets, pending_list_items)
            return final_widgets

        t = tokens[i]
        tt = t.type
        if tt in {"bullet_list_open", "ordered_list_open"}:
            return walk(i + 1, widgets, [*list_stack, tt], pending_list_items)
        if tt in {"bullet_list_close", "ordered_list_close"}:
            next_stack = list_stack[:-1] if list_stack else list_stack
            flushed_widgets, flushed_pending = (
                flush_list_items(widgets, pending_list_items)
                if not next_stack
                else (widgets, pending_list_items)
            )
            return walk(i + 1, flushed_widgets, next_stack, flushed_pending)
        if tt == "inline":
            html_text = _inline_html(t)
            return (
                walk(
                    i + 1,
                    widgets,
                    list_stack,
                    [*pending_list_items, f"• {html_text}"],
                )
                if list_stack
                else walk(
                    i + 1,
                    (
                        [*widgets, _paragraph_widget(html_text)]
                        if html_text.strip()
                        else widgets
                    ),
                    list_stack,
                    pending_list_items,
                )
            )
        if tt in {"fence", "code_block"}:
            flushed_widgets, _ = flush_list_items(widgets, pending_list_items)
            code_html = _escape(t.content).replace("\n", "<br>")
            return walk(
                i + 1,
                [
                    *flushed_widgets,
                    _paragraph_widget(f'<font face="monospace">{code_html}</font>'),
                ],
                list_stack,
                [],
            )
        if tt == "table_open":
            flushed_widgets, _ = flush_list_items(widgets, pending_list_items)
            table_widgets, end_idx = _table_to_widgets(tokens, i)
            return walk(end_idx + 1, [*flushed_widgets, *table_widgets], list_stack, [])
        return walk(i + 1, widgets, list_stack, pending_list_items)

    return walk(0, [], [], [])[:MAX_CARD_WIDGETS]


def markdown_to_card(md_text: str, title: str = "OpenClaw") -> dict[str, Any]:
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
