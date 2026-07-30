import re, html

def convert_inline(md):
    code_spans = []
    def save_code(m):
        code_spans.append(m.group(1))
        return f"__CODE_{len(code_spans)-1}__"
    md = re.sub(r'`([^`]+)`', save_code, md)
    md = html.escape(md)
    def to_link(m):
        text = m.group(1)
        url = html.escape(m.group(2), quote=True)
        return f'<a href="{url}">{text}</a>'
    md = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', to_link, md)
    # ใช้ .+? แทน [^*]+ เพื่อให้จับชื่อไฟล์ที่มีจุดได้ **1736913881567.jpg**
    md = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', md)
    md = re.sub(r'__([^_]+?)__', r'<b>\1</b>', md)
    md = re.sub(r'(?<!\*)\*([^*\n]+?)\*(?!\*)', r'<b>\1</b>', md)  # *xxx* ก็ให้หนาเลยใน gchat
    md = re.sub(r'(?<!_)_(.+?)_(?!_)', r'<i>\1</i>', md)
    md = re.sub(r'~~(.+?)~~', r'<s>\1</s>', md)
    for i, code in enumerate(code_spans):
        md = md.replace(f"__CODE_{i}__", f'<font face="monospace">{html.escape(code)}</font>')
    return md

def _parse_table_block(lines, start_idx):
    header_line = lines[start_idx].strip()
    sep_line = lines[start_idx+1].strip() if start_idx+1 < len(lines) else ""
    if '|' not in header_line or '---' not in sep_line:
        return None, start_idx
    headers = [c.strip() for c in header_line.strip('|').split('|')]
    widgets = []
    header_html = " | ".join([f"<b>{convert_inline(h)}</b>" for h in headers])
    widgets.append({"textParagraph": {"text": header_html}})
    idx = start_idx + 2
    while idx < len(lines):
        line = lines[idx].strip()
        if not line or '|' not in line: break
        cols = [c.strip() for c in line.strip('|').split('|')]
        if not cols or (len(cols)==1 and not cols[0]): 
            idx+=1; continue
        if all(re.match(r'^[\s\-:]+$', c) for c in cols):
            idx+=1; continue
        first = convert_inline(cols[0]) if len(cols)>0 else ""
        rest = " | ".join([convert_inline(c) for c in cols[1:]]) if len(cols)>1 else ""
        bottom = ""
        if len(cols) > 2:
            rest = convert_inline(cols[1])
            bottom = " | ".join([convert_inline(c) for c in cols[2:]])
        w = {"decoratedText": {"topLabel": re.sub(r'<[^>]+>', '', first)[:80], "text": rest, "wrapText": True}}
        w["decoratedText"]["text"] = rest or first
        if bottom:
            w["decoratedText"]["bottomLabel"] = re.sub(r'<[^>]+>', '', bottom)[:150]
        widgets.append(w)
        idx+=1
    return widgets, idx

def markdown_to_gchat_widgets(md_text):
    lines = md_text.split('\n')
    widgets = []
    i=0
    para_buf=[]
    def flush_para():
        nonlocal para_buf, widgets
        if not para_buf: return
        para_text = "\n".join(para_buf).strip()
        if not para_text:
            para_buf=[]; return
        if all(re.match(r'^\s*[-*•]\s+', l) for l in para_buf if l.strip()):
            items=[]
            for l in para_buf:
                m=re.match(r'^\s*[-*•]\s+(.*)', l)
                if m: items.append(f"• {convert_inline(m.group(1))}")
            widgets.append({"textParagraph": {"text": "<br>".join(items)}})
        else:
            html_text="<br>".join([convert_inline(l) if l.strip() else "" for l in para_buf])
            widgets.append({"textParagraph": {"text": html_text}})
        para_buf=[]
    while i < len(lines):
        line=lines[i]
        if line.strip().startswith('```'):
            flush_para()
            code_buf=[]
            i+=1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_buf.append(html.escape(lines[i])); i+=1
            widgets.append({"textParagraph": {"text": f'<font face="monospace">{"<br>".join(code_buf)}</font>'}})
            i+=1; continue
        if '|' in line and i+1 < len(lines) and '---' in lines[i+1]:
            flush_para()
            tbl, nxt = _parse_table_block(lines, i)
            if tbl:
                widgets.extend(tbl); i=nxt; continue
        if not line.strip():
            flush_para(); i+=1; continue
        para_buf.append(line); i+=1
    flush_para()
    return widgets[:30]

def markdown_to_card(md_text, title="Kimi K3"):
    return {"cardsV2": [{"cardId":"md","card":{"header":{"title":title[:60]},"sections":[{"widgets":markdown_to_gchat_widgets(md_text)}]}}]}
