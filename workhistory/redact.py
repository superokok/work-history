"""Mask secrets and personal identifiers in text before it leaves the PC.

Applied to every diff excerpt and note sent to an LLM. It errs on the side of
masking, but keeps ordinary code readable (`password = req.getParameter(...)`
is code, not a secret).
"""

import re

MASK = "***"

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM private keys and certificates
    (re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----",
                re.S), "-----BEGIN " + MASK + "-----"),
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1" + MASK),
    # AWS access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    # key = "value" / key: 'value' for secret-looking keys (quoted values)
    (re.compile(r"""(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"""
                r"""client[_-]?secret|credential)s?["']?\s*[:=]\s*)(["'])([^"'\s]{3,})\2"""), r"\1\2" + MASK + r"\2"),
    # key=value / key: value anywhere in a line (config files, diff lines, notes): the value must be a
    # plain token ending at whitespace, a quote, a separator or the end, so that code such as
    # `password = req.getParameter(...)` (followed by "(") is left alone.
    (re.compile(r"""(?i)(\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"""
                r"""client[_-]?secret)[\w.-]*\s*[:=]\s*)(?!["'])([^\s"'(){};,=&#]{3,})(?=[\s"',;)}&#]|$)"""),
     r"\1" + MASK),
    # Credentials in URLs: scheme://user:password@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+@", re.I), r"\1" + MASK + "@"),
    # .netrc / .pgpass style "password <value>" (a line of its own, or after machine/login)
    (re.compile(r"(?im)(^\s*password\s+|\b(?:machine|login)\s+\S+.*?\bpassword\s+)\S+"), r"\1" + MASK),
    # Well-known token formats: Google API keys, Slack tokens, OpenAI/Anthropic-style keys, GitHub tokens
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),
    # Korean resident registration numbers
    (re.compile(r"\b\d{6}-?[1-4]\d{6}\b"), "******-*******"),
    # Card numbers (4 groups of 4 digits)
    (re.compile(r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b"), "****-****-****-****"),
    # Long random-looking tokens, e.g. pasted keys: 40+ hex chars, or 40+ chars mixing upper case,
    # lower case and digits. Paths ('/') and snake_case identifiers do not qualify.
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), MASK),
    (re.compile(r"(?<![\w/.])(?=[A-Za-z0-9+_-]*[0-9])(?=[A-Za-z0-9+_-]*[a-z])(?=[A-Za-z0-9+_-]*[A-Z])"
                r"[A-Za-z0-9+_-]{40,}={0,2}"), MASK),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
