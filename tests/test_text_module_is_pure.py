"""RED contract for #230 — text.py is the pure-helpers module its docstring claims.

text.py's module docstring promises "Pure text helpers … depends on nothing in
TapScribe besides config paths", but ~half the module is an operator-config
persistence layer (CONFIG_KEYS / read_config / write_config, the summarizer-config
reader/writer with its secret-handling seam, the languages config) that validates
against — and lazily imports the privates of — two catalog packages
(transcribers.catalog, summarizers.catalog). Two costs the issue names: findability
(the summarizer secret-handling / api_key redaction seam and the batch-model
allowlist live in a file named "text", where neither a config grep nor a security
review looks) and the broken deletion test (you cannot reason about "pure text
helpers" in isolation because refactoring either catalog now breaks text.py).

The fix moves the config-store half into its own honestly-named module, leaving
text.py the dependency-free primitives (filename mint/parsers, sanitisers,
parse_iso, atomic_write_text, file_stat_sig).

What this file pins:
  * PURITY (RED at base): text.py imports NO catalog package. RED at base — it has
    five such imports today (inside the config-store functions); they can only leave
    by moving those functions out (they need the catalog for validation, so they
    can't just drop the import and stay). AST-based, so a comment/docstring that
    merely NAMES a catalog is fine — only real imports count. Design-agnostic: a
    re-export (`from .config_store import read_config`) keeps text.py catalog-free and
    passes, so it does not dictate whether the config API stays re-exported here.
  * SECURITY SEAM SURVIVES (green -> green guardrail): summarizer_default_public still
    redacts — api_key is NEVER in the public projection, only the key_set boolean;
    the non-secret base_url is surfaced. Resolved design-agnostically (config_store
    if the move landed it there, else text), so it holds wherever the seam now lives.
    The rest of the config-store behaviour (read/write round-trips, languages,
    validation) is carried by the existing suite, kept green by the full-suite gate.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import tapscribe.text


def _catalog_imports(source: str) -> list[tuple[int, str]]:
    """Every `import`/`from … import` in `source` whose module path names a
    catalog package. AST-based, so only real imports count — not comments or
    strings that merely mention a catalog."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and "catalog" in node.module:
            hits.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "catalog" in alias.name:
                    hits.append((node.lineno, alias.name))
    return hits


def test_text_module_imports_no_catalog_package() -> None:
    source = Path(tapscribe.text.__file__).read_text(encoding="utf-8")
    hits = _catalog_imports(source)
    assert hits == [], (
        f"text.py must not import a catalog package (found {hits}). The config-store half that "
        "validates against transcribers/summarizers catalogs belongs in its own module so text.py is "
        "the dependency-free pure-helpers module its docstring claims — and a catalog refactor stops "
        "reaching into it."
    )


def _resolve(name: str):
    """Find a config-store symbol wherever the split put it — the new config
    module if it exists, else still text — so this pin is agnostic to the exact
    new module name and to whether text re-exports it."""
    for modname in ("tapscribe.config_store", "tapscribe.text"):
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError:
            continue
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AssertionError(f"{name} not importable from config_store or text after the split")


def test_api_key_redaction_seam_survives_the_move() -> None:
    summarizer_default_public = _resolve("summarizer_default_public")
    pub = summarizer_default_public(
        {
            "source": "api",
            "api_key": "sk-secret-value",
            "base_url": "http://llm.local",
            "model": "gpt",
            "prompt": "p",
            "command": "",
            "max_tokens": 512,
        }
    )
    assert "api_key" not in pub, "the write-only api_key must NEVER appear in the public projection"
    assert pub["key_set"] is True, "key_set must report that a key is configured"
    assert pub["base_url"] == "http://llm.local", "the non-secret base_url must be surfaced"
    # And an unset key reports key_set False (still no api_key leak).
    cleared = summarizer_default_public({"source": "api", "api_key": "", "base_url": "http://llm.local"})
    assert cleared["key_set"] is False and "api_key" not in cleared
