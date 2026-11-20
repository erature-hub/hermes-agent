"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.

``gh`` runs as the card's assignee profile (``profile_home``), not the ambient
login: :func:`_gh_env` resolves that profile's own credentials/config for the
subprocess — a multi-profile host's default ``gh`` login cannot read another
org's private repos (#122689).

Repositories that declare NO required status checks
---------------------------------------------------
A repository can protect its base branch (required reviews, linear history,
signed commits) while requiring zero status checks — ``erature-hub/aisuite``
``main`` is exactly that: the protection object carries no
``required_status_checks`` key and the repo publishes no ruleset. For such a
repository the board must not invent a gate the repository never declared:
the old code refused every completion with "No repository-required checks are
configured", which no operator can act on once the contract is bound to a PR
URL (publication binds once), so approved PR cards deadlocked forever.

The zero-required path therefore verifies what the repository actually
publishes about the head:

* the PR must be landable (open, at the declared head, not conflicting);
* when the head published verdicts, a verdict that is red on the head while
  green on the base branch blocks, and the receipt names the check and the
  cheapest disambiguation (re-run the job: a verdict a re-run clears was
  runner noise, not a regression);
* a verdict that is red on BOTH the head and the base branch is pre-existing
  breakage (the base branch is red too) and is reported as an explicit
  ``advisory`` acceptance instead of a permanent block;
* when the head has published no verdict yet but CI is wired to it, the
  receipt is ``pending`` (CI has not reported) — retryable, never a deadlock;
* when no CI ran for this head at all, the receipt is an ``advisory``
  acceptance: the repository runs no status checks for this PR, so there is
  nothing to gate on and the deliverable is the published PR itself.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False,
         profile_home: str | None = None):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                check=True, env=_gh_env(profile_home))
    except subprocess.CalledProcessError as exc:
        # 401/403/404 = the login cannot see this repository (wrong profile identity
        # or missing grant), not a transient API failure. Persist only the status
        # code + endpoint, never gh's stderr (credentials/host details).
        denied = re.search(r"HTTP (40[134])", exc.stderr or "")
        if denied:
            raise _GateAuthError(f"HTTP {denied[1]} on {endpoint.split('?')[0]}") from None
        if exc.returncode == 4:  # gh's authentication-required exit: this profile has no login
            raise _GateAuthError(f"gh has no login for {endpoint.split('?')[0]}") from None
        raise
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


class _GateAuthError(RuntimeError):
    """gh was refused at HTTP 401/403/404 (or GraphQL returned no repository):
    this profile's login cannot see the repo — an identity problem to fix, not
    an infrastructure blip to retry."""


# Every key gh can read a login from, github.com and GitHub Enterprise alike. All of them are
# dropped from the launcher's env for a routed child; only _GH_GITHUB_LOGIN_KEYS come back.
_GH_LOGIN_KEYS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
)
# Every call here targets github.com (`--hostname github.com`, github.com PR URLs), where gh
# ignores the enterprise token vars and GH_HOST. So only these are re-added from the profile, and
# only these prove a login: an enterprise-only profile must still get the fail-closed pin below,
# or gh falls through to the launcher's ~/.config/gh/hosts.yml for github.com.
_GH_GITHUB_LOGIN_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")
# Not a login, but gh cannot reach github.com without them where the profile's .env sets them
# (a proxy in .env is documented; a routed child loses the launcher's .env copy).
_GH_TRANSPORT_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy",
                      "http_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR")


