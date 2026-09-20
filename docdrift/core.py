#!/usr/bin/env python3
"""Core of the doc-drift checker.

Code owns the workflow (parsing documents, retrieving code, aggregating); the model answers only
narrow typed questions about one doc line versus one code excerpt. Standard library only.
"""
import hashlib, json, math, os, re, time, urllib.request, urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.typesafe.ai/v1/systemone"
CACHE = Path(os.environ.get("DOCDRIFT_CACHE", Path(__file__).parent / ".cache"))
CODE_EXT = {".py", ".ts", ".js", ".go", ".rs", ".json", ".toml"}
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "__tests__", "tests", "test", ".venv"}
USAGE = Counter()


def ask(state, questions):
    """One Jev request, disk-cached by content hash, with backoff on 429/529."""
    body = json.dumps({"model": "jev-latest", "state": state, "questions": questions}, sort_keys=True)
    key = CACHE / (hashlib.sha256(body.encode()).hexdigest()[:24] + ".json")
    if key.exists():
        return json.loads(key.read_text())["answers"]
    for attempt in range(6):
        req = urllib.request.Request(API, data=body.encode(), headers={
            "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"],
            "Content-Type": "application/json", "User-Agent": "drift-exp/0.1"})
        try:
            out = json.loads(urllib.request.urlopen(req, timeout=60).read())
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 529, 500, 502, 503) and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:500]}")
    CACHE.mkdir(exist_ok=True)
    key.write_text(json.dumps(out))
    USAGE["requests"] += 1
    USAGE["input_tokens"] += out["usage"]["input_tokens"]
    USAGE["output_tokens"] += out["usage"]["output_tokens"]
    return out["answers"]


# ---------- 1. doc -> units (leaf line + ancestor chain) ----------

def doc_units(path, text=None):
    units, heads, bullets, in_code = [], [], [], False
    for n, raw in enumerate((Path(path).read_text() if text is None else text).splitlines(), 1):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not raw.strip() or raw.strip().startswith("<!--"):
            continue
        m = re.match(r"^(#+)\s+(.*)", raw)
        if m:
            heads = heads[:len(m.group(1)) - 1] + [m.group(2).strip()]
            heads = [h for h in heads if h]
            bullets = []
            continue
        m = re.match(r"^(\s*)(?:[-*+]|\d+\.)\s+(.*)", raw)
        if m:
            indent, text = len(m.group(1)), m.group(2).strip()
            bullets = [(i, t) for i, t in bullets if i < indent]
            parents = [t for _, t in bullets]
            bullets.append((indent, text))
        elif units and units[-1]["line"] == n - 1 - units[-1].get("extra", 0) and not raw.lstrip().startswith(("|", ">")) and not units[-1]["text"].startswith("|"):
            units[-1]["text"] += " " + raw.strip()
            units[-1]["extra"] = units[-1].get("extra", 0) + 1
            continue
        else:
            text, parents, bullets = raw.strip(), [], []
        if len(text) > 8:
            units.append({"line": n, "text": text, "context": heads + parents, "heads": list(heads), "parents": parents})
    return units


# ---------- 2. Jev: which units are verifiable claims about the code? ----------

def classify(units, batch=12):
    def run(chunk):
        state = {"units": {f"u{u['line']}": {"context": " > ".join(u["context"]), "text": u["text"]} for u in chunk}}
        qs = {}
        for u in chunk:
            qs[f"u{u['line']}"] = {
                "type": "choice",
                "instructions": f"`units.u{u['line']}.text` is one line from a software project's README, located under `units.u{u['line']}.context`. What kind of statement is it?",
                "criteria": {
                    "code_fact": "A specific, checkable fact about what the project's own source code does or exposes: a tool/function/command name, a parameter with its type or optionality, a default value, a return value, a concrete behavior, a config key or environment variable the code reads.",
                    "external_service": "A fact about a hosted or remote service, a web dashboard, an account system, pricing, quotas, or a third-party API's server-side behavior: something that happens outside this repository's own source code, even if it names keys, headers, or fields.",
                    "dependency_setting": "A setting, environment variable, option, or behavior that belongs to a third-party framework, library, runtime, or tool the project depends on (the README may even name that dependency in the heading), rather than something the project's own source code defines.",
                    "usage_setup": "Installation, launch, or client-configuration instructions (how to install, run, or wire the project into another app), not a fact about the source code's behavior.",
                    "narrative": "Overview, motivation, a label or heading-like line, a note about status, licensing, contributing, links, or anything else that cannot be checked against the source code.",
                },
            }
        return chunk, ask(state, qs)

    chunks = [units[i:i + batch] for i in range(0, len(units), batch)]
    with ThreadPoolExecutor(8) as ex:
        for chunk, ans in ex.map(run, chunks):
            for u in chunk:
                a = ans[f"u{u['line']}"]
                u["kind"], u["p_code_fact"] = a["choice"], round(a["probabilities"]["code_fact"], 3)
    return units


# ---------- 3. retrieval: BM25 over code windows ----------

def tokens(s):
    out = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", s):
        out.append(w.lower())
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", w.replace("_", " "))
        if len(parts) > 1:
            out += [p.lower() for p in parts]
    return out


def text_windows(files, size=40, step=25):
    """Same windows as code_windows, from an in-memory {path: source} mapping."""
    wins = []
    for name, src in files.items():
        lines = src.splitlines()
        for s in range(0, max(1, len(lines) - size + step), step):
            body = "\n".join(lines[s:s + size])
            if body.strip():
                wins.append({"file": name, "start": s + 1, "text": body, "tf": Counter(tokens(body))})
    return wins


