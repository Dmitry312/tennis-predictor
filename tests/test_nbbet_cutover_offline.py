"""Offline smoke test for the deployment script's guarded git cutover and rollback.

The production path is replaced only in a temporary script copy. No server access.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "infra/deploy/nbbet_python_cutover.sh"


def git(directory, *args):
    return subprocess.run(["git", *args], cwd=directory, check=True,
                          text=True, capture_output=True).stdout.strip()


class CutoverTest(unittest.TestCase):
    def test_failed_import_rolls_back_and_valid_import_switches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            git(upstream, "init", "-b", "main")
            git(upstream, "config", "user.email", "ci@example.invalid")
            git(upstream, "config", "user.name", "CI")
            script_path = upstream / "src/tennis_predictor/ingestion/nbbet/detail_job.py"
            script_path.parent.mkdir(parents=True)
            script_path.write_text("VALUE = 1\n")
            git(upstream, "add", ".")
            git(upstream, "commit", "-m", "old")
            old = git(upstream, "rev-parse", "HEAD")
            script_path.write_text("raise RuntimeError('bad import')\n")
            git(upstream, "commit", "-am", "bad")
            bad = git(upstream, "rev-parse", "HEAD")
            work = root / "work"
            git(root, "clone", str(upstream), str(work))
            git(work, "checkout", "--detach", old)
            (work / ".venv/bin").mkdir(parents=True)
            # Test shim: mimic production interpreter, execute module by path.
            interpreter = work / ".venv/bin/python"
            interpreter.write_text("#!/bin/sh\nexec python3 \"$@\"\n")
            interpreter.chmod(0o755)
            # venv is untracked; exclude it as on production.
            (work / ".git/info/exclude").write_text(".venv/\n")
            source = SCRIPT.read_text().replace(
                "ROOT=/root/Tennis_pelmen_model", f"ROOT={work}"
            ).replace(
                "root = pathlib.Path('/root/Tennis_pelmen_model/src').resolve()",
                f"root = pathlib.Path({str(work / 'src')!r}).resolve()"
            )
            # Provide import path to the isolated test interpreter.
            source = source.replace(
                '.venv/bin/python -B -', 'PYTHONPATH=src .venv/bin/python -B -'
            )
            staged = root / "cutover.sh"
            staged.write_text(source)
            env = os.environ.copy()
            env.update(EXPECTED_OLD_SHA=old, TARGET_SHA=bad)
            failed = subprocess.run(["bash", str(staged)], env=env,
                                    text=True, capture_output=True, timeout=25)
            self.assertNotEqual(failed.returncode, 0, failed.stdout)
            self.assertEqual(git(work, "rev-parse", "HEAD"), old)

            script_path.write_text("VALUE = 2\n")
            git(upstream, "commit", "-am", "good")
            good = git(upstream, "rev-parse", "HEAD")
            env["TARGET_SHA"] = good
            success = subprocess.run(["bash", str(staged)], env=env,
                                     text=True, capture_output=True, timeout=25)
            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(git(work, "rev-parse", "HEAD"), good)


if __name__ == "__main__":
    unittest.main()
