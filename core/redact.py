"""Keeping credentials out of the log.

`--dry-run` exists to print the request an action would make, and that request
carries `Authorization: Bearer ...`. state/ is gitignored, but a bearer token
sitting in a plaintext log forever is still a leak.
"""

import re

MASK = "<redacted>"

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|cookie|token|secret|password|passwd|api[-_]?key|^key$)", re.I
)

# Values seen coming out of ${VAR} interpolation. Registered by the config
# loader so a secret is masked even when it lands under an innocent key name.
_SECRET_VALUES = set()


def remember_secret(value):
    if isinstance(value, str) and len(value) >= 8:
        _SECRET_VALUES.add(value)


def redact(node):
    if isinstance(node, dict):
        return {
            k: MASK if _SENSITIVE_KEY_RE.search(str(k)) else redact(v) for k, v in node.items()
        }
    if isinstance(node, (list, tuple)):
        return [redact(v) for v in node]
    if isinstance(node, str):
        out = node
        for secret in _SECRET_VALUES:
            if secret in out:
                out = out.replace(secret, MASK)
        return out
    return node
