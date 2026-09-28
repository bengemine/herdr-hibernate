import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "herdr-hibernate")
LOADER = importlib.machinery.SourceFileLoader("herdr_hibernate_module", SCRIPT)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
hibernate = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(hibernate)


class HibernateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = self.tempdir.name
        self.paths = mock.patch.multiple(
            hibernate,
            CONFIG_DIR=root,
            STATE_FILE=os.path.join(root, "state.json"),
            LOG_FILE=os.path.join(root, "hibernate.log"),
            PANES_DIR=os.path.join(root, "panes"),
            PID_FILE=os.path.join(root, "watch.pid"),
            WATCH_LOCK_FILE=os.path.join(root, "watch.lock"),
            OPERATION_LOCK_FILE=os.path.join(root, "operation.lock"),
            UPDATE_CHECK_FILE=os.path.join(root, "update-check"),
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.tempdir.cleanup()

    def test_file_lock_allows_only_one_owner(self):
        with hibernate.file_lock(hibernate.WATCH_LOCK_FILE, blocking=False) as first:
            self.assertTrue(first)
            with hibernate.file_lock(
                    hibernate.WATCH_LOCK_FILE, blocking=False) as second:
                self.assertFalse(second)

    def test_wake_restores_exact_owned_marker(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "Review Orchestrator",
            "marker_added": True,
            "marked_label": "💤 Review Orchestrator",
        }
        state = {"w1:p1": rec}
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "💤 Review Orchestrator"}}
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, state)

        self.assertTrue(ok)
        self.assertIn(
            ("tab", "rename", "w1:t1", "Review Orchestrator"), calls)

    def test_legitimate_sleeping_emoji_is_not_removed(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "💤 My own label",
            "marker_added": False,
            "marked_label": None,
        }
        with mock.patch.object(hibernate, "herdr") as herdr:
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, {"w1:p1": rec})

        self.assertTrue(ok)
        herdr.assert_not_called()

    def test_user_changed_label_is_left_untouched(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "Original",
            "marker_added": True,
            "marked_label": "💤 Original",
        }

        def fake_herdr(*args):
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "User renamed this"}}
            raise AssertionError("rename must not be called")

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, {"w1:p1": rec})

        self.assertTrue(ok)

    def test_multi_pane_tab_keeps_marker_until_last_pane_wakes(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "Shared tab",
            "marker_added": True,
            "marked_label": "💤 Shared tab",
        }
        sibling = dict(rec)
        state = {"w1:p1": rec, "w1:p2": sibling}

        with mock.patch.object(hibernate, "herdr") as herdr:
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, state)

        self.assertTrue(ok)
        herdr.assert_not_called()

    def test_rename_failure_keeps_record_for_retry(self):
        rec = {
            "uuid": "90141d62-7130-4dd0-8083-211884e8e999",
            "agent": "claude",
            "tab_id": "w1:t1",
            "label": "Original",
            "marker_added": True,
            "marked_label": "💤 Original",
        }
        state = {"w1:p1": rec}
        pane = {
            "pane_id": "w1:p1",
            "tab_id": "w1:t1",
            "agent": "claude",
        }

        def fake_herdr(*args):
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "💤 Original"}}
            if args[:2] == ("tab", "rename"):
                raise RuntimeError("temporary failure")
            raise AssertionError("unexpected Herdr call: %r" % (args,))

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr), \
                mock.patch.object(hibernate, "transcript_age_minutes",
                                  return_value=1), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value={}), \
                mock.patch.object(hibernate, "gc_stub_files"), \
                mock.patch.object(hibernate, "log"):
            hibernate.restore_records(
                [pane], {"w1:t1": "💤 Original"}, state)

        self.assertIn("w1:p1", state)

    def test_second_pane_reuses_one_tab_marker(self):
        existing = {
            "tab_id": "w1:t1",
            "label": "Shared tab",
            "marker_added": True,
            "marked_label": "💤 Shared tab",
        }
        result = hibernate.marker_record(
            "💤 Shared tab", "w1:t1", {"w1:p1": existing})
        self.assertEqual(result, ("Shared tab", "💤 Shared tab", True))

    def test_stub_resets_terminal_before_banner_and_on_dismiss(self):
        rec = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "agent": "codex",
            "resume": ["codex", "resume",
                       "11111111-1111-1111-1111-111111111111"],
            "cwd": "/tmp",
            "freed_mb": 100,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        path = hibernate.write_stub_file("w1:p1", rec)
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()

        self.assertIn("stty sane", body)
        self.assertIn("\\033[?1004l", body)
        self.assertIn("_hb_bye() {\n    _hb_reset_terminal", body)
        self.assertIn("export HERDR_HIBERNATE_STUB=1\n"
                      "    _hb_reset_terminal", body)
        self.assertNotIn("\n    clear", body)

    def test_arm_command_resets_terminal_before_starting_stub(self):
        command = hibernate.stub_command("w1:p1")

        self.assertLess(command.index("stty sane"), command.index("bash "))
        self.assertIn("\\033[?1004l", command)
        self.assertNotIn("clear", command)


