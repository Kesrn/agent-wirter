"""文本差异对比服务"""

import difflib


def compute_diff(text_a: str, text_b: str) -> list[dict]:
    lines_a = text_a.splitlines(keepends=True)
    lines_b = text_b.splitlines(keepends=True)

    matcher = difflib.SequenceMatcher(None, lines_a, lines_b)
    result = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            result.append({
                "tag": "equal",
                "lines": lines_a[i1:i2],
            })
        elif tag == "insert":
            result.append({
                "tag": "insert",
                "lines": lines_b[j1:j2],
            })
        elif tag == "delete":
            result.append({
                "tag": "delete",
                "lines": lines_a[i1:i2],
            })
        elif tag == "replace":
            result.append({
                "tag": "replace",
                "lines_a": lines_a[i1:i2],
                "lines_b": lines_b[j1:j2],
            })

    return result
