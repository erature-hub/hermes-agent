"""Tests for execute_code sandbox failure hints.

The heredoc-terminator class is asserted through the kernel's own result
assembly (``tools.code_kernel._cell_result``): that is where the cell's source
is available, and keeping the rule there means the change touches ONE module
(the kernel imports ``code_execution_tool`` lazily and separately, so a
cross-module signature change would open a mixed-version window in a running
gateway).
"""

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
    class are the shell tokens themselves (PY 104, PYEOF 38, EOF 15, EOF2 1), and
    in every recovered case the failing line of the cell IS the bare token.

    The verdict is anchored to that line, not to the name's spelling: a plain
    all-caps rule also fires on ordinary Python bugs and tells the worker it
    wrote a heredoc when it did not.
    """

    @staticmethod
    def _trace(name, line):
        return ('Traceback (most recent call last):\n'
                '  File "C:\\\\hermes_kernel_abc\\\\hermes_kernel_runner.py", line 124, in run_cell\n'
                '    exec(compile(request["code"], "<cell>", "exec"), GLOBALS)\n'
                f'  File "<cell>", line {line}, in <module>\n'
                f"NameError: name '{name}' is not defined")

    @staticmethod
    def _hint(name, line, source):
        from tools.code_kernel import _heredoc_terminator_hint
        return _heredoc_terminator_hint(TestHeredocTerminatorHint._trace(name, line), source)

    def test_every_measured_shell_token_is_named(self):
        for name in ("PY", "PYEOF", "EOF", "EOF2"):
            h = self._hint(name, 2, f"print('work')\n{name}\n")
            assert h, name
            assert name in h
            assert "heredoc" in h.lower()
            assert "terminal()" in h

    def test_the_python_suggestion_suffix_survives(self):
        """CPython appends 'Did you mean ...?' — the rule must still match."""
        from tools.code_kernel import _heredoc_terminator_hint
        trace = self._trace("PY", 2) + ". Did you mean: 'P'?"
        h = _heredoc_terminator_hint(trace, "print('work')\nPY\n")
        assert h and "heredoc" in h.lower()

    def test_a_real_python_bug_is_not_called_a_heredoc(self):
        """The failing line is not the bare token -> an ordinary undefined name."""
        assert self._hint("NEW_ISS", 1, "f.write(NEW_ISS)\n") is None

    def test_a_missing_constant_is_not_called_a_heredoc(self):
        """`PISTIS_PUBLIC_PREFIXES` is a genuine underscore-prefix bug."""
        assert self._hint("PISTIS_PUBLIC_PREFIXES", 2, "import x\n*PISTIS_PUBLIC_PREFIXES,\n") is None

    def test_without_the_source_it_stays_silent_rather_than_guessing(self):
        """No source -> no verdict. A name's spelling alone is not evidence."""
        from tools.code_kernel import _heredoc_terminator_hint
        assert _heredoc_terminator_hint(self._trace("PY", 2), "") is None

    def test_a_failure_outside_the_cell_is_not_a_heredoc(self):
        """Raised in an imported module, not in the cell's own source."""
        from tools.code_kernel import _heredoc_terminator_hint
        trace = ('  File "<cell>", line 5, in <module>\n'
                 '    from app.mod import thing\n'
                 '  File "C:\\\\app\\\\mod.py", line 180, in <module>\n'
                 "NameError: name 'PISTIS_PUBLIC_PREFIXES' is not defined")
        assert _heredoc_terminator_hint(trace, "from app.mod import thing\n") is None

    def test_the_narrow_helper_rule_still_wins(self):
        """The narrow helper rule keeps priority over the heredoc class."""
        err = "NameError: name 'shell_quote' is not defined"
        h = _sandbox_failure_hint(err)
        assert h and "from hermes_tools import shell_quote" in h


class TestCellResultAssembly:
    """The kernel's own assembly of a cell result: the hint and the preserved stdout.

    These call ``_cell_result`` directly with the payload the runner produces,
    so they exercise the real code path without spawning a kernel process.
    """

    @staticmethod
    def _result(stdout="", traceback_text="", status="error", cell_source=""):
        from tools.code_kernel import SessionKernel, _cell_result
        kernel = SessionKernel(("owner", "local", "python", "cwd", ()))
        payload = {"status": status, "stdout": stdout, "stderr": "",
                   "traceback": traceback_text, "execution_count": 1}
        return _cell_result(kernel, ("owner", "local", "python", "cwd", ()), "ok", payload,
                            timeout=30, sandbox_tools=frozenset(), reused=False,
                            state_reset=False, exec_start=0.0, cell_source=cell_source)

    @staticmethod
    def _trace(name, line):
        return ('Traceback (most recent call last):\n'
                '  File "C:\\\\hermes_kernel_abc\\\\hermes_kernel_runner.py", line 124, in run_cell\n'
                '    exec(compile(request["code"], "<cell>", "exec"), GLOBALS)\n'
                f'  File "<cell>", line {line}, in <module>\n'
                f"NameError: name '{name}' is not defined")

    def test_a_bare_terminator_gets_the_heredoc_hint(self):
        r = self._result(traceback_text=self._trace("EOF", 2),
                          cell_source="print('work')\nEOF\n")
        assert "hint" in r
        assert "EOF" in r["hint"]
        assert "heredoc" in r["hint"].lower()

    def test_a_real_python_bug_gets_no_heredoc_hint(self):
        r = self._result(traceback_text=self._trace("NEW_ISS", 1),
                          cell_source="f.write(NEW_ISS)\n")
        assert "hint" not in r

    def test_a_failed_cell_preserves_its_stdout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = self._result(stdout="analysis done: 42 findings\n",
                          traceback_text=self._trace("EOF", 2),
                          cell_source="print('analysis done: 42 findings')\nEOF\n")
        assert "analysis done: 42 findings" in r["output"]
        assert r.get("cell_stdout_preserved") is True
        spill = r.get("stdout_spill_path")
        assert spill, "the failed cell's stdout must be spilled to a durable path"
        from pathlib import Path
        assert "analysis done: 42 findings" in Path(spill).read_text(encoding="utf-8")

    def test_a_successful_cell_preserves_nothing_extra(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = self._result(stdout="all good\n", status="ok")
        assert "cell_stdout_preserved" not in r
        assert "hint" not in r


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
