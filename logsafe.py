"""
logsafe.py
----------
Exception text that is safe for logs/agent.log (uploaded as a workflow artifact and read
by improve.py). Playwright errors carry a "Call log" with page HTML, which can include a
customer's phone number or e-mail: only the class name and the first 200 characters are
kept, with every digit and every address-like token masked. Stdlib only.
"""

import re

_ADDRESS = re.compile(r"\S*@\S*")
_DIGIT = re.compile(r"\d")


def describe(exc: BaseException, limit: int = 200) -> str:
    """'<ExceptionType>: <first `limit` characters, digits -> #, addresses -> [adress]>'."""
    text = " ".join(str(exc).split())
    text = _DIGIT.sub("#", _ADDRESS.sub("[adress]", text))
    return f"{type(exc).__name__}: {text[:limit]}"
