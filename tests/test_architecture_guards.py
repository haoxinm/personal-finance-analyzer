"""CI architecture guards (I2, I3, P0-1).

Walk the source tree with `ast` (not regex, so aliased and conditional imports are caught too)
to enforce two invariants that everything else in this codebase depends on:

- **I2**: exactly one module (`llm/client.py`) may import an HTTP client, and only the two egress
  seams (`classify/llm/egress.py`, `llm/statement_parser.py`) may import `llm.client`.
- no module imports `litellm` (replaced by the httpx client).
- **I3**: `ingest/` imports no HTTP client and makes no network call, ever.
- the opt-in yearly tax-parameter check (`api/services/tax_params_update.py`) is the only other
  module that imports an HTTP client and the only one that names its host.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "personal_finance_analyzer"

_HTTP_CLIENT_MODULES = {
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
    "http.client",
    "socket",
    "urllib.request",
}

_CLIENT = "src/personal_finance_analyzer/llm/client.py"
#: The opt-in once-a-year fetch of the federal tax parameter file (no user data in the request).
_PARAMS_UPDATER = "src/personal_finance_analyzer/api/services/tax_params_update.py"
_PARAMS_HOST = "raw.githubusercontent.com"
_CLIENT_CALLERS = {
    "src/personal_finance_analyzer/classify/llm/egress.py",
    "src/personal_finance_analyzer/llm/statement_parser.py",
    "src/personal_finance_analyzer/llm/tax_parser.py",
}


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_top_level_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def _full_imported_modules(tree: ast.AST) -> set[str]:
    """Like `_imported_top_level_modules` but keeps the full dotted path (e.g. 'http.client')."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def _rel(path: Path) -> str:
    return path.relative_to(_SRC_ROOT.parent.parent).as_posix()


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_http_client(tree: ast.AST) -> set[str]:
    full = _full_imported_modules(tree)
    return {m for m in full if m in _HTTP_CLIENT_MODULES} | (
        _imported_top_level_modules(tree) & _HTTP_CLIENT_MODULES
    )


