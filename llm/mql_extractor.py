from __future__ import annotations

import re
from typing import Optional


def strip_markdown_fences(text: str) -> str:
    text = text.strip()

    fence = re.match(
        r"^```(?:javascript|js|json|mongodb|mongo|text)?\s*(.*?)\s*```$",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence:
        text = fence.group(1).strip()

    text = re.sub(r"^\s*(MongoDB query|Mongo query|Query)\s*:\s*", "", text, flags=re.IGNORECASE)

    return text.strip()


def extract_first_mongo_query(text: str) -> str:
    """
    Extract the first complete MongoDB shell query from an LLM response.

    Finds db.<collection>.find(...) or db.<collection>.aggregate(...), tracks
    strings and bracket balance, and ignores semicolons until the query is
    balanced.
    """
    if not text:
        return ""

    cleaned = strip_markdown_fences(text).strip()

    starts = []
    idx = cleaned.find("db.")
    while idx != -1:
        starts.append(idx)
        idx = cleaned.find("db.", idx + 1)

    if not starts:
        return ""

    for start in sorted(starts):
        end = find_balanced_query_end(cleaned, start)
        if end is not None:
            return cleaned[start:end].strip()
   
    return ""


def find_balanced_query_end(text: str, start: int) -> Optional[int]:
    stack = []
    in_string: Optional[str] = None
    escape = False
    saw_open = False

    matching = {
        ")": "(",
        "]": "[",
        "}": "{",
    }

    i = start

    while i < len(text):
        ch = text[i]

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue

        if ch in ['"', "'", "`"]:
            in_string = ch
            i += 1
            continue

        if ch in "([{":
            stack.append(ch)
            saw_open = True
            i += 1
            continue

        if ch in ")]}":
            if stack and stack[-1] == matching[ch]:
                stack.pop()

            i += 1

            if saw_open and not stack:
                j = i
                while j < len(text) and text[j].isspace():
                    j += 1
                if j < len(text) and text[j] == ";":
                    return j + 1
                return i

            continue

        if ch == ";" and saw_open and not stack:
            return i + 1

        i += 1

    return None