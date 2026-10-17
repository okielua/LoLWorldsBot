"""Italic muted subtext; only explicitly marked footnotes carry bullets."""

import re


def format_footnotes(text):
    def replace(match):
        body = match.group(1)
        bullet = bool(re.match(r"^(?:[•-]\s+)", body))
        body = re.sub(r"^(?:[•-]\s+)+", "", body)
        if body.startswith("*") and body.endswith("*") and (not body.startswith("**") or body.startswith("***")):
            body = body[1:-1]
        body = re.sub(r"(?<!\\)\*\*(.+?)(?<!\\)\*\*",
                      lambda bold: bold.group() if re.fullmatch(r"\d+(?:\.\d+)?% correct", bold.group(1)) else bold.group(1), body)
        body = re.sub(r"(?<![\\*])\*([^*]+)\*(?!\*)", r"\1", body)
        return "-# " + ("• " if bullet else "") + "*" + body + "*"
    text = re.sub(r"(?m)^-#\s+([^\n]*)", replace, text)
    # Separate footnote blocks from the body, keeping consecutive notes compact.
    lines = text.split("\n")
    spaced = []
    for line in lines:
        if line.startswith("-# • ") and spaced and spaced[-1].strip() and not spaced[-1].startswith("-# • "):
            spaced.append("")
        spaced.append(line)
    return "\n".join(spaced)
