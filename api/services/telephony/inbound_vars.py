"""Shared helpers for promoting caller-supplied values into a call's context.

Multiple providers forward custom fields (usually from ``X-`` SIP headers) that
should become ``{{variable}}`` values in prompts. The normalization rules —
strip the ``x-``/``x_`` header prefix, keep only scalar values, and never
overwrite the reserved dispatcher keys — live here so every provider behaves
identically.
"""

import re
from typing import Any, Dict

from loguru import logger

# Keys the inbound dispatcher owns — passthrough vars must never clobber them.
RESERVED_CONTEXT_KEYS = frozenset(
    {
        "caller_number",
        "called_number",
        "direction",
        "provider",
        "telephony_configuration_id",
        "call_id",
    }
)

# Strips a leading x-/X-/x_/X_ header prefix, but only when followed by a
# separator, so keys like "xerox" are left untouched.
_HEADER_PREFIX_RE = re.compile(r"^[xX][-_]")


def normalize_passthrough_vars(raw: Any) -> Dict[str, Any]:
    """Normalize a dict of forwarded header/values into call-context variables.

    - Strips the ``x-`` / ``X-`` / ``x_`` / ``X_`` prefix (``X-first_name`` ->
      ``first_name``). Keys without the prefix pass through unchanged.
    - Accepts only scalar values (str/int/float/bool/None); anything else is
      dropped.
    - Drops keys that collide with reserved dispatcher keys (after stripping).
    - On duplicate normalized keys the last value wins.

    Returns a new dict safe to merge into ``initial_context``.
    """
    result: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return result

    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        norm = _HEADER_PREFIX_RE.sub("", key).strip()
        if not norm:
            continue
        if norm in RESERVED_CONTEXT_KEYS:
            logger.warning(f"Inbound passthrough var '{norm}' is reserved; ignoring")
            continue
        if not (isinstance(value, (str, int, float, bool)) or value is None):
            logger.warning(
                f"Inbound passthrough var '{norm}' has non-scalar value; ignoring"
            )
            continue
        if norm in result:
            logger.warning(
                f"Inbound passthrough var '{norm}' seen twice; using last value"
            )
        result[norm] = value
    return result
