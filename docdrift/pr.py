#!/usr/bin/env python3
"""PR-time doc drift check: did this diff make a documented claim false?

Runs inside a git checkout that has both the base and head commits. For every doc claim whose
backticked identifiers appear in the diff, the claim is verified against the changed files as
they were BEFORE the diff and as they are AFTER it. A claim is reported only when it was not in
conflict before and is in conflict after — so pre-existing drift stays quiet and the report is
about what this change broke.

  python3 check_pr.py --base <sha> --head <sha> [--docs README.md docs/*.md] [--repo .]

Writes a Markdown report to --report (default: drift-report.md) and exits 1 when it has findings.
"""
import argparse, json, os, re, subprocess, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docdrift.core import (USAGE, bm25, classify, classify_doc, doc_units, is_code_path,  # noqa: E402
                           is_symbol_path, text_windows, verify)

def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="ignore")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def blob(repo, rev, path):
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True, text=True, errors="ignore")
    return r.stdout if r.returncode == 0 else None


def is_code(f):
    return is_code_path(f) and not re.search(r"(^|/)(tests?|__tests__|spec|node_modules|dist|build|vendor)(/|$)", f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--docs", nargs="*", default=[])
    ap.add_argument("--report", default="drift-report.md")
    ap.add_argument("--max-claims", type=int, default=60, help="cap claims verified per run")
    ap.add_argument("--fail-on-finding", action="store_true")
    a = ap.parse_args()
    repo = a.repo

    changed = [f for f in git(repo, "diff", "--name-only", f"{a.base}..{a.head}").split("\n") if f.strip()]
    code = [f for f in changed if is_code(f)]
    docs = a.docs or [f for f in git(repo, "ls-files").split("\n")
                      if f.endswith(".md") and (f.count("/") == 0 or f.startswith("docs/"))]
    docs = [d for d in docs if d not in changed]  # a doc edited in this PR is the author's own call
    stats = Counter(changed_files=len(changed), code_files=len(code), docs_considered=len(docs))

    if not code or not docs:
        summary = "No code changes to check against documentation." if not code else "No unedited docs to check."
        Path(a.report).write_text(f"### Doc drift check\n\n{summary}\n")
        print(summary)
        return 0

    diff = git(repo, "diff", "--unified=0", f"{a.base}..{a.head}", "--", *code)
    touched = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}",
                             "\n".join(l[1:] for l in diff.split("\n")
                                       if l[:1] in "+-" and l[:3] not in ("+++", "---"))))

    # Text first: find the lines this diff could possibly affect, and only ask the model about
    # documents that have any. On a typical pull request that is one or two of them.
    per_doc = {}
    for d in docs:
        text = blob(repo, a.head, d)
        if not text:
            continue
        hits = [u for u in doc_units(None, text=text)
                if set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]{2,})`", u["text"])) & touched]
        if hits:
            per_doc[d] = (text, hits)

    candidates, skipped = [], []
    for d, (text, hits) in per_doc.items():
        genre, probs = classify_doc(d, text)
        if genre == "third_party" and probs["third_party"] >= 0.5:
            # A write-up of someone else's protocol names their functions, not ours. Checking it
            # against this repository's code only produces "that symbol does not exist" noise.
            skipped.append(d)
            continue
        for u in hits:
            u["doc"], u["genre"] = d, genre
            candidates.append(u)
    stats["claims_touched"] = len(candidates)
    stats["docs_skipped_third_party"] = len(skipped)
    if not candidates:
        note = ("Only third-party documentation mentions what this diff touched: "
                + ", ".join(f"`{d}`" for d in skipped)) if skipped else \
               "No documented claim mentions anything this diff touched."
        Path(a.report).write_text(f"### Doc drift check\n\n{note}\n")
        print(note)
        return 0

    classify(candidates)
    claims = [u for u in candidates if u["kind"] == "code_fact" and u["p_code_fact"] >= 0.5][:a.max_claims]
    stats["claims_verified"] = len(claims)
    if not claims:
        Path(a.report).write_text("### Doc drift check\n\nNothing verifiable: the matching doc lines are prose or setup instructions.\n")
        print("No verifiable claims among the matching doc lines.")
        return 0

    after = {f: s for f in code if (s := blob(repo, a.head, f))}
    before = {f: s for f in code if (s := blob(repo, a.base, f))}
    if not after:
        Path(a.report).write_text("### Doc drift check\n\nAll changed code files were deleted; nothing to verify against.\n")
        return 0

    post = verify([dict(u) for u in claims], bm25(text_windows(after)))
    pre = {(u["doc"], u["line"]): u for u in verify([dict(u) for u in claims], bm25(text_windows(before)))} if before else {}

    # Only an incremental run can do this: a symbol the docs name that the code had before and
    # does not have now was renamed or removed. Pure text, no model, and no false alarms from
    # symbols that belong to a dependency — those were never in the tree to begin with.
    seen = {}

    def in_tree(rev, sym):
        if (rev, sym) not in seen:
            hits = git(repo, "grep", "-l", "-F", "--", sym, rev, check=False).split("\n")
            seen[(rev, sym)] = any(is_symbol_path(h.split(":", 1)[-1]) for h in hits if h.strip())
        return seen[(rev, sym)]

    findings = []
    for u in post:
        was = pre.get((u["doc"], u["line"]), {}).get("status", "absent")
        u["vanished"] = [] if u["genre"] == "specification" else [
            s for s in re.findall(r"`([A-Za-z_][A-Za-z0-9_]{2,})`", u["text"])
            if s in touched and in_tree(a.base, s) and not in_tree(a.head, s)]
        if u["vanished"] or (u["status"] == "DRIFT" and was != "DRIFT"):
            u["before"] = was
            findings.append(u)
    stats["findings"] = len(findings)

    lines = ["### Doc drift check", ""]
    if findings:
        lines.append(f"This change appears to make **{len(findings)}** documented "
                     f"{'claim' if len(findings) == 1 else 'claims'} out of date:")
        lines.append("")
        for u in sorted(findings, key=lambda u: (not u["vanished"], -u["p_contradicted"])):
            ev = max(u["evidence"], key=lambda e: min(e["p"]["contradicted"], e["p"]["same"]), default=None)
            where = f"`{ev['file']}` (around line {ev['start']})" if ev else "the changed code"
            if u["vanished"]:
                why = ("This change removed or renamed "
                       + ", ".join(f"`{s}`" for s in u["vanished"])
                       + ", which this line documents."
                       + (f" It also now reads as contradicted by {where} (conflict {u['p_contradicted']:.2f})."
                          if u["status"] == "DRIFT" else ""))
            else:
                why = (f"Now contradicted by {where}. Conflict {u['p_contradicted']:.2f}, "
                       f"support {u['p_supported']:.2f} (before this change: {u['before']}).")
            lines += [f"**[{u['doc']}:{u['line']}]({u['doc']}#L{u['line']})** — under _{' > '.join(u['context'][-2:]) or 'top level'}_",
                      "", f"> {u['text'][:400]}", "", why, ""]
        lines.append("<sub>Each finding is a model judgment with its probability, not a verdict — "
                     "check the cited code before acting. Claims already stale before this change are not reported.</sub>")
    else:
        lines.append(f"No documentation went stale. Checked **{stats['claims_verified']}** "
                     f"{'claim' if stats['claims_verified'] == 1 else 'claims'} that mention something this diff touched.")
    specs = sorted({u["doc"] for u in claims if u["genre"] == "specification"})
    if specs:
        lines += ["", "<sub>" + ", ".join(f"`{d}`" for d in specs) + " read as specifications rather than "
                  "reference docs. They may describe behavior on purpose ahead of the code, so a missing "
                  "symbol there is not reported and prose claims are often left unverified.</sub>"]
    if skipped:
        lines += ["", "<sub>Skipped as third-party documentation: " + ", ".join(f"`{d}`" for d in skipped) + ".</sub>"]
    lines += ["", f"<sub>{dict(stats)} · {USAGE['requests']} model requests</sub>"]
    Path(a.report).write_text("\n".join(lines) + "\n")

    print(json.dumps(dict(stats)))
    for u in findings:
        print(f"::warning file={u['doc']},line={u['line']}::Possibly stale after this change: {u['text'][:180]}")
    if gh := os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(gh).write_text(Path(a.report).read_text())
    if gh := os.environ.get("GITHUB_OUTPUT"):
        Path(gh).write_text(f"findings={len(findings)}\n")
    return 1 if (findings and a.fail_on_finding) else 0


if __name__ == "__main__":
    sys.exit(main())
