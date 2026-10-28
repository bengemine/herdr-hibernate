import importlib.machinery
import importlib.util
import os
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
            CHATS_FILE=os.path.join(root, "chats.json"),
            SETTLED_FILE=os.path.join(root, "settled.json"),
            SETTLED_LOCK_FILE=os.path.join(root, "settled.lock"),
            SETTLED_VIEW_FILE=os.path.join(root, "settled-view.json"),
            KEPT_FILE=os.path.join(root, "kept.json"),
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
            "uuid": "00000000-0000-4000-8000-000000000001",
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

    def test_stub_lists_itself_before_waiting_for_enter(self):
        rec = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "agent": "claude", "cwd": "/tmp", "freed_mb": 100,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(hibernate.write_stub_file("w1:p1", rec),
                  encoding="utf-8") as fh:
            body = fh.read()
        mark = body.index("mark-asleep")
        self.assertLess(mark, body.index("IFS= read"))
        self.assertFalse(body[mark:body.index("\n", mark)].endswith("&"))

    def test_arm_command_resets_terminal_before_starting_stub(self):
        command = hibernate.stub_command("w1:p1")

        self.assertLess(command.index("stty sane"), command.index("bash "))
        self.assertIn("\\033[?1004l", command)
        self.assertNotIn("clear", command)


class SleepListTests(unittest.TestCase):
    """Hibernated panes stay in Herdr's agent sidebar until their tab closes."""
    setUp = HibernateTests.setUp
    tearDown = HibernateTests.tearDown

    REC = {
        "uuid": "11111111-1111-1111-1111-111111111111", "agent": "claude",
        "tab_id": "w1:t1", "label": "Review", "cwd": "/tmp",
        "marker_added": True, "marked_label": "💤 Review",
        "freed_mb": 700, "at": "2026-01-01 00:00:00",
    }
    BARE = {"foreground_processes": [{"name": "bash", "argv": ["bash"],
                                      "pid": 5}], "shell_pid": 5}

    @staticmethod
    def _stub():
        # STUB_MARKERS is fixed at import, so match its legacy arg0 marker.
        return {"foreground_processes": [{"cmdline": "hibernate-stub w1:p1"}]}

    def _restore(self, pane, info, age, stale_days=2.0):
        state = {"w1:p1": dict(self.REC)}
        with mock.patch.object(hibernate, "herdr") as herdr, \
                mock.patch.object(hibernate, "transcript_age_minutes",
                                  return_value=age), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value=info), \
                mock.patch.object(hibernate, "gc_stub_files"), \
                mock.patch.object(hibernate, "log"):
            hibernate.restore_records([pane], {"w1:t1": "💤 Review"}, state,
                                      stale_days=stale_days)
        return state, [c.args for c in herdr.call_args_list]

    def test_live_stub_is_listed_with_its_age(self):
        pane = {"pane_id": "w1:p1", "tab_id": "w1:t1"}
        _, calls = self._restore(pane, self._stub(), age=90)
        self.assertIn(("pane", "report-agent", "w1:p1", "--source",
                       hibernate.SLEEP_SOURCE, "--agent", hibernate.SLEEP_AGENT,
                       "--state", "unknown"), calls)
        self.assertIn("claude 1h30m", calls[-1])

    def test_old_chat_gets_the_stale_mark(self):
        pane = {"pane_id": "w1:p1", "tab_id": "w1:t1",
                "agent": hibernate.SLEEP_AGENT}
        _, calls = self._restore(pane, self._stub(), age=3 * 1440)
        self.assertNotIn("report-agent", [c[1] for c in calls])  # already listed
        self.assertIn("🕸 claude 3d00h", calls[-1])

    def test_zero_stale_days_never_marks(self):
        pane = {"pane_id": "w1:p1", "tab_id": "w1:t1"}
        _, calls = self._restore(pane, self._stub(), age=30 * 1440, stale_days=0)
        self.assertEqual(calls[-1][-1], "claude 30d00h")

    def test_sleep_report_is_not_mistaken_for_a_new_agent(self):
        # Stub dismissed with Ctrl-C: the pane still carries our own report.
        # It must be re-armed, never forgotten as "reused by another agent".
        pane = {"pane_id": "w1:p1", "tab_id": "w1:t1",
                "agent": hibernate.SLEEP_AGENT}
        state, calls = self._restore(pane, self.BARE, age=90)
        self.assertIn("w1:p1", state)
        verbs = [c[1] for c in calls]
        self.assertLess(verbs.index("release-agent"), verbs.index("run"))
        self.assertIn("report-agent", verbs[verbs.index("run"):])

    def test_leftover_report_without_a_record_is_released(self):
        pane = {"pane_id": "w1:p9", "tab_id": "w1:t9",
                "agent": hibernate.SLEEP_AGENT}
        with mock.patch.object(hibernate, "herdr") as herdr, \
                mock.patch.object(hibernate, "gc_stub_files"):
            hibernate.restore_records([pane], {}, {})
        self.assertEqual(herdr.call_args_list[0].args[:3],
                         ("pane", "release-agent", "w1:p9"))

    def test_classify_treats_a_sleep_report_as_no_agent(self):
        pane = {"pane_id": "w1:p9", "agent": hibernate.SLEEP_AGENT}
        decision, _ = hibernate.classify(pane, {}, dict(hibernate.DEFAULTS),
                                         {}, "")
        self.assertEqual(decision, "skip")


