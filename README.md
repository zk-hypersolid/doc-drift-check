# doc-drift-check

A GitHub Action that answers one question on every pull request: **did this change make something the docs say untrue?**

It does not review your code and it does not rewrite your docs. For each documented claim whose
identifiers appear in the diff, it verifies the claim against the changed code as it was *before*
the change and as it is *after*, and reports only the claims that flipped. Documentation that was
already out of date stays quiet, so the report is about what this pull request broke.

```
### Doc drift check

This change appears to make 1 documented claim out of date:

README.md:281 — under MCP Server API

> | `get_top_queries` | Reports the slowest SQL queries based on total execution time … |

Now contradicted by `src/postgres_mcp/top_queries/top_queries_calc.py` (around line 1).
Conflict 0.80, support 0.64 (before this change: ok).
```

## Usage

```yaml
name: Doc drift
on: pull_request

permissions:
  contents: read
  pull-requests: write

jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the check needs the base commit
      - uses: zk-hypersolid/doc-drift-check@v1
        with:
          api-key: ${{ secrets.TYPESAFE_API_KEY }}
```

Get a key at [typesafe.ai](https://typesafe.ai) and store it as a repository secret.

| Input | Default | Meaning |
| --- | --- | --- |
| `api-key` | — | TypeSafe API key. Required. |
| `docs` | root `*.md` and `docs/**.md` | Space-separated docs to check. Docs edited in the same pull request are always skipped. |
| `comment` | `true` | Post the report as one pull-request comment, edited in place on later pushes. |
| `fail-on-finding` | `false` | Fail the job when something is found. Start with `false`. |
| `max-claims` | `60` | Cap on claims verified per run. |

The job also writes the report to the run summary and emits a `::warning` annotation per finding,
so `comment: false` still gives you everything except the comment.

## How it works

Ordinary code does the parts ordinary code is good at: splitting Markdown into claims, matching
identifiers against the diff, retrieving candidate code with BM25, checking whether a symbol still
exists, and deciding what crosses the threshold. A [TypeSafe](https://typesafe.ai) System One model
answers only the semantic question, as three calibrated yes/no judgments per doc line and code
excerpt:

- is this line consistent with this excerpt?
- does this excerpt show a fact that conflicts with it?
- is this excerpt even about the same thing?

A conflict counts only on an excerpt that is about the same thing, which is what keeps a
similarly named parameter on a sibling tool from raising a false alarm.

Every finding carries its probabilities and the code it came from. **Treat findings as leads, not
verdicts** — read the cited code before you change anything.

## What it is good and bad at

Measured on 36 open-source MCP server repositories (about 2,000 documented claims) and a replay of
roughly 1,000 commits of their history:

- **Good at** parameter types, optionality, defaults, renamed identifiers, tool and option names,
  environment variables — the details that go stale silently. On synthetic falsifications it caught
  95% and never mislabelled one as consistent.
- **Bad at** architecture prose, behavior spread across many files, and claims about a hosted
  service rather than the code in the repository. These are classified out or reported as unknown
  rather than guessed at.
- **Pull-request mode is the point.** Scanning a whole repository at once, roughly half the findings
  were false alarms, because a healthy repository's real drift rate is about as low as the error
  rate. Restricted to what a change just touched, precision was 77–92%.

Costs a few cents per pull request and adds a few seconds.

## Prior art

[Swimm](https://swimm.io) and several LLM-based tools keep docs in sync by rewriting them. This one
is deliberately smaller: it only tells you what broke, it is cheap enough to run on every push, and
every alarm comes with its evidence and its probability.

## License

MIT