def code_windows(root, size=40, step=25):
    wins = []
    for p in sorted(Path(root).rglob("*")):
        if p.suffix not in CODE_EXT or not p.is_file() or SKIP_DIRS & set(p.parts) or "test" in p.name.lower():
            continue
        if p.name in ("package-lock.json", "tsconfig.json") or p.stat().st_size > 400_000:
            continue
        lines = p.read_text(errors="ignore").splitlines()
        for s in range(0, max(1, len(lines) - size + step), step):
            body = "\n".join(lines[s:s + size])
            if body.strip():
                wins.append({"file": str(p.relative_to(root)), "start": s + 1, "text": body, "tf": Counter(tokens(body))})
    return wins


def bm25(wins):
    n, df = len(wins), Counter()
    for w in wins:
        df.update(w["tf"].keys())
    avg = sum(sum(w["tf"].values()) for w in wins) / max(n, 1)

    def search(query, k, boost=()):
        q = Counter(tokens(query))
        for b in boost:
            for t in tokens(b):
                q[t] += 2
        scored = []
        for w in wins:
            dl, s = sum(w["tf"].values()), 0.0
            for t, qf in q.items():
                f = w["tf"].get(t, 0)
                if f:
                    idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                    s += qf * idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / avg))
            scored.append((s, w))
        scored.sort(key=lambda x: -x[0])
        return [w for s, w in scored[:k] if s > 0]
    search.corpus = "\n".join(w["text"] for w in wins)
    return search


# ---------- 4. Jev: verify each claim against each candidate window ----------

PRE = ("`doc.item` is one line from a project's README, found under the headings `doc.headings`. "
       "`doc.parents` lists the list items it is nested under, outermost first (empty if none); it is one of several sibling lines and the others are not shown. "
       "If the headings or parents state a condition or scenario (for example 'if the client does not support X', 'Docker', 'remote server'), the item only describes that scenario. "
       "`code.{c}.source` is an excerpt of the project's current source code. ")
CRIT = {
    "consistent": {"true": "The excerpt shows the thing the item describes and no stated detail (name, type, required/optional, default value, behavior) conflicts with it. The code may contain more than the item mentions.",
                   "false": "A stated detail conflicts with the excerpt, or the excerpt does not show the thing the item describes."},
    "conflict": {"true": "The excerpt defines the same parameter, tool, option, or behavior, and a specific detail differs: another type, another default value, required versus optional, another name, another behavior.",
                 "false": "Nothing conflicts: the excerpt agrees, only adds information, is about something else, or handles a different scenario than the one the item is conditioned on."},
}


def verify(claims, search, k=8):
    def run(u):
        idents = re.findall(r"`([^`]+)`", " ".join(u["context"][-2:] + [u["text"]]))
        cands = search(" ".join(u["context"][-2:]) + " " + u["text"], k, boost=idents)
        state = {"doc": {"headings": u["heads"], "parents": u["parents"], "item": u["text"]},
                 "code": {f"c{i}": {"file": c["file"], "source": c["text"]} for i, c in enumerate(cands)}}
        qs = {}
        for i in range(len(cands)):
            qs[f"ok{i}"] = {"type": "noul", "instructions": PRE.format(c=f"c{i}") + "Is `doc.item`, read in that context, consistent with this excerpt?", "criteria": CRIT["consistent"]}
            qs[f"same{i}"] = {"type": "noul", "instructions": PRE.format(c=f"c{i}") + "Does this excerpt contain the definition or implementation of the very thing `doc.item` describes, belonging to the same tool, command, or component named in the headings and parents?",
                              "criteria": {"true": "Yes: the same parameter, tool, option, or behavior, in the same tool or component.", "false": "No: the excerpt is about another tool or component (even one with a similarly named parameter), only calls or mentions the thing in passing, or is unrelated."}}
            qs[f"bad{i}"] = {"type": "noul", "instructions": PRE.format(c=f"c{i}") + "Does this excerpt show a fact that conflicts with `doc.item` read in that context?", "criteria": CRIT["conflict"]}
        return u, cands, (ask(state, qs) if cands else {})

    with ThreadPoolExecutor(8) as ex:
        for u, cands, ans in ex.map(run, claims):
            ev = [{"file": c["file"], "start": c["start"],
                   "p": {"supported": round(ans[f"ok{i}"]["noul"], 3), "contradicted": round(ans[f"bad{i}"]["noul"], 3), "same": round(ans[f"same{i}"]["noul"], 3)}} for i, c in enumerate(cands)]
            u["evidence"] = ev
            u["p_supported"] = max([e["p"]["supported"] for e in ev], default=0.0)
            # a conflict only counts on an excerpt that is about the same thing
            u["p_contradicted"] = max([min(e["p"]["contradicted"], e["p"]["same"]) for e in ev], default=0.0)
            # policy lives in code: a confirmation anywhere outweighs a contradiction elsewhere
            # exact symbol existence is code's job, not the model's: Jev forgives a renamed identifier
            u["missing_symbols"] = [s for s in re.findall(r"`([A-Za-z_][A-Za-z0-9_]{2,})`", u["text"]) if s not in search.corpus]
            leading = re.match(r"\W*`([A-Za-z_][A-Za-z0-9_]{2,})`", u["text"])
            strong_conflict = u["p_contradicted"] >= 0.7 and u["p_contradicted"] > u["p_supported"]
            if u["missing_symbols"] and ((u["p_supported"] < 0.7 and u["p_contradicted"] >= 0.4)):
                u["status"] = "DRIFT"
            elif strong_conflict:
                u["status"] = "DRIFT"
            elif u["p_supported"] >= 0.6:
                u["status"] = "ok"
            else:
                u["status"] = "unknown"
    return claims
