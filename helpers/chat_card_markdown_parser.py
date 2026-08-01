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


def _render_inline_text(tokens: list[Token]) -> str:
    def render_token(t: Token) -> str:
        tt = t.type
        if tt == "text":
            return t.content
        if tt == "code_inline":
            return f"`{t.content}`"
        if tt == "hardbreak":
            return "\n"
        if tt == "softbreak":
            return " "
        if tt in {"strong_open", "strong_close"}:
            return "*"
        if tt in {"em_open", "em_close"}:
            return "_"
        if tt in {"s_open", "s_close"}:
            return "~"
        if tt == "link_open":
            href = dict(t.attrs).get("href", "") if t.attrs else ""
            return f"<{href}|"
        if tt == "link_close":
            return ">"
        return ""

    def render_from(i: int) -> list[str]:
        return [] if i >= len(tokens) else [render_token(tokens[i]), *render_from(i + 1)]

    return "".join(render_from(0))


def _inline_text(token: Token) -> str:
    if token.type != "inline" or token.children is None:
        return token.content
    return _render_inline_text(token.children)


def _skip_to_table_close(tokens: list[Token], start: int) -> int:
    return start if tokens[start].type == "table_close" else _skip_to_table_close(tokens, start + 1)


def markdown_to_gchat_text(md_text: str) -> str:
    tokens = _MD.parse(md_text)
    source_lines = md_text.splitlines()

    def flush_list_items(
        blocks: list[str], pending_list_items: list[str]
    ) -> tuple[list[str], list[str]]:
        return (
            ([*blocks, "\n".join(pending_list_items)], [])
            if pending_list_items
            else (blocks, pending_list_items)
        )

    def quote_block(text: str) -> str:
        return "\n".join(f"> {line}" for line in text.split("\n"))

    def walk(
        i: int,
        blocks: list[str],
        list_stack: list[str],
        pending_list_items: list[str],
        in_heading: bool,
        in_blockquote: bool,
    ) -> list[str]:
        if i >= len(tokens):
            final_blocks, _ = flush_list_items(blocks, pending_list_items)
            return final_blocks

        t = tokens[i]
        tt = t.type

        if tt == "heading_open":
            return walk(i + 1, blocks, list_stack, pending_list_items, True, in_blockquote)
        if tt == "heading_close":
            return walk(i + 1, blocks, list_stack, pending_list_items, False, in_blockquote)
        if tt == "blockquote_open":
            return walk(i + 1, blocks, list_stack, pending_list_items, in_heading, True)
        if tt == "blockquote_close":
            return walk(i + 1, blocks, list_stack, pending_list_items, in_heading, False)
        if tt in {"bullet_list_open", "ordered_list_open"}:
            return walk(i + 1, blocks, [*list_stack, tt], pending_list_items, in_heading, in_blockquote)
        if tt in {"bullet_list_close", "ordered_list_close"}:
            next_stack = list_stack[:-1] if list_stack else list_stack
            flushed_blocks, flushed_pending = (
                flush_list_items(blocks, pending_list_items)
                if not next_stack
                else (blocks, pending_list_items)
            )
            return walk(i + 1, flushed_blocks, next_stack, flushed_pending, in_heading, in_blockquote)
        if tt == "inline":
            text = _inline_text(t)
            if in_heading and text.strip():
                text = f"*{text}*"
            if list_stack:
                indent = "    " * (len(list_stack) - 1)
                return walk(
                    i + 1,
                    blocks,
                    list_stack,
                    [*pending_list_items, f"{indent}* {text}"],
                    in_heading,
                    in_blockquote,
                )
            if not text.strip():
                return walk(i + 1, blocks, list_stack, pending_list_items, in_heading, in_blockquote)
            block = quote_block(text) if in_blockquote else text
            return walk(i + 1, [*blocks, block], list_stack, pending_list_items, in_heading, in_blockquote)
        if tt in {"fence", "code_block"}:
            flushed_blocks, _ = flush_list_items(blocks, pending_list_items)
            code = t.content.rstrip("\n")
            return walk(
                i + 1,
                [*flushed_blocks, f"```\n{code}\n```"],
                list_stack,
                [],
                in_heading,
                in_blockquote,
            )
        if tt == "table_open":
            flushed_blocks, _ = flush_list_items(blocks, pending_list_items)
            start, end = t.map if t.map else (i, i)
            table_source = "\n".join(source_lines[start:end])
            close_idx = _skip_to_table_close(tokens, i)
            return walk(
                close_idx + 1,
                [*flushed_blocks, f"```\n{table_source}\n```"],
                list_stack,
                [],
                in_heading,
                in_blockquote,
            )
        return walk(i + 1, blocks, list_stack, pending_list_items, in_heading, in_blockquote)

    return "\n\n".join(walk(0, [], [], [], False, False))


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
