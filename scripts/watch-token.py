#!/usr/bin/env python3
"""Report in one issue when KERNEL_FORK_TOKEN, the token kernel-update.yml pushes to the fork with, nears its expiry.

GitHub sends a fine-grained token's expiry in a response header. The issue opens once WARN_DAYS or fewer are left
(day 330 of a 366-day token), or when the token is missing or refused, and closes once a renewed token is set."""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

FORK = "MainstreamOS/linux"
WARN_DAYS = 36
MARK = "<!-- kernel-token-watch -->"
TITLE = "kernel: renew the fork token KERNEL_FORK_TOKEN"
STEPS = ("To renew: on github.com open Settings > Developer settings > Personal access tokens > Fine-grained "
         "tokens, regenerate the token (resource owner MainstreamOS, only `MainstreamOS/linux`, Contents read and "
         "write), then in this repository open Settings > Environments > `kernel-publish` and replace the secret "
         "`KERNEL_FORK_TOKEN`. This issue closes on the next run after that.")


def gh(*args):
    return subprocess.run(["gh", *args], stdout=subprocess.PIPE, text=True, check=True).stdout


def token_expiry(token):
    """The token's expiry as an aware datetime, None when it never expires. Raises PermissionError when refused."""
    request = urllib.request.Request(f"https://api.github.com/repos/{FORK}", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "kernel-token-watch/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            header = response.headers.get("github-authentication-token-expiration")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404):
            raise PermissionError(f"GitHub answered {error.code} for {FORK}") from error
        raise
    if not header:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", header)
    if not match:
        raise ValueError(f"unrecognized token expiry: {header}")
    return datetime.fromisoformat(f"{match[1]}T{match[2]}").replace(tzinfo=timezone.utc)


def problem(token, now):
    """What the issue should say, or None when all is well."""
    if not token:
        return "The secret `KERNEL_FORK_TOKEN` is not set in the `kernel-publish` environment."
    try:
        expiry = token_expiry(token)
    except PermissionError as error:
        return f"GitHub refused the token ({error}): it has expired, been revoked or lost access to `{FORK}`."
    if expiry is None:
        print("The token has no expiry.")
        return None
    left = (expiry - now).days
    print(f"The token expires on {expiry:%Y-%m-%d} ({left} days left).")
    if left > WARN_DAYS:
        return None
    if left < 0:
        return f"The token expired on {expiry:%Y-%m-%d}. Kernel updates cannot push to the fork until it is renewed."
    return (f"The token expires on {expiry:%Y-%m-%d}, in {left} days. After that, kernel updates cannot push to "
            "the fork.")


def watch(repository, token, now):
    issue = next((i for i in json.loads(gh("issue", "list", "--repo", repository, "--state", "open", "--limit",
                                           "1000", "--json", "number,body"))
                  if MARK in (i.get("body") or "")), None)
    text = problem(token, now)
    if text is None:
        if issue:
            gh("issue", "close", str(issue["number"]), "--repo", repository,
               "--comment", "The token was renewed; closing.")
            print(f"Closed #{issue['number']}.")
        return
    body = f"{text}\n\n{STEPS}\n\n{MARK}"
    if issue:
        gh("issue", "edit", str(issue["number"]), "--repo", repository, "--body", body)
        print(f"Updated #{issue['number']}.")
    else:
        gh("issue", "create", "--repo", repository, "--title", TITLE, "--body", body)
        print(f"Opened: {TITLE}")


if __name__ == "__main__":
    try:
        watch(os.environ.get("GITHUB_REPOSITORY", "MainstreamOS/packages"), os.environ.get("FORK_TOKEN", ""),
              datetime.now(timezone.utc))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"::error::token watch failed: {error}")