class AgentPanelTests(unittest.TestCase):
    """A hibernated pane stays in Herdr's agent panel, labelled hibernated."""

    setUp = HibernateTests.setUp
    tearDown = HibernateTests.tearDown

    SID = "11111111-1111-1111-1111-111111111111"

    def _run_stub(self, env_extra=None):
        """Run a real stub with a fake tool and a resume that reports its env."""
        root = self.tempdir.name
        tool = os.path.join(root, "fake-tool")
        seen = os.path.join(root, "seen")
        with open(tool, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "tool=${HERDR_AGENT-unset}" >> %s\n'
                     % seen)
        os.chmod(tool, 0o755)
        rec = {
            "uuid": self.SID, "agent": "codex", "cwd": root, "freed_mb": 1,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "resume": ["sh", "-c",
                       'echo "resume=${HERDR_AGENT-unset} '
                       'hint=${_HB_AGENT_HINT-unset}" >> %s' % seen],
        }
        with mock.patch.object(hibernate, "SELF", tool):
            path = hibernate.write_stub_file("w1:p1", rec)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HERDR_AGENT", "_HB_AGENT_HINT")}
        env.update(env_extra or {})
        subprocess.run(["bash", path], input="\n", text=True, env=env,
                       capture_output=True, timeout=30, check=True)
        with open(seen, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_stub_runs_as_the_agent_and_the_resume_does_not(self):
        self.assertEqual(self._run_stub(),
                         ["tool=codex", "resume=unset hint=unset"])

    def test_stub_keeps_an_agent_hint_the_user_set(self):
        self.assertEqual(self._run_stub({"HERDR_AGENT": "codex"}),
                         ["tool=codex", "resume=codex hint=unset"])

    def test_stub_titles_the_pane_with_the_sessions_own_title(self):
        self.assertEqual(hibernate.stub_title(
            {"title": "Fix the parser", "label": "tab"}), "💤 Fix the parser")
        # Records from older versions have no title: the tab label stands in.
        self.assertEqual(hibernate.stub_title({"label": "tab"}), "💤 tab")
        # A title is terminal input: control characters never reach it.
        self.assertEqual(hibernate.stub_title(
            {"title": "a\x1b]0;x\x07b"}), "💤 a]0;xb")

    def test_mark_labels_every_waiting_state_for_the_agent_only(self):
        calls = []
        with mock.patch.object(hibernate, "herdr",
                               side_effect=lambda *a: calls.append(a) or {}):
            hibernate.mark_hibernated("w1:p1", "claude")
        self.assertEqual(calls, [(
            "pane", "report-metadata", "w1:p1",
            "--source", hibernate.PANEL_SOURCE, "--agent", "claude",
            "--state-label", "idle=hibernated",
            "--state-label", "done=hibernated",
            "--state-label", "unknown=hibernated")])

    def test_label_waits_until_herdr_lists_the_stub(self):
        # First the killed agent is still on record, then the stub is listed.
        stub = {"foreground_processes": [
            {"cmdline": "bash %s/w1_p1.sh" % hibernate.STUB_MARKERS[0]}]}
        dead = {"foreground_processes": [{"cmdline": "bash"}]}
        infos = iter([dead, stub])
        calls = []
        with mock.patch.object(hibernate, "herdr",
                               side_effect=lambda *a: calls.append(a) or
                               {"pane": {"agent": "claude"}}), \
                mock.patch.object(hibernate, "pane_process_info",
                                  side_effect=lambda _: next(infos)), \
                mock.patch.object(hibernate.time, "sleep"):
            hibernate.mark_when_listed("w1:p1", "claude")
        marks = [c for c in calls if c[1] == "report-metadata"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(calls[-1], marks[0])

    def test_label_gives_up_quietly_when_the_stub_is_never_listed(self):
        calls = []
        with mock.patch.object(hibernate, "herdr",
                               side_effect=lambda *a: calls.append(a) or
                               {"pane": {}}), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value=None):
            hibernate.mark_when_listed("w1:p1", "claude", timeout=0)
        self.assertNotIn("report-metadata", [c[1] for c in calls])

    def test_panel_labels_never_raise(self):
        with mock.patch.object(hibernate, "herdr",
                               side_effect=RuntimeError("pane gone")):
            hibernate.mark_hibernated("w1:p1", "claude")
            hibernate.clear_hibernated("w1:p1")

    def test_resume_clears_the_label_before_the_agent_starts(self):
        state = {"w1:p1": {"tab_id": "w1:t1", "label": "Tab",
                           "marker_added": False, "marked_label": ""}}
        hibernate.save_state(state)
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            if args[:2] == ("pane", "get"):
                return {"pane": {"tab_id": "w1:t1"}}
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "Tab"}}
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            hibernate.cmd_label_awake("w1:p1")
        self.assertIn(("pane", "report-metadata", "w1:p1",
                       "--source", hibernate.PANEL_SOURCE,
                       "--clear-state-labels"), calls)


