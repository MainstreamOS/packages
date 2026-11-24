#!/usr/bin/env python3
"""Open, update and close the issues of kernel-update.yml: one issue per Arch version and failed stage.

  kernel-issue.py report --archpkg 7.3.6.arch1-1 --mode update --run-url URL --needs JSON [FAILURE.md ...]

--needs is the workflow's toJSON(needs) for the jobs that decide the run. When every one succeeded, the open
issues of that Arch version are closed with a comment. Otherwise each failure file (first line "stage: <stage>")
opens an issue, or comments on (and reopens) the one for that version and stage. A job that failed without
leaving a file gets one with the job's name as the stage. A cancelled run changes nothing."""

import argparse
import json
import os
import subprocess
import sys

MARK = "<!-- kernel-update "

HINTS = {
    "rebase": ("Resolve it on the fork: rebase the branch by hand onto Arch's new tag, tag it "
               "`mainstream-<flavor>-<pkgver>-1` and push the branch and the tag. The next run uses a tag "
               "that is already on the fork as it is and goes on from there."),
    "config": ("The recipes' `config` files are no longer Arch's file, or Arch's could not be read. Move the "
               "recipes by hand, or make the configs Arch's again."),
    "decide": "The recipes or the requested version are not what the update expects; see the details.",
    "recipe": "The recipe update failed; see the details.",
    "publish": ("Nothing is half done on GitHub's side that a re-run does not finish: the update reuses fork tags "
                "that are already pushed and skips kernels that are already published."),
}
BUILD_HINT = ("If the fix belongs in the fork, push it as a new revision on the branch and tag "
              "(`mainstream-<flavor>-<pkgver>-1` if this version has no tag on the fork yet), then run "
              "**Update kernels** again.")


def gh(*args):
    return subprocess.run(["gh", *args], stdout=subprocess.PIPE, text=True, check=True).stdout


def marker(archpkg, stage):
    return f"{MARK}{archpkg} {stage} -->"


def parse_failure(text):
    first, _, rest = text.partition("\n")
    if not first.startswith("stage: ") or not first[len("stage: "):].strip():
        raise ValueError(f"a failure file has no stage line: {first!r}")
    return first[len("stage: "):].strip(), rest.strip()


def issue_body(archpkg, mode, stage, details, run_url):
    where = ("in `core`; the update" if mode == "update"
             else "in `core-testing`; the early check (build and boot only, nothing is published)")
    hint = HINTS.get(stage.split()[0], BUILD_HINT)
    return "\n".join([
        f"Arch's `linux` {archpkg} is {where} stopped at **{stage}**.",
        "", details or "The job ended without leaving details (a timeout or a runner problem); see the run.",
        "", f"Run: {run_url}",
        "", hint,
        "", marker(archpkg, stage)])


def fail(repository, issues, archpkg, mode, stage, details, run_url):
    mark = marker(archpkg, stage)
    found = sorted((i for i in issues if mark in (i.get("body") or "")), key=lambda i: i["number"])
    title = f"kernel: Arch linux {archpkg} failed at {stage}" + (" (early check)" if mode == "check" else "")
    if not found:
        gh("issue", "create", "--repo", repository, "--title", title,
           "--body", issue_body(archpkg, mode, stage, details, run_url))
        print(f"Opened: {title}")
        return
    number = str(found[0]["number"])
    if found[0]["state"] != "OPEN":
        gh("issue", "reopen", number, "--repo", repository)
    gh("issue", "comment", number, "--repo", repository,
       "--body", f"Failed again at **{stage}** ({mode} run): {run_url}\n\n{details}")
    print(f"Updated #{number}: {title}")


def resolve(repository, issues, archpkg, mode, run_url):
    done = ("built, boot-tested and published it" if mode == "update"
            else "built and boot-tested it (early check)")
    for issue in issues:
        if issue["state"] == "OPEN" and f"{MARK}{archpkg} " in (issue.get("body") or ""):
            gh("issue", "close", str(issue["number"]), "--repo", repository,
               "--comment", f"Run {run_url} {done}.")
            print(f"Closed #{issue['number']}")


def job_of(path):
    """The job a failure file came from: its artifact folder is failure-<job>[-<kernel>]."""
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return folder.split("-")[1] if folder.startswith("failure-") else ""


def report(repository, archpkg, mode, run_url, needs, files):
    results = {job: value.get("result") for job, value in needs.items()}
    if any(result == "cancelled" for result in results.values()):
        print("The run was cancelled; the issues stay as they are.")
        return
    issues = json.loads(gh("issue", "list", "--repo", repository, "--state", "all", "--limit", "1000",
                           "--json", "number,state,body"))
    if all(result in ("success", "skipped") for result in results.values()) and not files:
        resolve(repository, issues, archpkg, mode, run_url)
        return
    failures, jobs_with_files = [], set()
    for path in files:
        with open(path) as f:
            stage, details = parse_failure(f.read())
        failures.append((stage, details))
        jobs_with_files.add(job_of(path))
    for job, result in results.items():
        if result == "failure" and job not in jobs_with_files:
            failures.append((job, ""))
    for stage, details in failures:
        fail(repository, issues, archpkg, mode, stage, details, run_url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["report"])
    parser.add_argument("--archpkg", required=True)
    parser.add_argument("--mode", choices=["update", "check"], required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--needs", required=True)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    report(os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"), args.archpkg, args.mode, args.run_url,
           json.loads(args.needs), args.files)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f"::error::kernel issue report failed: {error}")
