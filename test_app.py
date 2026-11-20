import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import app


class AllowedOriginsTests(unittest.TestCase):
    def test_normalizes_configured_origins_and_removes_duplicates(self):
        with patch.dict(
            os.environ,
            {
                "ALLOWED_ORIGINS": (
                    " https://example.com/ ,https://second.example.com, "
                    "https://example.com "
                )
            },
            clear=False,
        ):
            origins = app.allowed_origins()

        self.assertIn("https://example.com", origins)
        self.assertIn("https://second.example.com", origins)
        self.assertNotIn("https://example.com/", origins)
        self.assertEqual(len(origins), len(set(origins)))


class RunCodeTests(unittest.TestCase):
    def test_rejects_code_over_50_kb(self):
        request = app.RunRequest(code="x" * 50_001)

        with self.assertRaises(HTTPException) as raised:
            app.run_code(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "Code exceeds the 50 KB limit")

    @patch("app.subprocess.run")
    def test_returns_process_output_and_exit_code(self, run):
        run.return_value = SimpleNamespace(
            stdout="hello\n",
            stderr="warning\n",
            returncode=3,
        )

        result = app.run_code(app.RunRequest(code="print('hello')"))

        self.assertEqual(
            result,
            {"stdout": "hello\n", "stderr": "warning\n", "exit_code": 3},
        )
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], [app.sys.executable, "-I"])
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["input"], "")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    @patch("app.subprocess.run")
    def test_custom_stdin_timeout_and_memory_are_passed(self, run):
        run.return_value = SimpleNamespace(stdout="Ada\n", stderr="", returncode=0)

        result = app.run_code(app.RunRequest(
            code="name=input(); print(name)",
            stdin="Ada\n",
            timeout=12,
            memory_mb=256,
        ))

        self.assertEqual(result["stdout"], "Ada\n")
        _, kwargs = run.call_args
        self.assertEqual(kwargs["input"], "Ada\n")
        self.assertEqual(kwargs["timeout"], 12)

    def test_rejects_unallowlisted_dependency(self):
        with self.assertRaises(HTTPException) as raised:
            app.run_code(app.RunRequest(code="import requests", dependencies=["requests"]))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("requests", raised.exception.detail)

    @patch("app.subprocess.run")
    def test_unittest_framework_appends_test_runner(self, run):
        captured = {}

        def successful_run(argv, **kwargs):
            captured["source"] = open(argv[3], "r", encoding="utf-8").read()
            return SimpleNamespace(stdout=".", stderr="", returncode=0)

        run.side_effect = successful_run

        app.run_code(app.RunRequest(
            code="import unittest\nclass SampleTest(unittest.TestCase):\n"
                 "    def test_true(self):\n        self.assertTrue(True)",
            test_framework="unittest",
        ))

        self.assertIn("loadTestsFromModule", captured["source"])


class SourceToolTests(unittest.TestCase):
    def test_lint_reports_syntax_error_location(self):
        result = app.lint_code(app.RunRequest(code="def broken(\n"))

        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["line"], 1)
        self.assertTrue(result["errors"][0]["message"])

    def test_format_returns_valid_ast_unparsed_code(self):
        result = app.format_code(app.RunRequest(code="x={'a':1}\n"))

        self.assertIn("x =", result["code"])

    @patch("app.subprocess.run")
    def test_timeout_is_reported_and_temporary_file_is_removed(self, run):
        source_path = None

        def expire(*args, **kwargs):
            nonlocal source_path
            source_path = args[0][2]
            raise subprocess.TimeoutExpired(args[0], 5)

        run.side_effect = expire

        result = app.run_code(app.RunRequest(code="while True:\n    pass"))

        self.assertEqual(
            result,
            {
                "stdout": "",
                "stderr": "Execution timed out after 5 seconds.",
                "exit_code": 124,
            },
        )
        self.assertIsNotNone(source_path)
        self.assertFalse(os.path.exists(source_path))


if __name__ == "__main__":
    unittest.main()
