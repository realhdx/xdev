"""Local GitHub REST contract tests. These never contact GitHub or read account credentials."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / "deploy" / "github-mail.py"
spec = importlib.util.spec_from_file_location("xdev_github_setup", source)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def repository(name):
    return {
        "id": 101,
        "full_name": name,
        "default_branch": "main",
        "private": True,
        "visibility": "private",
        "description": setup.DESCRIPTION,
        "fork": False,
        "archived": False,
    }


def release():
    return {
        "id": 207,
        "tag_name": setup.TAG,
        "prerelease": True,
        "draft": False,
        "immutable": False,
        "body": "<!-- xdev.github-mail.v1 repository_id=101 -->",
    }


class API:
    def __init__(self, owner="alice"):
        self.owner = owner
        self.name = owner + "/box"
        self.base = "/repos/" + self.name
        self.calls = []
        self.repo = None
        self.release = None
        self.contents = [{"name": "README.md", "type": "file"}]
        self.existing = []
        self.tags = []
        self.policy = (404, None)
        self.conflict = False

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST":
            if path == self.base + "/releases":
                self.release = {**release(), **payload}
                return (422, None) if self.conflict else (201, self.release)
            if path in ("/user/repos", "/orgs/team/repos"):
                self.repo = repository(self.name)
                return (422, None) if self.conflict else (201, self.repo)
            raise AssertionError(path)
        if path == "/user":
            return 200, {"login": "alice"}
        if path == "/orgs/" + self.owner:
            return (200, {"login": "team"}) if self.owner == "team" else (404, None)
        if path == self.base:
            return (200, self.repo.copy()) if self.repo else (404, None)
        if path == self.base + "/releases/tags/" + setup.TAG:
            return (200, self.release.copy()) if self.release else (404, None)
        if path == self.base + "/contents":
            return 200, self.contents
        if path == self.base + "/commits/main":
            return 200, {"sha": "d" * 40}
        if path == self.base + "/immutable-releases":
            return self.policy
        if path == self.base + "/releases?per_page=1":
            return 200, self.existing
        if path == self.base + "/tags?per_page=1":
            return 200, self.tags
        raise AssertionError(path)

    def writes(self):
        return [(p, b) for m, p, b in self.calls if m != "GET"]


class GitHubSetup(unittest.TestCase):
    def test_personal_and_organisation_create_then_read_only_rerun(self):
        for owner in ("alice", "team"):
            with self.subTest(owner=owner):
                api = API(owner)
                result = setup.setup(api, "init", api.name)
                self.assertEqual((result["repository_id"], result["release_id"]), (101, 207))
                self.assertFalse(result["asset_write_verified"])
                repo, rel = api.writes()
                self.assertEqual(repo[0], "/user/repos" if owner == "alice" else "/orgs/team/repos")
                self.assertEqual(repo[1]["private"], True)
                self.assertEqual(repo[1]["auto_init"], True)
                self.assertEqual(rel[1]["target_commitish"], "d" * 40)
                self.assertEqual(rel[1]["prerelease"], True)
                self.assertEqual(rel[1]["draft"], False)
                for mode in ("doctor", "init"):
                    api.calls.clear()
                    self.assertEqual(setup.setup(api, mode, api.name), result)
                    self.assertEqual(api.writes(), [])

    def test_private_repo_without_markers_or_with_source_is_untouched(self):
        for changes, contents in (
            ({"description": "Research code"}, None),
            ({"private": False}, None),
            ({"visibility": "public"}, None),
            ({"fork": True}, None),
            ({"archived": True}, None),
            ({}, [{"name": "README.md", "type": "file"}, {"name": "src", "type": "dir"}]),
        ):
            with self.subTest(changes=changes, contents=contents):
                api = API()
                api.repo = {**repository(api.name), **changes}
                if contents:
                    api.contents = contents
                with self.assertRaises(setup.SetupError):
                    setup.setup(api, "init", api.name)
                self.assertEqual(api.writes(), [])

    def test_wrong_or_locked_release_and_pinned_ids(self):
        for overrides in (
            {"immutable": True},
            {"immutable": None},
            {"prerelease": False},
            {"draft": True},
            {"tag_name": "other"},
            {"body": "missing"},
            {"id": True},
        ):
            api = API()
            api.repo = repository(api.name)
            api.release = {**release(), **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(setup.SetupError):
                setup.setup(api, "init", api.name)
            self.assertEqual(api.writes(), [])
        api.release = release()
        for ids in ((102, None), (None, 208), (101, 207)):
            api.calls.clear()
            if ids == (101, 207):
                self.assertTrue(setup.setup(api, "doctor", api.name, *ids)["ok"])
            else:
                with self.assertRaises(setup.SetupError):
                    setup.setup(api, "doctor", api.name, *ids)
            self.assertEqual(api.writes(), [])

    def test_doctor_never_creates_or_attaches_to_another_product(self):
        api = API()
        with self.assertRaises(setup.SetupError):
            setup.setup(api, "doctor", api.name)
        api.repo = repository(api.name)
        with self.assertRaises(setup.SetupError):
            setup.setup(api, "doctor", api.name)
        api.existing = [{"id": 151}]
        with self.assertRaises(setup.SetupError):
            setup.setup(api, "init", api.name)
        api.existing, api.tags = [], [{"name": "v1"}]
        with self.assertRaises(setup.SetupError):
            setup.setup(api, "init", api.name)
        self.assertEqual(api.writes(), [])

    def test_creation_race_and_resuming_after_repository_creation(self):
        api = API()
        api.conflict = True
        self.assertEqual(setup.setup(api, "init", api.name)["release_id"], 207)
        api.release = None
        api.calls.clear()
        setup.setup(api, "init", api.name)
        self.assertEqual(len(api.writes()), 1)
        self.assertEqual(api.writes()[0][0], api.base + "/releases")

    def test_immutable_repository_refused_before_release_creation(self):
        api = API()
        api.repo = repository(api.name)
        api.policy = (200, {"enabled": True})
        with self.assertRaises(setup.SetupError):
            setup.setup(api, "init", api.name)
        self.assertEqual(api.writes(), [])

    def test_save_token_creates_private_file_and_refuses_existing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            path = home / "private" / "xdev" / "test.token"
            with (
                patch.object(setup.Path, "home", return_value=home),
                patch.object(setup, "hidden_token", return_value="unitTestToken123") as prompt,
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(setup.main(["save-token", "--token-file", str(path)]), 0, output.getvalue())
                self.assertNotIn("unitTestToken123", output.getvalue())
                self.assertEqual(path.read_text("ascii"), "unitTestToken123")
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                else:
                    setup.windows_access(path, "file")
                with self.assertRaises(setup.SetupError):
                    setup.save_token(str(path))
                self.assertEqual(prompt.call_count, 1)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_save_token_handles_racing_target_cancel_and_git_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            target = home / "private" / "new.token"
            with patch.object(setup.Path, "home", return_value=home):

                def race():
                    target.write_bytes(b"first")
                    return "second"

                with patch.object(setup, "hidden_token", side_effect=race), self.assertRaises(setup.SetupError):
                    setup.save_token(str(target))
                self.assertEqual(target.read_bytes(), b"first")
                cancelled = target.with_name("cancelled.token")
                with (
                    patch.object(setup, "hidden_token", side_effect=setup.SetupError("cancelled")),
                    self.assertRaises(setup.SetupError),
                ):
                    setup.save_token(str(cancelled))
                self.assertFalse(cancelled.exists())
                project = home / "project"
                project.mkdir()
                (project / ".git").mkdir()
                with patch.object(setup, "hidden_token") as prompt, self.assertRaises(setup.SetupError):
                    setup.save_token(str(project / "private" / "new.token"))
                prompt.assert_not_called()

    def test_different_account_and_unchecked_name_do_not_write(self):
        api = API("unrelated")
        for name in (api.name, "-", "alice/..", "alice/box?redirect=yes", "https://else.invalid/foo"):
            with self.subTest(name=name), self.assertRaises(setup.SetupError):
                setup.setup(api, "init", name)
        self.assertEqual(api.writes(), [])

    def test_token_file_and_cli_diagnostics_do_not_disclose_credentials(self):
        secret = "fake_nonsecret_for_test"
        with tempfile.TemporaryDirectory() as folder:
            f = Path(folder) / "cred"
            f.write_text(secret + "\n", encoding="ascii")
            f.chmod(0o600)
            self.assertEqual(setup.credential(str(f)), secret)
            if os.name == "posix":
                f.chmod(0o644)
                with self.assertRaises(setup.SetupError):
                    setup.credential(str(f))
            with self.assertRaises(setup.SetupError):
                setup.credential("relative.txt")
        with (
            patch.object(setup.shutil, "which", return_value="gh"),
            patch.object(
                setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout=secret.encode())
            ) as run,
        ):
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(setup.main(["doctor", "alice/box"]), 1)
            self.assertNotIn(secret, stream.getvalue())
            self.assertNotIn(secret, str(run.call_args))
            self.assertFalse(json.loads(stream.getvalue())["ok"])

    def test_api_host_and_secrets_on_redirect_or_refusal(self):
        with patch.object(setup.http.client, "HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 302
            self.assertEqual(setup.GitHub("credential-only").request("GET", "/repos/alice/box"), (302, None))
            self.assertEqual(connection.call_count, 1)
            self.assertEqual(connection.call_args.args[0], "api.github.com")
            response.read.assert_not_called()
