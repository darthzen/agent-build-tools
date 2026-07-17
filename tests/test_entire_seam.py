#!/usr/bin/env python3
"""Tests for the Entire session-provenance seam in agent-tools.py.

Self-contained: no pytest required (`python3 tests/test_entire_seam.py`), but the
test_* functions are also collectable by pytest. GitHub and Ollama are never
touched — a stub `entire` on PATH stands in for the real CLI so the seam's
behavior (gating, PR-body threading, attach arguments) is verified deterministically.
"""
import importlib.util, os, stat, sys, tempfile, textwrap

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "agent-tools.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("agent_tools", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _configure(entire=None):
    """Inject a config dict (bypasses reading config.yaml)."""
    cfg = {"project": {"repo": "owner/repo"}}
    if entire is not None:
        cfg["entire"] = entire
    mod._CONFIG = cfg


class _StubEntire:
    """Context manager: put a fake `entire` executable first on PATH.

    mode 'json'  -> `session current --json` prints a session_id JSON
    mode 'plain' -> prints `Session <uuid>` (exercises the regex fallback)
    mode 'fail'  -> exits non-zero (exercises best-effort/non-fatal path)
    Every invocation appends its argv to `self.log`.
    """
    def __init__(self, mode="json", sid="3f1b19c7-0f32-4352-ba7c-ee0b488ccc0f"):
        self.mode, self.sid = mode, sid
        self._tmp = None
        self.log = None

    def __enter__(self):
        self._tmp = tempfile.mkdtemp(prefix="stub-entire-")
        self.log = os.path.join(self._tmp, "calls.log")
        script = os.path.join(self._tmp, "entire")
        with open(script, "w") as fh:
            fh.write(textwrap.dedent(f"""\
                #!/usr/bin/env python3
                import sys, json
                with open({self.log!r}, "a") as f:
                    f.write(" ".join(sys.argv[1:]) + "\\n")
                mode, sid = {self.mode!r}, {self.sid!r}
                args = sys.argv[1:]
                if args[:2] == ["session", "current"]:
                    if mode == "json":
                        print(json.dumps({{"session_id": sid}})); sys.exit(0)
                    if mode == "plain":
                        print("Session " + sid); sys.exit(0)
                    sys.exit(1)
                if mode == "fail":
                    sys.stderr.write("stub: simulated failure\\n"); sys.exit(1)
                sys.exit(0)
            """))
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self._tmp + os.pathsep + self._old_path
        return self

    def calls(self):
        if not os.path.exists(self.log):
            return []
        return [l for l in open(self.log).read().splitlines() if l]

    def __exit__(self, *exc):
        os.environ["PATH"] = self._old_path


# ---- gating ---------------------------------------------------------------
def test_disabled_by_default():
    _configure(entire=None)
    assert mod.entire_on() is False
    assert mod.entire_pr_marker("abc") == ""

def test_disabled_never_calls_entire():
    _configure(entire={"enabled": False})
    with _StubEntire() as stub:
        assert mod.entire_session_id() is None
        mod.entire_relink("abc-123")
        assert stub.calls() == [], "entire must not be invoked when disabled"


# ---- PR-body session threading (pure string round-trip) -------------------
def test_marker_roundtrip():
    _configure(entire={"enabled": True})
    marker = mod.entire_pr_marker("sess-42")
    assert mod.ENTIRE_MARKER in marker
    body = "Closes #5" + marker
    assert "Closes #5" in body
    assert mod.entire_session_from_body(body) == "sess-42"

def test_session_from_body_none():
    _configure(entire={"enabled": True})
    assert mod.entire_session_from_body("Closes #5\n\nno marker here") is None
    assert mod.entire_session_from_body("") is None


# ---- session id discovery -------------------------------------------------
def test_session_id_from_json():
    _configure(entire={"enabled": True})
    with _StubEntire(mode="json", sid="11111111-2222-3333-4444-555555555555"):
        assert mod.entire_session_id() == "11111111-2222-3333-4444-555555555555"

def test_session_id_plaintext_fallback():
    _configure(entire={"enabled": True})
    with _StubEntire(mode="plain", sid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
        assert mod.entire_session_id() == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---- re-anchor (attach) arguments -----------------------------------------
def test_relink_default_no_force():
    _configure(entire={"enabled": True, "agent": "claude-code"})
    with _StubEntire() as stub:
        mod.entire_relink("sess-xyz")
        calls = stub.calls()
        assert any(c.startswith("session attach sess-xyz --agent claude-code") for c in calls), calls
        assert not any("--force" in c for c in calls), "must not amend/force by default"

def test_relink_amend_adds_force():
    _configure(entire={"enabled": True, "amend": True})
    with _StubEntire() as stub:
        mod.entire_relink("sess-xyz")
        assert any("--force" in c for c in stub.calls())

def test_relink_failure_is_nonfatal():
    _configure(entire={"enabled": True})
    with _StubEntire(mode="fail"):
        mod.entire_relink("sess-xyz")   # must not raise / sys.exit

def test_relink_noops_without_sid():
    _configure(entire={"enabled": True})
    with _StubEntire() as stub:
        mod.entire_relink(None)
        assert stub.calls() == []


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