def _gh_env(profile_home: str | None) -> dict[str, str] | None:
    """Child env for ``gh``: the card's profile identity when one is resolvable.

    The completion boundary runs in the worker (assignee), the CLI, or a
    reviewer/dispatcher turn, so an ambient ``gh`` login is whichever process
    happened to call it (#122689). gh needs only its own login, so the env keeps
    the provider scrub — ``served_profile_child_env(inherit_credentials=False)``,
    its "caller re-adds the few keys the child needs" contract — and re-adds
    exactly gh's github.com login keys (``_GH_GITHUB_LOGIN_KEYS``) from the target profile's own secret scope
    instead of overlaying the whole scope (#134669: the gh child never needs
    the assignee's provider keys or adapter secrets).
    ``None`` keeps the ambient env — unassigned cards behave exactly as before.
    """
    if not profile_home:
        return None
    from agent.secret_scope import build_profile_secret_scope
    from tools.environments.local import _is_routed_home, hermes_subprocess_env, served_profile_child_env
    base = hermes_subprocess_env(inherit_credentials=False)
    routed = _is_routed_home(profile_home)
    if routed:
        # The launcher's own gh login must not reach the child: GH_TOKEN/GITHUB_TOKEN are
        # already scrubbed by hermes_subprocess_env, but the enterprise trio (GH_HOST,
        # GH_/GITHUB_ENTERPRISE_TOKEN) is on no strip list, and a surviving ambient token
        # would reach the child (#122689, the GHE case). The target's own github.com login keys
        # are re-added from its secret scope below.
        for key in _GH_LOGIN_KEYS:
            base.pop(key, None)
    env = served_profile_child_env(base=base, target_home=profile_home, inherit_credentials=False)
    scope = build_profile_secret_scope(Path(profile_home))
    env.update((key, scope[key]) for key in _GH_GITHUB_LOGIN_KEYS if scope.get(key))
    env.update((key, scope[key]) for key in _GH_TRANSPORT_KEYS if key in scope)  # NO_PROXY= clears
    if routed and not (env.keys() & set(_GH_GITHUB_LOGIN_KEYS)):
        # HOME/XDG_CONFIG_HOME are still the launch process's: without a login of its own the
        # child would fall through to ~/.config/gh/hosts.yml — the ambient login. Pin gh's config
        # to a profile-owned dir so it fails "not logged in" (exit 4 -> auth) instead.
        env["GH_CONFIG_DIR"] = str(Path(profile_home) / "gh")
    return env


def _assignee_profile_home(assignee: str | None) -> str | None:
    """Home whose ``gh`` login must read the contract repo — the assignee's, resolved
    exactly as the dispatcher resolves the worker's home — or None (unassigned) so the
    ambient login is used. An assigned card whose profile cannot be resolved is an
    identity failure (``auth``), never a silent fall-through to the ambient login."""
    if not assignee:
        return None
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env
    try:
        return resolve_profile_env(normalize_profile_name(assignee))
    except (FileNotFoundError, ValueError):
        raise _GateAuthError(f"assignee profile {assignee!r} cannot be resolved") from None


def collect_acceptance(contract: str, published_pr: str | None,
                       assignee: str | None = None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        profile_home = _assignee_profile_home(assignee)
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = f'{{repository(owner:{json.dumps(owner)},name:{json.dumps(name)}){{pullRequest(number:{number:d}){{headRefOid baseRefName state\n            baseRef{{branchProtectionRule{{requiredStatusChecks{{context app{{databaseId}}}}}}}}}}}}}}'
        repository = _api("graphql", query=query, profile_home=profile_home)["data"]["repository"]
        if repository is None:
            # A private repo the login cannot read resolves to null, not an error.
            raise _GateAuthError(f"HTTP 404 on graphql {repo}")
        pr = repository["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
                     paginate=True, profile_home=profile_home)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    required.update((r["context"], r.get("integration_id"))
                                    for r in rule["parameters"]["required_status_checks"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest",
                     paginate=True, profile_home=profile_home)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                                                       paginate=True, profile_home=profile_home) for s in page]
        if not required:
            # The repository declares no merge gate, so there is no policy to
            # enforce. Verify the evidence the head actually published instead
            # of refusing every completion (which deadlocked approved PR cards).
            return _accept_without_required_checks(
                receipt, repo=repo, number=number, sha=sha, branch=branch,
                runs=runs, statuses=statuses, profile_home=profile_home,
            )
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            # A legacy status can satisfy an unpinned context, but never a check pinned to an app.
            legacy = [s for s in statuses if s["context"] == context] if app_id in (None, -1) else []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
        if current["head"]["sha"] != sha or current["base"]["ref"] != branch or (current["state"] == "closed" and not current.get("merged")):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except _GateAuthError as exc:
        login = f"assignee profile {assignee!r}'s gh login" if assignee else "the ambient gh login"
        receipt.update(classification="auth",
                       detail=f"GitHub refused the acceptance read ({exc}) as {login}; "
                              "fix that profile's GitHub credentials/access to the repository, then retry completion.")
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")


