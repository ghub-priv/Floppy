import subprocess

from django.test import SimpleTestCase


class PostWatchRuffProbe(SimpleTestCase):
    def test_report_ci_ruff_fixes(self):
        paths = [
            "src/app/admin.py",
            "src/app/models/__init__.py",
            "src/app/models/post_watch.py",
            "src/app/post_watch.py",
            "src/app/post_watch_urls.py",
            "src/app/tests/test_post_watch_queue_semantics.py",
            "src/app/tests/test_post_watch_workflow.py",
        ]
        lint = subprocess.run(
            ["uvx", "ruff@0.15.8", "check", "--fix", *paths],
            capture_output=True,
            text=True,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "--", *paths],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.fail(
            f"ruff return={lint.returncode}\nSTDOUT:\n{lint.stdout}\n"
            f"STDERR:\n{lint.stderr}\nDIFF:\n{diff}"
        )
