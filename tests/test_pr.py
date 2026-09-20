"""Offline tests for the parts that are ordinary code.

The model is stubbed out: these cover document parsing, which lines a diff puts in scope,
the deterministic symbol rules, the before/after policy and the rendered report — everything
except the judgment itself. Run with: python3 -m unittest discover -s tests
"""
import subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docdrift import core, pr  # noqa: E402

# (doc fragment, code fragment) pairs a test declares in conflict. The verdict has to depend on
# the code excerpt as well as the doc line, because the checker asks the same question about the
# code before the change and after it, and only reports a claim whose answer flipped.
CONFLICTING = []


def fake_ask(state, questions):
    """Stand in for the model: classify everything as a code fact, and answer the verify
    questions from CONFLICTING so a test controls the verdict without a network call."""
    out = {}
    for qid, q in questions.items():
        if q["type"] == "choice":
            out[qid] = {"type": "choice", "choice": "code_fact", "confidence": 0.9,
                        "probabilities": {"code_fact": 0.9, "external_service": 0.03,
                                          "dependency_setting": 0.03, "usage_setup": 0.02, "narrative": 0.02}}
            continue
        kind, idx = qid[:-1], qid[-1]
        source = state["code"][f"c{idx}"]["source"]
        bad = any(d in state["doc"]["item"] and c in source for d, c in CONFLICTING)
        out[qid] = {"type": "noul",
                    "noul": {"ok": 0.05 if bad else 0.9, "bad": 0.95 if bad else 0.05, "same": 0.9}[kind]}
    return out


def run(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


class Parsing(unittest.TestCase):
    def test_is_code_excludes_tests_lockfiles_and_build_output(self):
        for f in ("src/server.py", "lib/index.ts", "cmd/main.go", "config.toml"):
            self.assertTrue(pr.is_code(f), f)
        for f in ("tests/test_server.py", "src/server.test.ts", "package-lock.json", "uv.lock",
                  "node_modules/x/index.js", "dist/bundle.js", "README.md", "docs/guide.md"):
            self.assertFalse(pr.is_code(f), f)

    def test_doc_units_keep_heading_and_bullet_ancestry(self):
        u = {x["text"]: x for x in core.doc_units(None, text=(
            "# Title\n\n## Tools\n\n- `search` — find things\n  - `limit` (number): how many\n"))}
        self.assertEqual(u["`limit` (number): how many"]["heads"], ["Title", "Tools"])
        self.assertEqual(u["`limit` (number): how many"]["parents"], ["`search` — find things"])
        self.assertEqual(u["`search` — find things"]["parents"], [])

    def test_doc_units_skip_fenced_code_and_join_hard_wrapped_prose(self):
        units = core.doc_units(None, text=(
            "# T\n\nThe server reads the port\nfrom the environment.\n\n```\nnot-a-claim --flag\n```\n"))
        self.assertEqual([x["text"] for x in units],
                         ["The server reads the port from the environment."])


class Pipeline(unittest.TestCase):
    """Build a throwaway repository, make a change, and run the check over it."""

    def setUp(self):
        CONFLICTING.clear()  # each test declares its own conflicts
        self._real_ask, core.ask = core.ask, fake_ask
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        run(self.repo, "init", "-q", "-b", "main")
        run(self.repo, "config", "user.email", "t@example.com")
        run(self.repo, "config", "user.name", "t")
        self.write("server.py", "def search(query, limit=10, include_archived=False):\n    return []\n")
        self.write("README.md", "# App\n\n## Tools\n\n- `search` — find things\n"
                                "  - `limit` (number, optional): how many to return (default: 10).\n"
                                "  - `include_archived` (boolean, optional): also search archived.\n")
        self.commit("base")

    def tearDown(self):
        core.ask = self._real_ask
        self.tmp.cleanup()

    def write(self, name, body):
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / name).write_text(body)

    def commit(self, msg):
        run(self.repo, "add", "-A")
        run(self.repo, "commit", "-q", "-m", msg)
        return run(self.repo, "rev-parse", "HEAD").strip()

    def check(self, *extra):
        report = self.repo / "report.md"
        sys.argv = ["pr.py", "--repo", str(self.repo), "--base", "main~1", "--head", "HEAD",
                    "--report", str(report), *extra]
        code = pr.main()
        return code, report.read_text()

    def test_reports_a_claim_the_change_contradicts(self):
        CONFLICTING.append(("default: 10", "limit=25"))
        self.write("server.py", "def search(query, limit=25, include_archived=False):\n    return []\n")
        self.commit("raise the default")
        _, report = self.check()
        self.assertIn("**1** documented claim out of date", report)
        self.assertIn("default: 10", report)
        self.assertIn("README.md:6", report)

    def test_reports_a_documented_symbol_the_change_removed(self):
        self.write("server.py", "def search(query, limit=10, include_trashed=False):\n    return []\n")
        self.commit("rename the flag")
        _, report = self.check()
        # No model verdict needed: the symbol was in the tree before and is gone now.
        self.assertIn("removed or renamed `include_archived`", report)

    def test_a_symbol_still_present_elsewhere_is_not_reported_as_removed(self):
        self.write("other.py", "include_archived = True\n")
        self.commit("add another user of the flag")
        self.write("server.py", "def search(query, limit=10):\n    return []\n")
        self.commit("drop the parameter here")
        _, report = self.check()
        self.assertNotIn("removed or renamed", report)

    def test_drift_that_predates_the_change_stays_quiet(self):
        CONFLICTING.append(("default: 10", "limit"))  # wrong before this change and still wrong after
        self.write("server.py", "def search(query, limit=25, include_archived=False):\n    return []\n")
        self.commit("first change, introduces the drift")
        # Touches `limit` again so the claim is back in scope, but the verdict does not flip.
        self.write("server.py", "def search(query, limit = 25, include_archived=False):\n    return []\n")
        self.commit("second change, reformat only")
        _, report = self.check()
        self.assertIn("No documentation went stale", report)

    def test_a_doc_edited_in_the_same_change_is_left_alone(self):
        CONFLICTING.append(("default: 10", "limit=25"))
        self.write("server.py", "def search(query, limit=25, include_archived=False):\n    return []\n")
        self.write("README.md", (self.repo / "README.md").read_text() + "\nUpdated by the author.\n")
        self.commit("change code and docs together")
        _, report = self.check()
        self.assertIn("No unedited docs to check", report)

    def test_a_test_only_change_is_skipped(self):
        self.write("tests/test_search.py", "def test_x():\n    assert True\n")
        self.commit("add a test")
        _, report = self.check()
        self.assertIn("No code changes to check", report)

    def test_a_change_no_documented_claim_mentions_is_skipped(self):
        self.write("server.py", (self.repo / "server.py").read_text().replace("return []", "return list()"))
        self.commit("unrelated internal tweak")
        _, report = self.check()
        self.assertIn("No documented claim mentions", report)

    def test_fail_on_finding_controls_the_exit_code(self):
        CONFLICTING.append(("default: 10", "limit=25"))
        self.write("server.py", "def search(query, limit=25, include_archived=False):\n    return []\n")
        self.commit("raise the default")
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.check("--fail-on-finding")[0], 1)


if __name__ == "__main__":
    unittest.main()
