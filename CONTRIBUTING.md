# Contributing to `Gelvey/gelvey-fcc`

This is a personal fork of [`Alishahryar1/free-claude-code`](https://github.com/Alishahryar1/free-claude-code). Pull requests are **not accepted on this fork**; please open bug fixes and broadly-useful patches against the upstream repo instead. This document covers how to work with this fork locally if you maintain it or mirror it.

## Development setup

Clone the fork with the upstream remote preserved so you can sync from upstream:

```bash
git clone https://github.com/Gelvey/gelvey-fcc.git
cd gelvey-fcc
git remote add upstream https://github.com/Alishahryar1/free-claude-code.git
git fetch upstream
```

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.14 (`.python-version` pins it).

## Running the full CI locally

The same five checks GitHub Actions runs on `push` and `pull_request`:

```bash
./scripts/ci.sh
```

The Windows equivalent:

```powershell
.\scripts\ci.ps1
```

Gates:

1. `CI / Ban type ignore suppressions` — no `# type: ignore` or `# ty: ignore` allowed in any tracked `.py`.
2. `CI / ruff-format` — `uv run ruff format --check`.
3. `CI / ruff-check` — `uv run ruff check`.
4. `CI / ty` — `uv run ty check`.
5. `CI / pytest` — `uv run pytest -v --tb=short`.

Useful flags (PowerShell equivalents in parentheses):

- `--only pytest` (`-Only pytest`) — run only pytest.
- `--skip pytest` (`-Skip pytest`) — skip the long-running test step.
- `--dry-run` (`-DryRun`) — list the gates without executing them.

## Branch protection on `main`

`main` is protected:

- **5 required status checks** (the full context names GitHub uses): `CI / Ban type ignore suppressions`, `CI / ruff-format`, `CI / ruff-check`, `CI / ty`, `CI / pytest`. `strict: false` so a check that has not yet reported cannot block a fresh push.
- **`enforce_admins: true`** — every push to `main` must have all 5 checks green on the same SHA, even for the repo owner.
- **`allow_force_pushes: false`**, **`allow_deletions: false`** — history is protected.

### Routine workflow

1. Branch off `main`: `git checkout -b fix/foo`.
2. Edit, commit, `git push -u origin fix/foo`.
3. Open a PR into `main`. GitHub Actions runs all 5 checks on the PR head.
4. After checks pass, merge the PR (squash or merge commit both work) — the merge commit is auto-verified by the same checks.

Alternatively, after pushing a branch, re-run any of the 5 gates on `main` directly via the **Run workflow** button on the Actions tab (the workflow exposes `workflow_dispatch`).

### Emergency / recovery push to `main`

If you must push straight to `main` (e.g., a one-off fix when CI is unreachable or you need to rewrite a tag), lift protection, push, and restore it:

```bash
# 1. Lift protection (destructive: only history changes after this step is allowed)
gh api repos/Gelvey/gelvey-fcc/branches/main/protection -X DELETE

# 2. Push
git push origin main

# 3. Restore protection (note: GitHub requires `required_pull_request_reviews`
# and `restrictions` to be present in the body — set to `null` if you don't
# want them enabled).
gh api repos/Gelvey/gelvey-fcc/branches/main/protection -X PUT --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": [
      "CI / Ban type ignore suppressions",
      "CI / ruff-format",
      "CI / ruff-check",
      "CI / ty",
      "CI / pytest"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Re-run `./scripts/ci.sh` afterwards to confirm the new commit passes all 5 gates locally before pushing again.

## Sanitization contract

Anything you commit to this fork must remain portable and free of secrets:

- **No host-specific absolute paths** — use `Path(__file__).resolve().parent` in Python, `cd "$(dirname "$0")" && pwd` in shell, `$HOME` derivatives in launchers, or a pure-string-substitution `expand_path()` helper (no `eval`) like the one in `scripts/mcp/start_mcp.sh`. Example configs should use portable `~/...` placeholders.
- **No real API keys, tokens, or secrets** — only obvious placeholders such as `sk_live_REPLACE_ME` are allowed. Real `sk_live_*`, `sk_test_*`, `pk_live_*`, `rk_live_*`, `Bearer ...`, refresh tokens, account IDs, etc. must never land in a commit.
- **Public URLs only** — example SSH/SCP/URL strings should reference the public fork URL (`https://github.com/Gelvey/gelvey-fcc.git`) or upstream (`https://github.com/Alishahryar1/free-claude-code.git`), not private workspaces.

Before each push, run these three greps. Each one ignores `.git/`, `.venv/`, and `CONTRIBUTING.md` itself (which legitimately contains the example patterns and grep recipes) so any remaining match is a real leak:

```bash
# 1. Host-specific absolute paths (bare or with trailing slash).
grep -rE '/home/[A-Za-z0-9._-]+(/|\b)' --include='*.py' --include='*.json' --include='*.toml' --include='*.sh' --include='*.md' . \
  | grep -v '\.git/' | grep -v '\.venv/' | grep -v 'CONTRIBUTING\.md' || echo 'CLEAN: no host-specific paths'

# 2. Live API keys (Stripe-shape prefixes; sk_live_, sk_test_, pk_live_, rk_live_,
# plus Anthropic sk-ant-…, OpenAI sk-proj-… (legacy sk-… is intentionally
# excluded to avoid false positives on identifiers), and GitHub PAT shapes
# ghp_/gho_/ghu_/ghs_/ghr_, etc.).
# The grep -v strips the documented placeholder in .env.example and this recipe.
grep -rE '(sk|pk|rk)_(live|test)_[A-Za-z0-9]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|sk-proj-[A-Za-z0-9_-]{8,}|gh[psoru]_[A-Za-z0-9]{8,}' --include='*.py' --include='*.json' --include='*.toml' --include='*.env*' --include='*.md' --include='*.sh' . \
  | grep -v 'sk_live_REPLACE_ME' | grep -v 'CONTRIBUTING\.md' || echo 'CLEAN: no live API keys'

# 3. Bearer tokens (alphanumeric-leading payload of 8+ chars after "Bearer " + whitespace).
# Tightened from \S{8,} to reject Bash `${TOKEN}` placeholders that happen to be 8 chars long.
grep -rE 'Bearer[[:space:]]+[A-Za-z0-9._+/=-]{8,}' --include='*.py' --include='*.json' --include='*.toml' --include='*.env*' --include='*.md' --include='*.sh' . \
  | grep -v 'CONTRIBUTING\.md' || echo 'CLEAN: no Bearer tokens'
```

If any of the three greps prints results before the `CLEAN:` line, fix the leak before pushing.

## Sync from upstream (one-way)

We do not push our patches back to upstream. To bring upstream changes into this fork:

```bash
git fetch upstream
git checkout main
git merge --no-ff upstream/main   # or: git rebase upstream/main
git push origin main             # CI runs on the new tip
```

Resolve any merge conflicts locally; CI will validate the result.

## Re-running CI on demand

The workflow also exposes a `workflow_dispatch` trigger. From the GitHub Actions tab, choose **Run workflow** against the `tests.yml` workflow to re-evaluate any of the five gates against the current `main` tip without a new commit.

## License

This fork, like upstream, is MIT-licensed. See [LICENSE](LICENSE).
