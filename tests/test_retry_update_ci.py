import copy
import http.client
import io
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from scripts import retry_update_ci as retry


REPOSITORY = "futuping/nix-packages"
BASE = "/repos/futuping/nix-packages"
RUN_ID = 43906199981
RUN_PATH = f"{BASE}/actions/runs/{RUN_ID}"


def run_fixture(attempt=1):
    return {
        "id": RUN_ID,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "path": ".github/workflows/update-packages.yml",
        "head_branch": "main",
        "head_sha": "d37a283" + "0" * 33,
        "event": "schedule",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": attempt,
    }


def jobs_fixture(attempt=1):
    jobs = []
    for identifier, name in enumerate((
        "Update binary packages on Apple silicon",
        "Update Neomacs after native validation",
        "Check ToDesk stable release for manual import",
    ), 1):
        capacity = identifier < 3
        jobs.append({
            "id": identifier,
            "run_id": RUN_ID,
            "run_attempt": attempt,
            "name": name,
            "status": "completed",
            "conclusion": "cancelled" if capacity else "success",
            "runner_id": 0 if capacity else 100,
            "check_run_url": f"https://api.github.com{BASE}/check-runs/{identifier}",
            "steps": [] if capacity else [
                {"name": "Check stable release", "conclusion": "success"}
            ],
        })
    return jobs


# These are the GitHub annotations on the unallocated Nov 17 native jobs.
CAPACITY_ANNOTATIONS = [
    {
        "annotation_level": "failure",
        "message": "The job was not acquired by Runner of type hosted even after multiple attempts",
    },
    {
        "annotation_level": "notice",
        "message": "Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times.",
    },
]


