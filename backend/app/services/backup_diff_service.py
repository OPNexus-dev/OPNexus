"""Readable, redacted diff between two stored config backups of the same host.

Both files are parsed (defusedxml), the volatile ``revision`` block is dropped, and every leaf that
looks like a secret (password, key, token, PSK, community...) or is a long blob (certificates,
private keys) is replaced by a short hash. A changed secret therefore still shows up as a change,
but its value never leaves the server. Output is a unified diff, capped in size.
"""
import difflib
import hashlib
import re
from xml.etree.ElementTree import Element, indent, tostring

import defusedxml.ElementTree as safe_ET

MAX_LINES = 3000
MAX_BYTES = 32 * 1024 * 1024
_SECRET = re.compile(r"password|passwd|psk|secret|key|passphrase|token|prv|hash|community|bindpw|privpass", re.I)
_HARMLESS_SUFFIX = re.compile(r"(len|length|type|size|bits|file|name)$", re.I)
_BLOB = 120


class InvalidConfig(ValueError):
    pass


def _redact(root: Element) -> None:
    for el in root.iter():
        if len(el) == 0 and el.text and el.text.strip():
            text = el.text.strip()
            if (_SECRET.search(el.tag) and not _HARMLESS_SUFFIX.search(el.tag)) or len(text) > _BLOB:
                el.text = "‹sha256:" + hashlib.sha256(text.encode()).hexdigest()[:12] + "›"


def normalise(xml_bytes: bytes) -> list[str]:
    if not xml_bytes.strip() or len(xml_bytes) > MAX_BYTES:
        raise InvalidConfig("leer oder zu groß")
    try:
        root = safe_ET.fromstring(xml_bytes)
    except Exception as exc:  # defusedxml raises several parse/forbidden-entity types
        raise InvalidConfig(type(exc).__name__) from None
    if root.tag != "opnsense":
        raise InvalidConfig("keine OPNsense-Konfiguration")
    for volatile in root.findall("revision"):
        root.remove(volatile)
    _redact(root)
    indent(root, space="  ")
    return tostring(root, encoding="unicode").splitlines()


def diff(old: bytes, new: bytes) -> dict:
    a, b = normalise(old), normalise(new)
    lines = list(difflib.unified_diff(a, b, "älter", "neuer", lineterm="", n=2))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return {"identical": not lines, "added": added, "removed": removed, "truncated": len(lines) > MAX_LINES, "lines": lines[:MAX_LINES]}
