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

import tapscribe.config_store
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


def _text_imports(source: str) -> list[tuple[int, str]]:
    """Every import in `source` that reaches into tapscribe.text — the relative
    `from .text import …` / `from . import text`, the absolute
    `from tapscribe.text import …`, or `import tapscribe.text`. AST-based, so
    only real imports count (a comment naming text.py is fine)."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module == "text" and node.level == 1:
                hits.append((node.lineno, ".text"))
            elif node.module == "tapscribe.text":
                hits.append((node.lineno, "tapscribe.text"))
            elif node.module is None and node.level == 1:
                for alias in node.names:
                    if alias.name == "text":
                        hits.append((node.lineno, ". import text"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tapscribe.text" or alias.name.endswith(".text"):
                    hits.append((node.lineno, alias.name))
    return hits


def test_config_store_imports_nothing_from_text() -> None:
    """Symmetric leaf pin (the other half of the split's acyclic module graph):
    config_store must NOT import back from tapscribe.text.

    The purity pin above fixes text.py's direction (catalog-free, so it can only
    depend on config_store, never the reverse). This pins the return edge. It is
    not incidental: the build's first cut had config_store importing the pure
    helpers back FROM text.py — a text ↔ config_store cycle that still passed the
    green gate, caught only by self-review. Pinning both edges makes the acyclic
    graph enforced, not accidental."""
    source = Path(tapscribe.config_store.__file__).read_text(encoding="utf-8")
    hits = _text_imports(source)
    assert hits == [], (
        f"config_store must not import from tapscribe.text (found {hits}). config_store is the leaf "
        "that owns the config-store layer; text.py re-exports FROM it. An import back into text.py "
        "re-introduces the circular dependency the split exists to remove."
    )


# Every symbol the text.py re-export block promises stays importable from
# tapscribe.text — its comment: "Existing callers (from .text import X) keep
# working without change". Consumers (the route modules, sessions.py,
# batch_transcribe.py,
# and many tests) rely on this surface, currently guarded only incidentally by
# the full suite; pin it so API preservation is stated, not accidental.
_RE_EXPORTED = (
    "_CONFIG_TEXT_CACHE",
    "CONFIG_KEYS",
    "MAX_CONFIG_TEXT_LEN",
    "SUMMARY_SOURCES",
    "atomic_write_text",
    "file_stat_sig",
    "parse_language_codes",
    "read_config",
    "read_languages",
    "read_summarizer_config",
    "read_text_file",
    "summarizer_default_public",
    "validate_config_text",
    "write_config",
    "write_languages",
    "write_summarizer_config",
)


def test_config_store_public_surface_is_re_exported_from_text() -> None:
    missing = [name for name in _RE_EXPORTED if not hasattr(tapscribe.text, name)]
    assert missing == [], (
        f"tapscribe.text must re-export {missing} from config_store — the split promises existing "
        "`from tapscribe.text import X` callers keep working. A dropped re-export silently breaks "
        "a consumer import."
    )
    # Each re-export is the SAME object as config_store's (a genuine re-export,
    # not an accidental shadowing definition that could drift).
    for name in _RE_EXPORTED:
        assert getattr(tapscribe.text, name) is getattr(tapscribe.config_store, name), (
            f"tapscribe.text.{name} must be the same object as tapscribe.config_store.{name}"
        )
