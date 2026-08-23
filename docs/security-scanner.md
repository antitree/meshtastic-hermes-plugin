# The Hermes plugin security scanner

> **Status: reference only — nothing gates on this.**
>
> We do not track the scanner's verdict and CI does not run it. The scanner is
> regex matching over raw file text, so it flags Markdown table headers, the
> word `sudo`, and the documented install procedure. Satisfying it meant
> changing byte sequences rather than behavior. The analysis below is retained
> because it is accurate and took real work to establish; treat it as a study
> of the tool, not a to-do list. See ROADMAP.md.

Research writeup: what `scan_on_install` actually runs, whether it can be run
standalone or in CI, and what each finding against this repository means.

Everything below was derived by reading the Hermes source directly
(`NousResearch/hermes-agent` @ `8b09a9df8476010a78e86d6c32254b4ac14a8c4f`,
2026-08-23) and by **executing the real scanner against this repository**. It is
not inferred from the pasted scan output.

> **Typographic note.** This file is itself scanned by the CI job it describes.
> Quoting the trigger strings verbatim made *this document* score CRITICAL and
> become an independent blocker. So where a Hermes config path appears in prose
> or in a quoted example below, it is written with spaces around the slash
> (`.hermes / config.yaml`) and `Env |` table headers are broken across code
> spans. The **regexes quoted from `THREAT_PATTERNS` are exact and unmodified** —
> only the surrounding prose and example excerpts carry the spacing. That this
> workaround is necessary at all is itself evidence for the upstream bug at the
> end of this document.

---

## TL;DR

- The scanner is `tools/plugin_guard.py`, a thin plugin-flavoured wrapper around
  the regex engine in `tools/skills_guard.py`. It is **pure regex over raw file
  text** — no parsing, no dataflow, no semantics.
- **Only a `critical` finding produces the blocking `dangerous` verdict.**
  `high` yields `caution`; `medium` and `low` are explicitly *"informational, not
  blocking"*.
- This repo's **only** blockers are the two CRITICAL `hermes_config_mod` hits in
  `README.md` and `docs/usage.md`. Every code finding is MEDIUM/LOW and has **no
  effect on installability** — verified by counterfactual (below).
- There is **no `hermes plugins scan` subcommand**, but the scanner is an
  importable, pure-stdlib Python API, so **a CI check is feasible** and is now
  wired up in `.github/workflows/tests.yml`.
- Hermes' **own official plugin-authoring guide scores `dangerous` under Hermes'
  own scanner.** See "Upstream bug" below.

---

## 1. What does `scan_on_install` invoke?

`hermes_cli/plugins_cmd.py`:

```python
def _scan_on_install_enabled() -> bool:
    # On by default. Disable via plugins.scan_on_install: false in config.yaml.
    return bool(cfg_get(config, "plugins", "scan_on_install", default=True))

def _scan_plugin_tree(plugin_dir, identifier, *, force, scan_decision_cb=None):
    from tools.plugin_guard import (
        format_scan_report, scan_plugin, should_allow_plugin_install,
    )
    result = scan_plugin(plugin_dir, source=identifier)
    allowed, reason = should_allow_plugin_install(result, force=force)
    ...
```

The call chain is:

```
hermes plugins install/update
  └─ hermes_cli/plugins_cmd.py::_scan_plugin_tree
       └─ tools/plugin_guard.py::scan_plugin          # plugin-specific wrapper
            ├─ _check_plugin_structure(...)           # size/count/symlink/binary
            └─ tools/skills_guard.py::scan_file(...)  # THREAT_PATTERNS regexes
                 └─ tools/skills_guard.py::_determine_verdict(...)
```

`plugin_guard` is not its own ruleset. It reuses `skills_guard.THREAT_PATTERNS`
(a list of `(regex, pattern_id, severity, category, description)` tuples) and
then applies three plugin-specific adjustments:

