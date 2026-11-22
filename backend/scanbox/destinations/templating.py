"""Safe ``{name}`` template rendering for user-supplied templates.

Unlike :meth:`str.format`, this never evaluates attribute access (``{a.b}``),
indexing (``{a[0]}``), conversions (``{a!r}``) or format specs (``{a:>10}``):
only a bare identifier that is a key of ``vars`` is substituted. Anything else
(unknown names, malformed tokens) is left verbatim, so a typo in a template is
visible in the output instead of raising. ``{{`` and ``}}`` produce literal
braces.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(template: str, vars: Mapping[str, Any]) -> str:
    """Substitute ``{name}`` tokens with ``str(vars[name])``; see module docstring."""

    def _sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        name = m.group(1)
        if name in vars:
            value = vars[name]
            return "" if value is None else str(value)
        return tok

    return _TOKEN.sub(_sub, template)
