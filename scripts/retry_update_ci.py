#!/usr/bin/env python3
"""Retry only unallocated macOS update jobs, with three total attempts."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request


API_URL = "https://api.github.com"
WORKFLOW_PATH = ".github/workflows/update-packages.yml"
NATIVE_JOBS = (
    "Update binary packages on Apple silicon",
    "Update Neomacs after native validation",
)
KNOWN_JOBS = NATIVE_JOBS + ("Check ToDesk stable release for manual import",)
CAPACITY_FAILURE = (
    "The job was not acquired by Runner of type hosted even after multiple attempts"
)
CAPACITY_NOTICE = (
    "Due to capacity constraints, jobs targeting macOS arm64 runners may "
    "experience longer queue times."
)
MAX_JSON_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 30
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class RetryError(RuntimeError):
    """A request or response could not be verified safely."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # These JSON endpoints need no redirects, and the token must never be
        # forwarded to another host. Do not include the target in the error.
        raise RetryError("GitHub API redirects are not allowed")


class GitHubClient:
    def __init__(self, token: str):
        if not token or "\n" in token or "\r" in token:
            raise RetryError("A valid GITHUB_TOKEN is required")
        self.token = token

    def request(self, path: str, *, post: bool = False) -> bytes:
        if not path.startswith("/repos/") or ".." in path or "#" in path:
            raise RetryError("Refusing an unexpected GitHub API path")
        request = urllib.request.Request(
            API_URL + path,
            data=b"{}" if post else None,
            method="POST" if post else "GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "User-Agent": "nix-packages-ci-retry",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                if response.status != (201 if post else 200):
                    raise RetryError("Unexpected GitHub response status")
                body = response.read(MAX_JSON_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.close()
            raise RetryError("GitHub request failed (HTTP %s)" % error.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            # Exception details may contain URLs, response data or credentials.
            raise RetryError(
                "GitHub request failed (%s)" % type(error).__name__
            ) from None
        if len(body) > MAX_JSON_BYTES:
            raise RetryError("GitHub response exceeded the size limit")
        return body

    def get_json(self, path: str):
        try:
            return json.loads(self.request(path))
        except (ValueError, UnicodeError):
            raise RetryError("GitHub returned invalid JSON") from None

    def rerun_failed_jobs(self, path: str) -> None:
        # Never retry POST: a lost response does not prove the request failed.
        self.request(path, post=True)


def run_rejection(run: dict, repository: str, run_id: int, attempt: int) -> str:
    if not isinstance(run, dict):
        raise RetryError("GitHub returned an invalid workflow run")
    if (
        run.get("id") != run_id
        or not isinstance(run.get("repository"), dict)
        or run["repository"].get("full_name") != repository
        or not isinstance(run.get("head_repository"), dict)
        or run["head_repository"].get("full_name") != repository
        or run.get("path") != WORKFLOW_PATH
        or run.get("head_branch") != "main"
        or run.get("event") not in ("schedule", "workflow_dispatch")
    ):
        return "run source is outside the approved update workflow"
    if run.get("run_attempt") != attempt:
        return "run attempt has changed"
    if attempt >= 3:
        return "three-attempt budget exhausted"
    if run.get("status") != "completed" or run.get("conclusion") not in (
        "failure", "cancelled"
    ):
        return "run is not a completed failure"
    return ""


def has_capacity_annotations(client: GitHubClient, repository: str, job: dict) -> bool:
    check_url = job.get("check_run_url")
    if not isinstance(check_url, str):
        raise RetryError("Job has no verified check-run URL")
    base = "/repos/" + repository
    match = re.fullmatch(
        re.escape(API_URL + base + "/check-runs/") + r"([0-9]+)", check_url
    )
    if not match:
        raise RetryError("Job has no verified check-run URL")
    annotations = client.get_json(
        f"{base}/check-runs/{match[1]}/annotations?per_page=100"
    )
    if (
        not isinstance(annotations, list)
        or len(annotations) >= 100
        or any(not isinstance(annotation, dict) for annotation in annotations)
    ):
        raise RetryError("Incomplete or invalid check annotations")
    failures = [
        annotation.get("message") for annotation in annotations
        if annotation.get("annotation_level") == "failure"
    ]
    notices = [
        annotation.get("message") for annotation in annotations
        if annotation.get("annotation_level") == "notice"
    ]
    return bool(
        failures
        and all(message == CAPACITY_FAILURE for message in failures)
        and CAPACITY_NOTICE in notices
    )


def capacity_jobs(
    client: GitHubClient, repository: str, run_id: int, attempt: int, response: dict
) -> list:
    if not isinstance(response, dict) or not isinstance(response.get("jobs"), list):
        raise RetryError("GitHub returned an invalid jobs response")
    jobs = response["jobs"]
    # The rerun endpoint covers every failed job, so require the complete
    # workflow inventory, including ToDesk. A subset cannot prove that no
    # unchecked application failure would be retried with the capacity jobs.
    if response.get("total_count") != len(KNOWN_JOBS) or len(jobs) != len(KNOWN_JOBS):
        return []
    names = set()
    identifiers = set()
    failed = []
    for job in jobs:
        if not isinstance(job, dict):
            raise RetryError("GitHub returned an invalid workflow job")
        name = job.get("name")
        identifier = job.get("id")
        if (
            name not in KNOWN_JOBS
            or name in names
            or type(identifier) is not int
            or identifier < 1
            or identifier in identifiers
            or job.get("run_id") != run_id
            or job.get("run_attempt") != attempt
            or job.get("status") != "completed"
        ):
            return []
        names.add(name)
        identifiers.add(identifier)
        if job.get("conclusion") == "success":
            continue
        if (
            name not in NATIVE_JOBS
            or job.get("conclusion") not in ("failure", "cancelled")
            or job.get("runner_id") != 0
            or job.get("steps") != []
        ):
            return []
        failed.append(job)
    if not failed or not all(
        has_capacity_annotations(client, repository, job) for job in failed
    ):
        return []
    return [{"id": job["id"], "name": job["name"]} for job in failed]


def retry_run(
    client: GitHubClient, repository: str, run_id: int, attempt: int, *, apply: bool = False
) -> dict:
    if not REPOSITORY_PATTERN.fullmatch(repository) or ".." in repository:
        raise RetryError("Invalid repository name")
    if run_id < 1 or attempt < 1:
        raise RetryError("Run ID and attempt must be positive integers")
    path = f"/repos/{repository}/actions/runs/{run_id}"
    run = client.get_json(path)
    rejection = run_rejection(run, repository, run_id, attempt)
    decision = {
        "run_id": run_id, "attempt": attempt, "retry": False,
        "applied": False, "failed_jobs": [],
    }
    if rejection:
        return {**decision, "reason": rejection}
    jobs = client.get_json(f"{path}/attempts/{attempt}/jobs?per_page=100")
    failed = capacity_jobs(client, repository, run_id, attempt, jobs)
    if not failed:
        return {**decision, "reason": "jobs are outside the runner-capacity allowlist"}
    if apply:
        current = client.get_json(path)
        rejection = run_rejection(current, repository, run_id, attempt)
        if rejection or any(
            current.get(key) != run.get(key)
            for key in ("head_sha", "status", "conclusion", "run_attempt")
        ):
            return {**decision, "reason": "run changed before retry; no request sent"}
        client.rerun_failed_jobs(path + "/rerun-failed-jobs")
    return {
        **decision, "retry": True, "applied": apply, "failed_jobs": failed,
        "reason": "unallocated macOS arm64 runners with confirmed capacity failures",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = retry_run(
            GitHubClient(os.environ.get("GITHUB_TOKEN", "")),
            args.repository, args.run_id, args.attempt, apply=args.apply,
        )
    except RetryError as error:
        print("CI retry failed: %s" % error, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