class RetryDecisionTests(unittest.TestCase):
    def make_client(self, *, run=None, jobs=None, refreshed=None, annotations=None, total=None):
        client = Mock()
        run = run_fixture() if run is None else run
        jobs = jobs_fixture(run["run_attempt"]) if jobs is None else jobs
        run_reads = 0

        def get_json(path):
            nonlocal run_reads
            if path == RUN_PATH:
                run_reads += 1
                return copy.deepcopy(refreshed if run_reads > 1 and refreshed is not None else run)
            if path == RUN_PATH + f"/attempts/{run['run_attempt']}/jobs?per_page=100":
                return {"total_count": len(jobs) if total is None else total, "jobs": copy.deepcopy(jobs)}
            if path in [f"{BASE}/check-runs/{i}/annotations?per_page=100" for i in (1, 2)]:
                return copy.deepcopy(CAPACITY_ANNOTATIONS if annotations is None else annotations)
            self.fail(f"Unexpected API read: {path}")

        client.get_json.side_effect = get_json
        return client

    def decide(self, client, *, attempt=1, apply=False):
        return retry.retry_run(client, REPOSITORY, RUN_ID, attempt, apply=apply)

    def test_both_native_capacity_failures_are_read_only_by_default(self):
        client = self.make_client()
        result = self.decide(client)
        self.assertTrue(result["retry"])
        self.assertFalse(result["applied"])
        self.assertEqual([job["id"] for job in result["failed_jobs"]], [1, 2])
        client.rerun_failed_jobs.assert_not_called()

    def test_only_neomacs_capacity_with_other_jobs_successful_is_allowed(self):
        jobs = jobs_fixture()
        jobs[0].update(conclusion="success", runner_id=100, steps=[
            {"name": "Native validation", "conclusion": "success"}
        ])
        result = self.decide(self.make_client(jobs=jobs))
        self.assertTrue(result["retry"])
        self.assertEqual(result["failed_jobs"], [
            {"id": 2, "name": "Update Neomacs after native validation"}
        ])

    def test_second_attempt_requests_one_failed_jobs_rerun(self):
        client = self.make_client(run=run_fixture(2))
        result = self.decide(client, attempt=2, apply=True)
        self.assertTrue(result["applied"])
        client.rerun_failed_jobs.assert_called_once_with(RUN_PATH + "/rerun-failed-jobs")
        self.assertIn(
            ((RUN_PATH + "/attempts/2/jobs?per_page=100",),),
            [tuple(call)[:1] for call in client.get_json.call_args_list],
        )

    def test_one_capacity_and_one_executed_failure_prevents_any_retry(self):
        for failed_index in (0, 1):
            with self.subTest(failed_index=failed_index):
                jobs = jobs_fixture()
                jobs[failed_index].update(conclusion="failure", runner_id=100, steps=[
                    {"name": "Verify application signature", "conclusion": "failure"}
                ])
                client = self.make_client(jobs=jobs)
                self.assertFalse(self.decide(client, apply=True)["retry"])
                client.rerun_failed_jobs.assert_not_called()

    def test_todesk_failure_is_never_retried(self):
        for conclusion in ("failure", "cancelled"):
            jobs = jobs_fixture()
            jobs[2].update(conclusion=conclusion, runner_id=0, steps=[])
            client = self.make_client(jobs=jobs)
            self.assertFalse(self.decide(client, apply=True)["retry"])
            client.rerun_failed_jobs.assert_not_called()

    def test_refuses_incomplete_inventory_including_missing_todesk(self):
        for omitted in (0, 1, 2):
            for total in (2, 3):
                with self.subTest(omitted=omitted, total=total):
                    jobs = jobs_fixture()
                    jobs.pop(omitted)
                    client = self.make_client(jobs=jobs, total=total)
                    self.assertFalse(self.decide(client, apply=True)["retry"])
                    client.rerun_failed_jobs.assert_not_called()

    def test_rejects_unknown_or_duplicate_jobs(self):
        for change in ("unknown", "duplicate_name", "duplicate_id", "extra"):
            jobs = jobs_fixture()
            if change == "unknown":
                jobs[2]["name"] = "Unreviewed job"
            elif change == "duplicate_name":
                jobs[2]["name"] = jobs[0]["name"]
            elif change == "duplicate_id":
                jobs[1]["id"] = jobs[0]["id"]
            else:
                jobs.append(copy.deepcopy(jobs[0]))
            self.assertFalse(self.decide(self.make_client(jobs=jobs))["retry"])

    def test_rejects_skipped_unknown_and_incomplete_job_states(self):
        for changes in (
            {"conclusion": "skipped"},
            {"conclusion": "timed_out"},
            {"conclusion": "neutral"},
            {"conclusion": None},
            {"status": "queued"},
            {"status": "in_progress"},
            {"run_attempt": 2},
            {"run_id": 123},
            {"runner_id": 100},
            {"steps": [{"name": "Set up job", "conclusion": "success"}]},
        ):
            with self.subTest(changes=changes):
                jobs = jobs_fixture()
                jobs[0].update(changes)
                self.assertFalse(self.decide(self.make_client(jobs=jobs))["retry"])

    def test_all_successful_jobs_do_not_request_retry(self):
        jobs = jobs_fixture()
        for job in jobs:
            job["conclusion"] = "success"
        self.assertFalse(self.decide(self.make_client(jobs=jobs))["retry"])

    def test_capacity_requires_both_exact_annotations_and_no_other_failure(self):
        for annotations in (
            [], CAPACITY_ANNOTATIONS[:1], CAPACITY_ANNOTATIONS[1:],
            CAPACITY_ANNOTATIONS + [{"annotation_level": "failure", "message": "Policy rejected"}],
            [{"annotation_level": "failure", "message": "Runner unexpectedly disconnected"},
             CAPACITY_ANNOTATIONS[1]],
        ):
            with self.subTest(annotations=annotations):
                client = self.make_client(annotations=annotations)
                self.assertFalse(self.decide(client, apply=True)["retry"])
                client.rerun_failed_jobs.assert_not_called()

    def test_refuses_unverified_annotation_endpoint(self):
        for url in (
            "https://evil.example/check-runs/1",
            "https://api.github.com/repos/other/repo/check-runs/1",
            "https://api.github.com" + BASE + "/check-runs/1?redirect=other",
            None,
        ):
            jobs = jobs_fixture()
            jobs[0]["check_run_url"] = url
            with self.assertRaises(retry.RetryError):
                self.decide(self.make_client(jobs=jobs))

    def test_refuses_truncated_annotation_page(self):
        with self.assertRaisesRegex(retry.RetryError, "Incomplete"):
            self.decide(self.make_client(annotations=CAPACITY_ANNOTATIONS * 50))

    def test_rejects_wrong_run_source_branch_path_and_event(self):
        for field, value in (
            ("repository", {"full_name": "outsider/repository"}),
            ("head_repository", {"full_name": "outsider/repository"}),
            ("head_repository", None),
            ("head_branch", "unreviewed"),
            ("path", ".github/workflows/check-flake.yml"),
            ("event", "pull_request"),
            ("event", "push"),
            ("event", "workflow_run"),
            ("id", 123),
        ):
            with self.subTest(field=field, value=value):
                run = run_fixture()
                run[field] = value
                client = self.make_client(run=run)
                self.assertFalse(self.decide(client, apply=True)["retry"])
                client.get_json.assert_called_once_with(RUN_PATH)
                client.rerun_failed_jobs.assert_not_called()

    def test_accepts_manual_dispatch_cancelled_capacity_run(self):
        run = run_fixture()
        run.update(event="workflow_dispatch", conclusion="cancelled")
        self.assertTrue(self.decide(self.make_client(run=run))["retry"])

    def test_three_total_attempt_budget_and_stale_event(self):
        for current_attempt, event_attempt in ((3, 3), (4, 4), (2, 1)):
            client = self.make_client(run=run_fixture(current_attempt))
            self.assertFalse(self.decide(client, attempt=event_attempt, apply=True)["retry"])
            client.get_json.assert_called_once_with(RUN_PATH)
            client.rerun_failed_jobs.assert_not_called()

    def test_rejects_active_or_successful_run(self):
        for changes in (
            {"status": "in_progress", "conclusion": None},
            {"status": "queued", "conclusion": None},
            {"conclusion": "success"},
            {"conclusion": "timed_out"},
        ):
            run = run_fixture()
            run.update(changes)
            self.assertFalse(self.decide(self.make_client(run=run))["retry"])

    def test_refresh_before_post_rejects_manual_rerun_or_source_change(self):
        for changes in (
            {"run_attempt": 2},
            {"run_attempt": 3},
            {"status": "in_progress"},
            {"head_sha": "a" * 40},
            {"conclusion": "success"},
            {"head_branch": "other"},
            {"repository": {"full_name": "other/repo"}},
        ):
            with self.subTest(changes=changes):
                refreshed = run_fixture()
                refreshed.update(changes)
                client = self.make_client(refreshed=refreshed)
                self.assertFalse(self.decide(client, apply=True)["applied"])
                client.rerun_failed_jobs.assert_not_called()

    def test_invalid_arguments_do_not_make_requests(self):
        for repository, run_id, attempt in (
            ("owner/repo/../../bad", RUN_ID, 1),
            ("owner/repo?query=x", RUN_ID, 1),
            (REPOSITORY, 0, 1),
            (REPOSITORY, RUN_ID, 0),
        ):
            client = Mock()
            with self.assertRaises(retry.RetryError):
                retry.retry_run(client, repository, run_id, attempt)
            client.get_json.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_redirects_are_rejected_without_forwarding_authorization(self):
        request = urllib.request.Request(
            "https://api.github.com" + RUN_PATH,
            headers={"Authorization": "Bearer secret"},
        )
        for destination in (
            "https://evil.example/steal?secret=value",
            "http://api.github.com/insecure",
            "https://api.github.com/other",
            "https://bad.example:invalid/?secret=value",
        ):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(retry.RetryError, "^GitHub API redirects are not allowed$"):
                    retry.NoRedirect().redirect_request(
                        request, None, 302, "Found", {}, destination
                    )

    @patch("scripts.retry_update_ci.urllib.request.build_opener")
    def test_get_uses_fixed_api_host_timeout_and_bounded_json(self, build):
        response = build.return_value.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"id": 1}'
        self.assertEqual(retry.GitHubClient("token").get_json(RUN_PATH), {"id": 1})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com" + RUN_PATH)
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 30)
        self.assertIsInstance(build.call_args.args[0], retry.NoRedirect)
        response.read.assert_called_once_with(retry.MAX_JSON_BYTES + 1)

    @patch("scripts.retry_update_ci.urllib.request.build_opener")
    def test_http_failure_is_redacted_and_post_is_never_retried(self, build):
        error = urllib.error.HTTPError(
            "https://api.github.com/secret-url", 503, "token=secret", {}, io.BytesIO()
        )
        build.return_value.open.side_effect = error
        with self.assertRaisesRegex(retry.RetryError, r"^GitHub request failed \(HTTP 503\)$"):
            retry.GitHubClient("token").rerun_failed_jobs(RUN_PATH + "/rerun-failed-jobs")
        build.return_value.open.assert_called_once()
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"{}")

    @patch("scripts.retry_update_ci.urllib.request.build_opener")
    def test_transport_error_details_are_not_exposed(self, build):
        for error in (
            urllib.error.URLError("url?token=secret"),
            http.client.InvalidURL("url?token=secret"),
            http.client.IncompleteRead(b"secret"),
            ValueError("token=secret"),
            TimeoutError("token=secret"),
        ):
            build.return_value.open.side_effect = error
            with self.assertRaises(retry.RetryError) as caught:
                retry.GitHubClient("token").get_json(RUN_PATH)
            self.assertNotIn("secret", str(caught.exception))

    @patch("scripts.retry_update_ci.urllib.request.build_opener")
    def test_oversized_response_is_rejected(self, build):
        response = build.return_value.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b"x" * 17
        with patch.object(retry, "MAX_JSON_BYTES", 16):
            with self.assertRaisesRegex(retry.RetryError, "size limit"):
                retry.GitHubClient("token").get_json(RUN_PATH)
        response.read.assert_called_once_with(17)

    def test_malformed_json_is_an_error(self):
        client = retry.GitHubClient("token")
        with patch.object(client, "request", return_value=b"not JSON"):
            with self.assertRaisesRegex(retry.RetryError, "invalid JSON"):
                client.get_json(RUN_PATH)

    def test_empty_or_invalid_token_is_rejected(self):
        for token in ("", "token\rheader", "token\nheader"):
            with self.assertRaises(retry.RetryError):
                retry.GitHubClient(token)


if __name__ == "__main__":
    unittest.main()
