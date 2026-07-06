"""Safety checks: secret detection, PII risk, sensitivity classification.

Skeleton — implementation in Phase 1A (T03).
"""

from __future__ import annotations

import re

# Common secret patterns — content matching these should be blocked.
SECRET_PATTERNS = [
    # AWS access keys
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    # AWS secret keys (40 chars of base64-ish after 'aws_secret')
    (re.compile(r"aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}"), "AWS secret key"),
    # GitHub tokens (classic + fine-grained)
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "GitHub token"),
    # Generic API key patterns
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9]{32,}"), "Generic API key"),
    # Generic password in config
    (re.compile(r"(?i)password\s*[=:]\s*['\"]?[^\s'\"]{8,}"), "Password in config"),
    # Private keys
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"), "Private key"),
    # Slack tokens
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]+"), "Slack token"),
]


def detect_secrets(content: str) -> list[dict]:
    """Check content for secret patterns.

    Returns list of matches: [{pattern_name, match_start, match_end}].
    Empty list = no secrets detected.
    """
    matches = []
    for pattern, name in SECRET_PATTERNS:
        for m in pattern.finditer(content):
            matches.append({
                "pattern_name": name,
                "match_start": m.start(),
                "match_end": m.end(),
            })
    return matches


def has_secrets(content: str) -> bool:
    """Quick boolean check."""
    return len(detect_secrets(content)) > 0
