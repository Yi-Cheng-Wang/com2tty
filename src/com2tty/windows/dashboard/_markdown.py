"""A small, self-contained Markdown renderer for the dashboard's README view.

This is deliberately our own renderer -- it imports no Markdown library, no
``rich.markdown`` and not Textual's ``Markdown`` widget. :func:`render_markdown`
turns Markdown source into a single Textual :class:`~textual.content.Content`
(styling carried by spans) plus a map of heading anchors to line numbers. A
single selectable ``Static`` renders that Content, which is what makes Ctrl+C
copy reliable (the copied plain text has markup stripped and code kept verbatim)
while still supporting clickable links: link spans carry an ``@click`` action so
the table-of-contents entries jump to their heading and external links open.

Supported: ATX and setext headings (registered as ``#anchor`` jump targets),
fenced code blocks (rendered as a padded block, content verbatim), unordered and
ordered lists (nesting via indentation), blockquotes, horizontal rules, pipe
tables, and the inline run of bold, italic, bold-italic, inline code, links,
images (alt text) and backslash escapes. Unrecognised input renders as plain
text, so the renderer never fails.
"""
import re

from textual.content import Content
from textual.style import Style

# -- styles (Textual markup style strings; resolved against the active theme) --
_CODE = "on $panel"                 # fenced + inline code background
_HEADING = {1: "bold $accent", 2: "bold $accent", 3: "bold $text",
            4: "bold $text-muted", 5: "bold $text-muted", 6: "bold $text-muted"}
_RULE = "$text-muted"               # horizontal rule and the H1 underline
_QUOTE = "italic $text-muted"
_QUOTE_BAR = "$accent"
_BULLET = "$accent"
_IMAGE = "$text-muted"

_HR_WIDTH = 56

# -- inline parser -------------------------------------------------------------
# One pass, leftmost match. Order matters at a shared position: escape, code
# (verbatim), image, link, then emphasis longest-first (*** > ** > *). ``_``
# emphasis needs word boundaries so it never mangles snake_case identifiers.
_INLINE = re.compile(
    r"(?P<esc>\\(?P<esc_ch>.))"
    r"|(?P<code>`(?P<code_txt>[^`]+)`)"
    r"|(?P<img>!\[(?P<img_alt>[^\]]*)\]\((?P<img_url>[^)]*)\))"
    r"|(?P<link>\[(?P<link_txt>[^\]]+)\]\((?P<link_url>[^)]*)\))"
    r"|(?P<bi>\*\*\*(?P<bi_txt>.+?)\*\*\*)"
    r"|(?P<bd>\*\*(?P<bd1>.+?)\*\*|__(?P<bd2>.+?)__)"
    r"|(?P<it>\*(?P<it1>[^*]+?)\*|(?<!\w)_(?P<it2>[^_]+?)_(?!\w))"
)

# -- block patterns ------------------------------------------------------------
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_SETEXT_H1 = re.compile(r"^=+\s*$")
_SETEXT_H2 = re.compile(r"^-{2,}\s*$")
_QUOTE_RE = re.compile(r"^(\s*)>\s?(.*)$")
_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")


def _combine(base, extra):
    """Layer ``extra`` over ``base`` into one markup style string (either '' )."""
    return " ".join(part for part in (base, extra) if part)


def _link_style(href):
    """A clickable underline style for ``href`` (anchor, web or relative).

    The href is embedded in an ``@click=link(...)`` action that the README
    widget handles. A href containing a quote or backslash would break the
    action string, so such a link is rendered as a plain underline instead.
    """
    if "'" in href or "\\" in href:
        return "underline"
    return Style(underline=True) + Style.from_meta({"@click": "link('%s')" % href})


def _slug(text, seen):
    """A GitHub-style anchor slug for a heading, de-duplicated within a document.

    Punctuation is dropped, spaces become hyphens, and the result is lower-cased;
    a repeated heading gets ``-1``, ``-2`` suffixes so each anchor is unique --
    matching how the README's table-of-contents links are written.
    """
    base = re.sub(r"[^\w\s-]", "", text.strip().lower())
    base = re.sub(r"\s+", "-", base).strip("-") or "section"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else "%s-%d" % (base, count)


def _inline_parts(s, base=""):
    """Resolve inline markup in ``s`` into a list of ``(text, style)`` parts."""
    parts = []
    pos, end = 0, len(s)
    while pos < end:
        match = _INLINE.search(s, pos)
        if match is None:
            parts.append((s[pos:], base))
            break
        if match.start() > pos:
            parts.append((s[pos:match.start()], base))
        if match.group("esc"):
            parts.append((match.group("esc_ch"), base))
        elif match.group("code"):
            parts.append((match.group("code_txt"), _combine(base, _CODE)))
        elif match.group("img"):
            alt = match.group("img_alt") or match.group("img_url") or "image"
            parts.append((alt, _combine(base, _IMAGE)))
        elif match.group("link"):
            parts.append((match.group("link_txt"), _link_style(match.group("link_url"))))
        elif match.group("bi"):
            parts.extend(_inline_parts(match.group("bi_txt"),
                                       _combine(base, "bold italic")))
        elif match.group("bd"):
            parts.extend(_inline_parts(match.group("bd1") or match.group("bd2"),
                                       _combine(base, "bold")))
        elif match.group("it"):
            parts.extend(_inline_parts(match.group("it1") or match.group("it2"),
                                       _combine(base, "italic")))
        pos = match.end()
    return parts


