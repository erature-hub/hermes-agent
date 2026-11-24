"""Tests for execute_code sandbox failure hints."""

import json


from tools.code_execution_tool import _sandbox_failure_hint, execute_code


class TestSandboxFailureHint:
    def test_unavailable_tool_import_lists_available(self):
        err = ("Traceback (most recent call last):\n  File \"script.py\", line 1\n"
               "ImportError: cannot import name 'browser_navigate' from 'hermes_tools'")
        h = _sandbox_failure_hint(err, enabled_tools={"terminal", "read_file"})
        assert "browser_navigate" in h
        assert "read_file" in h and "terminal" in h




    def test_unknown_failure_no_hint(self):
        assert _sandbox_failure_hint("ZeroDivisionError: division by zero") is None

    def test_empty_stderr_no_hint(self):
        assert _sandbox_failure_hint("") is None


class TestHeredocTerminatorHint:
    """A shell heredoc terminator executed as Python must NAME the quoting trap.

    Measured across the fleet's own session store, the undefined names in this
    class are the shell tokens themselves: PYEOF (32), PY (15), DB_PATH (9),
    EOF (5). The worker cannot guess that PYEOF is a shell terminator, so the
    hint has to say so.
    """

    def test_the_measured_shell_tokens_all_get_the_hint(self):
        for name in ("PYEOF", "PY", "EOF", "DB_PATH", "KEYS"):
            err = (f'  File "<cell>", line 2, in <module>\n'
                   f"NameError: name '{name}' is not defined")
            h = _sandbox_failure_hint(err)
            assert h, name
            assert name in h
            assert "heredoc" in h.lower()
            assert "terminal()" in h

    def test_the_python_suggestion_survives(self):
        """CPython appends 'Did you mean ...?' — the rule must still match."""
        err = ("  File \"<cell>\", line 2, in <module>\n"
               "NameError: name 'PY' is not defined. Did you mean: 'P'?")
        h = _sandbox_failure_hint(err)
        assert h and "heredoc" in h.lower()

    def test_a_lower_case_undefined_name_keeps_its_own_hint(self):
        """A lower-case name is a different mistake — the rule must not swallow it."""
        err = "  File \"<cell>\", line 2, in <module>\nNameError: name 'load' is not defined"
        h = _sandbox_failure_hint(err)
        assert h is None or "heredoc" not in h.lower()

    def test_the_sandbox_helper_import_still_wins(self):
        """The narrow helper rule must keep priority over the broad shell-token one."""
        err = "NameError: name 'shell_quote' is not defined"
        h = _sandbox_failure_hint(err)
        assert h and "from hermes_tools import shell_quote" in h


class TestLiveSandboxHint:
    def test_bad_import_produces_hint_field(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = json.loads(execute_code(
            "from hermes_tools import totally_fake_tool\nprint('unreachable')",
            task_id="t-sbhint",
        ))
        assert r["status"] == "error"
        assert "hint" in r
        assert "totally_fake_tool" in r["hint"]


    def test_successful_script_has_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = json.loads(execute_code("print('fine')", task_id="t-sbhint"))
        assert r["status"] == "success"
        assert "hint" not in r
