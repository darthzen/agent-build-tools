#!/usr/bin/env python3
"""Tests for the model-provenance seam (`Generated-By:`) in agent-tools.py.

Self-contained: no pytest required (`python3 tests/test_provenance_seam.py`), but
the test_* functions are also collectable by pytest. Nothing here touches git,
GitHub or Ollama — the seam is pure string construction plus config/env gating,
which is exactly what makes it safe to assert deterministically.
"""
import importlib.util, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "agent-tools.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("agent_tools", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _configure(provenance=None):
    """Inject a config dict (bypasses reading config.yaml)."""
    cfg = {"project": {"repo": "owner/repo"}}
    if provenance is not None:
        cfg["provenance"] = provenance
    mod._CONFIG = cfg


def _clear_env():
    for k in ("AGENT_GENERATED_BY", "NIFFLER_GENERATED_BY"):
        os.environ.pop(k, None)


# ---- gating ---------------------------------------------------------------
def test_disabled_by_default():
    _clear_env(); _configure(provenance=None)
    assert mod.provenance_on() is False
    assert mod.generated_by() is None
    assert mod.provenance_trailer() == "", "absent config block must add nothing"

def test_explicitly_disabled_adds_nothing():
    _clear_env()
    _configure(provenance={"enabled": False, "generated_by": "qwen3-coder:30b"})
    assert mod.provenance_trailer() == ""

def test_enabled_without_model_adds_nothing():
    """enabled: true with no model is a misconfiguration, not a reason to emit
    an empty trailer that would land a bare `Generated-By:` line on main."""
    _clear_env(); _configure(provenance={"enabled": True})
    assert mod.generated_by() is None
    assert mod.provenance_trailer() == ""

def test_blank_model_is_treated_as_unset():
    _clear_env(); _configure(provenance={"enabled": True, "generated_by": "   "})
    assert mod.generated_by() is None
    assert mod.provenance_trailer() == ""


# ---- trailer construction -------------------------------------------------
def test_trailer_shape():
    _clear_env()
    _configure(provenance={"enabled": True, "generated_by": "qwen3-coder:30b (ollama-code-mcp)"})
    tr = mod.provenance_trailer()
    assert tr.startswith("\n\n"), "trailer must be its own paragraph"
    assert tr.strip() == "Generated-By: qwen3-coder:30b (ollama-code-mcp)"

def test_trailer_survives_body_composition():
    """The trailer must remain a line-anchored trailer once appended to a real
    PR body, because that body becomes the squash commit message."""
    _clear_env()
    _configure(provenance={"enabled": True, "generated_by": "qwen3-coder:30b"})
    body = "Closes #9" + mod.provenance_trailer()
    assert "Closes #9" in body
    assert mod.generated_by_from_body(body) == "qwen3-coder:30b"

def test_coexists_with_entire_marker():
    """submit appends both trailers; neither may swallow the other."""
    _clear_env()
    mod._CONFIG = {"project": {"repo": "owner/repo"},
                   "provenance": {"enabled": True, "generated_by": "qwen3-coder:30b"},
                   "entire": {"enabled": True}}
    body = "Closes #9" + mod.provenance_trailer() + mod.entire_pr_marker("sess-42")
    assert mod.generated_by_from_body(body) == "qwen3-coder:30b"
    assert mod.entire_session_from_body(body) == "sess-42"


# ---- env override ---------------------------------------------------------
def test_env_overrides_config():
    _clear_env()
    _configure(provenance={"enabled": True, "generated_by": "from-config"})
    os.environ["AGENT_GENERATED_BY"] = "devstral2:24b"
    try:
        assert mod.generated_by() == "devstral2:24b"
    finally:
        _clear_env()

def test_env_does_not_enable_a_disabled_seam():
    """A stray env var must not start writing trailers into a repo that never
    opted in."""
    _clear_env(); _configure(provenance=None)
    os.environ["AGENT_GENERATED_BY"] = "devstral2:24b"
    try:
        assert mod.generated_by() is None
        assert mod.provenance_trailer() == ""
    finally:
        _clear_env()

def test_legacy_niffler_env_fallback():
    _clear_env(); _configure(provenance={"enabled": True})
    os.environ["NIFFLER_GENERATED_BY"] = "qwen3-coder:30b"
    try:
        assert mod.generated_by() == "qwen3-coder:30b"
    finally:
        _clear_env()


# ---- read-back ------------------------------------------------------------
def test_from_body_none_when_absent():
    assert mod.generated_by_from_body("Closes #5\n\nno trailer here") is None
    assert mod.generated_by_from_body("") is None
    assert mod.generated_by_from_body(None) is None

def test_from_body_ignores_inline_mention():
    """Only a line-anchored trailer counts — prose mentioning the marker must
    not be mistaken for provenance."""
    assert mod.generated_by_from_body("we discussed Generated-By: foo in review") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
