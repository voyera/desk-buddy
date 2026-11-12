"""Tests for the bridge's mood rules, heartbeat and saved state.

Run with the bridge's own Python (it has pyserial):
    ~/.desk-buddy/venv/bin/python -m unittest discover -s tests -v
Nothing here touches the real board, speakers or ~/.desk-buddy: the bridge is pointed at a temp
folder and given fake device / focus / sound objects.
"""
import importlib.util, json, os, sys, tempfile, time, unittest
from unittest import mock

HOME = tempfile.mkdtemp(prefix="deskbuddy-test-")
os.environ["DESKBUDDY_HOME"] = HOME
_spec = importlib.util.spec_from_file_location(
    "buddy_bridge", os.path.join(os.path.dirname(__file__), "..", "tools", "buddy_bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

# a fixed moment at local noon, so "night" never kicks in by accident
NOON = time.mktime(time.struct_time((2026, 11, 11, 12, 0, 0, 0, 0, -1)))


class FakeDevice:
    connected = True
    def __init__(self): self.sent = []
    def send(self, cmd, quiet=False): self.sent.append(cmd); return True


class FakeFocus:
    front, code_session = False, None


class FakeSounds:
    def __init__(self): self.played = []
    def settings(self): return {}
    def play(self, moment, force=False): self.played.append(moment); return True
    def play_gag(self, event, force=False): self.played.append(event); return True


class Clock:
    """Stands in for time.time() inside the bridge."""
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class BridgeTest(unittest.TestCase):
    def setUp(self):
        for f in os.listdir(HOME):
            os.remove(os.path.join(HOME, f))
        self.clock = Clock(NOON)
        p = mock.patch.object(bridge.time, "time", self.clock)
        p.start(); self.addCleanup(p.stop)
        self.b = self.make()

    def make(self):
        return bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)

    def hook(self, ev, sid, **extra):
        self.b.hook({"hook_event_name": ev, "session_id": sid, "cwd": "/x", **extra})

    def mood(self):
        return self.b.mood()


class MoodRules(BridgeTest):
    def test_idle_with_nothing_going_on(self):
        self.assertEqual(self.mood(), "idle")

    def test_working_after_a_prompt(self):
        self.hook("UserPromptSubmit", "a")
        self.assertEqual(self.mood(), "working")

    def test_needs_you_beats_working(self):
        self.hook("UserPromptSubmit", "a")
        self.hook("UserPromptSubmit", "b")
        self.hook("PermissionRequest", "b")
        self.assertEqual(self.mood(), "alert")
        c = self.b.counts_locked(self.clock())
        self.assertEqual((c["working"], c["alert"]), (1, 1))

    def test_question_tool_counts_as_needs_you(self):
        self.hook("PreToolUse", "a", tool_name="AskUserQuestion")
        self.assertEqual(self.mood(), "alert")

    def test_answering_goes_back_to_work(self):
        self.hook("PermissionRequest", "a")
        self.hook("PostToolUse", "a")
        self.assertEqual(self.mood(), "working")

    def test_finish_while_others_busy_is_a_short_celebration(self):
        self.hook("UserPromptSubmit", "a")
        self.hook("UserPromptSubmit", "b")
        self.hook("Stop", "b")
        self.assertEqual(self.mood(), "done")
        self.clock.advance(bridge.CELEBRATE_SECS + 0.1)
        self.assertEqual(self.mood(), "working")

    def test_finish_when_quiet_celebrates_longer_then_waits_to_be_seen(self):
        self.hook("UserPromptSubmit", "a")
        self.hook("Stop", "a")
        self.clock.advance(bridge.CELEBRATE_SECS + 0.1)
        self.assertEqual(self.mood(), "done")
        self.clock.advance(bridge.DONE_SECS)
        self.assertEqual(self.mood(), "awake")                 # finished but you haven't looked
        self.assertEqual(self.b.counts_locked(self.clock())["unseen"], 1)

    def test_next_prompt_marks_result_seen(self):
        self.hook("UserPromptSubmit", "a")
        self.hook("Stop", "a")
        self.clock.advance(bridge.DONE_SECS + 1)
        self.hook("UserPromptSubmit", "a")
        self.assertEqual(self.b.counts_locked(self.clock())["unseen"], 0)

    def test_unseen_mark_expires(self):
        self.hook("UserPromptSubmit", "a")
        self.hook("Stop", "a")
        self.clock.advance(bridge.UNSEEN_SECS + 1)
        self.assertEqual(self.mood(), "idle")

    def test_silent_working_session_goes_stale(self):
        self.hook("UserPromptSubmit", "a")
        self.clock.advance(bridge.STALE["working"] + 1)
        self.assertEqual(self.mood(), "idle")

    def test_session_end_forgets_it(self):
        self.hook("PermissionRequest", "a")
        self.hook("SessionEnd", "a")
        self.assertEqual(self.mood(), "idle")

    def test_failure_never_interrupts_needs_you(self):
        self.hook("PermissionRequest", "a")
        self.hook("PostToolUseFailure", "b")
        self.assertFalse(any(c.startswith("e oops") for c in self.b.dev.sent))

    def test_failure_while_working_plays_oops(self):
        self.hook("UserPromptSubmit", "a")
        self.hook("PostToolUseFailure", "a")
        self.assertIn("e oops", self.b.dev.sent)

    def test_tap_snoozes_needs_you_but_keeps_the_count(self):
        self.hook("PermissionRequest", "a")
        self.b.tapped()
        self.assertNotEqual(self.mood(), "alert")
        self.assertEqual(self.b.counts_locked(self.clock())["alert"], 1)
        self.clock.advance(bridge.TAP_SNOOZE + 1)
        self.assertEqual(self.mood(), "alert")

    def test_night(self):
        self.clock.t = NOON + 13 * 3600                         # 01:00
        self.assertEqual(self.mood(), "night")

    def test_night_is_midnight_to_eight(self):
        for hour, want in ((23, "idle"), (0, "night"), (7, "night"), (8, "idle")):
            self.clock.t = NOON + (hour - 12) % 24 * 3600 + 30 * 24 * 3600   # well after anything happened
            self.assertEqual(self.mood(), want, f"{hour}:00")

    def test_regular_sleep_for_a_few_minutes_before_night(self):
        self.clock.t = NOON + 13 * 3600                         # 01:00, busy
        self.hook("PermissionRequest", "a")
        self.assertEqual(self.mood(), "alert")
        self.hook("SessionEnd", "a")
        self.clock.advance(4 * 60)
        self.assertEqual(self.mood(), "idle")                   # just been up: plain sleep first
        self.clock.advance(2 * 60)
        self.assertEqual(self.mood(), "night")                  # then the blanket comes up


class MeetingPrep(BridgeTest):
    """While a meeting is coming up (or its alarm is ringing) the board is told "p 1", so a tap plays meeting prep."""
    def meeting_in(self, secs):
        with open(bridge.MEETINGS_FILE, "w") as f:
            json.dump([{"start": self.clock() + secs, "title": "standup"}], f)

    def prep_flags(self):
        return [c for c in self.b.dev.sent if c.startswith("p ")]

    def test_no_meeting_means_no_prep(self):
        self.b.update()
        self.assertEqual(self.prep_flags(), ["p 0"])

    def test_countdown_turns_prep_on_then_off_after_the_alarm(self):
        self.meeting_in(30 * 60)
        self.b.update()
        self.clock.advance(21 * 60); self.b.update()              # 9 minutes to go: countdown
        self.clock.advance(9 * 60 + 10); self.b.update()           # alarm ringing
        self.clock.advance(60); self.b.update()                    # alarm window over
        self.assertEqual(self.prep_flags(), ["p 0", "p 1", "p 0"])

    def test_resend_repeats_the_prep_flag(self):
        self.meeting_in(5 * 60)
        self.b.update(); self.b.dev.sent.clear()
        self.b.resend()
        self.assertIn("p 1", self.b.dev.sent)


class PlayOnRequest(BridgeTest):
    def test_named_gag_plays_and_mood_is_reasserted_after(self):
        self.assertTrue(self.b.play("Disco\n"))
        self.assertEqual(self.b.dev.sent[-1], "e tap_disco")
        self.assertGreater(self.b.resync_at, 0)

    def test_random_gag_is_left_to_the_board(self):
        self.b.play("tap")
        self.assertEqual(self.b.dev.sent[-1], "e tap")

    def test_startle_and_events_map_to_board_names(self):
        self.b.play("startle"); self.assertEqual(self.b.dev.sent[-1], "e tap_startle_night")
        self.b.play("hello"); self.assertEqual(self.b.dev.sent[-1], "e hello")

    def test_unknown_animation_sends_nothing(self):
        before = list(self.b.dev.sent)
        self.assertIsNone(self.b.play("e tap; rm -rf"))
        self.assertEqual(self.b.dev.sent, before)

    def test_playing_is_not_a_tap(self):
        self.hook("PermissionRequest", "a")
        self.b.play("tap")
        self.assertEqual(self.mood(), "alert")

    def test_no_board_reports_failure(self):
        self.b.dev.send = lambda cmd, quiet=False: False
        self.assertFalse(self.b.play("bomb"))

    def test_every_gag_has_a_name(self):
        for g in bridge.GAGS:
            self.assertEqual(bridge.ANIMATIONS[g], f"tap_{g}")


class AppWatcher(BridgeTest):
    """Chat/Cowork state read from the Claude app's screen."""
    def snap(self, title, buttons=(), statuses=()):
        self.b_pages.snapshot({"trusted": True, "pages": {title: {"buttons": list(buttons), "statuses": list(statuses), "markers": []}}})

    def setUp(self):
        super().setUp()
        self.b_pages = bridge.AppPages(self.b)

    def test_claudes_stop_button_means_a_chat_is_working(self):
        self.snap("Planning - Claude", buttons=["Stop response"])
        self.assertEqual(self.mood(), "working")

    def test_a_web_page_with_its_own_stop_button_is_not_a_chat(self):
        self.snap("Lump Gag Sounds", buttons=["Play all", "Stop"])
        self.assertEqual(self.mood(), "idle")


class Heartbeat(BridgeTest):
    def beats(self):
        return [c for c in self.b.dev.sent if c.startswith("h ")]

    def test_heartbeat_carries_time_of_day(self):
        self.b.tick(self.clock())
        self.assertEqual(self.beats(), ["h 43200"])             # noon

    def test_heartbeat_is_paced(self):
        self.b.tick(self.clock())
        self.clock.advance(5); self.b.tick(self.clock())
        self.assertEqual(len(self.beats()), 1)
        self.clock.advance(bridge.HEARTBEAT_EVERY); self.b.tick(self.clock())
        self.assertEqual(len(self.beats()), 2)

    def test_resend_beats_straight_away(self):
        self.b.tick(self.clock())
        self.clock.advance(1)
        self.b.resend()
        self.b.tick(self.clock())
        self.assertEqual(len(self.beats()), 2)

    def test_resend_repeats_mood_colour_and_overlay(self):
        self.b.update()
        self.b.dev.sent.clear()
        self.b.resend()
        kinds = {c.split()[0] for c in self.b.dev.sent}
        self.assertTrue({"s", "c", "o"} <= kinds, self.b.dev.sent)

    def test_a_broken_sound_doesnt_stop_heartbeat_or_saving(self):
        with mock.patch.object(self.b, "sound_tick", side_effect=RuntimeError("boom")):
            self.b.tick(self.clock())
        self.assertEqual(len(self.beats()), 1)
        self.assertTrue(os.path.exists(bridge.STATE_FILE))


class SavedState(BridgeTest):
    def test_sessions_survive_a_restart(self):
        self.hook("PermissionRequest", "a")
        self.hook("UserPromptSubmit", "b")
        self.b.save_state()
        again = self.make()
        self.assertEqual(again.mood(), "alert")
        self.assertEqual(set(again.sessions), {"a", "b"})

    def test_app_sessions_and_old_ones_are_not_restored(self):
        self.b.external("app:Some chat", "working")
        self.hook("UserPromptSubmit", "old")
        self.b.save_state()
        self.clock.advance(bridge.FORGET + 1)
        again = self.make()
        self.assertEqual(again.sessions, {})

    def test_no_hello_after_a_quick_restart(self):
        self.hook("UserPromptSubmit", "a")
        self.b.save_state()
        again = self.make()
        again.dev.sent.clear()
        again.hook({"hook_event_name": "SessionStart", "session_id": "b", "source": "startup"})
        self.assertNotIn("e hello", again.dev.sent)

    def test_unreadable_state_is_ignored(self):
        with open(bridge.STATE_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(self.make().sessions, {})

    def test_only_writes_when_something_changed(self):
        self.b.save_state()
        m = os.path.getmtime(bridge.STATE_FILE)
        os.utime(bridge.STATE_FILE, (m - 100, m - 100))
        self.b.save_state()
        self.assertEqual(os.path.getmtime(bridge.STATE_FILE), m - 100)


class DeviceLinks(unittest.TestCase):
    def device(self):
        d = bridge.Device.__new__(bridge.Device)               # no thread, no real ports
        d._last_err = {}
        return d

    def write_config(self, cfg):
        with open(bridge.CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        self.addCleanup(lambda: os.path.exists(bridge.CONFIG_FILE) and os.remove(bridge.CONFIG_FILE))

    def test_callback_errors_are_contained(self):
        def bad(): raise RuntimeError("boom")
        bridge.Device._call(bad)                                # must not raise

    def test_usb_first_then_wifi_when_paired(self):
        self.write_config({"lump_token": "abcdef123456"})
        with mock.patch.object(bridge.Device, "usb_ports", staticmethod(lambda: ["/dev/cu.usbmodem1"])):
            self.assertEqual(self.device()._targets(), ["/dev/cu.usbmodem1", "socket://lump.local:4210"])

    def test_no_wifi_without_pairing(self):
        self.write_config({"calendar_ics": "x"})
        with mock.patch.object(bridge.Device, "usb_ports", staticmethod(lambda: [])):
            self.assertEqual(self.device()._targets(), [])

    def test_repeated_failures_are_logged_once(self):
        d = self.device()
        with self.assertLogs("bridge", "INFO") as logs:
            for _ in range(5): d._fail("socket://lump.local:4210", OSError("no route"))
            d._fail("socket://lump.local:4210", OSError("refused"))
        self.assertEqual(len(logs.output), 2)


def ics(*events):
    """A tiny iCal feed. Each event: (uid, start "YYYYMMDDTHHMMSSZ", title, extra lines)."""
    body = "".join(f"BEGIN:VEVENT\r\nUID:{u}\r\nDTSTART:{st}\r\nDURATION:PT30M\r\nSUMMARY:{t}\r\n{x}END:VEVENT\r\n"
                   for u, st, t, x in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n{body}END:VCALENDAR\r\n".encode()


REAL_FETCH = bridge.Calendar._fetch


class CalendarTest(unittest.TestCase):
    """config.json "calendar_ics" may be one link or several (Google + Outlook); they're merged."""

    def setUp(self):
        try:
            import icalendar, recurring_ical_events  # noqa: F401
        except ImportError:
            self.skipTest("needs icalendar + recurring-ical-events (the bridge venv has them)")
        import datetime
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        self.t1 = soon.strftime("%Y%m%dT%H0000Z")
        self.t2 = (soon + datetime.timedelta(hours=1)).strftime("%Y%m%dT%H0000Z")
        self.feeds = {}
        p = mock.patch.object(bridge.Calendar, "_fetch", staticmethod(lambda url: self.feeds[url]))
        p.start(); self.addCleanup(p.stop)
        self.cal = bridge.Calendar.__new__(bridge.Calendar)    # no polling thread
        self.addCleanup(lambda: [os.path.exists(f) and os.remove(f) for f in (bridge.CONFIG_FILE, bridge.MEETINGS_FILE)])

    def config(self, cfg):
        with open(bridge.CONFIG_FILE, "w") as f: json.dump(cfg, f)

    def meetings(self):
        with open(bridge.MEETINGS_FILE) as f: return json.load(f)

    def test_single_link_still_works(self):
        self.feeds["https://g/a.ics"] = ics(("a", self.t1, "Standup", ""))
        self.config({"calendar_ics": "https://g/a.ics"})
        self.cal.refresh()
        self.assertEqual([m["title"] for m in self.meetings()], ["Standup"])

    def test_google_and_outlook_are_merged_and_deduped(self):
        self.feeds["https://google/a.ics"] = ics(("a", self.t2, "Client call", ""), ("b", self.t1, "Standup", ""))
        self.feeds["https://outlook/b.ics"] = ics(("x", self.t1, "standup ", ""), ("y", self.t2, "1:1", ""))
        self.config({"calendar_ics": ["https://google/a.ics", "https://outlook/b.ics"]})
        self.cal.refresh()
        ms = self.meetings()
        self.assertEqual(len(ms), 3)                            # the shared standup counts once
        self.assertEqual(ms[0]["title"], "Standup")
        self.assertEqual(sorted(m["title"] for m in ms[1:]), ["1:1", "Client call"])

    def test_one_failing_calendar_does_not_hide_the_others(self):
        self.feeds["https://google/a.ics"] = ics(("a", self.t1, "Standup", ""))
        self.config({"calendar_ics": ["https://broken/x.ics", "https://google/a.ics"]})
        with self.assertLogs("bridge", "INFO") as logs:
            self.cal.refresh()
        self.assertEqual([m["title"] for m in self.meetings()], ["Standup"])
        self.assertTrue(any("broken failed" in l for l in logs.output))
        self.assertFalse(any("x.ics" in l for l in logs.output))   # the secret link isn't logged

    def test_all_failing_keeps_the_last_good_list(self):
        with open(bridge.MEETINGS_FILE, "w") as f: json.dump([{"start": 1, "end": 2, "title": "old"}], f)
        self.config({"calendar_ics": ["https://broken/x.ics"]})
        self.cal.refresh()
        self.assertEqual(self.meetings()[0]["title"], "old")

    def test_declined_on_any_of_my_addresses_is_skipped(self):
        dec = "ATTENDEE;PARTSTAT=DECLINED:mailto:me@work.com\r\n"
        self.feeds["https://outlook/b.ics"] = ics(("a", self.t1, "Skip me", dec), ("b", self.t2, "Keep", ""))
        self.config({"calendar_ics": ["https://outlook/b.ics"], "my_email": ["me@gmail.com", "Me@Work.com"]})
        self.cal.refresh()
        self.assertEqual([m["title"] for m in self.meetings()], ["Keep"])

    def test_webcal_links_are_fetched_over_https(self):
        seen = []
        with mock.patch("urllib.request.urlopen", side_effect=lambda u, timeout: seen.append(u) or mock.Mock(read=lambda: b"")):
            REAL_FETCH("webcal://outlook.office365.com/owa/calendar/x/calendar.ics")
        self.assertEqual(seen, ["https://outlook.office365.com/owa/calendar/x/calendar.ics"])


class GagSoundTest(unittest.TestCase):
    """The board says "gag tap_<gag>_<mood>" when a gag starts; the bridge plays tap_<gag>.wav in step."""

    def setUp(self):
        import wave
        self.dir = tempfile.mkdtemp(prefix="deskbuddy-sounds-")
        for g in ("shock", "prep", "prep_alarm", "startle"):
            with wave.open(os.path.join(self.dir, f"tap_{g}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\0\0" * 44100)
        self.cfg = os.path.join(self.dir, "sounds.json")
        self.write({"enabled": True, "volume": 0.4})
        self.patches = [mock.patch.object(bridge, "SOUNDS_DIR", self.dir), mock.patch.object(bridge, "SOUNDS_FILE", self.cfg)]
        for p in self.patches: p.start()
        self.timers = []
        self.patches.append(mock.patch.object(bridge.threading, "Timer", side_effect=lambda d, fn, args: self.timers.append((d, fn, args)) or mock.Mock()))
        self.patches[-1].start()
        self.s = bridge.Sounds()

    def tearDown(self):
        for p in self.patches: p.stop()

    def write(self, cfg):
        with open(self.cfg, "w") as f: json.dump(cfg, f)
        os.utime(self.cfg, (time.time() + len(getattr(self, "timers", [])), time.time() + len(getattr(self, "timers", []))))

    def fire(self):
        d, fn, args = self.timers[-1]
        with mock.patch.object(bridge.subprocess, "Popen") as popen:
            fn(*args)
        return d, popen.call_args[0][0] if popen.called else None

    def test_plays_the_gags_sound_after_his_way_in_at_your_volume(self):
        self.assertTrue(self.s.play_gag("tap_shock_idle"))
        d, cmd = self.fire()
        self.assertAlmostEqual(d, bridge.GAG_LEAD - bridge.AFPLAY_LAG)
        self.assertEqual(cmd, ["afplay", "-v", "0.4", os.path.join(self.dir, "tap_shock.wav")])

    def test_volume_is_read_when_the_sound_starts(self):
        self.s.play_gag("tap_shock_working")
        self.write({"enabled": True, "volume": 1.7})
        self.assertEqual(self.fire()[1][2], "1.7")

    def test_meeting_alarm_prep_has_its_own_sound_and_startle_has_no_lead(self):
        self.s.play_gag("tap_prep_meeting"); self.assertTrue(self.fire()[1][3].endswith("tap_prep_alarm.wav"))
        self.s.play_gag("tap_prep_idle"); self.assertTrue(self.fire()[1][3].endswith("tap_prep.wav"))
        self.s.play_gag("tap_startle_night"); self.assertEqual(self.fire()[0], 0)

    def test_switched_off_means_silent(self):
        self.write({"enabled": False, "volume": 1})
        self.assertFalse(self.s.play_gag("tap_shock_idle"))
        self.write({"enabled": True, "moments": {"tap_gags": {"on": False}}})
        self.assertFalse(self.s.play_gag("tap_shock_idle"))
        self.assertEqual(self.timers, [])

    def test_other_moments_dont_talk_over_a_gag(self):
        self.write({"enabled": True, "moments": {"thanks": {"on": True, "pick": ["a"]}, "needs_you": {"on": True, "pick": ["a"]}}})
        for m in ("thanks", "needs_you"):
            open(os.path.join(self.dir, f"{m}_a.wav"), "wb").close()
        self.s.play_gag("tap_shock_idle")
        self.s.last_any = 0
        with mock.patch.object(bridge.subprocess, "Popen"):
            self.assertFalse(self.s.play("thanks"))
            self.assertTrue(self.s.play("needs_you"))          # needing you still gets through

    def test_board_gag_line_reaches_the_sounds(self):
        b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)
        with mock.patch.object(bridge.time, "time", return_value=NOON):
            b.board_line("gag tap_moon_idle")
        self.assertEqual(b.sounds.played, ["tap_moon_idle"])


class FakeSlack:
    """Stands in for slack.com: a few users, DMs with them, unread counts and search results."""
    def __init__(self):
        self.users = [{"id": "UME", "name": "joe", "profile": {"display_name": "Joe"}},
                      {"id": "UBOSS", "name": "sam", "real_name": "Sam Boss", "profile": {"display_name": "Sam"}},
                      {"id": "UPAL", "name": "pat", "real_name": "Pat Pal", "profile": {}},
                      {"id": "UBOT", "name": "jira", "is_bot": True, "profile": {}}]
        self.ims = {"UBOSS": "DBOSS", "UPAL": "DPAL"}
        self.unread = {"DBOSS": 0, "DPAL": 0}
        self.latest = {"DBOSS": "1.0", "DPAL": "1.0"}
        self.found = {"to:me": [], "<@UME>": []}
        self.calls = []

    def __call__(self, method, **p):
        self.calls.append((method, p))
        if method == "auth.test": return {"ok": True, "user_id": "UME"}
        if method == "users.list": return {"ok": True, "members": self.users}
        if method == "users.conversations": return {"ok": True, "channels": [{"id": c, "user": u} for u, c in self.ims.items()]}
        if method == "conversations.info":
            ch = p["channel"]
            return {"ok": True, "channel": {"id": ch, "unread_count_display": self.unread[ch], "latest": {"ts": self.latest[ch]}, "last_read": "0"}}
        if method == "search.messages": return {"ok": True, "messages": {"matches": self.found[p["query"]]}}
        raise AssertionError("unexpected call " + method)

    def message(self, query, user, ts, ch="DPAL", im=True, **extra):
        self.found[query].insert(0, {"user": user, "ts": "%.6f" % ts, "channel": {"id": ch, "is_im": im}, "text": "secret words", **extra})


class SlackTest(BridgeTest):
    def setUp(self):
        super().setUp()
        with open(bridge.CONFIG_FILE, "w") as f:
            json.dump({"slack_token": "xoxp-test-token", "slack_vips": ["Sam Boss"]}, f)
        self.api = FakeSlack()
        self.w = bridge.SlackWatch(self.b, api=self.api, start=False)
        p = mock.patch.object(bridge.SlackWatch, "slack_in_front", staticmethod(lambda: self.in_slack))
        p.start(); self.addCleanup(p.stop)
        self.in_slack = False

    def poll(self):
        self.w.poll(self.clock())

    def planes(self):
        return self.b.dev.sent.count("e tap_plane")

    def test_vip_dm_holds_a_letter_until_read(self):
        self.poll()
        self.assertEqual(self.mood(), "idle")
        self.api.unread["DBOSS"], self.api.latest["DBOSS"] = 1, "5.0"
        self.poll()
        self.assertEqual(self.mood(), "letter")
        self.b.update()
        self.assertIn("s letter", self.b.dev.sent)
        self.clock.advance(60); self.poll()
        self.assertEqual(self.mood(), "letter")            # still unread: still holding it
        self.api.unread["DBOSS"] = 0
        self.poll()
        self.assertEqual(self.mood(), "idle")              # read in Slack: put away

    def test_letter_beats_working_but_not_needs_you(self):
        self.hook("UserPromptSubmit", "a")
        self.api.unread["DBOSS"] = 1
        self.poll()
        self.assertEqual(self.mood(), "letter")
        self.hook("PermissionRequest", "a")
        self.assertEqual(self.mood(), "alert")
        self.hook("PostToolUse", "a")
        self.assertEqual(self.mood(), "letter")

    def test_letter_times_out_and_a_newer_message_brings_it_back(self):
        self.api.unread["DBOSS"], self.api.latest["DBOSS"] = 1, "5.0"
        self.poll()
        self.clock.advance(bridge.SLACK_VIP_HOLD + 1)
        self.assertEqual(self.mood(), "idle")
        self.poll()                                         # same unread message: stays put away
        self.assertEqual(self.mood(), "idle")
        self.api.latest["DBOSS"] = "9.0"
        self.poll()
        self.assertEqual(self.mood(), "letter")

    def test_hold_time_comes_from_config(self):
        with open(bridge.CONFIG_FILE, "w") as f:
            json.dump({"slack_token": "xoxp-test-token", "slack_vips": ["Sam Boss"], "slack_vip_minutes": 5}, f)
        self.api.unread["DBOSS"] = 1
        self.poll()
        self.clock.advance(5 * 60 + 1)
        self.assertEqual(self.mood(), "idle")

    def test_letters_survive_a_restart(self):
        self.api.unread["DBOSS"] = 1
        self.poll(); self.b.save_state()
        self.b = self.make()
        self.assertEqual(self.mood(), "letter")

    def test_vips_matched_by_display_name_too(self):
        with open(bridge.CONFIG_FILE, "w") as f:
            json.dump({"slack_token": "xoxp-test-token", "slack_vips": ["sam"]}, f)
        self.api.unread["DBOSS"] = 1
        self.poll()
        self.assertEqual(self.mood(), "letter")

    def test_dm_or_mention_sends_one_plane(self):
        self.poll()
        self.clock.advance(30)
        self.api.message("to:me", "UPAL", self.clock() - 5)
        self.api.message("<@UME>", "UPAL", self.clock() - 3, ch="CGEN", im=False)
        self.poll()
        self.assertEqual(self.planes(), 1)
        self.clock.advance(30); self.poll()
        self.assertEqual(self.planes(), 1)                 # nothing new: no second plane
        self.clock.advance(bridge.PLANE_GAP)
        self.api.message("<@UME>", "UPAL", self.clock() - 1, ch="CGEN", im=False)
        self.poll()
        self.assertEqual(self.planes(), 2)

    def test_planes_are_rationed(self):
        self.api.message("to:me", "UPAL", self.clock() + 1)
        self.clock.advance(5); self.poll()
        self.api.message("to:me", "UPAL", self.clock() + 1)
        self.clock.advance(5); self.poll()
        self.assertEqual(self.planes(), 1)

    def test_no_plane_for_own_bot_old_or_vip_messages(self):
        t = self.clock()
        self.api.message("to:me", "UME", t + 1)
        self.api.message("to:me", "UBOT", t + 2)
        self.api.message("to:me", "UPAL", t + 3, bot_id="B1")
        self.api.message("to:me", "UBOSS", t + 4, ch="DBOSS")
        self.clock.advance(10); self.poll()
        self.assertEqual(self.planes(), 0)
        self.w.seen = 0                                     # Mac asleep for an hour: stale messages
        self.api.found["to:me"] = []
        self.api.message("to:me", "UPAL", t - bridge.PLANE_FRESH - 60)
        self.poll()
        self.assertEqual(self.planes(), 0)

    def test_needs_you_and_night_get_no_plane(self):
        self.hook("PermissionRequest", "a")
        self.api.message("to:me", "UPAL", self.clock() + 1)
        self.clock.advance(5); self.poll()
        self.assertEqual(self.planes(), 0)

    def test_no_plane_while_youre_in_slack_and_letter_waits_a_poll(self):
        self.in_slack = True
        self.api.message("to:me", "UPAL", self.clock() + 1)
        self.api.unread["DBOSS"] = 1
        self.clock.advance(5); self.poll()
        self.assertEqual(self.planes(), 0)
        self.assertEqual(self.mood(), "idle")              # you may be reading it right now
        self.api.unread["DBOSS"] = 0
        self.poll()
        self.assertEqual(self.mood(), "idle")              # you did: no letter at all
        self.api.unread["DBOSS"], self.api.latest["DBOSS"] = 1, "7.0"
        self.poll(); self.poll()
        self.assertEqual(self.mood(), "letter")            # still unread a poll later: letter

    def test_only_searches_for_messages_to_you(self):
        self.poll()
        methods = {m for m, _ in self.api.calls}
        self.assertNotIn("conversations.history", methods)
        self.assertEqual({p["query"] for m, p in self.api.calls if m == "search.messages"}, {"to:me", "<@UME>"})
        self.assertEqual({p["channel"] for m, p in self.api.calls if m == "conversations.info"}, {"DBOSS"})   # only VIP DMs

    def test_without_a_token_nothing_is_called(self):
        with open(bridge.CONFIG_FILE, "w") as f:
            json.dump({}, f)
        self.poll()
        self.assertEqual(self.api.calls, [])

    def test_token_and_message_text_never_reach_the_log(self):
        self.api.message("to:me", "UPAL", self.clock() + 1)
        def boom(method, **p): raise RuntimeError("bad url https://slack.com/api/x token=xoxp-test-token")
        with self.assertLogs("bridge", level="INFO") as logs:
            self.clock.advance(5); self.poll()
            self.w.call = boom
            with mock.patch.object(bridge.time, "time", self.clock):
                try: self.w.poll(self.clock())
                except RuntimeError as e: self.w._fail(type(e).__name__ + ": " + str(e).replace(self.w.token, "<token>"))
        text = "\n".join(logs.output)
        self.assertNotIn("xoxp-test-token", text)
        self.assertNotIn("secret words", text)


WORK_TRANSCRIPT = "/Users/me/.claude-work/projects/-x/abc.jsonl"


class TwoAccounts(BridgeTest):
    """Personal and work Claude accounts running side by side (Claude Switcher)."""
    def copy(self, account, title=None, buttons=(), sidebar=()):
        c = {"account": account, "pages": {}, "sidebar": [{"row": r, "status": st} for r, st in sidebar], "tabs": [], "labels": []}
        if title: c["pages"][title] = {"buttons": list(buttons), "statuses": [], "markers": []}
        return c

    def setUp(self):
        super().setUp()
        self.pages = bridge.AppPages(self.b)
        self.b.app = self.pages

    def test_accounts_from_paths(self):
        self.assertEqual(bridge.account_of_config(WORK_TRANSCRIPT), "work")
        self.assertEqual(bridge.account_of_config("/Users/me/.claude/projects/-x/abc.jsonl"), "personal")
        self.assertEqual(bridge.account_of_config(None), "personal")
        self.assertEqual(bridge.account_of_data_dir("/Users/me/Library/Application Support/Claude-Work"), "work")
        self.assertEqual(bridge.account_of_data_dir("/Users/me/Library/Application Support/Claude"), "personal")

    def test_a_work_code_session_drives_lump(self):
        self.hook("UserPromptSubmit", "work1", transcript_path=WORK_TRANSCRIPT)
        self.assertEqual(self.mood(), "working")
        self.hook("PermissionRequest", "work1", transcript_path=WORK_TRANSCRIPT)
        self.assertEqual(self.mood(), "alert")
        self.hook("PostToolUse", "work1", transcript_path=WORK_TRANSCRIPT)
        self.hook("Stop", "work1", transcript_path=WORK_TRANSCRIPT)
        self.assertEqual(self.mood(), "done")
        self.assertEqual(self.b.status()["sessions"]["work1"]["account"], "work")

    def test_needs_you_in_either_account_wins(self):
        self.hook("UserPromptSubmit", "mine", transcript_path="/Users/me/.claude/projects/-x/m.jsonl")
        self.hook("PermissionRequest", "work1", transcript_path=WORK_TRANSCRIPT)
        self.assertEqual(self.mood(), "alert")
        self.assertEqual(self.b.status()["sessions"]["mine"]["account"], "personal")

    def test_usage_colour_follows_the_busier_account(self):
        self.b.set_usage({"five_hour": {"used_percentage": 30, "resets_at": NOON + 3600}})
        self.b.set_usage({"five_hour": {"used_percentage": 85, "resets_at": NOON + 3600}}, account="work")
        self.assertEqual(self.b.usage["five_hour"]["pct"], 30)
        self.assertEqual(self.b.usage["work:five_hour"]["pct"], 85)
        self.assertAlmostEqual(self.b.usage_frac(NOON), 0.85)

    def test_each_copy_of_the_app_has_its_own_chats(self):
        self.pages.snapshot({"trusted": True, "copies": [self.copy("personal", "Plans - Claude"),
                                                         self.copy("work", "Plans - Claude", buttons=["Stop response"])]})
        self.assertEqual(self.b.sessions["app:work:Plans"]["state"], "working")
        self.assertEqual(self.b.sessions["app:work:Plans"]["account"], "work")
        self.assertNotIn("app:Plans", self.b.sessions)
        self.assertEqual(self.mood(), "working")
        # the work copy quits: its conversation gets the short grace, the personal copy is untouched
        self.pages.snapshot({"trusted": True, "copies": [self.copy("personal", "Plans - Claude")]})
        self.assertEqual(self.b.sessions["app:work:Plans"]["stale"], bridge.AppPages.APP_STALE)

    def test_work_cowork_needing_you_from_the_sidebar(self):
        self.pages.snapshot({"trusted": True, "copies": [self.copy("work", sidebar=[("Quarterly deck", "Awaiting answer")])]})
        self.assertEqual(self.mood(), "alert")

    def test_an_older_helper_is_read_as_the_personal_copy(self):
        self.pages.snapshot({"trusted": True, "pages": {"Plans - Claude": {"buttons": ["Stop response"], "statuses": [], "markers": []}}})
        self.assertEqual(self.b.sessions["app:Plans"]["state"], "working")

    def test_a_chat_only_counts_as_seen_in_the_copy_in_front(self):
        self.pages.snapshot({"trusted": True, "copies": [self.copy("work", "Plans - Claude", buttons=["Stop response"])]})
        self.b.focus.front = True
        self.b.focus.account = "personal"                 # the work window is behind the personal one
        self.pages.snapshot({"trusted": True, "copies": [self.copy("work", "Plans - Claude")]})
        self.assertTrue(self.b.sessions["app:work:Plans"].get("unseen"))
        self.assertTrue(self.pages.showing("work:Plans", "work"))
        self.assertFalse(self.pages.showing("work:Plans", "personal"))

    def test_app_usage_ring_per_account(self):
        c = self.copy("work"); c["labels"] = ["Usage: Weekly · all models: 64%, Resets Mon 8:00 AM"]
        self.pages.snapshot({"trusted": True, "copies": [c]})
        self.assertEqual(self.b.usage["work:seven_day"]["pct"], 64)


class FocusAcrossAccounts(unittest.TestCase):
    """Which Code session is on screen, with both copies writing to the same log."""
    def setUp(self):
        self.support = tempfile.mkdtemp(prefix="deskbuddy-support-")
        for folder, local, cli in (("Claude", "local_aaa", "cli-personal"), ("Claude-Work", "local_bbb", "cli-work")):
            d = os.path.join(self.support, folder, "claude-code-sessions", "acct", "org")
            os.makedirs(d)
            with open(os.path.join(d, local + ".json"), "w") as f:
                json.dump({"cliSessionId": cli}, f)
        p = mock.patch.object(bridge.Focus, "SUPPORT", self.support)
        p.start(); self.addCleanup(p.stop)
        self.f = bridge.Focus(start=False)

    def test_each_copy_remembers_its_own_session(self):
        self.f.set_front("personal"); self.f._apply("local_aaa")
        self.f.set_front("work"); self.f._apply("local_bbb")
        self.assertEqual(self.f.code_session, "cli-work")
        self.f.set_front("personal")
        self.assertEqual(self.f.code_session, "cli-personal")

    def test_a_session_id_is_credited_to_its_own_copy_even_from_behind(self):
        self.f.set_front("personal"); self.f._apply("local_aaa")
        self.f._apply("local_bbb")                           # the work copy logged it in the background
        self.assertEqual(self.f.code_session, "cli-personal")
        self.assertEqual(self.f.focused["work"], "cli-work")

    def test_null_clears_the_copy_in_front(self):
        self.f.set_front("work"); self.f._apply("local_bbb"); self.f._apply("null")
        self.assertIsNone(self.f.code_session)
        self.f.set_front(None)                               # another app in front: remembers the last copy
        self.assertFalse(self.f.front)
        self.assertEqual(self.f.last_account, "work")

    def test_front_account_from_lsappinfo(self):
        with mock.patch.object(bridge, "account_of_pid", lambda pid: "work" if pid == 7 else "personal"):
            self.assertEqual(self.f._front_account('"CFBundleIdentifier"="com.anthropic.claudefordesktop"\n"pid"=7'), "work")
            self.assertEqual(self.f._front_account('"CFBundleIdentifier"="com.anthropic.claudefordesktop"\n"pid"=8'), "personal")
            self.assertIsNone(self.f._front_account('"CFBundleIdentifier"="com.apple.Terminal"\n"pid"=9'))


if __name__ == "__main__":
    unittest.main()