#: A check-run verdict that is a genuine failure of the code under test.
#: ``skipped``/``neutral`` are not failures: a check the repository gates
#: conditionally (or a job that declined to run) says nothing about the PR.
_FAILING_VERDICTS = frozenset({"failure", "timed_out", "action_required", "cancelled"})


def _verdict_classification(conclusion: str | None) -> str:
    """How one observed check reads in the receipt's evidence list.

    A RED check must not be filed under the same label as a job that merely
    declined to run. The receipt is the record an operator audits, and
    "infra" reads as "not the PR's fault" - which is exactly the wrong
    reading for the check the receipt is blocking on. "skipped" and
    "neutral" are reported as themselves: a gated job that declined to run
    says nothing about the PR, and neither is a failure.
    """
    if conclusion == "success":
        return "success"
    if conclusion in _FAILING_VERDICTS:
        return "failure"
    if conclusion in ("skipped", "neutral", "pending"):
        return conclusion
    return "infra"


def _accept_without_required_checks(
    receipt: dict, *, repo: str, number: int, sha: str, branch: str,
    runs: list, statuses: list, profile_home: str | None,
) -> dict:
    """Acceptance for a repository that declares NO required status checks.

    ``erature-hub/aisuite`` protects ``main`` (reviews, signed commits, linear
    history) while requiring ZERO status checks and publishing no ruleset, so
    ``required`` is empty. The old code answered that with
    ``classification="missing"`` — a permanent refusal no operator could act
    on once the contract was bound to a PR URL, which deadlocked approved PR
    cards (``t_87ff3ac2``) in ``todo`` and let the auto-decomposer fan them
    out into duplicate children.

    The repository never declared a gate, so there is no policy to enforce.
    What IS verifiable is the evidence the head published:

    * the PR must be open (or merged), at the declared head and base, and must
      not conflict with its base — an unmergeable PR is not a deliverable;
    * a check-run that is red on the head while GREEN on the base branch is a
      regression this PR introduced, and must block;
    * a check-run that is red on the head AND red on the base branch is
      pre-existing breakage (the base branch is red too), so it must not
      deadlock the card — it is recorded as an advisory instead;
    * a head that published no verdict yet while CI is wired to it is
      ``pending`` (retryable), never a permanent refusal;
    * a head with no CI at all is an advisory acceptance: the repository runs
      no status checks for this PR, so the deliverable is the published PR.

    Every receipt this returns names the checks it looked at and why the
    verdict is what it is, so an operator can audit the decision.
    """
    receipt["required"] = []
    head_checks: dict[str, dict] = {}
    for run in runs:
        if run.get("head_sha", run.get("sha")) != sha:
            continue
        head_checks[run["name"]] = run
    legacy: dict[str, dict] = {}
    for status in statuses:
        if status.get("sha") != sha:
            continue
        legacy.setdefault(status.get("context"), status)

    # The PR must be a landable deliverable: open (or merged), on the declared
    # head and base. A conflicting PR cannot be merged, so it is not accepted
    # even though the repository declares no checks.
    current = _api(f"repos/{repo}/pulls/{number}", profile_home=profile_home)
    base_sha = ((current.get("base") or {}).get("sha")) or ""
    if current.get("head", {}).get("sha") != sha or (current.get("base") or {}).get("ref") != branch:
        receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
        return receipt
    # A conflicting PR cannot be merged, so it is not a deliverable even
    # though the repository declares no checks. ``mergeable`` is null while
    # GitHub recomputes it, so only an explicit False / "dirty" blocks.
    if (current.get("state") == "closed" and not current.get("merged")) \
            or current.get("mergeable") is False or current.get("mergeable_state") == "dirty":
        receipt.update(
            classification="conflict",
            detail="PR is closed without being merged, or conflicts with its base branch; "
                   "it is not a mergeable deliverable at this head.",
        )
        return receipt

    observed: list[dict] = []
    failing: list[str] = []
    pending: list[str] = []
    for name, run in sorted(head_checks.items()):
        conclusion = run.get("conclusion")
        if run.get("status") != "completed":
            pending.append(name)
            observed.append({"name": name, "id": run.get("id"), "classification": "pending",
                             "conclusion": conclusion, "head_sha": run.get("head_sha")})
            continue
        if conclusion in _FAILING_VERDICTS:
            failing.append(name)
        observed.append({
            "name": name, "id": run.get("id"),
            "url": run.get("html_url"), "head_sha": run.get("head_sha"),
            "classification": _verdict_classification(conclusion),
            "conclusion": conclusion,
        })
    for context, status in sorted(legacy.items()):
        if context in head_checks:
            continue
        state = status.get("state")
        if state == "pending":
            pending.append(context)
        elif state == "failure":
            failing.append(context)
        observed.append({
            "name": context, "id": status.get("id"), "url": status.get("target_url"),
            "head_sha": status.get("sha"), "classification": _verdict_classification(state),
            "conclusion": state,
        })
    receipt["checks"] = observed

    if failing:
        # Distinguish a regression from pre-existing breakage: a check that is
        # also red on the base branch is not this PR's fault.
        base_red = set()
        if base_sha:
            try:
                base_pages = _api(f"repos/{repo}/commits/{base_sha}/check-runs?per_page=100&filter=latest",
                                  paginate=True, profile_home=profile_home)
            except (_GateAuthError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
                base_pages = []
            for page in base_pages:
                for run in page["check_runs"]:
                    if run.get("head_sha") == base_sha and run.get("conclusion") in _FAILING_VERDICTS:
                        base_red.add(run["name"])
        regressions = sorted(name for name in failing if name not in base_red)
        if regressions:
            receipt.update(
                classification="failure",
                detail="Check(s) red on this PR's head but green on the base branch: "
                       + ", ".join(regressions) + ". Re-run the job first - a verdict that a "
                       "re-run clears was runner noise (host, timing or environment), not a "
                       "regression; if it stays red, this PR introduced it and must be fixed "
                       "or rebased onto the base.",
            )
            return receipt
        receipt.update(
            classification="advisory",
            detail="Check(s) red on this PR's head are red on the base branch too ("
                   + ", ".join(sorted(failing)) + "), so the breakage is pre-existing on "
                   "the base branch and not introduced by this PR; accepting as advisory.",
            ok=True,
        )
        return receipt

    if pending:
        receipt.update(
            classification="pending",
            detail="CI is wired to this PR's head but has not published every verdict yet "
                   "(still reporting: " + ", ".join(sorted(pending)) + "); "
                   "retry once the remaining checks report.",
        )
        return receipt

    if not observed:
        receipt.update(
            classification="advisory",
            detail="This repository declares no required status checks and published no "
                   "check-run for this PR's head, so there is no CI evidence to verify; "
                   "accepting as advisory on the published PR.",
            ok=True,
        )
        return receipt

    receipt.update(
        classification="success",
        detail="This repository declares no required status checks; verified that the "
               "PR's head published no failing verdict (" + str(len(observed)) + " check(s) observed).",
        ok=True,
    )
    return receipt
