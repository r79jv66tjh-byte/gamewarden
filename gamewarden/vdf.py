"""Minimaler Parser/Writer für Valves Text-KeyValues (.vdf / .acf)."""
from __future__ import annotations


class VdfError(ValueError):
    pass


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
            yield ("s", "".join(buf))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            yield ("s", text[i:j])
            i = j


def loads(text: str) -> dict:
    """Parst KeyValues-Text in verschachtelte dicts (Reihenfolge bleibt erhalten)."""
    if text.startswith("﻿"):
        text = text[1:]
    root: dict = {}
    stack = [root]
    key = None
    for tok in _tokens(text):
        if tok == "{":
            if key is None:
                raise VdfError("'{' ohne Schlüssel")
            new: dict = {}
            stack[-1][key] = new
            stack.append(new)
            key = None
        elif tok == "}":
            if len(stack) == 1:
                raise VdfError("Unerwartetes '}'")
            stack.pop()
            key = None
        else:
            if key is None:
                key = tok[1]
            else:
                stack[-1][key] = tok[1]
                key = None
    if len(stack) != 1:
        raise VdfError("Datei endet mitten in einem Block")
    return root


def load(path) -> dict:
    with open(path, encoding="utf-8", errors="replace") as f:
        return loads(f.read())


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def dumps(data: dict, indent: int = 0) -> str:
    out = []
    tab = "\t" * indent
    for k, v in data.items():
        if isinstance(v, dict):
            out.append(f'{tab}"{_esc(k)}"\n{tab}{{\n')
            out.append(dumps(v, indent + 1))
            out.append(f"{tab}}}\n")
        else:
            out.append(f'{tab}"{_esc(k)}"\t\t"{_esc(str(v))}"\n')
    return "".join(out)


def get_ci(d: dict, key: str, default=None):
    """Case-insensitiver Zugriff (Valve ist bei Groß-/Kleinschreibung uneinheitlich)."""
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return default