def render_markdown(source):
    """Render Markdown ``source`` to ``(Content, anchors)``.

    ``Content.plain`` is the document with markup removed and code verbatim
    (what a selection copy yields); ``anchors`` maps each heading's ``#slug`` to
    the line index of that heading, used by the README screen to jump there.
    """
    parts = []
    anchors = {}
    seen = {}
    line = [0]  # boxed so the closures can mutate it

    def emit(text, style=""):
        parts.append((text, style))
        line[0] += text.count("\n")

    def emit_inline(text, base=""):
        for fragment, style in _inline_parts(text, base):
            emit(fragment, style)

    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        stripped = raw.strip()

        fence = _FENCE.match(raw)
        if fence:
            i = _emit_fenced(emit, lines, i, fence.group(2))
            continue

        if stripped == "":
            emit("\n")
            i += 1
            continue

        if _HR.match(raw):
            emit("─" * _HR_WIDTH, _RULE)
            emit("\n")
            i += 1
            continue

        atx = _ATX.match(raw)
        if atx:
            level = len(atx.group(1))
            anchors[_slug(atx.group(2), seen)] = line[0]
            emit_inline(atx.group(2), _HEADING[level])
            emit("\n")
            if level == 1:
                emit("─" * min(len(atx.group(2)), _HR_WIDTH), _RULE)
                emit("\n")
            i += 1
            continue

        if i + 1 < n and stripped and _SETEXT_H1.match(lines[i + 1]):
            anchors[_slug(stripped, seen)] = line[0]
            emit_inline(stripped, _HEADING[1])
            emit("\n")
            i += 2
            continue
        if (i + 1 < n and stripped and _SETEXT_H2.match(lines[i + 1])
                and not _UL.match(raw)):
            anchors[_slug(stripped, seen)] = line[0]
            emit_inline(stripped, _HEADING[2])
            emit("\n")
            i += 2
            continue

        quote = _QUOTE_RE.match(raw)
        if quote:
            emit("▌ ", _QUOTE_BAR)
            emit_inline(quote.group(2), _QUOTE)
            emit("\n")
            i += 1
            continue

        unordered = _UL.match(raw)
        if unordered:
            emit(unordered.group(1) + "• ", _BULLET)
            emit_inline(unordered.group(2))
            emit("\n")
            i += 1
            continue

        ordered = _OL.match(raw)
        if ordered:
            emit(ordered.group(1) + ordered.group(2) + ". ", _BULLET)
            emit_inline(ordered.group(3))
            emit("\n")
            i += 1
            continue

        if _is_table_row(raw):
            i = _emit_table(emit, emit_inline, lines, i)
            continue

        emit_inline(raw)
        emit("\n")
        i += 1

    content = Content.assemble(*parts) if parts else Content("")
    # Each block emits its own trailing newline; drop the final one so the view
    # has no dangling blank line and empty input yields empty output.
    if content.plain.endswith("\n"):
        content = content[:-1]
    return content, anchors


def _is_table_row(line):
    return line.strip().startswith("|") and line.count("|") >= 2


def _is_table_divider(line):
    return bool(re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", line)) and "-" in line


def _emit_fenced(emit, lines, start, fence_token):
    """Emit a fenced code block as a padded rectangle; return the index past it.

    Content is verbatim (so a copied command pastes exactly); lines are padded
    to the block's widest line so the code background forms a neat block. An
    unclosed fence at EOF is still rendered.
    """
    fence_char = fence_token[0]
    fence_len = len(fence_token)
    code = []
    i = start + 1
    n = len(lines)
    while i < n:
        close = _FENCE.match(lines[i])
        if (close and close.group(2)[0] == fence_char
                and len(close.group(2)) >= fence_len):
            i += 1
            break
        code.append(lines[i])
        i += 1
    width = max((len(row) for row in code), default=0)
    for row in code:
        emit(row.ljust(width), _CODE)
        emit("\n")
    return i


def _emit_table(emit, emit_inline, lines, start):
    """Emit a contiguous run of pipe-table rows as aligned text; return the index
    past the table. The divider row is consumed but not shown; the header row is
    bold."""
    rows = []
    i = start
    n = len(lines)
    while i < n and _is_table_row(lines[i]):
        if _is_table_divider(lines[i]):
            i += 1
            continue
        rows.append([cell.strip()
                     for cell in lines[i].strip().strip("|").split("|")])
        i += 1
    if not rows:
        return i
    columns = max(len(row) for row in rows)
    widths = [0] * columns
    for row in rows:
        for col in range(len(row)):
            widths[col] = max(widths[col], len(row[col]))
    for index, row in enumerate(rows):
        style = "bold" if index == 0 else ""
        for col in range(columns):
            cell = row[col] if col < len(row) else ""
            if col:
                emit("  ")
            emit_inline(cell.ljust(widths[col]), style)
        emit("\n")
    return i