class SettledTests(unittest.TestCase):
    """Closing a tab moves its chat to the settled list the F7 popup shows."""
    setUp = HibernateTests.setUp
    tearDown = HibernateTests.tearDown

    SID = "11111111-1111-1111-1111-111111111111"
    SID2 = "22222222-2222-2222-2222-222222222222"

    def _live(self, pane_id, tab_id, sid, agent="claude"):
        return {"pane_id": pane_id, "tab_id": tab_id, "agent": agent,
                "cwd": "/work", "agent_session": {"value": sid}}

    def _settled(self):
        return hibernate.load_settled(90)

    def test_closing_a_tab_settles_its_remembered_chat(self):
        hibernate.remember_chats([self._live("w1:p1", "w1:t1", self.SID)],
                                 {"w1:t1": "Review PR"})
        hibernate.settle_closed("tab.closed", {"tab_id": "w1:t1"}, 90)
        [entry] = self._settled()
        self.assertEqual((entry["label"], entry["uuid"], entry["workspace_id"]),
                         ("Review PR", self.SID, "w1"))
        self.assertEqual(entry["resume"], ["claude", "--resume", self.SID])
        self.assertEqual(hibernate.load_json(hibernate.CHATS_FILE, None), {})

    def test_hibernated_chat_settles_from_its_record(self):
        hibernate.save_state({"w1:p2": {
            "uuid": self.SID, "agent": "codex", "tab_id": "w1:t2",
            "label": "Codex task", "cwd": "/work",
            "resume": ["codex", "resume", self.SID, "-m", "x"]}})
        hibernate.settle_closed("pane.closed", {"pane_id": "w1:p2"}, 90)
        [entry] = self._settled()
        self.assertEqual(entry["label"], "Codex task")
        self.assertEqual(entry["resume"][-2:], ["-m", "x"])

    def test_closing_a_workspace_settles_every_chat_in_it(self):
        hibernate.remember_chats(
            [self._live("w1:p1", "w1:t1", self.SID),
             self._live("w2:p1", "w2:t1", self.SID2)], {})
        hibernate.settle_closed("workspace.closed", {"workspace_id": "w1"}, 90)
        self.assertEqual([e["uuid"] for e in self._settled()], [self.SID])

    def test_leftover_record_does_not_settle_twice(self):
        hibernate.save_state({"w1:p2": {"uuid": self.SID, "tab_id": "w1:t2",
                                         "label": "x"}})
        hibernate.settle_closed("tab.closed", {"tab_id": "w1:t2"}, 90)
        first = self._settled()[0]["at"]
        with mock.patch.object(hibernate.time, "strftime",
                               return_value="2099-01-01 00:00:00"):
            hibernate.settle_closed("workspace.closed", {"workspace_id": "w1"}, 90)
        self.assertEqual([e["at"] for e in self._settled()], [first])

    def test_remember_skips_panes_it_could_not_resume(self):
        hibernate.remember_chats([
            self._live("w1:p1", "w1:t1", "not-a-uuid"),
            self._live("w1:p2", "w1:t2", self.SID, agent="hibernated"),
            self._live("w1:p3", "w1:t3", self.SID, agent="unknown-agent"),
        ], {})
        self.assertEqual(hibernate.load_json(hibernate.CHATS_FILE, None), {})

    def test_old_entries_drop_off_the_list(self):
        hibernate.save_json(hibernate.SETTLED_FILE, [
            {"uuid": self.SID, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            {"uuid": self.SID2, "at": "2000-01-01 00:00:00"}])
        self.assertEqual([e["uuid"] for e in self._settled()], [self.SID])

    def test_last_reply_reads_claude_and_codex_transcripts(self):
        root = self.tempdir.name
        claude = os.path.join(root, self.SID + ".jsonl")
        with open(claude, "w", encoding="utf-8") as fh:
            fh.write('{"type":"assistant","message":{"role":"assistant",'
                     '"content":[{"type":"text","text":"All  done.\\nPR posted."}]}}\n'
                     '{"type":"assistant","message":{"role":"assistant",'
                     '"content":[{"type":"tool_use","name":"Bash"}]}}\n')
        codex = os.path.join(root, "rollout-%s.jsonl" % self.SID2)
        with open(codex, "w", encoding="utf-8") as fh:
            fh.write('{"payload":{"type":"message","role":"assistant",'
                     '"content":[{"type":"output_text","text":"Fixed it."}]}}\n')
        agents = {"claude": dict(hibernate.AGENTS["claude"],
                                 transcript_glob=os.path.join(root, "{sid}.jsonl")),
                  "codex": dict(hibernate.AGENTS["codex"],
                                transcript_glob=os.path.join(root, "rollout-{sid}.jsonl"))}
        with mock.patch.dict(hibernate.AGENTS, agents):
            self.assertEqual(hibernate.last_reply(self.SID, "claude"),
                             "All done. PR posted.")
            self.assertEqual(hibernate.last_reply(self.SID2, "codex"), "Fixed it.")
            self.assertIsNone(hibernate.last_reply(
                "33333333-3333-3333-3333-333333333333", "claude"))

    def test_fit_counts_wide_characters_as_two_columns(self):
        self.assertEqual(hibernate.fit("📌 ab", 6), "📌 ab ")
        self.assertEqual(hibernate.fit("abcdefgh", 5), "abcd…")

    def test_on_close_reads_herdrs_event_payload(self):
        hibernate.remember_chats([self._live("w1:p1", "w1:t1", self.SID)], {})
        env = {"HERDR_PLUGIN_EVENT": "tab.closed",
               "HERDR_PLUGIN_EVENT_JSON": '{"event":"tab.closed","data":'
               '{"type":"tab_closed","tab_id":"w1:t1","workspace_id":"w1"}}'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(hibernate, "load_config",
                                  return_value=dict(hibernate.DEFAULTS)):
            hibernate.cmd_on_close()
        self.assertEqual([e["uuid"] for e in self._settled()], [self.SID])

    # -- auto settle: hibernated chats untouched for AUTO_SETTLE_AFTER_DAYS

    def _auto(self, age_days, tab_label="Old work", dry_run=False, **cfg):
        state = {"w1:p1": {"uuid": self.SID, "agent": "claude",
                           "tab_id": "w1:t1", "label": "Old work", "cwd": "/w"}}
        hibernate.save_state(state)
        conf = dict(hibernate.DEFAULTS, **cfg)
        with mock.patch.object(hibernate, "herdr") as herdr, \
                mock.patch.object(hibernate, "transcript_age_minutes",
                                  return_value=age_days * 1440):
            hibernate.auto_settle([{"pane_id": "w1:p1", "tab_id": "w1:t1"}],
                                  {"w1:t1": tab_label}, state, conf, dry_run)
        return state, [c.args for c in herdr.call_args_list]

    def test_old_hibernated_chat_is_closed_and_settled(self):
        state, calls = self._auto(age_days=3)
        self.assertEqual(calls, [("pane", "close", "w1:p1")])
        self.assertEqual(state, {})
        self.assertEqual([e["uuid"] for e in self._settled()], [self.SID])

    def test_recent_chat_stays_open(self):
        state, calls = self._auto(age_days=1.5)
        self.assertEqual(calls, [])
        self.assertIn("w1:p1", state)

    def test_chat_reopened_just_to_read_gets_the_full_wait(self):
        # Last message 10 days old, but hibernated (so last used) an hour ago.
        state = {"w1:p1": {"uuid": self.SID, "tab_id": "w1:t1", "label": "x",
                           "at": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(time.time() - 3600))}}
        with mock.patch.object(hibernate, "herdr") as herdr, \
                mock.patch.object(hibernate, "transcript_age_minutes",
                                  return_value=10 * 1440):
            hibernate.auto_settle([{"pane_id": "w1:p1", "tab_id": "w1:t1"}], {},
                                  state, dict(hibernate.DEFAULTS), False)
        herdr.assert_not_called()

    def test_kept_chat_is_never_auto_settled(self):
        hibernate.save_json(hibernate.KEPT_FILE, [self.SID])
        state, calls = self._auto(age_days=30)
        self.assertEqual(calls, [])
        self.assertIn("w1:p1", state)

    def test_keep_toggles_on_the_focused_chat_and_shows_in_the_sidebar(self):
        hibernate.save_state({"w1:p1": {"uuid": self.SID, "agent": "claude",
                                         "label": "Old work"}})
        pane = {"pane_id": "w1:p1", "agent": hibernate.SLEEP_AGENT}

        def fake_herdr(*args):
            return {"pane": pane} if args[:2] == ("pane", "current") else {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr) as herdr, \
                mock.patch.object(hibernate, "load_config",
                                  return_value=dict(hibernate.DEFAULTS)), \
                mock.patch.object(hibernate, "transcript_age_minutes",
                                  return_value=60), \
                mock.patch.object(hibernate, "toast"):
            hibernate.cmd_keep()
            self.assertEqual(hibernate.kept_ids(), {self.SID})
            label = [c.args for c in herdr.call_args_list
                     if c.args[1] == "report-metadata"][-1][-1]
            self.assertTrue(label.startswith("🔖"))
            hibernate.cmd_keep()
        self.assertEqual(hibernate.kept_ids(), set())

    def test_running_kept_chat_shows_a_bookmark(self):
        pane = {"pane_id": "w1:p5", "agent": "claude",
                "agent_session": {"value": self.SID}}
        with mock.patch.object(hibernate, "herdr") as herdr:
            hibernate.show_kept(pane, {self.SID})
        self.assertEqual(herdr.call_args.args[-2:], ("--display-agent", "🔖 claude"))

    def test_unkept_chat_loses_its_bookmark_and_others_are_left_alone(self):
        pane = {"pane_id": "w1:p5", "agent": "claude", "display_agent": "🔖 claude",
                "agent_session": {"value": self.SID}}
        with mock.patch.object(hibernate, "herdr") as herdr:
            hibernate.show_kept(pane, set())
            self.assertEqual(herdr.call_args.args[-1], "--clear-display-agent")
            herdr.reset_mock()
            hibernate.show_kept(dict(pane, display_agent="custom"), set())
            hibernate.show_kept(dict(pane, display_agent="🔖 claude"), {self.SID})
            hibernate.show_kept({"pane_id": "w1:p6", "agent": hibernate.SLEEP_AGENT},
                                {self.SID})
        herdr.assert_not_called()

    def test_keep_on_a_running_chat_marks_it_at_once(self):
        pane = {"pane_id": "w1:p5", "agent": "claude",
                "agent_session": {"value": self.SID}}

        def fake_herdr(*args):
            return {"pane": pane} if args[:2] == ("pane", "current") else {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr) as herdr, \
                mock.patch.object(hibernate, "toast"):
            hibernate.cmd_keep()
        self.assertEqual(hibernate.kept_ids(), {self.SID})
        self.assertIn("🔖 claude", herdr.call_args.args)

    def test_keep_on_a_chat_that_just_woke_marks_it_as_running(self):
        hibernate.save_state({"w1:p5": {"uuid": self.SID, "agent": "claude",
                                         "label": "Old work"}})
        pane = {"pane_id": "w1:p5", "agent": "claude",
                "agent_session": {"value": self.SID}}

        def fake_herdr(*args):
            return {"pane": pane} if args[:2] == ("pane", "current") else {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr) as herdr, \
                mock.patch.object(hibernate, "toast"):
            hibernate.cmd_keep()
        self.assertEqual(hibernate.kept_ids(), {self.SID})
        self.assertNotIn("report-agent", [c.args[1] for c in herdr.call_args_list])
        self.assertIn("🔖 claude", herdr.call_args.args)

    def test_groups_and_chats_sort_by_last_use(self):
        entries = [
            {"uuid": "a", "workspace_id": "w1", "used": "2026-01-01 00:00:00"},
            {"uuid": "b", "workspace_id": "w2", "used": "2026-03-01 00:00:00"},
            {"uuid": "c", "workspace_id": "w1", "used": "2026-02-01 00:00:00"},
        ]
        groups = hibernate.settled_groups(entries, {"w1": "app", "w2": "KB"})
        self.assertEqual([(n, [e["uuid"] for e in es]) for n, es in groups],
                         [("KB", ["b"]), ("app", ["c", "a"])])

    def test_gone_workspace_groups_by_folder(self):
        groups = hibernate.settled_groups(
            [{"uuid": "a", "workspace_id": "gone", "cwd": "/srv/project"}], {})
        self.assertEqual(groups[0][0], "project")

    def test_pinned_tab_is_never_auto_settled(self):
        _, calls = self._auto(age_days=30, tab_label="💤 📌 Keep me")
        self.assertEqual(calls, [])

    def test_auto_settle_respects_dry_run_and_zero_days(self):
        _, calls = self._auto(age_days=30, dry_run=True)
        self.assertEqual(calls, [])
        _, calls = self._auto(age_days=30, AUTO_SETTLE_AFTER_DAYS="0")
        self.assertEqual(calls, [])
        self.assertEqual(self._settled(), [])

    def test_watcher_settles_a_hibernated_tab_whose_close_it_missed(self):
        state = {"w1:p1": {"uuid": self.SID, "tab_id": "w1:t1", "label": "x",
                           "missing_since": "2000-01-01 00:00:00"}}
        hibernate.save_state(state)
        with mock.patch.object(hibernate, "gc_stub_files"), \
                mock.patch.object(hibernate, "herdr"):
            hibernate.restore_records([], {}, state)
        self.assertEqual([e["uuid"] for e in self._settled()], [self.SID])


class ViewTests(unittest.TestCase):
    """F8 cycles the agents list: all -> running -> hibernated -> all."""

    def _step(self, active):
        calls = []

        def fake_api(method, params):
            calls.append((method, params))
            if params.get("source", "").endswith("-probe"):
                return active
            return {}

        with mock.patch.object(hibernate, "herdr_api", side_effect=fake_api):
            hibernate.cmd_view()
        return calls[-1]

    def _ours(self, label):
        return {"active": True, "source": hibernate.SLEEP_SOURCE, "label": label}

    def test_no_filter_steps_to_running(self):
        method, params = self._step({"active": False})
        self.assertEqual(method, "agent.view.set")
        self.assertEqual(params["label"], "running")

    def test_running_steps_to_hibernated(self):
        method, params = self._step(self._ours("running"))
        self.assertEqual(params["label"], "hibernated")
        self.assertEqual(params["filter"]["value"], hibernate.SLEEP_AGENT)

    def test_hibernated_steps_back_to_all(self):
        method, params = self._step(self._ours("hibernated"))
        self.assertEqual((method, params),
                         ("agent.view.clear", {"source": hibernate.SLEEP_SOURCE}))

    def test_another_tools_filter_counts_as_all(self):
        method, params = self._step(
            {"active": True, "source": "someone-else", "label": "hibernated"})
        self.assertEqual(params["label"], "running")


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
        with mock.patch.object(hibernate, "herdr"):
            hibernate.cmd_label_awake("w1:p1")
        self.assertTrue(os.path.exists(hibernate.resume_stamp_path("w1:p1")))

    def test_label_awake_takes_the_pane_off_the_sleep_list(self):
        with mock.patch.object(hibernate, "herdr") as herdr:
            hibernate.cmd_label_awake("w1:p1")
        calls = [c.args for c in herdr.call_args_list]
        self.assertIn(("pane", "release-agent", "w1:p1", "--source",
                       hibernate.SLEEP_SOURCE, "--agent",
                       hibernate.SLEEP_AGENT), calls)

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
                pane, {"w1:t1": "Nightly build orchestrator"}, self._cfg(),
                {}, "w9:p9")
        self.assertEqual(decision, "skip")
        self.assertTrue(reason.startswith("busy:"))


