#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 Markdown -> 自包含 HTML（只覆盖本仓库文档用到的语法）。

用法：python3 md2html.py 输入.md 输出.html "标题"
覆盖：h1~h3、表格、围栏代码块、无序/有序列表、引用块（渲染成提示框）、
      水平线、**粗体**、`行内代码`、[链接](url)。无第三方依赖。
"""
import html
import re
import sys

CSS = """
:root { --fg:#1c2530; --dim:#5b6b7c; --line:#e3e8ee; --accent:#1f6feb;
        --code-bg:#f6f8fa; --callout:#fff8e6; --callout-line:#f0c14b; }
* { box-sizing: border-box; }
body { margin:0; background:#f2f5f8; color:var(--fg);
       font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
            "Hiragino Sans GB","Microsoft YaHei",sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 32px 20px 80px; }
main { background:#fff; border:1px solid var(--line); border-radius:14px;
       padding: 40px 44px; box-shadow:0 6px 24px rgba(20,40,70,.06); }
h1 { font-size:26px; margin:0 0 6px; padding-bottom:14px; border-bottom:2px solid var(--line); }
h2 { font-size:21px; margin:38px 0 12px; padding-top:22px; border-top:1px solid var(--line); }
h2:first-of-type { border-top:none; padding-top:0; }
h3 { font-size:17px; margin:26px 0 10px; color:#123; }
p, li { color:var(--fg); }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
code { background:var(--code-bg); border:1px solid var(--line); border-radius:5px;
       padding:1px 5px; font:14px/1.5 "SFMono-Regular",Consolas,"Liberation Mono",monospace; }
pre { background:var(--code-bg); border:1px solid var(--line); border-radius:10px;
      padding:14px 16px; overflow:auto; }
pre code { background:none; border:none; padding:0; font-size:13.5px; line-height:1.6; }
table { border-collapse:collapse; width:100%; margin:14px 0; font-size:14.5px; display:block; overflow-x:auto; }
th, td { border:1px solid var(--line); padding:8px 12px; text-align:left; vertical-align:top; }
th { background:#f7f9fc; white-space:nowrap; }
tbody tr:nth-child(even) { background:#fbfcfe; }
blockquote { margin:14px 0; padding:12px 16px; background:var(--callout);
             border-left:4px solid var(--callout-line); border-radius:0 8px 8px 0; }
blockquote p { margin:0; }
hr { border:none; border-top:1px solid var(--line); margin:30px 0; }
ul, ol { padding-left:26px; }
li { margin:4px 0; }
.meta { color:var(--dim); font-size:13px; margin:0 0 22px; }
.toc { background:#fff; border:1px solid var(--line); border-radius:12px;
       padding:16px 20px; margin:0 0 22px; }
.toc b { display:block; margin-bottom:6px; font-size:14px; color:var(--dim); letter-spacing:.04em; }
.toc a { display:inline-block; margin:2px 14px 2px 0; font-size:14px; }
@media print { body{background:#fff} .wrap{padding:0} main{border:none;box-shadow:none;padding:0}
                .toc{page-break-after:always} h2{page-break-after:avoid} pre,table{page-break-inside:avoid} }
"""


def esc(t: str) -> str:
    return html.escape(t, quote=False)


def inline(t: str) -> str:
    t = esc(t)
    t = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    return t


def convert(md: str) -> tuple:
    out, toc, i = [], [], 0
    lines = md.split("\n")
    in_code = False
    in_ul = in_ol = in_tbl = False

    def close_blocks():
        nonlocal in_ul, in_ol, in_tbl
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_ol:
            out.append("</ol>"); in_ol = False
        if in_tbl:
            out.append("</tbody></table>"); in_tbl = False

    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>"); in_code = False
            else:
                close_blocks()
                out.append('<pre><code>')
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(esc(ln))
            i += 1
            continue
        if re.match(r"^\|[-: |]+\|$", ln.strip()) and out and out[-1].startswith("<table"):
            i += 1
            continue                                   # 表格分隔行
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if not in_tbl:
                close_blocks()
                out.append("<table><thead><tr>" +
                           "".join(f"<th>{inline(c)}</th>" for c in cells) +
                           "</tr></thead><tbody>")
                in_tbl = True
            else:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            i += 1
            continue
        close_blocks()
        m = re.match(r"^(#{1,3})\s+(.*)$", ln)
        if m:
            lvl, txt = len(m.group(1)), m.group(2).strip()
            if lvl == 1:
                out.append(f"<h1>{inline(txt)}</h1>")
            else:
                anchor = f"h{len(toc)}"
                out.append(f'<h{lvl} id="{anchor}">{inline(txt)}</h{lvl}>')
                toc.append((lvl, anchor, re.sub(r"[`*]", "", txt)))
        elif ln.strip() == "---":
            out.append("<hr>")
        elif ln.startswith(">"):
            out.append(f"<blockquote><p>{inline(ln.lstrip('> ').strip())}</p></blockquote>")
        elif re.match(r"^\d+\.\s", ln):
            if not in_ol:
                out.append("<ol>"); in_ol = True
            out.append(f"<li>{inline(re.sub(r'^\d+\.\s', '', ln))}</li>")
        elif ln.startswith("- "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(ln[2:])}</li>")
        elif ln.strip():
            out.append(f"<p>{inline(ln.strip())}</p>")
        i += 1
    if in_code:
        out.append("</code></pre>")
    close_blocks()
    return "\n".join(out), toc


def main():
    src = open(sys.argv[1], encoding="utf-8").read()
    title = sys.argv[3] if len(sys.argv) > 3 else "接口文档"
    body, toc = convert(src)
    toc_html = "".join(
        f'<a href="#{a}">{"　" if lvl == 3 else ""}{esc(t)}</a>' for lvl, a, t in toc if lvl == 2)
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<nav class="toc"><b>目录</b>{toc_html}</nav>
<main>{body}</main>
</div></body></html>"""
    open(sys.argv[2], "w", encoding="utf-8").write(doc)
    print(f"已生成 {sys.argv[2]}（{len(doc)} 字节，{len(toc)} 个标题）")


if __name__ == "__main__":
    main()