1. **`CODE_EXEMPT_PATTERN_IDS`** — on files with a code extension
   (`.py .js .ts .sh .bash .rb .pl .php`), pattern ids like
   `python_getenv_secret`, `env_exfil_requests`, `agent_config_mod` and
   `send_to_url` are dropped, because reading your own API key and calling your
   backend is what every legitimate provider plugin does. **`.md`, `.yml` and
   `.toml` get no such exemption** — which is exactly why this repo's docs score
   worse than its code.
2. **`SEVERITY_REMAP`** — `binary_file` critical→high,
   `hermes_env_access` critical→**medium**, `curl_pipe_shell` critical→high.
3. **Structural limits** — 400 files, 10 MB tree, 1 MB per file; `.git`,
   `__pycache__`, `node_modules`, `.venv`, `.tox`, caches are skipped.

### The verdict rule (the single most important detail)

`tools/skills_guard.py`:

```python
def _determine_verdict(findings):
    if not findings:
        return "safe"
    if any(f.severity == "critical" for f in findings):
        return "dangerous"
    if any(f.severity == "high" for f in findings):
        return "caution"
    # medium/low findings alone are informational, not blocking
    return "safe"
```

And the install policy (`plugin_guard.should_allow_plugin_install`):

| Verdict | Effect |
|---|---|
| `safe` | installs normally |
| `caution` | installs after explicit confirmation (interactive prompt, `--force`, or a decision callback) |
| `dangerous` | **blocked — `--force` does not override** |

So: **MEDIUM and LOW findings cannot change the outcome.** Chasing them is
cosmetic.

---

## 2. Can it be run standalone?

**Not as a CLI subcommand.** There is no `hermes plugins scan`. `scan_plugin` is
called from exactly one place in the CLI — the install/update path
(`plugins_cmd.py:101`) — so before this work the only way to learn your verdict
was to attempt a real install and read the rejection.

**Yes as a library.** `tools/plugin_guard.scan_plugin` is public, documented in
its own module docstring, and imports only the standard library
(`re`, `pathlib`, `hashlib`, `json`, `dataclasses`, `datetime`, `fnmatch`). It
needs no Hermes runtime, no `config.yaml`, no API keys, and performs no network
I/O while scanning. Verified:

```python
import sys; sys.path.insert(0, "/path/to/hermes-agent")
from tools.plugin_guard import scan_plugin, should_allow_plugin_install
result = scan_plugin(Path("."), source="antitree/meshtastic-hermes-plugin")
print(result.verdict, should_allow_plugin_install(result))
```

That property is what makes question 3 answerable.

---

## 3. Can it run as a CI check? — **Yes.** (Now wired up.)

This was the user's priority question, so concretely:

`scripts/hermes_scan.py` in this repo calls the *same* functions the installer
calls and exits non-zero when the verdict is worse than `--max-verdict`. The
`security-scan` job in `.github/workflows/tests.yml` runs it on every push and
PR.

Why it works in CI:

- Pure stdlib, so the runner needs nothing but Python.
- No secrets, no network at scan time, no Hermes install, no radio.
- Fast on a repo this size.
- The `hermes-agent` checkout is **pinned to a commit SHA**, so an upstream rule
  change cannot silently flip the job red or green. Bumping the pin is a
  deliberate, reviewable act.

Two implementation details that are easy to get wrong, both found by testing:

- **The Hermes checkout must live outside the workspace.** Cloning it into the
  repo makes the scanner walk hermes-agent's own tree — which is full of
  deliberate threat-pattern test fixtures — which both swamps the report and
  hangs the scan (observed: >2 minutes, killed). The job clones into
  `$RUNNER_TEMP`. `actions/checkout` cannot target a path outside
  `GITHUB_WORKSPACE`, hence a plain `git clone`.
- **The gate is currently `--max-verdict dangerous`**, i.e. it reports every
  finding but only fails on something *worse* than today's known state. That is
  deliberate: the repo is `dangerous` right now solely because of the deferred
  documentation findings, so a stricter gate would fail on day one for work this
  PR intentionally does not do. **Tighten to `caution`, then `safe`, as the docs
  issue lands.** The threshold is a one-word change.