class NewAgentAdapterTests(unittest.TestCase):
    """Adapters for droid, pi, opencode, cursor, devin and amp."""

    UUID = "01a02acb-6848-7db5-8cbb-9b8163e18c3e"
    HEX32 = "59256306b3846fee39139da32939cc75"
    CWD = "/tmp/demo-proj"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def _mk(self, rel, content=b"{}"):
        path = os.path.join(self.tmp.name, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def _touch(self, path, age_seconds):
        ts = time.time() - age_seconds
        os.utime(path, (ts, ts))

    # -- session id shapes ------------------------------------------------

    def test_sid_shape_gate_per_agent(self):
        self.assertTrue(hibernate.valid_sid("claude", self.UUID))
        self.assertTrue(hibernate.valid_sid("droid", self.UUID))
        self.assertFalse(hibernate.valid_sid("droid", "not a uuid"))
        self.assertTrue(hibernate.valid_sid("opencode", "ses_72d8abc"))
        self.assertFalse(hibernate.valid_sid("opencode", self.UUID))
        self.assertTrue(hibernate.valid_sid("cursor", self.HEX32))
        self.assertFalse(hibernate.valid_sid("cursor", self.UUID))
        self.assertTrue(hibernate.valid_sid("devin", "deadpan-squid"))
        self.assertFalse(hibernate.valid_sid("devin", "two words"))
        self.assertTrue(hibernate.valid_sid(
            "amp", "T-ce40adae-e173-451f-a772-d56fc4cff3e1"))
        self.assertFalse(hibernate.valid_sid("amp", self.UUID))
        self.assertFalse(hibernate.valid_sid("hermes", self.UUID))

    # -- numeric timestamp fields ------------------------------------------

    def test_num_ts_fields_epoch_ms(self):
        path = self._mk("t.jsonl",
                        b'{"sentAt": %d}\n' % ((time.time() - 300) * 1000))
        stamp = hibernate.last_entry_epoch(path, ("sentAt",))
        self.assertAlmostEqual(stamp, time.time() - 300, delta=2)

    def test_num_ts_fields_epoch_seconds(self):
        path = self._mk("t.jsonl",
                        b'{"timestamp": %d}\n' % (time.time() - 600))
        stamp = hibernate.last_entry_epoch(path, ("timestamp",))
        self.assertAlmostEqual(stamp, time.time() - 600, delta=2)

    def test_num_ts_fields_mixed_with_iso(self):
        # pi mixes ISO strings and ms ints on the same field name
        path = self._mk("t.jsonl", (
            b'{"timestamp":"%s"}\n'
            b'{"timestamp":%d}\n'
        ) % (time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.gmtime(time.time() - 900)).encode(),
             int((time.time() - 60) * 1000)))
        stamp = hibernate.last_entry_epoch(path, ("timestamp",))
        self.assertAlmostEqual(stamp, time.time() - 60, delta=2)

    # -- transcript globs ---------------------------------------------------

    def test_multiple_main_globs_any_match_is_resumable(self):
        spec = dict(hibernate.AGENTS["droid"])
        spec["transcript_glob"] = (
            os.path.join(self.tmp.name, "nested", "{sid}.jsonl"),
            os.path.join(self.tmp.name, "flat", "{sid}.jsonl"),
        )
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(time.time() - 3600))
        self._mk("nested/%s.jsonl" % self.UUID,
                 b'{"timestamp":"%s"}\n' % stamp.encode())
        with mock.patch.dict(hibernate.AGENTS, {"droid": spec}):
            age = hibernate.transcript_age_minutes(self.UUID, "droid")
        self.assertIsNotNone(age)
        self.assertGreater(age, 50)

    # -- cwd-keyed resolvers ------------------------------------------------

    def test_droid_resolves_newest_session_in_cwd_dir(self):
        self._mk(".factory/sessions/-tmp-demo-proj/%s.jsonl" % self.UUID)
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.droid_session_id(pane), self.UUID)

    def test_droid_resolves_droid_suffixed_dir(self):
        self._mk(".factory/sessions/-tmp-demo-proj-droid/%s.jsonl" % self.UUID)
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.droid_session_id(pane), self.UUID)

    def test_pi_resolves_uuid_from_filename(self):
        self._mk(".pi/agent/sessions/--tmp-demo-proj--/"
                 "2026-08-22T18-46-08-713Z_%s.jsonl" % self.UUID)
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.pi_session_id(pane), self.UUID)

    def test_opencode_resolves_ses_id_in_project_dir(self):
        self._mk(".local/share/opencode/project/tmp-demo-proj/"
                 "storage/session/info/ses_abc123.json")
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.opencode_session_id(pane), "ses_abc123")

    def test_opencode_falls_back_to_global_project(self):
        self._mk(".local/share/opencode/project/global/"
                 "storage/session/info/ses_glob1.json")
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.opencode_session_id(pane), "ses_glob1")

    def test_cursor_resolves_chat_by_meta_cwd(self):
        self._mk(".cursor/chats/%s/e044a85d-2064-4367-aaf1-8bc501a6d5e7/"
                 "meta.json" % self.HEX32,
                 b'{"cwd": "%s", "updatedAtMs": 1}' % self.CWD.encode())
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.cursor_session_id(pane), self.HEX32)

    def test_cursor_ignores_chats_from_other_directories(self):
        self._mk(".cursor/chats/%s/e044a85d/meta.json" % self.HEX32,
                 b'{"cwd": "/elsewhere"}')
        pane = {"cwd": self.CWD}
        self.assertIsNone(hibernate.cursor_session_id(pane))

    def test_devin_resolves_and_ages_from_sessions_db(self):
        import sqlite3
        db = os.path.join(self.tmp.name,
                          ".local/share/devin/cli/sessions.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, "
                     "working_directory TEXT, last_activity_at INTEGER)")
        old = int(time.time() - 7200)
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                     ("deadpan-squid", self.CWD, old))
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                     ("other-slug", "/elsewhere", old))
        conn.commit()
        conn.close()
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.devin_session_id(pane), "deadpan-squid")
        age = hibernate.devin_age_minutes("deadpan-squid", {})
        self.assertGreater(age, 110)
        self.assertLess(age, 130)

    def test_devin_missing_db_is_skip_not_crash(self):
        pane = {"cwd": self.CWD}
        self.assertIsNone(hibernate.devin_session_id(pane))
        self.assertIsNone(hibernate.devin_age_minutes("x-y", {}))

    def test_devin_two_quiet_same_cwd_sessions_abstain(self):
        import sqlite3
        db = os.path.join(self.tmp.name,
                          ".local/share/devin/cli/sessions.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, "
                     "working_directory TEXT, last_activity_at INTEGER)")
        old = int(time.time() - 7200)
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                     ("deadpan-squid", self.CWD, old))
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                     ("cooing-gadget", self.CWD, old - 60))
        conn.commit()
        conn.close()
        pane = {"cwd": self.CWD}
        self.assertIsNone(hibernate.devin_session_id(pane))

    def test_amp_resolves_thread_by_tree_uri(self):
        tid = "T-ce40adae-e173-451f-a772-d56fc4cff3e1"
        self._mk(".local/share/amp/threads/%s.json" % tid,
                 b'{"env": {"initial": {"trees": [{"uri": "file://%s"}]}}}'
                 % self.CWD.encode())
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.amp_session_id(pane), tid)

    # -- ambiguity: two quiet sessions in one directory are unknowable ------

    def test_two_quiet_sessions_in_one_dir_abstain(self):
        a = self._mk(".pi/agent/sessions/--tmp-demo-proj--/t1_%s.jsonl"
                     % self.UUID)
        b_uuid = "6f81d0e7-1111-2222-3333-444455556666"
        b = self._mk(".pi/agent/sessions/--tmp-demo-proj--/t2_%s.jsonl"
                     % b_uuid)
        for p in (a, b):
            self._touch(p, 3600)  # both older than the 30m default
        pane = {"cwd": self.CWD}
        self.assertIsNone(hibernate.pi_session_id(pane))

    def test_one_quiet_one_active_resolves_the_active(self):
        quiet = self._mk(".pi/agent/sessions/--tmp-demo-proj--/t1_%s.jsonl"
                         % self.UUID)
        self._touch(quiet, 3600)
        active = self._mk(".pi/agent/sessions/--tmp-demo-proj--/t2_"
                          "6f81d0e7-1111-2222-3333-444455556666.jsonl")
        self._touch(active, 60)  # written a minute ago — the live session
        pane = {"cwd": self.CWD}
        self.assertEqual(hibernate.pi_session_id(pane),
                         "6f81d0e7-1111-2222-3333-444455556666")

    # -- dispatch ------------------------------------------------------------

    def test_session_uuid_prefers_herdrs_own_report(self):
        pane = {"agent": "pi", "agent_session": {"value": self.UUID},
                "cwd": self.CWD}
        self.assertEqual(hibernate.session_uuid(pane), self.UUID)

    def test_session_uuid_falls_back_to_the_adapter_resolver(self):
        self._mk(".pi/agent/sessions/--tmp-demo-proj--/t_%s.jsonl" % self.UUID)
        pane = {"agent": "pi", "cwd": self.CWD}
        self.assertEqual(hibernate.session_uuid(pane), self.UUID)

    def test_session_uuid_unknown_agent_is_none(self):
        pane = {"agent": "hermes", "cwd": self.CWD}
        self.assertIsNone(hibernate.session_uuid(pane))


if __name__ == "__main__":
    unittest.main()