class WatcherGuardTests(unittest.TestCase):
    # Every test class must redirect the tool's paths: a test that runs against
    # the real ~/.config/herdr-hibernate wipes real hibernation records.
    setUp = HibernateTests.setUp
    tearDown = HibernateTests.tearDown

    """Guards that keep a watching orchestrator from being hibernated."""

    SID = "11111111-1111-1111-1111-111111111111"

    @staticmethod
    def _cfg(**over):
        cfg = dict(hibernate.DEFAULTS)
        cfg.update(over)
        return cfg

    @staticmethod
    def _write_jsonl(path, age_seconds):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(time.time() - age_seconds))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"timestamp":"%s","type":"assistant"}\n' % stamp)

    def _claude_spec(self, root):
        spec = dict(hibernate.AGENTS["claude"])
        spec["transcript_glob"] = os.path.join(root, "{sid}.jsonl")
        spec["activity_globs"] = (
            os.path.join(root, "{sid}", "subagents", "*.jsonl"),)
        return spec

    def test_subagent_writes_count_as_session_activity(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_jsonl(os.path.join(root, self.SID + ".jsonl"),
                              age_seconds=3 * 3600)  # main stale for 3h
            subdir = os.path.join(root, self.SID, "subagents")
            os.makedirs(subdir)
            self._write_jsonl(os.path.join(subdir, "agent-worker.jsonl"),
                              age_seconds=60)  # worker wrote a minute ago
            with mock.patch.dict(hibernate.AGENTS,
                                 {"claude": self._claude_spec(root)}):
                age = hibernate.transcript_age_minutes(self.SID, "claude")
            self.assertIsNotNone(age)
            self.assertLess(age, 10)

    def test_subagent_files_alone_do_not_make_a_session_resumable(self):
        with tempfile.TemporaryDirectory() as root:
            subdir = os.path.join(root, self.SID, "subagents")
            os.makedirs(subdir)
            self._write_jsonl(os.path.join(subdir, "agent-worker.jsonl"), 60)
            with mock.patch.dict(hibernate.AGENTS,
                                 {"claude": self._claude_spec(root)}):
                age = hibernate.transcript_age_minutes(self.SID, "claude")
            self.assertIsNone(age)  # no main transcript -> never killed anyway

    # -- resume stamp: exec keeps the stub's start time, the stamp corrects it

    def test_resumed_agent_age_uses_the_stub_stamp_not_the_process_clock(self):
        hibernate.write_resume_stamp("w1:p1")
        with mock.patch.object(hibernate, "proc_uptime_minutes",
                               return_value=3 * 24 * 60.0):  # stub waited 3 days
            age = hibernate.agent_age_minutes(4242, "w1:p1")
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)

    def test_agent_age_without_a_stamp_is_the_process_age(self):
        with mock.patch.object(hibernate, "proc_uptime_minutes",
                               return_value=42.0):
            self.assertEqual(hibernate.agent_age_minutes(4242, "w1:p1"), 42.0)

    def test_stale_stamp_loses_to_a_younger_process(self):
        # A stub fired long ago; the user later started a fresh agent by hand.
        os.makedirs(hibernate.PANES_DIR, exist_ok=True)
        with open(hibernate.resume_stamp_path("w1:p1"), "w") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(time.time() - 5 * 3600)))
        with mock.patch.object(hibernate, "proc_uptime_minutes",
                               return_value=7.0):
            self.assertEqual(hibernate.agent_age_minutes(4242, "w1:p1"), 7.0)

    def test_label_awake_writes_the_stamp_even_without_a_record(self):
        hibernate.cmd_label_awake("w1:p1")
        self.assertTrue(os.path.exists(hibernate.resume_stamp_path("w1:p1")))

    def test_stamp_survives_resume_but_not_the_panes_closing(self):
        hibernate.write_resume_stamp("w1:p1")
        hibernate.write_resume_stamp("w1:p2")
        # p1 still open (record already cleared by RESUMED), p2's tab is gone.
        hibernate.gc_stub_files({}, panes=[{"pane_id": "w1:p1"}])
        self.assertTrue(os.path.exists(hibernate.resume_stamp_path("w1:p1")))
        self.assertFalse(os.path.exists(hibernate.resume_stamp_path("w1:p2")))
        hibernate.gc_stub_files({})  # no pane list: stamps are never touched
        self.assertTrue(os.path.exists(hibernate.resume_stamp_path("w1:p1")))

    def test_forget_record_drops_the_stamp(self):
        hibernate.write_resume_stamp("w1:p1")
        hibernate.forget_record("w1:p1", {"w1:p1": {"label": "x"}}, "tab closed")
        self.assertFalse(os.path.exists(hibernate.resume_stamp_path("w1:p1")))

    # -- update check: announce only, at most once per UPDATE_CHECK_HOURS

    def test_update_check_runs_once_per_interval_and_announces(self):
        cfg = self._cfg(UPDATE_CHECK_HOURS="24")
        calls = []

        def fake_git(*args, **kw):
            calls.append(args)
            if args[0] == "fetch":
                return ""
            if args[:2] == ("rev-parse", "--verify"):
                return "abc" if args[3] == "origin/main" else None
            if args[0] == "rev-list":
                return "2"
            if args[0] == "show":
                return 'version = "9.9.9"\n'
            return None

        with mock.patch.object(hibernate, "git", side_effect=fake_git), \
                mock.patch.object(hibernate, "toast") as toast, \
                mock.patch.object(hibernate, "plugin_version",
                                  side_effect=lambda rev=None: "9.9.9" if rev else "1.0.0"):
            hibernate.maybe_check_update(cfg)
            hibernate.maybe_check_update(cfg)  # same day: no second fetch
        self.assertEqual(sum(1 for c in calls if c[0] == "fetch"), 1)
        toast.assert_called_once()
        self.assertIn("9.9.9", toast.call_args[0][0])
        self.assertIn("UPDATE available: 9.9.9", open(hibernate.LOG_FILE).read())

    def test_update_check_disabled_by_zero(self):
        with mock.patch.object(hibernate, "git") as git:
            hibernate.maybe_check_update(self._cfg(UPDATE_CHECK_HOURS="0"))
        git.assert_not_called()
        self.assertFalse(os.path.exists(hibernate.UPDATE_CHECK_FILE))

    def test_unreachable_origin_is_quiet_and_does_not_retry_every_scan(self):
        cfg = self._cfg(UPDATE_CHECK_HOURS="24")
        with mock.patch.object(hibernate, "git", return_value=None) as git, \
                mock.patch.object(hibernate, "toast") as toast:
            hibernate.maybe_check_update(cfg)
            hibernate.maybe_check_update(cfg)
        self.assertEqual(git.call_count, 1)
        toast.assert_not_called()

    def test_late_started_child_is_a_busy_background_job(self):
        procs = {
            100: (1, 0, "claude"),
            101: (100, 0, "bash -c 'herdr wait agent-status w1:p2 --status done'"),
        }
        ups = {100: 120.0, 101: 4.0}  # child began ~116m after the agent
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "claude",
                                                self._cfg())
        self.assertIsNotNone(job)
        self.assertIn("herdr wait", job)

    def test_startup_children_and_mcp_servers_do_not_count(self):
        procs = {
            100: (1, 0, "claude"),
            101: (100, 0, "node /x/mcp-server-figma"),  # late but ignored token
            102: (100, 0, "node /x/some-helper"),       # started with the agent
        }
        ups = {100: 120.0, 101: 4.0, 102: 119.5}
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "claude",
                                                self._cfg())
        self.assertIsNone(job)

    def test_reexeced_agent_binary_is_not_a_background_job(self):
        # codex node wrapper (root) relaunched its vendored TUI 71m in; the
        # TUI carries the session id, its code-mode-host helper follows 26s
        # later. Neither is background work.
        procs = {
            100: (1, 0, "node /x/bin/codex resume %s -c model=y" % self.SID),
            101: (100, 0, "/x/vendor/bin/codex resume %s -c model=y" % self.SID),
            102: (101, 0, "/x/vendor/bin/codex-code-mode-host"),
        }
        ups = {100: 120.0, 101: 49.0, 102: 48.5}
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "codex",
                                                self._cfg())
        self.assertIsNone(job)

    def test_worker_spawned_after_a_reexec_still_counts(self):
        procs = {
            100: (1, 0, "node /x/bin/codex resume %s" % self.SID),
            101: (100, 0, "/x/vendor/bin/codex resume %s" % self.SID),
            102: (101, 0, "codex exec --session other 'review the diff'"),
        }
        ups = {100: 120.0, 101: 49.0, 102: 10.0}  # worker began 39m after re-exec
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "codex",
                                                self._cfg())
        self.assertIsNotNone(job)
        self.assertIn("codex exec", job)

    def test_zero_minutes_disables_the_busy_check(self):
        with mock.patch.object(hibernate, "find_agent_proc") as finder:
            job = hibernate.busy_background_job(
                "w1:p1", self.SID, "claude",
                self._cfg(BUSY_CHILD_MINUTES="0"))
        self.assertIsNone(job)
        finder.assert_not_called()

    def test_classify_skips_a_pane_with_a_busy_background_job(self):
        pane = {
            "pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "claude",
            "agent_status": "idle",
            "agent_session": {"value": self.SID},
        }
        with mock.patch.object(hibernate, "transcript_age_minutes",
                               return_value=999.0), \
                mock.patch.object(hibernate, "busy_background_job",
                                  return_value="herdr wait agent-status w1:p2"):
            decision, reason = hibernate.classify(
                pane, {"w1:t1": "KB Delta orchestrator"}, self._cfg(),
                {}, "w9:p9")
        self.assertEqual(decision, "skip")
        self.assertTrue(reason.startswith("busy:"))


if __name__ == "__main__":
    unittest.main()