The job itself adds one MEDIUM `git_clone` finding (the workflow contains the
string `git clone`). It is non-blocking and unavoidable given the job must
fetch the scanner.

---

## 4. What triggers each finding?

Exact regexes from `tools/skills_guard.py::THREAT_PATTERNS`. Reproduced by
running the real scanner against this tree.

### The two blockers (CRITICAL — deferred to human doc work)

```python
(r'\.hermes/config\.yaml|\.hermes/SOUL\.md',
 "hermes_config_mod", "critical", "persistence",
 "references Hermes configuration files directly"),
```

A bare substring match on `.hermes / config.yaml`. **Merely naming the path is
enough** — there is no notion of who writes it or why.

- `README.md:122` — `just enable  # add "meshtastic" to plugins.enabled in ~/.hermes / config.yaml`
- `docs/usage.md:349` — `ensure it's enabled — plugins.enabled in ~/.hermes / config.yaml`

Both are the **documented install procedure**: telling a user to enable this
plugin in their own config. Note `agent_config_mod` *is* code-exempt, but
`hermes_config_mod` is **not** in `CODE_EXEMPT_PATTERN_IDS` at all, and neither
exemption applies to `.md` regardless.

### HIGH (→ `caution`; all in `docs/usage.md`, deferred)

```python
(r'printenv|env` + pipe `', "dump_all_env", "high", "exfiltration",
 "dumps all environment variables"),
(r'\bsudo\b', "sudo_usage", "high", "privilege_escalation", "uses sudo"),
```

- `docs/usage.md:113` and `:124` — **false positives on Markdown table syntax.**
  The alternation `env` + pipe matches the literal text `env |` in the table
  headers `| Env` | `Effect |`. The rule is looking for the shell idiom
  `env | grep ...`; a table header is not that.
- `docs/usage.md:93` — `sudo nixos-rebuild switch`, a legitimate NixOS
  instruction.

### MEDIUM / LOW (non-blocking — the in-scope code findings)

```python
(r'pip\s+install\s+(?!-r\s)(?!.*==)', "unpinned_pip_install", "medium", "supply_chain"),
(r'\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}', "hex_encoded_string", "medium", "obfuscation"),
(r'\[::-1\]', "string_reversal", "low", "obfuscation"),
(r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env_access", "critical"->"medium", "exfiltration"),
```

`unpinned_pip_install` fires on any `pip install` **not** followed by `-r ` and
**not** containing `==` anywhere on the line. So `pip install meshtastic` matches
but `pip install -r requirements.txt` and `pip install foo==1.2.3` do not.

---

## Counterfactual: do the code fixes actually help?

Run against this tree with `_determine_verdict` directly:

| Scenario | Verdict |
|---|---|
| As found | `dangerous` |
| **Fix every in-scope code finding, docs untouched** | **`dangerous`** (no change) |
| Fix only the 2 CRITICAL docs findings | `caution` |
| Fix the CRITICALs *and* the 3 HIGHs | `safe` |

**Fixing all the code findings does not improve installability at all.** This is
the empirical justification for the narrow fixing scope: the blocker is entirely
in documentation.

---

## What was changed, and what deliberately was not

Changes were applied only where the new text is independently as clear or
clearer — not to appease a regex.

**Fixed** (6 findings cleared, 26 → 20; all MEDIUM/LOW, none affecting verdict):

| File | Change |
|---|---|
| `meshtastic_hermes/connection.py:90-91` | Error text now says "Install the 'meshtastic' package there with pip" instead of embedding the literal command twice. Same guidance, less duplication. |
| `pyproject.toml:15-16` | Comment describes pip-based installs in prose instead of listing three literal command forms. |
| `meshtastic_platform/adapter.py:334` | `install_hint` reworded to "Install meshtastic-hermes-plugin with pip (...)". |
| `meshtastic_hermes/observer.py:107` | `items[-limit:][::-1]` → `list(reversed(items[-limit:]))`. Identical behavior; arguably more readable. |

