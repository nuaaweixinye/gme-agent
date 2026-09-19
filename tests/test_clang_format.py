from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from gme_agent.execution.clang_format import (  # noqa: E402
    INSTALL_HINT,
    describe_version_mismatch,
    parse_clang_format_version,
    resolve_clang_format,
)
from gme_agent.settings.config import GME_CLANG_FORMAT_VERSION, AgentConfig  # noqa: E402


VERSION_LINE = "clang-format version 17.0.2 (https://github.com/llvm/llvm-project abc123)"


def _probe_from(mapping: dict[str, str]):
    """A version probe keyed by path, so no process is ever started."""

    def probe(path: str) -> str:
        return mapping.get(path, "")

    return probe


class ClangFormatCase(unittest.TestCase):
    """Isolates the resolver from the machine it runs on.

    `candidate_paths` looks at the interpreter's own environment and at PATH, so a
    test that did not control both would depend on whatever clang-format happens to
    be installed here.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.interpreter = self.root / "venv" / "python.exe"
        self.interpreter.parent.mkdir(parents=True, exist_ok=True)
        self.interpreter.write_text("stub", encoding="utf-8")
        self.patchers = [
            mock.patch("gme_agent.execution.clang_format.sys.executable", str(self.interpreter)),
            mock.patch("gme_agent.execution.clang_format.shutil.which", return_value=None),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def binary_in(self, directory: Path, name: str = "clang-format.exe") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("stub", encoding="utf-8")
        return path

    def venv_binary(self) -> Path:
        # The resolver resolves sys.executable before looking beside it, so build
        # the fixture under the resolved parent: a temp directory can carry a short
        # name that resolve() expands, and the two would then differ.
        scripts = Path(str(self.interpreter)).resolve().parent / "Scripts"
        return self.binary_in(scripts)

    def configured_binary(self) -> Path:
        return self.binary_in(self.root / "tools")


class ParseVersionTests(unittest.TestCase):
    def test_the_version_line_is_parsed(self) -> None:
        self.assertEqual(parse_clang_format_version(VERSION_LINE), "17.0.2")
        self.assertEqual(parse_clang_format_version("clang-format version 22.1.8\n"), "22.1.8")

    def test_an_unrecognised_line_yields_no_version(self) -> None:
        self.assertEqual(parse_clang_format_version(""), "")
        self.assertEqual(parse_clang_format_version("clang-format 17\n"), "")


class ResolutionOrderTests(ClangFormatCase):
    def test_the_interpreter_environment_wins_over_path(self) -> None:
        venv_binary = self.venv_binary()
        probe = _probe_from({str(venv_binary): "17.0.2", "C:\\path\\clang-format.exe": "17.0.2"})
        with mock.patch("gme_agent.execution.clang_format.shutil.which", return_value="C:\\path\\clang-format.exe"):
            resolved = resolve_clang_format(AgentConfig(), probe=probe)

        self.assertEqual(resolved.path, str(venv_binary))
        self.assertTrue(resolved.matches_gme)

    def test_a_matching_install_beats_a_mismatched_one_earlier_in_the_order(self) -> None:
        venv_binary = self.venv_binary()
        configured = self.configured_binary()
        probe = _probe_from({str(venv_binary): "22.1.8", str(configured): "17.0.2"})
        config = AgentConfig(clang_format_path=str(configured))

        resolved = resolve_clang_format(config, probe=probe)

        self.assertEqual(resolved.path, str(configured))
        self.assertTrue(resolved.matches_gme)

    def test_path_is_the_last_resort(self) -> None:
        probe = _probe_from({"C:\\path\\clang-format.exe": "17.0.2"})
        with mock.patch("gme_agent.execution.clang_format.shutil.which", return_value="C:\\path\\clang-format.exe"):
            resolved = resolve_clang_format(AgentConfig(), probe=probe)

        self.assertEqual(resolved.path, "C:\\path\\clang-format.exe")

    def test_a_configured_path_that_is_not_a_file_is_refused(self) -> None:
        config = AgentConfig(clang_format_path=str(self.root / "tools" / "missing.exe"))

        with self.assertRaises(RuntimeError) as caught:
            resolve_clang_format(config, probe=_probe_from({}))

        self.assertIn("which is not a file", str(caught.exception))


class MismatchTests(ClangFormatCase):
    def test_a_mismatch_is_refused_with_both_versions_and_the_fix(self) -> None:
        configured = self.configured_binary()
        config = AgentConfig(clang_format_path=str(configured))

        with self.assertRaises(RuntimeError) as caught:
            resolve_clang_format(config, probe=_probe_from({str(configured): "22.1.8"}))

        message = str(caught.exception)
        self.assertIn("22.1.8", message)
        self.assertIn(GME_CLANG_FORMAT_VERSION, message)
        self.assertIn(INSTALL_HINT, message)

    def test_no_candidate_at_all_is_refused_with_the_install_hint(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            resolve_clang_format(AgentConfig(), probe=_probe_from({}))

        self.assertIn("was not found", str(caught.exception))
        self.assertIn(INSTALL_HINT, str(caught.exception))

    def test_an_unreadable_version_is_refused_rather_than_assumed(self) -> None:
        configured = self.configured_binary()
        config = AgentConfig(clang_format_path=str(configured))

        with self.assertRaises(RuntimeError) as caught:
            resolve_clang_format(config, probe=_probe_from({str(configured): ""}))

        self.assertIn("unknown version", str(caught.exception))

    def test_the_escape_hatch_uses_the_mismatched_binary_and_says_so(self) -> None:
        configured = self.configured_binary()
        config = AgentConfig(clang_format_path=str(configured), allow_clang_format_version_mismatch=True)

        resolved = resolve_clang_format(config, probe=_probe_from({str(configured): "22.1.8"}))

        self.assertFalse(resolved.matches_gme)
        self.assertEqual(resolved.version, "22.1.8")
        self.assertIn("22.1.8", describe_version_mismatch(resolved))

    def test_the_escape_hatch_is_off_by_default(self) -> None:
        self.assertFalse(AgentConfig().allow_clang_format_version_mismatch)
        self.assertEqual(AgentConfig().clang_format_path, "")
        self.assertEqual(GME_CLANG_FORMAT_VERSION, "17.0.2")


if __name__ == "__main__":
    unittest.main()