def _imports_llm_client(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "personal_finance_analyzer.llm.client" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            if node.module == "personal_finance_analyzer.llm.client":
                return True
            if node.module == "personal_finance_analyzer.llm" and any(
                alias.name == "client" for alias in node.names
            ):
                return True
    return False


def test_only_llm_client_imports_an_http_client() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _iter_py_files(_SRC_ROOT):
        hits = _imports_http_client(_parse(path))
        if hits and _rel(path) not in (_CLIENT, _PARAMS_UPDATER):
            offenders[_rel(path)] = hits
    assert not offenders, f"HTTP client imported outside {_CLIENT}/{_PARAMS_UPDATER}: {offenders}"


def test_only_egress_seams_import_llm_client() -> None:
    offenders = [
        _rel(path)
        for path in _iter_py_files(_SRC_ROOT)
        if _imports_llm_client(_parse(path)) and _rel(path) not in _CLIENT_CALLERS
    ]
    assert not offenders, f"llm.client imported outside the egress seams: {offenders}"


def test_nothing_imports_litellm() -> None:
    offenders = [
        _rel(path)
        for path in _iter_py_files(_SRC_ROOT)
        if "litellm" in _imported_top_level_modules(_parse(path))
    ]
    assert not offenders, f"litellm is no longer a dependency: {offenders}"


def test_ingest_imports_no_http_client() -> None:
    ingest_root = _SRC_ROOT / "ingest"
    offenders: dict[str, set[str]] = {}
    for path in _iter_py_files(ingest_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        full_modules = _full_imported_modules(tree)
        top_modules = _imported_top_level_modules(tree)
        hits = {m for m in full_modules if m in _HTTP_CLIENT_MODULES} | (
            top_modules & _HTTP_CLIENT_MODULES
        )
        if hits:
            offenders[str(path.relative_to(_SRC_ROOT.parent.parent))] = hits

    assert not offenders, f"ingest/ imports a network-capable module: {offenders}"


def _tax_module_paths() -> list[Path]:
    """Tax-feature modules that exist right now (some land in later work packages)."""
    paths: list[Path] = []
    tax_pkg = _SRC_ROOT / "ingest" / "tax"
    if tax_pkg.is_dir():
        paths.extend(_iter_py_files(tax_pkg))
    for rel in (
        "analytics/tax_query.py",
        "api/routers/tax.py",
        "core/tax_types.py",
        "core/tax_boxes.py",
        "core/tax_calc.py",
        "core/tax_params.py",
        "analytics/tax_estimate.py",
    ):
        candidate = _SRC_ROOT / rel
        if candidate.is_file():
            paths.append(candidate)
    return paths


def test_tax_modules_import_no_llm_or_http() -> None:
    """Tax data never reaches an egress seam: no LLM package and no HTTP client in the feature."""
    paths = _tax_module_paths()
    assert any(p.name == "tax_types.py" for p in paths)
    offenders: dict[str, set[str]] = {}
    for path in paths:
        tree = _parse(path)
        hits = {
            m
            for m in _full_imported_modules(tree)
            if m == "personal_finance_analyzer.llm"
            or m.startswith("personal_finance_analyzer.llm.")
            or m == "personal_finance_analyzer.classify.llm"
            or m.startswith("personal_finance_analyzer.classify.llm.")
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                in ("personal_finance_analyzer", "personal_finance_analyzer.classify")
            ):
                hits |= {f"{node.module}.{a.name}" for a in node.names if a.name in ("llm",)}
        hits |= _imports_http_client(tree)
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, f"tax modules import an LLM or network module: {offenders}"


def test_only_the_tax_llm_factory_imports_the_tax_llm_parser() -> None:
    """The tax package gets the fallback injected; `api/services/tax_llm.py` is the sole importer
    of the egress module (the CLI reaches it through that factory)."""
    allowed = {"src/personal_finance_analyzer/api/services/tax_llm.py"}
    offenders = [
        _rel(path)
        for path in _iter_py_files(_SRC_ROOT)
        if any(
            m == "personal_finance_analyzer.llm.tax_parser"
            or m.startswith("personal_finance_analyzer.llm.tax_parser.")
            for m in _full_imported_modules(_parse(path))
        )
        and _rel(path) not in allowed
    ]
    assert not offenders, f"llm.tax_parser imported outside the factory: {offenders}"


def test_tax_llm_parser_imports_no_database_or_analytics() -> None:
    tree = _parse(_SRC_ROOT / "llm" / "tax_parser.py")
    bad = {
        m
        for m in _full_imported_modules(tree)
        if m.startswith(
            (
                "personal_finance_analyzer.db",
                "personal_finance_analyzer.analytics",
                "personal_finance_analyzer.api",
            )
        )
    }
    assert not bad, f"llm/tax_parser.py must stay free of DB/analytics/API code: {bad}"


def _net_worth_module_paths() -> list[Path]:
    """Net-worth modules (balances are local-only financial data)."""
    rels = (
        "core/net_worth.py",
        "analytics/net_worth_query.py",
        "api/routers/net_worth.py",
        "api/services/net_worth.py",
        "core/forecast.py",
        "analytics/forecast_baseline.py",
        "api/routers/forecast.py",
        "api/services/forecast.py",
    )
    return [_SRC_ROOT / rel for rel in rels if (_SRC_ROOT / rel).is_file()]


def test_net_worth_modules_import_no_llm_or_http() -> None:
    """Balances never reach an egress seam: no LLM package and no HTTP client in the feature."""
    paths = _net_worth_module_paths()
    assert {p.name for p in paths} >= {"net_worth.py", "net_worth_query.py"}
    assert len(paths) == 8
    offenders: dict[str, set[str]] = {}
    for path in paths:
        tree = _parse(path)
        hits = {
            m
            for m in _full_imported_modules(tree)
            if m == "personal_finance_analyzer.llm"
            or m.startswith("personal_finance_analyzer.llm.")
            or m == "personal_finance_analyzer.classify.llm"
            or m.startswith("personal_finance_analyzer.classify.llm.")
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
                in ("personal_finance_analyzer", "personal_finance_analyzer.classify")
            ):
                hits |= {f"{node.module}.{a.name}" for a in node.names if a.name in ("llm",)}
        hits |= _imports_http_client(tree)
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, f"net-worth modules import an LLM or network module: {offenders}"


def test_backup_modules_import_no_llm_or_http() -> None:
    """A backup holds everything the app knows and has no egress path: no LLM, no HTTP client."""
    paths = [
        *_iter_py_files(_SRC_ROOT / "backup"),
        _SRC_ROOT / "api" / "routers" / "backup.py",
        _SRC_ROOT / "api" / "restore_gate.py",
        _SRC_ROOT / "cli_backup.py",
    ]
    assert any(p.name == "archive.py" for p in paths)
    offenders: dict[str, set[str]] = {}
    for path in paths:
        tree = _parse(path)
        hits = {
            m
            for m in _full_imported_modules(tree)
            if m in ("personal_finance_analyzer.llm", "personal_finance_analyzer.classify.llm")
            or m.startswith(
                ("personal_finance_analyzer.llm.", "personal_finance_analyzer.classify.llm.")
            )
        }
        hits |= _imports_http_client(tree)
        if hits:
            offenders[_rel(path)] = hits
    assert not offenders, f"backup modules import an LLM or network module: {offenders}"


def test_only_the_params_updater_names_the_tax_params_host() -> None:
    """The yearly parameter check is the one place that knows the URL it requests."""
    offenders = [
        _rel(path)
        for path in _iter_py_files(_SRC_ROOT)
        if _PARAMS_HOST in path.read_text(encoding="utf-8") and _rel(path) != _PARAMS_UPDATER
    ]
    assert not offenders, f"{_PARAMS_HOST} named outside {_PARAMS_UPDATER}: {offenders}"


def test_params_updater_is_isolated_from_user_data_and_llm_code() -> None:
    """The request carries no user data because the module cannot reach any: it imports no
    database, analytics, tax-ingest or LLM code."""
    tree = _parse(_SRC_ROOT / "api" / "services" / "tax_params_update.py")
    bad = {
        m
        for m in _full_imported_modules(tree)
        if m.startswith(
            (
                "personal_finance_analyzer.db",
                "personal_finance_analyzer.analytics",
                "personal_finance_analyzer.ingest",
                "personal_finance_analyzer.llm",
                "personal_finance_analyzer.classify",
            )
        )
    }
    assert not bad, f"api/services/tax_params_update.py must stay isolated: {bad}"
    assert _imports_http_client(tree) == {"httpx"}


def test_tax_params_store_makes_no_network_call() -> None:
    for rel in ("core/tax_params.py", "core/tax_calc.py"):
        assert not _imports_http_client(_parse(_SRC_ROOT / rel)), rel