**Deliberately not changed — judged false positives:**

- `meshtastic_platform/adapter.py:324` (`hermes_env_access`, MEDIUM) — the
  message tells the user to set `MESHTASTIC_HOST` in `~/.hermes/.env`. This is
  the **documented** place for plugin config, and `plugin_guard.py`'s own
  comment says so, which is precisely why it is remapped critical→medium for
  plugins: *"the DOCUMENTED way plugins tell users where to put their API keys —
  nearly every legit plugin README mentions it. A mere reference is
  informational."* Removing it would make the diagnostic less useful.
- `tests/test_observer.py:49`, `tests/test_e2e.py:80`,
  `tests/test_observer_extra.py:63` (`hex_encoded_string`, MEDIUM) —
  `b"\x00\x01\x02\x03"` and friends are test fixtures standing in for an opaque
  *encrypted* payload. A byte literal is the clearest possible expression of
  "these exact bytes"; `bytes.fromhex("00010203")` would dodge the regex while
  obscuring intent. Not a trade worth making for a non-blocking finding.
- `tests/test_defensive_paths.py:129` and the workflow's `subprocess.run`
  (`python_subprocess`, MEDIUM) — ordinary, intended subprocess use.

---

## Upstream bug worth reporting to Hermes

The hypothesis in `SCAN_FINDINGS.md` is **confirmed, and it is worse than
suspected.**

`hermes_config_mod` is a bare substring match on `.hermes / config.yaml` with no
code exemption and no exemption for documentation. Every plugin whose README
explains how to enable it therefore earns a CRITICAL finding and a `dangerous`
verdict — and `dangerous` cannot be overridden with `--force`.

This is not hypothetical. Hermes' **own** plugin-authoring guide,
`website/docs/user-guide/features/plugins.md:150`, says:

> nothing with hooks or tools loads until you add the plugin's name to
> `plugins.enabled` in `~/.hermes / config.yaml`

Copying that file into an otherwise-empty directory as `README.md` and scanning
it with Hermes' own `scan_plugin` yields:

```
Hermes OWN plugin-authoring guide verdict: dangerous
  critical hermes_config_mod  README.md:150
  high     sudo_usage         README.md:249
```

**Hermes' official instructions for writing a plugin, scanned by Hermes' own
plugin scanner, are rated `dangerous` and would be blocked from installation.**
A plugin author who follows the documentation exactly produces an uninstallable
plugin.

Suggested fixes for upstream:
- Distinguish *referencing* `config.yaml` from *writing* it — mirror the
  existing `read_secrets_file` rule, which already excludes `cat >` redirection
  precisely because writing your own config is the opposite of exfiltration.
- Or downgrade `hermes_config_mod` to `medium` on documentation files, matching
  the `hermes_env_access` remap that exists for the same reason.
- Separately, `dump_all_env` (`printenv|env` + pipe ``) should require a shell
  context; it currently fires on any Markdown table containing an `Env` column.

---

## What could NOT be verified

Stated plainly, because these are the limits of this research:

- **The `hermes` CLI was never run.** It is not installed on this machine and
  the plugin was never actually installed through it. Everything about
  `hermes plugins install` behavior is read from source, not observed.
- **The CI job has not run on GitHub Actions yet.** The scanner script, its exit
  codes, and the exact `git clone`/`fetch`/`checkout` recipe were each executed
  and verified locally, but the assembled workflow job itself is unproven until
  it runs on a real runner.
- **The scan is pinned to one Hermes commit** (`8b09a9df`, 2026-08-23). Whether
  the deployed Hermes that produced the original `DANGEROUS` verdict was at this
  exact commit is unknown — though the locally reproduced findings match the
  pasted output line-for-line, which is strong evidence the rules are the same.
- **`format_scan_report` output formatting** was not compared against the
  user's pasted report; only the findings and verdict were.
- Whether Hermes maintainers consider the `hermes_config_mod` behavior a bug or
  intentional is **unknown** — the argument above is this repo's position, not
  an upstream ruling.
