#!/usr/bin/env python3
"""
Red-first behavioural tests for the SCOPED receipt profile in tools/verify_receipt.py.

A scoped receipt (emitted by tools/emit_receipt.py when only some test shards are affected)
is accepted ONLY if the verifier can independently recompute, from the PR diff against
origin/main, which shards the change requires. The receipt's own changed-file list is never
trusted: it is compared to the recomputed list and any mismatch is INVALID. Any change outside
the test-shard mapping requires profile=full. Each test drives the real verifier over a real
temp git repo and asserts the exit code (0 valid, 1 invalid).

Fixture shard map (round-robin over sorted test stems, as ci_shard_runner runs locally):
  test_alpha -> py-shard-0, test_bravo -> py-shard-1.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name + "_scoped_under_test_v", TOOLS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load("receipt_common")
emit = _load("emit_receipt")
vr = _load("verify_receipt")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd, *args):
    env = dict(os.environ)
    env.update(_GIT_ENV)
    env["HOME"] = str(cwd)
    return subprocess.run(["git", "-C", str(cwd)] + list(args), cwd=str(cwd), env=env,
                          capture_output=True, encoding="utf-8", errors="replace", check=True).stdout.strip()


def _write_commit(repo, relpath, text):
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-q", "-m", "edit " + relpath)
    return _git(repo, "rev-parse", "HEAD")


def _secret():
    return "verify" + "-" + "scoped" + "-" + "dummy" + "-" + "1" * 20


ALL_SHARDS = ["py-shard-0", "py-shard-1", "py-shard-2", "py-shard-3"]


class ScopedBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _write_commit(self.repo, "README.md", "m1\n")
        _write_commit(self.repo, "tests/test_alpha.py", "alpha = 1\n")
        _write_commit(self.repo, "tests/test_bravo.py", "bravo = 1\n")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(self.repo, "checkout", "-q", "-b", "feat")
        self.head = self.base

    def tearDown(self):
        self.tmp.cleanup()

    def change(self, relpath, text="changed\n"):
        self.head = _write_commit(self.repo, relpath, text)
        return self.head

    def envelope(self, parts, profile="scoped", shards=None, changed_files=None, drop=(), **overrides):
        receipt = emit.build_receipt(self.repo, parts, skipped=[], slug="o/r", profile=profile)
        if profile == "scoped":
            receipt["shards"] = shards
            receipt["changed_files"] = changed_files
        for key in drop:
            receipt.pop(key, None)
        receipt.update(overrides)
        sig = rc.sign(rc.canonical_json(receipt), scheme="hmac-sha256", hmac_secret=_secret())
        return {"receipt": receipt, "sig": sig}

    def part(self, name, exit_code=0):
        return {"name": name, "exit_code": exit_code, "test_count": 3, "duration_s": 1.0}

    def run_verify(self, env, required=None):
        code, reasons = vr.verify(
            env, repo=self.repo, head=self.head, main_ref="origin/main", max_behind=50,
            required=list(rc.DEFAULT_REQUIRED_PARTS) if required is None else required,
            pubkey_path=None, hmac_secret=_secret(),
        )
        return code, reasons


class TestScopedAcceptance(ScopedBase):
    def test_one_file_test_change_with_owning_shard_is_valid(self):
        self.change("tests/test_alpha.py")
        env = self.envelope([self.part("py-shard-0")], shards=["py-shard-0"],
                            changed_files=["tests/test_alpha.py"])
        code, reasons = self.run_verify(env)
        self.assertEqual((code, reasons), (0, []))

    def test_wrong_shard_for_changed_test_is_invalid(self):
        self.change("tests/test_alpha.py")
        env = self.envelope([self.part("py-shard-1")], shards=["py-shard-1"],
                            changed_files=["tests/test_alpha.py"])
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)
        self.assertTrue(any("py-shard-0" in r for r in reasons), reasons)

    def test_change_to_shared_tools_module_is_invalid_for_scoped(self):
        self.change("tools/common.py", "shared = 1\n")
        env = self.envelope([self.part("py-shard-0")], shards=["py-shard-0"],
                            changed_files=["tools/common.py"])
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)
        self.assertTrue(any("profile=full" in r for r in reasons), reasons)

    def test_receipt_hiding_a_changed_file_is_invalid(self):
        self.change("tests/test_alpha.py")
        env = self.envelope([self.part("py-shard-1")], shards=["py-shard-1"],
                            changed_files=["tests/test_bravo.py"])
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)
        self.assertTrue(any("changed_files" in r for r in reasons), reasons)

    def test_missing_profile_field_is_invalid(self):
        self.change("tests/test_alpha.py")
        env = self.envelope([self.part("py-shard-0")], profile="scoped", shards=["py-shard-0"],
                            changed_files=["tests/test_alpha.py"], drop=("profile",))
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)
        self.assertTrue(any("profile" in r for r in reasons), reasons)

    def test_scoped_owning_shard_with_nonzero_exit_is_invalid(self):
        self.change("tests/test_alpha.py")
        env = self.envelope([self.part("py-shard-0", exit_code=1)], shards=["py-shard-0"],
                            changed_files=["tests/test_alpha.py"])
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)

    def test_missing_shard_declaration_is_invalid(self):
        self.change("tests/test_alpha.py")
        env = self.envelope([self.part("py-shard-0")], shards=None, changed_files=["tests/test_alpha.py"])
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)


class TestFullProfileUnchanged(ScopedBase):
    def test_full_receipt_for_shared_change_is_valid_with_all_required_shards(self):
        self.change("tools/common.py", "shared = 1\n")
        parts = [self.part(n) for n in ALL_SHARDS]
        env = self.envelope(parts, profile="full")
        code, reasons = self.run_verify(env)
        self.assertEqual((code, reasons), (0, []))

    def test_full_receipt_missing_a_shard_is_still_invalid(self):
        self.change("tests/test_alpha.py")
        parts = [self.part(n) for n in ALL_SHARDS[:3]]
        env = self.envelope(parts, profile="full")
        code, reasons = self.run_verify(env)
        self.assertEqual(code, 1, reasons)
        self.assertTrue(any("py-shard-3" in r for r in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
