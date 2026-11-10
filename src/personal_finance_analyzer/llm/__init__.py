"""LLM transport (`client.py`) and the statement-parsing egress seam (`statement_parser.py`).

`client.py` is the only module in this codebase that imports an HTTP client. It is called only
from the two egress seams — `classify/llm/egress.py` (merchant descriptions) and
`llm/statement_parser.py` (the page text of a statement the user explicitly chose to parse with
the LLM) — enforced by `tests/test_architecture_guards.py`.
"""
