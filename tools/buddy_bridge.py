"""Desk Buddy bridge: Claude Code hooks -> Lump on the ESP32.

Claude Code hooks POST their JSON to http://127.0.0.1:47823/hook. The bridge keeps
a little state per session, merges all sessions into one mood, and tells the board
over USB serial only when that mood changes:

  any session waiting on you (permission prompt, question, plan approval) -> alert
  otherwise any session working                                            -> working
  otherwise, something finished that you haven't looked at yet             -> awake
  otherwise                                                                -> idle (night after 22:00)
A session finishing always gets a celebration ("done"): 3.5 s if other things are still going
on (then back to alert/working), 8 s if not. Corner extras on the display ("o" command) show
how many sessions are working / waiting on you / finished but not looked at yet.

It also sends one-off events: "oops" when a tool call fails, and "hello" the first
time you start a session after a long break. The board itself handles escalation
(alert -> waiting after 45 s, working -> longhaul after 10 min) and idle fidgets.

Chat and Cowork have no hooks, so a small helper (tools/appwatch, its own login
item) reads the Claude app's accessibility tree and posts page states to /app.

Two Claude accounts (personal and work) can run side by side: Claude Switcher starts each as its own
copy of the app, the work one with its own data folder (Claude-Work) and Claude Code settings
(~/.claude-work, which has the same hooks and status line). Both feed the same mood, and the usage
colour follows whichever account is closer to a limit. See account_of_config.

Slack (class SlackWatch, with your own read-only user token in config.json): an unread DM from one
of your VIPs makes him hold up a letter until Slack says you've read it (or half an hour passes);
any other DM, or a message that @-mentions you, sends a paper plane looping round him, once.
Channel traffic that doesn't tag you is never looked at, and "needs you" from Claude always wins.

He also has a little ocarina voice that plays on the Mac's speakers (class Sounds): the WAVs
come from tools/make_sounds.py and which moments make a sound lives in ~/.desk-buddy/sounds.json
("enabled" is the master switch; Claude Switcher's right-click menu flips it).

It sends a heartbeat ("h", which also carries the time of day) every 15 s; without it the board
goes its own way after a minute and shows he's not connected. Session state is kept in
~/.desk-buddy/state.json across restarts.

Extras for debugging:  GET /status   POST /cmd  (body: a raw command such as "s done")
                       GET /sound    POST /sound (body: on | off | toggle | play <moment>)
                       (play tap_<gag>_<mood> plays a gag's sound, e.g. play tap_shock_idle)
Install as a login item with tools/install_bridge.sh.
"""
import glob, http.server, json, logging, logging.handlers, os, random, re, subprocess, sys, threading, time, wave
import serial

HTTP_PORT = 47823
HOME = os.environ.get("DESKBUDDY_HOME") or os.path.expanduser("~/.desk-buddy")   # tests point this elsewhere
NIGHT_START, NIGHT_END = 0, 8           # local hours: night (deep sleep under the blanket) is midnight to 8am
NIGHT_SETTLE = 5 * 60                   # after he was last up to something (or you were), regular sleep this long first


def night_hour(h):
    """Is local hour h in the night window? (Works whether or not the window wraps past midnight.)"""
    return NIGHT_START <= h < NIGHT_END if NIGHT_START < NIGHT_END else (h >= NIGHT_START or h < NIGHT_END)
DONE_SECS = 8                           # celebration when nothing else is going on
CELEBRATE_SECS = 3.5                    # quick celebration when other things are still going on
UNSEEN_SECS = 15 * 60                   # "check me" sparkle lasts until you reply, or this long
HELLO_AFTER = 2 * 3600                  # say hello if nothing happened for this long
# Give up on a state with no news. Pressing Esc fires no hook at all, so an interrupted
# session would otherwise look busy forever; a working session normally sends tool events
# every few seconds, so 8 quiet minutes means it's almost certainly stopped.
STALE = {"working": 8 * 60, "alert": 15 * 60}
NEEDS_YOU = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}
FORGET = 6 * 3600                       # drop sessions silent for this long
OOPS_GAP = 20                           # at most one oops per this many seconds
DEFAULT_COLOUR = "FF7A45"               # Tangerine
TAP_SNOOZE = 120                        # tapping "needs you" holds off the nagging this long
# Animations you can ask for from the computer (menu bar, hotkey, Shortcuts: POST /play <name>),
# mapped to the board's event. "tap" is a random gag picked by the board, as when you tap the glass
# (at night the startle, before a meeting the prep), but without a tap's meaning: nothing is snoozed
# or silenced. A named gag plays in whatever mood he's in.
GAGS = ("shock", "moto", "wolf", "accordion", "anvil", "disco", "glass", "banana", "balloon", "sneeze", "moon", "bomb")
ANIMATIONS = {"tap": "tap", **{g: f"tap_{g}" for g in GAGS}, "startle": "tap_startle_night", "prep": "tap_prep",
              **{e: e for e in ("fidget_toss", "fidget_twitch", "fidget_bubble", "oops", "hello")}}
# Usage gauge: his colour warms from Tangerine to red as the busier of your 5-hour / weekly
# Claude limits fills up (fed by the Claude Code status line, see tools/statusline.sh).
USAGE_STOPS = [(0.0, "FF7A45"), (0.5, "FF7A45"), (0.8, "FF4B2B"), (0.97, "C8102E"), (1.0, "8F7C78")]
# Meetings (read from ~/.desk-buddy/meetings.json, written by a calendar source): colour slides
# to MEETING_COLOUR over the 10 minutes before a meeting, he plays "meeting time" when it starts,
# and keeps the colour for the first few minutes.
MEETING_COLOUR = "3D8BFF"               # electric blue (reached via pink and purple, see meeting_blend)
MEETING_LEAD = 10 * 60
MEETING_ALERT_SECS = 45
MEETING_HOLD = 5 * 60
MEETINGS_FILE = os.path.join(HOME, "meetings.json")
CONFIG_FILE = os.path.join(HOME, "config.json")    # {"calendar_ics": "<link>" or ["<link>", ...], "my_email": "..." or [...]}
CALENDAR_EVERY = 5 * 60
# Sounds: see class Sounds. At night (NIGHT_START..NIGHT_END) he only makes noise if you've been
# active in the last NIGHT_AWAKE seconds, so a background job finishing at 2am stays silent.
SOUNDS_FILE = os.path.join(HOME, "sounds.json")
SOUNDS_DIR = os.path.join(HOME, "sounds")
SOUND_GAP = 2.0                         # at least this long between any two sounds...
SOUND_REPEAT = 5.0                      # ...and between two of the same moment
URGENT_SOUNDS = {"needs_you"}           # may cut in on the general gap
STILL_WAITING_AFTER = 45                # matches the board's alert -> waiting escalation
LONG_JOB_AFTER = 10 * 60                # matches the board's working -> longhaul
NIGHT_AWAKE = 15 * 60
FIDGET_EVERY = (5 * 60, 15 * 60)
# Gag sounds (tools/make_gag_sounds.py -> sounds/tap_<gag>.wav): the board prints "gag tap_<gag>_<mood>"
# the moment a gag starts. Each sound is timed from the gag's first drawing, which comes GAG_LEAD after
# that (his way in from the mood is padded to 12 frames at 24 fps); the night startle has no way in.
GAG_LEAD = 0.5
AFPLAY_LAG = 0.05                       # afplay takes a moment to start making sound
# Heartbeat: "h <seconds since local midnight>" every HEARTBEAT_EVERY. If the board hears nothing
# for a minute (bridge crashed, Mac asleep, USB power only) he goes his own way: idle and fidgeting,
# asleep at night, with a little unplugged-cable mark. The heartbeat also tells him the time.
HEARTBEAT_EVERY = 15
NET_PORT = 4210                                    # the board's command port on Wi-Fi (tools/wifi_setup.py)
STATE_FILE = os.path.join(HOME, "state.json")     # sessions etc. survive a bridge restart
# Slack, polled with your user token (see class SlackWatch). config.json: "slack_token" (xoxp-..., never
# logged), "slack_vips" (names as Slack shows them), optional "slack_vip_minutes" (how long a letter lasts).
SLACK_EVERY = 20                        # seconds between polls
SLACK_VIP_HOLD = 30 * 60                # a VIP's letter goes away by itself after this, read or not
PLANE_GAP = 60                          # at most one paper plane a minute, however many messages land
PLANE_FRESH = 10 * 60                   # older messages (the bridge was down, the Mac asleep) get no plane

os.makedirs(HOME, exist_ok=True)
log = logging.getLogger("bridge")
log.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(os.path.join(HOME, "bridge.log"), maxBytes=500_000, backupCount=2)
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
if sys.stderr.isatty():          # under launchd stderr is bridge.out, which only needs crashes
    log.addHandler(logging.StreamHandler())


def read_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


class Device:
    """Owns the link to the board: USB serial when it's plugged in, otherwise Wi-Fi (TCP to
    lump.local:4210, set up with tools/wifi_setup.py, which stores "lump_token" in config.json).
    Reconnects when the board is unplugged, reboots or drops off the network, and moves back to
    USB as soon as a cable appears."""

    USB_CHECK = 5                      # while on Wi-Fi, look for a USB cable this often (seconds)

    def __init__(self, on_ready, on_line=None):
        self.ser, self.lock, self.on_ready, self.on_line = None, threading.Lock(), on_ready, on_line
        self.link, self._last_err = None, {}
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def connected(self):
        return self.ser is not None

    @staticmethod
    def usb_ports():
        return sorted(glob.glob("/dev/cu.usbmodem*"))

    def _targets(self):
        if os.environ.get("DESKBUDDY_PORT"):
            return [os.environ["DESKBUDDY_PORT"]]
        cfg = read_config()
        net = [f"socket://{cfg.get('lump_host') or 'lump.local'}:{NET_PORT}"] if cfg.get("lump_token") else []
        return self.usb_ports() + net

    def _fail(self, p, e):
        msg = str(e)
        if self._last_err.get(p) != msg:   # a missing board is retried every few seconds: say it once
            self._last_err[p] = msg
            log.info("can't open %s: %s", p, msg)

    def _open(self):
        for p in self._targets():
            try:
                if p.startswith("socket://"):
                    import socket as so
                    # look up lump.local as IPv4 only: a general lookup waits 5 s for an IPv6
                    # answer the board never gives
                    host, port = p[len("socket://"):].rsplit(":", 1)
                    ip = so.getaddrinfo(host, int(port), so.AF_INET, so.SOCK_STREAM)[0][4][0]
                    s = serial.serial_for_url(f"socket://{ip}:{port}", timeout=0.3)
                    sock = getattr(s, "_socket", None)
                    if sock:                   # notice a board that vanished from the network
                        sock.setsockopt(so.SOL_SOCKET, so.SO_KEEPALIVE, 1)
                        for opt, v in (("TCP_KEEPALIVE", 10), ("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
                            if hasattr(so, opt):
                                try: sock.setsockopt(so.IPPROTO_TCP, getattr(so, opt), v)
                                except OSError: pass
                    s.write(f"k {read_config().get('lump_token', '')}\n".encode())
                    self.link = "wifi"
                else:
                    s = serial.Serial(); s.port, s.baudrate, s.timeout = p, 115200, 0.3
                    s.dtr = False; s.rts = False      # don't reset the board when opening
                    s.open()
                    self.link = "usb"
                self._last_err.pop(p, None)
                log.info("connected to %s", p)
                return s
            except Exception as e:
                self._fail(p, e)
        return None

    def _drop(self, why):
        log.info("%s", why)
        with self.lock:
            try: self.ser.close()
            except Exception: pass
            self.ser, self.link = None, None

    def _run(self):
        buf, usb_checked = b"", 0.0
        while True:
            if not self.ser:
                buf = b""
                self.ser = self._open()
                if not self.ser:
                    time.sleep(3); continue
                time.sleep(0.3); self._call(self.on_ready)
            if self.link == "wifi" and time.time() - usb_checked > self.USB_CHECK:
                usb_checked = time.time()
                if self.usb_ports() and not os.environ.get("DESKBUDDY_PORT"):
                    self._drop("USB cable found: switching from Wi-Fi"); continue
            try:
                buf += self.ser.read(256)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode(errors="replace").strip()
                    if text:
                        log.info("board: %s", text)
                    # board rebooted, or it had given up on us (no heartbeat) and gone its own way
                    if "lump ready" in text or text == "resync":
                        self._call(self.on_ready)
                    elif (text == "tap" or text.startswith("gag ")) and self.on_line:
                        self._call(self.on_line, text)
                    elif text == "err pairing":
                        log.info("the board refused our pairing token: run tools/wifi_setup.py again")
            except Exception as e:
                self._drop(f"{self.link or 'link'} lost: {e}")

    @staticmethod
    def _call(fn, *a):
        """Run a bridge callback; a bug in one must not take down the serial reader."""
        try: fn(*a)
        except Exception: log.exception("serial callback failed")

    def send(self, cmd, quiet=False):
        with self.lock:
            if not self.ser:
                return False
            try:
                self.ser.write((cmd + "\n").encode()); self.ser.flush()
                if not quiet: log.info("-> %s", cmd)
                return True
            except Exception as e:
                log.info("write failed: %s", e); return False


class Calendar:
    """Polls your calendars' private iCal links (config.json "calendar_ics": one link or a list,
    e.g. Google's "Secret address in iCal format" and Outlook's published ICS link) every 5 minutes
    and writes the next day and a half of timed meetings to meetings.json, which drives the
    blue countdown and the "meeting time" animation. Recurring meetings are expanded properly;
    all-day events, cancelled events, events marked "free", and (if "my_email" is set, one address
    or a list) meetings you've declined are skipped. The same meeting on two calendars counts once,
    and one calendar failing doesn't hide the others. Nothing leaves your Mac except the fetches."""

    def __init__(self):
        self.wake = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def refresh_now(self):
        self.wake.set()

    def _run(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                log.info("calendar refresh failed: %s", e)
            self.wake.wait(CALENDAR_EVERY)        # or sooner if someone asks (POST /calendar)
            self.wake.clear()

    @staticmethod
    def _list(v):
        """A config value that may be one string or a list of them -> clean list."""
        v = v if isinstance(v, list) else [v]
        return [str(x).strip() for x in v if x and str(x).strip()]

    @staticmethod
    def _fetch(url):
        import urllib.request
        if url.startswith("webcal://"):                    # Outlook and Apple hand these out
            url = "https://" + url[len("webcal://"):]
        return urllib.request.urlopen(url, timeout=20).read()

    @staticmethod
    def _host(url):
        from urllib.parse import urlparse
        return urlparse(url).hostname or "?"               # never log the secret path

    def refresh(self):
        try:
            cfg = json.load(open(CONFIG_FILE))
        except Exception:
            return
        urls = self._list(cfg.get("calendar_ics"))
        if not urls:
            return
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        mes = [m.lower() for m in self._list(cfg.get("my_email"))]
        out, ok = [], 0
        for url in urls:
            try:
                out += self.meetings(self._fetch(url), now, mes)
                ok += 1
            except Exception as e:
                log.info("calendar %s failed: %s", self._host(url), str(e).replace(url, "<link>").replace(url.split("://", 1)[-1], "<link>"))
        if not ok:
            return                                         # keep the last good meetings.json
        seen, merged = set(), []
        for m in sorted(out, key=lambda m: (m["start"], m["title"])):
            key = (m["start"], m["title"].strip().lower())
            if key not in seen:
                seen.add(key); merged.append(m)
        tmp = MEETINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f)
        os.replace(tmp, MEETINGS_FILE)
        log.info("calendar: %d upcoming meetings from %d/%d calendars", len(merged), ok, len(urls))

    @staticmethod
    def meetings(data, now, mes=()):
        """The timed, not-declined meetings in one iCal feed from an hour ago to 36 h ahead."""
        import datetime
        import icalendar, recurring_ical_events
        cal = icalendar.Calendar.from_ical(data)
        out = []
        for e in recurring_ical_events.of(cal).between(now - datetime.timedelta(hours=1), now + datetime.timedelta(hours=36)):
            st = e.get("DTSTART").dt
            if not isinstance(st, datetime.datetime):
                continue                                   # all-day
            if str(e.get("STATUS", "")).upper() == "CANCELLED" or str(e.get("TRANSP", "")).upper() == "TRANSPARENT":
                continue
            if mes:
                atts = e.get("ATTENDEE") or []
                atts = atts if isinstance(atts, list) else [atts]
                if any(str(a).lower().endswith(me) and str(a.params.get("PARTSTAT", "")).upper() == "DECLINED"
                       for a in atts for me in mes):
                    continue
            en = e.get("DTEND").dt if e.get("DTEND") else st + datetime.timedelta(minutes=30)
            if st.tzinfo is None: st = st.astimezone()
            if en.tzinfo is None: en = en.astimezone()
            out.append({"start": st.timestamp(), "end": en.timestamp(), "title": str(e.get("SUMMARY", ""))[:80]})
        return out


# ---- Claude accounts ----
# Claude Switcher runs each account as its own copy of the Claude app: personal with the default
# folders, any other with --user-data-dir=~/Library/Application Support/Claude-<Name> and
# CLAUDE_CONFIG_DIR=~/.claude-<name>. Personal is the plain default everywhere in the bridge; another
# account's Chat/Cowork sessions are "app:<account>:<name>" and its usage keys "<account>:five_hour".
PERSONAL = "personal"
APP_SUPPORT = os.path.expanduser("~/Library/Application Support")


def account_of_config(path):
    """The account behind a Claude Code path, e.g. a hook's transcript_path (~/.claude-work/... -> work)."""
    m = re.search(r"/\.claude-([^/]+)/", path or "")
    return m.group(1).lower() if m else PERSONAL


def account_of_data_dir(path):
    """The account behind a Claude app data folder (.../Application Support/Claude-Work -> work)."""
    name = os.path.basename((path or "").rstrip("/"))
    return name[len("Claude-"):].lower() if name.startswith("Claude-") else PERSONAL


def account_of_pid(pid):
    """Which account a running copy of the Claude app belongs to (from its --user-data-dir)."""
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return None
    m = re.search(r"--user-data-dir=(.+?)(?= --|$)", cmd.strip())
    return account_of_data_dir(m.group(1)) if m else PERSONAL


class Focus:
    """What you're looking at: whether the Claude app is in front, and which Code session it shows.

    The desktop app logs "LocalSessions.setFocusedSession: sessionId=local_…" whenever you switch
    Code sessions, and keeps a small file per session (claude-code-sessions/…/local_….json) whose
    cliSessionId is the session_id the hooks report. Chat/Cowork pages on screen come from the
    app watcher instead (AppPages.visible).

    With two accounts running, both copies of the app write to the same log. A session id says
    which copy it came from (its file is in that copy's data folder); "null" (no Code session
    showing) doesn't, so it's put down to the copy in front, or the one that was in front last."""

    LOGS = os.path.expanduser("~/Library/Logs/Claude")
    SUPPORT = APP_SUPPORT                 # Claude/ (personal), Claude-Work/ ... each with claude-code-sessions/
    FOCUS_RE = re.compile(r"setFocusedSession: sessionId=(local_[0-9a-f-]+|null)")

    def __init__(self, start=True):
        self.front, self._log, self._pos, self._map = False, None, 0, {}
        self.account, self.last_account = None, PERSONAL   # the Claude copy in front (None: none is), and the last one that was
        self.focused = {}                                 # account -> cli session id showing in that copy's Code tab
        self._pids = {}
        if start:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def code_session(self):
        """The Code session showing in the Claude copy in front (or the one in front last)."""
        return self.focused.get(self.account or self.last_account)

    def _cli_id(self, local):
        """(account, cliSessionId) for a desktop Code session; misses aren't cached (the file may come later)."""
        if local not in self._map:
            files = glob.glob(os.path.join(glob.escape(self.SUPPORT), "Claude*", "claude-code-sessions", "*", "*", local + ".json"))
            if not files:
                return None, None
            path = files[0]
            acct = account_of_data_dir(path[:path.index(os.sep + "claude-code-sessions" + os.sep)])
            try:
                with open(path) as f: self._map[local] = (acct, json.load(f).get("cliSessionId"))
            except Exception: return acct, None
        return self._map[local]

    def _apply(self, local):
        if local == "null":
            self.focused[self.account or self.last_account] = None
            return
        acct, cli = self._cli_id(local)
        self.focused[acct or self.account or self.last_account] = cli

    def set_front(self, account):
        """account: the Claude copy now in front, or None when another app is."""
        self.front, self.account = account is not None, account
        if account:
            self.last_account = account

    def _front_account(self, info):
        """From `lsappinfo info -only bundleid -only pid` output: the Claude copy in front, or None."""
        if "com.anthropic.claudefordesktop" not in info:
            return None
        m = re.search(r'"pid"=(\d+)', info)
        if not m:
            return PERSONAL
        pid = int(m.group(1))
        if pid not in self._pids:
            if len(self._pids) > 50: self._pids.clear()
            self._pids[pid] = account_of_pid(pid)
        return self._pids[pid] or PERSONAL

    def _poll_log(self):
        logs = sorted(glob.glob(os.path.join(self.LOGS, "main*.log")), key=os.path.getmtime)
        if not logs: return
        if logs[-1] != self._log:                    # first run or the app rotated its log
            self._log = logs[-1]
            size = os.path.getsize(self._log)
            with open(self._log, "rb") as f:          # catch up from the recent past
                f.seek(max(0, size - 4_000_000)); hits = self.FOCUS_RE.findall(f.read().decode(errors="replace"))
            if hits: self._apply(hits[-1])
            self._pos = size
            return
        size = os.path.getsize(self._log)
        if size < self._pos: self._pos = 0
        if size > self._pos:
            with open(self._log, "rb") as f:
                f.seek(self._pos); hits = self.FOCUS_RE.findall(f.read().decode(errors="replace"))
            self._pos = size
            if hits: self._apply(hits[-1])

    def _run(self):
        import subprocess
        while True:
            try:
                info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", "-only", "pid", subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()],
                                      capture_output=True, text=True, timeout=2).stdout
                self.set_front(self._front_account(info))
                self._poll_log()
            except Exception as e:
                log.info("focus check failed: %s", e)
            time.sleep(1.5)


class Sounds:
    """Plays Lump's voice through afplay. Settings are re-read whenever sounds.json changes, so
    flipping "enabled" (Claude Switcher, POST /sound, or by hand) takes effect immediately.
      {"enabled": true, "volume": 1.0, "quiet_at_night": true,
       "moments": {"needs_you": {"on": true, "pick": ["a", "c"]}, ...}}
    Several picks: a random one each time, never the same one twice in a row."""

    def __init__(self):
        self.lock, self.cfg, self.mtime = threading.Lock(), {}, None
        self.last_any, self.last_at, self.last_pick, self.proc = 0.0, {}, {}, None
        self.gag_until, self.gag_timer = 0.0, None             # a gag's sound is playing (or about to) until then

    def settings(self):
        try:
            m = os.path.getmtime(SOUNDS_FILE)
            if m != self.mtime:
                with open(SOUNDS_FILE) as f:
                    self.cfg = json.load(f)
                self.mtime = m
        except FileNotFoundError:
            self.cfg, self.mtime = {}, None
        except Exception as e:
            log.info("can't read %s: %s", SOUNDS_FILE, e)     # keep the last good settings
        return self.cfg

    def set_enabled(self, value):
        with self.lock:
            cfg = dict(self.settings())
            cfg["enabled"] = (not cfg.get("enabled", False)) if value == "toggle" else bool(value)
            tmp = SOUNDS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, SOUNDS_FILE)
            log.info("sounds %s", "on" if cfg["enabled"] else "off")
            return cfg["enabled"]

    def play(self, moment, force=False):
        """Play one of the picked versions of a moment. force skips every check (for testing)."""
        with self.lock:
            cfg, now = self.settings(), time.time()
            m = (cfg.get("moments") or {}).get(moment, {})
            picks = m.get("pick") or []
            if not force:
                if not cfg.get("enabled") or not m.get("on") or not picks:
                    return False                              # switched off: nothing worth logging
                why = ("same sound %.0fs ago" % (now - self.last_at[moment]) if now - self.last_at.get(moment, 0) < SOUND_REPEAT else
                       "another sound %.1fs ago" % (now - self.last_any) if now - self.last_any < SOUND_GAP and moment not in URGENT_SOUNDS else
                       "a gag is playing" if now < self.gag_until and moment not in URGENT_SOUNDS else None)   # don't talk over a gag
                if why:
                    log.info("sound %s skipped: %s", moment, why); return False
            if not picks:
                picks = sorted(os.path.basename(p)[len(moment) + 1:-4] for p in glob.glob(os.path.join(SOUNDS_DIR, moment + "_?.wav")))
            pool = [k for k in picks if k != self.last_pick.get(moment)] or picks
            if not pool:
                return False
            k = random.choice(pool)
            path = os.path.join(SOUNDS_DIR, f"{moment}_{k}.wav")
            if not os.path.exists(path):
                log.info("sound %s missing (run tools/make_sounds.py)", path)
                return False
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()                         # one voice at a time
            try:
                self.proc = subprocess.Popen(["afplay", "-v", str(cfg.get("volume", 1.0)), path],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log.info("afplay failed: %s", e); return False
            self.last_any = self.last_at[moment] = now
            self.last_pick[moment] = k
            log.info("sound %s %s", moment, k)
            return True


    def play_gag(self, event, force=False):
        """The board started a gag (event "tap_<gag>_<mood>"): play its sound in step with the drawings,
        at the volume from sounds.json. On/off: "enabled" and the "tap_gags" moment (on if missing)."""
        gag, _, mood = event[4:].rpartition("_")
        if gag == "prep" and mood == "meeting":
            gag = "prep_alarm"                                # at the alarm itself, prep starts with the clock ringing
        path = os.path.join(SOUNDS_DIR, f"tap_{gag}.wav")
        with self.lock:
            cfg = self.settings()
            if not force and (not cfg.get("enabled") or not (cfg.get("moments") or {}).get("tap_gags", {"on": True}).get("on", True)):
                return False
            if not os.path.exists(path):
                log.info("sound %s missing (run tools/make_gag_sounds.py)", path); return False
            try:
                with wave.open(path) as w:
                    length = w.getnframes() / w.getframerate()
            except Exception:
                length = 5.0
            delay = max(0.0, (0 if gag == "startle" else GAG_LEAD) - AFPLAY_LAG)
            if self.gag_timer:
                self.gag_timer.cancel()
            self.gag_until = time.time() + delay + length
            self.gag_timer = threading.Timer(delay, self._start_gag, (path, gag))
            self.gag_timer.daemon = True
            self.gag_timer.start()
            return True

    def _start_gag(self, path, gag):
        with self.lock:
            volume = self.settings().get("volume", 1.0)          # read now, so a just-moved slider counts
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()                         # one voice at a time
            try:
                self.proc = subprocess.Popen(["afplay", "-v", str(volume), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log.info("afplay failed: %s", e); return
            self.last_any = time.time()
            log.info("sound gag %s (volume %s)", gag, volume)


def _hex(c): return [int(c[i:i + 2], 16) for i in (0, 2, 4)]
def _mix(a, b, k): A, B = _hex(a), _hex(b); return "".join(f"{round(x + (y - x) * k):02X}" for x, y in zip(A, B))


# --- vivid blending: OKLCH, around the colour wheel (a straight RGB blend from orange toward
# blue/teal passes through grey). Same maths as the colour lab on design/lump.html.
def _s2l(c): return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
def _l2s(c): return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hex_to_oklch(h):
    import math
    r, g, b = (_s2l(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return L, math.hypot(A, B), (math.degrees(math.atan2(B, A)) + 360) % 360


def oklch_to_hex(L, C, H):
    import math
    for _ in range(40):                       # pull chroma in until it fits sRGB
        A, B = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
        l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3
        m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3
        s = (L - 0.0894841775 * A - 1.2914855480 * B) ** 3
        rgb = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
               -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
               -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s]
        if all(-0.002 <= v <= 1.002 for v in rgb):
            return "".join(f"{round(_l2s(min(1, max(0, v))) * 255):02X}" for v in rgb)
        C *= 0.93
    return "888888"


def meeting_blend(base, t):
    """Countdown colour: from his current colour, hue going down (through pink and purple) to blue."""
    L1, C1, H1 = hex_to_oklch(base)
    L2, C2, H2 = hex_to_oklch(MEETING_COLOUR)
    dh = H2 - H1
    if dh > 0: dh -= 360
    return oklch_to_hex(L1 + (L2 - L1) * t, max(0.13, C1 + (C2 - C1) * t), (H1 + dh * t + 360) % 360)


def usage_colour(frac):
    frac = max(0.0, min(1.0, frac))
    for (f0, c0), (f1, c1) in zip(USAGE_STOPS, USAGE_STOPS[1:]):
        if frac <= f1:
            return _mix(c0, c1, 0 if f1 == f0 else (frac - f0) / (f1 - f0))
    return USAGE_STOPS[-1][1]


class Buddy:
    def __init__(self, dev=None, focus=None, sounds=None, start=True):
        self.lock = threading.Lock()
        self.sessions = {}            # session_id -> {"state", "t", "cwd"}
        self.last_event = 0.0
        self.sent_mood = None
        self.sent_overlay = None
        self.celebrate_until = 0.0
        self.usage = {}                # from the status line: five_hour / seven_day pct + resets
        self.sent_colour = None
        self.sent_prep = None          # meeting countdown flag last sent (p 0 / p 1)
        self.meeting_alerted = set()   # meeting start times we've already announced
        self.last_oops = 0.0
        self.resync_at = 0.0
        self.sounds = sounds or Sounds()
        self.last_active = 0.0         # last time you did something (prompt, answer, new session)
        self.last_up = time.time()     # last time he was showing anything but sleep (night waits NIGHT_SETTLE after it)
        self.goodnight_night = None    # which night we've already said goodnight for
        self.woke_at = 0.0
        self.working_since, self.long_played = None, False
        self.next_fidget = time.time() + random.uniform(*FIDGET_EVERY)
        self.focus = focus or Focus()
        self.meeting_dismissed = set()            # meeting starts you've tapped to silence
        self.last_beat = 0.0
        self.saved = None                         # last state.json we wrote
        self.letters = {}                         # Slack: VIP DMs you haven't read, channel -> {"from", "ts", "since"}
        self.last_plane = 0.0
        self.load_state()
        self.dev = dev or Device(self.resend, self.board_line)
        if start:
            threading.Thread(target=self._tick, daemon=True).start()

    # ---- surviving restarts ----
    # Code sessions, usage readings and a few "already done that" markers are kept in state.json so
    # reinstalling or restarting the bridge doesn't forget a pending "needs you" or unread result.
    # Chat/Cowork ("app:") sessions aren't kept: the app watcher re-reports them within seconds.
    def state_locked(self):
        return {"sessions": {k: v for k, v in self.sessions.items() if not k.startswith("app:")},
                "usage": self.usage, "last_event": self.last_event, "last_active": self.last_active,
                "goodnight_night": self.goodnight_night,
                "meeting_dismissed": sorted(self.meeting_dismissed), "meeting_alerted": sorted(self.meeting_alerted),
                "letters": self.letters}

    def save_state(self):
        with self.lock:
            data = json.dumps(self.state_locked(), sort_keys=True)
        if data == self.saved:
            return
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, STATE_FILE)
        self.saved = data

    def load_state(self):
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.info("ignoring unreadable %s: %s", STATE_FILE, e); return
        now = time.time()
        self.sessions = {k: v for k, v in (st.get("sessions") or {}).items()
                         if isinstance(v, dict) and now - v.get("t", 0) < FORGET}
        self.usage = st.get("usage") or {}
        self.last_event = st.get("last_event", 0.0)
        self.last_active = st.get("last_active", 0.0)
        self.goodnight_night = st.get("goodnight_night")
        self.meeting_dismissed = set(st.get("meeting_dismissed") or [])
        self.meeting_alerted = set(st.get("meeting_alerted") or [])
        self.letters = {k: v for k, v in (st.get("letters") or {}).items() if isinstance(v, dict) and v.get("since")}
        log.info("restored %d session(s) from %s", len(self.sessions), STATE_FILE)

    # ---- session bookkeeping ----
    def hook(self, p):
        ev, sid, now = p.get("hook_event_name", ""), p.get("session_id", "?"), time.time()
        event = None
        with self.lock:
            fresh = now - self.last_event > HELLO_AFTER
            self.last_event = now
            s = self.sessions.setdefault(sid, {"state": "idle", "t": now, "cwd": p.get("cwd", "")})
            if p.get("transcript_path"):
                s["account"] = account_of_config(p["transcript_path"])
            state = None
            if ev == "SessionStart":
                state = "idle"
                if p.get("source") in (None, "startup", "resume"):
                    self.last_active = now
                if fresh and self.mood_locked(now) in ("idle", "night"):
                    event = "hello"
            elif ev == "UserPromptSubmit":
                state = "working"
                s.pop("unseen", None)           # you're back in this session: result seen
                self.last_active = now
                self.sound("on_it")
            elif ev == "PreToolUse":
                state = "alert" if p.get("tool_name") in ("AskUserQuestion", "ExitPlanMode") else "working"
            elif ev in ("PermissionRequest", "Elicitation"):
                state = "alert"
            elif ev in ("PostToolUse", "PermissionDenied", "ElicitationResult"):
                state = "working"
            elif ev == "PostToolUseFailure":
                state = "working"
                if now - self.last_oops > OOPS_GAP and not p.get("is_interrupt"):
                    event = "oops"                # only played if he's showing "working" (see below)
            elif ev == "Notification":
                kind = (p.get("notification_type") or "").lower()
                if kind in NEEDS_YOU:
                    state = "alert"
                # idle_prompt ("still waiting for your input") and the rest aren't new requests: ignore
            elif ev in ("Stop", "StopFailure"):
                state = "done"
            elif ev == "SessionEnd":
                self.sessions.pop(sid, None)
            if state:
                self._set(s, state, now)
            if event == "oops":
                # a failure never interrupts "needs you": only show it while he's working
                if self.base_locked(now) == "working":
                    self.last_oops = now
                else:
                    event = None
            log.info("hook %s %s%s -> %s", ev, sid[:8], "" if s.get("account", PERSONAL) == PERSONAL else " (%s)" % s["account"], state or "-")
        if event:
            self.event(event)
            self.sound({"hello": "hello", "oops": "uh_oh"}[event])
        self.update()

    def external(self, sid, state, stale=None, account=PERSONAL):
        """State from a non-hook source (the app watcher for Chat and Cowork)."""
        now = time.time()
        with self.lock:
            fresh = now - self.last_event > HELLO_AFTER
            self.last_event = now
            s = self.sessions.setdefault(sid, {"state": "idle", "t": now, "cwd": ""})
            s["account"] = account
            if state in ("working", "alert") and s.get("state") == "done":
                s.pop("unseen", None)           # a new message in that conversation: result seen
            if state == "working" and s.get("state") in ("idle", "done"):
                self.last_active = now          # you just sent a message there
            self._set(s, state, now)
            s["stale"] = stale
            log.info("app %s -> %s", sid[:40], state)
        self.update()

    def forget(self, sid):
        with self.lock:
            if self.sessions.pop(sid, None) is not None:
                log.info("app %s -> gone", sid[:40])
        self.update()

    # ---- Slack ----
    def letter_hold(self):
        try: return float(read_config().get("slack_vip_minutes") or 0) * 60 or SLACK_VIP_HOLD
        except (TypeError, ValueError): return SLACK_VIP_HOLD

    def letters_locked(self, now):
        """VIP letters still worth holding up (each lasts until read, or letter_hold() at most). A timed-out
        one is kept, marked expired, so the same unread message doesn't bring it straight back."""
        hold = self.letter_hold()
        for l in self.letters.values():
            if not l.get("expired") and now - l["since"] > hold:
                log.info("slack: letter from %s timed out", l.get("from"))
                l["expired"] = True
        return {ch: l for ch, l in self.letters.items() if not l.get("expired")}

    def slack_letter(self, ch, sender, ts):
        """An unread DM from a VIP (ts: Slack's id for the newest one). He holds up a letter."""
        with self.lock:
            if (self.letters.get(ch) or {}).get("ts") == ts:
                return False
            new = ch not in self.letters_locked(time.time())
            self.letters[ch] = {"from": sender, "ts": ts, "since": time.time()}
        log.info("slack: letter from %s", sender)
        if new:
            self.sound("slack_vip")
        self.update()
        return True

    def slack_read(self, ch):
        """Slack no longer counts that VIP's DM as unread: put the letter away."""
        with self.lock:
            l = self.letters.pop(ch, None)
        if l:
            log.info("slack: letter from %s read", l.get("from"))
            self.update()

    def slack_plane(self, sender, kind):
        """A DM or @-mention from anyone else: a paper plane, once. Not while he needs you (Claude wins),
        at the meeting alarm, mid-celebration, at night, or within PLANE_GAP of the last one."""
        now = time.time()
        with self.lock:
            mood = self.mood_locked(now)
            if mood in ("alert", "meeting", "done", "night") or now - self.last_plane < PLANE_GAP:
                log.info("slack: %s from %s, no plane (%s)", kind, sender, mood if mood in ("alert", "meeting", "done", "night") else "one just flew")
                return False
            self.last_plane = now
        log.info("slack: %s from %s -> plane", kind, sender)
        return self.event("tap_plane")

    def _set(self, s, state, now):
        """Record a session's new state; a finish starts a celebration and a "check me" mark."""
        was = s.get("state")
        s["state"], s["t"] = state, now
        if state == "alert" and was != "alert":
            s.pop("nagged", None)
            s.pop("snooze", None)                   # a new request: no longer snoozed
            self.sound("needs_you")
        elif was == "alert" and state == "working":
            self.last_active = now                  # you answered: he's back to work
            self.sound("thanks")
        if state == "done" and was != "done":
            watching = self.on_screen_locked(s)
            if not watching:
                s["unseen"] = now                   # finished while you weren't looking at it
            # celebrate briefly if other things are still going on, longer if it's all quiet
            busy = self.base_locked(now, skip=s) in ("alert", "working")
            self.celebrate_until = max(self.celebrate_until, now + (CELEBRATE_SECS if busy else DONE_SECS))
            if not watching:                        # you can already see that reply: no need to call out
                self.sound("finished_while_busy" if busy else "finished")
            else:
                log.info("sound finished skipped: you're looking at it")

    def board_line(self, text):
        if text == "tap":
            self.tapped()
        elif text.startswith("gag "):
            self.gag_sound(text[4:].strip())

    def gag_sound(self, event):
        """A gag just started on the board: its sound, unless it's night and you've not been around
        (a tap counts as being around, so tapping him at night still gets the startle's sound)."""
        now = time.time()
        h = time.localtime(now).tm_hour
        if (night_hour(h) and now - self.last_active > NIGHT_AWAKE
                and self.sounds.settings().get("quiet_at_night", True)):
            return False
        return self.sounds.play_gag(event)

    def tapped(self):
        """You tapped the glass. What it means depends on what he's showing:
        meeting alarm -> silence it; needs you -> "seen it", snooze 2 min (amber dots stay);
        awake with unread results -> mark them read so he can sleep. Otherwise the board giggles."""
        now = time.time()
        with self.lock:
            mood, base = self.mood_locked(now), self.base_locked(now)
            m = self.next_meeting(now)
            if mood == "meeting" and m:
                self.meeting_dismissed.add(m["start"]); what = "meeting alarm silenced"
            elif base == "alert":
                for st in self.sessions.values():
                    if self._effective(st, now) == "alert":
                        st["snooze"] = now + TAP_SNOOZE
                what = "needs-you snoozed"
            elif base == "awake":
                for st in self.sessions.values():
                    st.pop("unseen", None)
                what = "marked as read"
            else:
                what = "giggle"
            self.last_active = now
        log.info("tap: %s", what)
        self.update()

    def sound(self, moment):
        """Play a moment's sound, unless it's night and you've not been around for a while
        (goodnight is the exception: it's the last thing he says)."""
        now = time.time()
        h = time.localtime(now).tm_hour
        night = night_hour(h)
        if (night and moment != "goodnight" and now - self.last_active > NIGHT_AWAKE
                and self.sounds.settings().get("quiet_at_night", True)):
            log.info("sound %s skipped: night and you've been away", moment); return False
        return self.sounds.play(moment)

    def on_screen_locked(self, s):
        """Is this session's conversation showing in the Claude app, with the app in front?"""
        if not self.focus.front:
            return False
        sid = next((k for k, v in self.sessions.items() if v is s), None)
        if sid is None: return False
        if sid.startswith("app:"):
            app = getattr(self, "app", None)
            return bool(app) and app.showing(sid[4:], getattr(self.focus, "account", None))
        return sid == self.focus.code_session

    def clear_seen_locked(self):
        for s in self.sessions.values():
            if s.get("unseen") and self.on_screen_locked(s):
                s.pop("unseen", None)

    def _effective(self, s, now):
        st, age = s["state"], now - s["t"]
        limit = s.get("stale") or STALE.get(st)
        if st in STALE and limit and age > limit:
            return "idle"
        if st == "alert" and s.get("snooze", 0) > now:
            return "snoozed"                        # you tapped to say "seen it": hold off nagging
        return "idle" if st == "done" else st

    def counts_locked(self, now):
        for sid, s in list(self.sessions.items()):
            if now - s["t"] > FORGET:
                del self.sessions[sid]
        self.clear_seen_locked()                    # looking at it counts as reading it
        eff = [self._effective(s, now) for s in self.sessions.values()]
        unseen = sum(1 for s in self.sessions.values() if s.get("unseen") and now - s["unseen"] < UNSEEN_SECS)
        return {"alert": eff.count("alert") + eff.count("snoozed"), "working": eff.count("working"), "unseen": unseen}

    def base_locked(self, now, skip=None):
        """Most urgent ongoing state across sessions: alert > working > awake (something finished
        that you haven't looked at yet) > asleep (idle, or night)."""
        eff = {self._effective(s, now) for s in self.sessions.values() if s is not skip}
        if "alert" in eff: return "alert"
        if "working" in eff: return "working"
        if "snoozed" in eff or any(s.get("unseen") and now - s["unseen"] < UNSEEN_SECS for s in self.sessions.values() if s is not skip):
            return "awake"
        if not night_hour(time.localtime(now).tm_hour):
            return "idle"
        # just been up to something (or you have): plain sleep for a few minutes, then the blanket comes up
        return "idle" if now - max(self.last_up, self.last_active) < NIGHT_SETTLE else "night"

    def next_meeting(self, now):
        try:
            with open(MEETINGS_FILE) as f:
                ms = json.load(f)
        except Exception:
            return None
        live = [m for m in ms if m.get("start") and now < m["start"] + MEETING_HOLD]
        return min(live, key=lambda m: m["start"]) if live else None

    def mood_locked(self, now):
        mood = self._pick_mood_locked(now)
        if mood not in ("idle", "night"):
            self.last_up = now                  # night waits NIGHT_SETTLE after this
        return mood

    def _pick_mood_locked(self, now):
        self.counts_locked(now)                 # also drops forgotten sessions
        m = self.next_meeting(now)
        if m and 0 <= now - m["start"] < MEETING_ALERT_SECS and m["start"] not in self.meeting_dismissed:
            return "meeting"                    # it's time: this beats everything, briefly
        if now < self.celebrate_until:
            return "done"                       # a finish just happened: celebrate, then back to base
        base = self.base_locked(now)
        if base != "alert" and self.letters_locked(now):
            return "letter"                     # a VIP wrote: beats everything but "needs you"
        return base

    def mood(self):
        with self.lock:
            return self.mood_locked(time.time())

    # ---- talking to the board ----
    def update(self, force=False):
        with self.lock:
            now = time.time()
            m, c = self.mood_locked(now), self.counts_locked(now)
        if force or m != self.sent_mood:
            if m == "night" and self.sent_mood not in (None, "night"):
                night = time.strftime("%Y-%m-%d", time.localtime(now - NIGHT_END * 3600))
                if self.goodnight_night != night:        # once a night, however often he dozes off
                    self.goodnight_night = night
                    self.sound("goodnight")
            if self.dev.send("s " + m):
                self.sent_mood = m
        colour = self.colour(now)
        if force or colour != self.sent_colour:
            if self.dev.send("c " + colour):
                self.sent_colour = colour
        # corner extras on the display: things working / waiting / finished-but-unseen
        overlay = f"o {c['working']} {c['alert']} {c['unseen']}"
        if force or overlay != self.sent_overlay:
            if self.dev.send(overlay):
                self.sent_overlay = overlay
        # meeting countdown (or its alarm) running: a tap on the glass then plays meeting prep
        prep = "p 1" if self.prep_time(now) else "p 0"
        if force or prep != self.sent_prep:
            if self.dev.send(prep):
                self.sent_prep = prep

    def prep_time(self, now):
        """True from MEETING_LEAD before a meeting until its alarm window is over."""
        m = self.next_meeting(now)
        return bool(m) and -MEETING_ALERT_SECS <= m["start"] - now < MEETING_LEAD

    def usage_frac(self, now):
        fr = [w["pct"] / 100 for w in self.usage.values() if w.get("pct") is not None
              and ((w.get("resets") and now < w["resets"]) or (not w.get("resets") and now - w["t"] < 12 * 3600))]
        return max(fr) if fr else None

    def colour(self, now):
        """Meeting countdown beats the usage gauge, which beats plain Tangerine."""
        base = DEFAULT_COLOUR
        frac = self.usage_frac(now)
        if frac is not None:
            base = usage_colour(round(frac * 50) / 50)      # 2% steps: no flicker
        m = self.next_meeting(now)
        if m:
            until = m["start"] - now
            if until <= 0:
                return MEETING_COLOUR
            if until < MEETING_LEAD:
                return meeting_blend(base, round((1 - until / MEETING_LEAD) * 40) / 40)
        return base

    def set_usage(self, rl, source="statusline", account=PERSONAL):
        """Usage readings from the Claude Code status line, or the Claude app's usage ring. Each account
        has its own limits (keys "five_hour", "work:five_hour", ...); the colour follows the busiest."""
        changed = False
        with self.lock:
            for window in ("five_hour", "seven_day"):
                key = window if account == PERSONAL else f"{account}:{window}"
                w = (rl or {}).get(window)
                if isinstance(w, dict) and w.get("used_percentage") is not None:
                    old = self.usage.get(key, {})
                    resets = w.get("resets_at") or (old.get("resets") if source == "app" else None)
                    new = {"pct": float(w["used_percentage"]), "resets": resets, "t": time.time(), "source": source}
                    changed |= old.get("pct") != new["pct"]
                    self.usage[key] = new
        if changed:
            log.info("usage %s", {k: round(v["pct"]) for k, v in self.usage.items()})
        self.update()

    def resend(self):
        self.sent_mood = self.sent_overlay = self.sent_colour = self.sent_prep = None
        self.last_beat = 0.0                            # and tell him the time straight away
        if time.time() - self.woke_at > 60:            # board plugged in or restarted
            self.woke_at = time.time()
            self.sound("waking_up")
        self.update()

    def sound_tick(self, now):
        """Sounds that come from time passing rather than from a hook."""
        play = []
        with self.lock:
            for s in self.sessions.values():                 # one nudge per unanswered request
                if s["state"] == "alert" and not s.get("nagged") and now - s["t"] >= STILL_WAITING_AFTER \
                        and self._effective(s, now) == "alert":
                    s["nagged"] = True
                    play.append("still_waiting")
            base = self.base_locked(now)
            if base == "working":
                self.working_since = self.working_since or now
                if now - self.working_since >= LONG_JOB_AFTER and not self.long_played:
                    self.long_played = True
                    play.append("long_job")
            else:
                self.working_since, self.long_played = None, False
            if now >= self.next_fidget:
                self.next_fidget = now + random.uniform(*FIDGET_EVERY)
                if base == "idle" and now - self.last_active < 30 * 60:   # only if you're around
                    play.append("fidget")
        for moment in dict.fromkeys(play):
            self.sound(moment)

    def event(self, name):
        # an event plays in its home mood (oops: working, hello: idle); the board walks
        # there by itself, so re-assert our mood once it has finished
        if self.dev.send("e " + name):
            self.resync_at = time.time() + 8
            return True
        return False

    def play(self, name):
        """An animation asked for from the computer: None if there's no such animation,
        otherwise whether the board got it."""
        ev = ANIMATIONS.get(name.strip().lower())
        if ev is None:
            return None
        log.info("play: %s", name.strip().lower())
        return self.event(ev)

    def _tick(self):
        # Nothing in here may kill this loop: it drives the board, the heartbeat and saving. (If it
        # did die anyway, the heartbeat would stop and the board would show he's on his own.)
        while True:
            time.sleep(1)
            try: self.tick(time.time())
            except Exception: log.exception("tick failed")

    def tick(self, now):
        if self.resync_at and now > self.resync_at:
            self.resync_at = 0; self.update(force=True)
        else:
            self.update()
        if now - self.last_beat >= HEARTBEAT_EVERY:
            lt = time.localtime(now)
            if self.dev.send(f"h {lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec}", quiet=True):
                self.last_beat = now
        try: self.sound_tick(now)
        except Exception: log.exception("sound tick failed")
        try: self.save_state()
        except Exception: log.exception("saving %s failed", STATE_FILE)

    def status(self):
        with self.lock:
            now = time.time()
            app = getattr(self, "app", None)
            return {"mood": self.mood_locked(now), "sent": self.sent_mood, "counts": self.counts_locked(now), "device": self.dev.connected, "link": getattr(self.dev, "link", None),
                    "colour": self.colour(now), "usage": self.usage,
                    "sound": bool(self.sounds.settings().get("enabled")),
                    "next_meeting": self.next_meeting(now),
                    "focus": {"claude_in_front": self.focus.front, "account": getattr(self.focus, "account", None), "code_session": (self.focus.code_session or "")[:8],
                              "app_pages": sorted(getattr(getattr(self, "app", None), "visible", []))},
                    "app_watcher": None if not app or not app.seen else {"trusted": app.trusted, "last_report_s": int(now - app.seen)},
                    "slack": {"letters": {v.get("from"): int(now - v["since"]) for v in self.letters_locked(now).values()},
                              "watch": getattr(getattr(self, "slack", None), "status", None)},
                    "sessions": {(k if k.startswith("app:") else k[:8]): {"state": v["state"], "age_s": int(now - v["t"]), "cwd": v["cwd"],
                                                                          "account": v.get("account", PERSONAL)} for k, v in self.sessions.items()}}


class AppPages:
    """Chat and Cowork state from the deskbuddy-appwatch helper (tools/appwatch), which reports raw
    labels read from the Claude app's accessibility tree. The rules for turning those labels into
    moods live here, so they can be tuned without rebuilding (and re-permitting) the helper.

    Each conversation becomes a session "app:<name>" ("app:work:<name>" in the work account's copy).
    Sidebar status badges cover conversations that aren't on screen; a page that leaves the screen
    while working gets a short expiry.

    The helper reports {"trusted", "copies": [{"account", "pages", "sidebar", "tabs", "labels"}, ...]},
    one entry per running copy of the app. (An older helper sent one copy's fields at the top level:
    that's read as the personal copy.) A copy that stops being reported (quit) counts as empty."""

    APP_STALE = 3 * 60
    # Claude's own "Stop response" button. Not a bare "Stop": web pages open in the app (like the gag
    # sound review page, with its own Stop button for audio) would otherwise look like a chat responding.
    STOP_BTN = re.compile(r"^stop (response|generating)$", re.I)
    NEEDS_BTN = re.compile(r"\b(allow|approve|deny|don.?t allow|reject|decline)\b", re.I)
    WORK_STATUS = re.compile(r"^(claude is (responding|thinking)|running a command.*|working…?|running|thinking|responding)$", re.I)
    USAGE_LABEL = re.compile(r"^usage:\s*(.+?):\s*(\d+(?:\.\d+)?)\s*%", re.I)
    TAB_NEEDS = re.compile(r"awaiting|needs you|needs your", re.I)
    TAB_WORK = re.compile(r"working|running|responding|thinking", re.I)
    NEEDS_TEXT = re.compile(r"asking a question|awaiting (answer|approval|permission|response)|needs (approval|permission|an answer)|approval needed|waiting for (your )?(approval|answer|permission)", re.I)

    def __init__(self, buddy):
        self.buddy, self.trusted, self.seen = buddy, None, 0.0
        self.last, self.tabs, self.on_screen = {}, {}, {}   # per account: page states, mode tabs, names on screen

    @property
    def visible(self):
        """Conversations on screen in any copy, as their session names without "app:"."""
        return {n for names in self.on_screen.values() for n in names}

    def showing(self, key, front_account=None):
        """Is conversation `key` (a session id without "app:") on screen in the copy that's in front?
        With front_account None (not known) any copy counts."""
        return any(key in names and front_account in (None, acct) for acct, names in self.on_screen.items())

    @staticmethod
    def name(title):
        return (title[:-len(" - Claude")] if title.endswith(" - Claude") else title) or "Claude"

    def snapshot(self, snap):
        self.seen = time.time()
        if snap.get("trusted") != self.trusted:
            self.trusted = snap.get("trusted")
            log.info("app watcher %s", "has Accessibility permission" if self.trusted else "is waiting for Accessibility permission")
        copies = snap.get("copies")
        if copies is None:                          # an older helper: one copy, at the top level
            copies = [dict(snap, account=PERSONAL)]
        by = {}
        for c in copies:
            if isinstance(c, dict):
                by[(c.get("account") or PERSONAL).lower()] = c
        for acct in sorted(set(by) | set(self.last)):
            self.copy(acct, by.get(acct, {}))

    def copy(self, acct, snap):
        """One copy of the app (one account)."""
        prefix = "" if acct == PERSONAL else acct + ":"
        last = self.last.setdefault(acct, {})
        tabs = snap.get("tabs") or []
        if tabs != self.tabs.get(acct, []):        # logged to learn what the mode tabs announce
            self.tabs[acct] = tabs
            log.info("app tabs%s: %s", "" if acct == PERSONAL else " (%s)" % acct, " | ".join(tabs))
        self.on_screen[acct] = {prefix + self.name(t) for t in (snap.get("pages") or {})}
        # usage ring in the app, e.g. "Usage: Weekly · all models: 25%, Resets Mon 8:00 AM"
        for lab in snap.get("labels") or []:
            m = self.USAGE_LABEL.match(lab)
            if m:
                key = "seven_day" if re.search(r"week", m.group(1), re.I) else "five_hour"
                self.buddy.set_usage({key: {"used_percentage": float(m.group(2)), "resets_at": None}}, source="app", account=acct)
        states = {}
        def bump(name, st):
            rank = {"working": 1, "alert": 2}
            if rank[st] > rank.get(states.get(name), 0):
                states[name] = st
        for title, pg in (snap.get("pages") or {}).items():
            n = self.name(title)
            states.setdefault(n, None)
            if any(self.NEEDS_BTN.search(b) for b in pg.get("buttons", [])) or any(self.NEEDS_TEXT.search(m) for m in pg.get("markers", [])):
                bump(n, "alert")
            if any(self.STOP_BTN.match(b) for b in pg.get("buttons", [])) or any(self.WORK_STATUS.match(x) for x in pg.get("statuses", [])):
                bump(n, "working")
        # Section tabs announce sections you're not looking at, e.g. "Claude, awaiting your input"
        # while you're in Code: that's what covers Chat and Cowork in the background. (The Code tab
        # is ignored: hooks already cover Code, with more detail.)
        for t in snap.get("tabs") or []:
            sec, sep, st = t.partition(",")
            if not sep or sec.strip().lower() in ("code", ""): continue
            n = "section " + sec.strip()
            if self.NEEDS_TEXT.search(st) or self.TAB_NEEDS.search(st): bump(n, "alert")
            elif self.WORK_STATUS.match(st.strip()) or self.TAB_WORK.search(st): bump(n, "working")
        for r in snap.get("sidebar") or []:
            n = r.get("row") or "Claude"
            st = r.get("status", "")
            states.setdefault(n, None)
            if self.NEEDS_TEXT.search(st): bump(n, "alert")
            elif self.WORK_STATUS.match(st): bump(n, "working")
        for n, state in states.items():
            prev = last.get(n)
            if state != prev:
                if state:
                    self.buddy.external("app:" + prefix + n, state, account=acct)
                elif prev in ("working", "alert"):
                    self.buddy.external("app:" + prefix + n, "done", account=acct)
                last[n] = state
        for n in [x for x in last if x not in states]:    # no longer on screen or in the sidebar
            # A conversation that still needs you keeps its sidebar badge, so if it vanished
            # entirely it was answered elsewhere, closed or deleted: drop it. Work in progress
            # we simply can't see any more gets a short grace period instead.
            if last[n] == "working":
                self.buddy.external("app:" + prefix + n, "working", stale=self.APP_STALE, account=acct)
            else:
                self.buddy.forget("app:" + prefix + n)
            del last[n]


class SlackError(Exception):
    pass


class SlackWatch:
    """Slack, read with your own user token (a private "Desk status" app with read-only user scopes:
    im:read im:history mpim:read mpim:history users:read search:read). Every SLACK_EVERY seconds:

      VIPs ("slack_vips" in config.json, names as Slack shows them): their 1:1 DMs are checked for
      unread messages. One you haven't read -> Lump holds up a letter until Slack no longer counts it
      unread (read it anywhere, phone included) or the hold time passes. A newer message re-arms it.
      Everyone else: a search for messages sent to you (DMs and group DMs) and messages that @-mention
      you by name. Each new one, unless it's yours or a bot's, sends one paper plane.

    Channel traffic that doesn't tag you never comes back from those searches, so it's never seen.
    The token is only ever sent to slack.com in a header; it and message text are never logged.
    The config is re-read every poll: add a token or VIP and it's picked up within SLACK_EVERY."""

    USERS_EVERY, IMS_EVERY = 3600, 600
    DM_QUERY, MENTION_QUERY = "to:me", "<@{me}>"

    def __init__(self, buddy, api=None, start=True):
        self.buddy, self.call = buddy, api or self._api
        self.token, self.me, self.users, self.users_at, self.ims, self.ims_at = None, None, {}, 0.0, {}, 0.0
        self.seen = time.time()                        # newest message (Slack ts) already reacted to
        self.unread_once = {}                          # VIP DM -> ts seen unread once while Slack was in front
        self.status, self.backoff, self.last_err = "off", 0.0, None
        if start:
            threading.Thread(target=self._run, daemon=True).start()

    def _api(self, method, **params):
        import urllib.error, urllib.parse, urllib.request
        req = urllib.request.Request("https://slack.com/api/" + method + "?" + urllib.parse.urlencode(params),
                                     headers={"Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise SlackError("ratelimited, retry after %s" % e.headers.get("Retry-After", "?"))
            raise SlackError("HTTP %d" % e.code)
        if not body.get("ok"):
            raise SlackError(body.get("error", "unknown") + (" (needs %s)" % body["needed"] if body.get("needed") else ""))
        return body

    def _run(self):
        while True:
            time.sleep(SLACK_EVERY)
            if time.time() < self.backoff:
                continue
            try:
                self.poll(time.time())
            except SlackError as e:
                self._fail(str(e))
            except Exception as e:                      # never kill the loop (and never log the token)
                self._fail(type(e).__name__ + ": " + str(e).replace(self.token or "\0", "<token>"))

    def _fail(self, why):
        self.status = "error: " + why
        if why != self.last_err:
            log.info("slack: %s", why)
            self.last_err = why
        # a bad token or a missing scope won't fix itself: wait for the config to change
        fatal = any(k in why for k in ("invalid_auth", "token_revoked", "account_inactive", "not_authed", "missing_scope", "not_allowed_token_type"))
        self.backoff = time.time() + (600 if fatal else 120 if "ratelimited" in why else 60)
        if fatal:
            self.me = None

    @staticmethod
    def slack_in_front():
        """Is Slack the app you're using right now? (Then a plane is just noise, and a VIP message you're
        reading as it lands shouldn't get the whole letter routine.)"""
        try:
            asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=2).stdout.strip()
            out = subprocess.run(["lsappinfo", "info", "-only", "bundleID", asn], capture_output=True, text=True, timeout=2).stdout
            return "com.tinyspeck.slackmacgap" in out
        except Exception:
            return False

    def name(self, uid):
        u = self.users.get(uid) or {}
        return u.get("name") or uid or "?"

    def poll(self, now):
        cfg = read_config()
        token = cfg.get("slack_token")
        if not token:
            self.status = "off (no slack_token in config.json)"
            return
        if token != self.token:
            self.token, self.me, self.users_at, self.ims_at, self.backoff = token, None, 0.0, 0.0, 0.0
        if not self.me:
            self.me = self.call("auth.test")["user_id"]
            log.info("slack: connected")
        vip_names = {str(n).strip().lower() for n in cfg.get("slack_vips") or [] if str(n).strip()}
        if now - self.users_at > self.USERS_EVERY:
            self.load_users()
            self.users_at = now
        vips = {uid for uid, u in self.users.items() if vip_names & u["names"]}
        if now - self.ims_at > self.IMS_EVERY:
            self.load_ims()
            self.ims_at = now
        front = self.slack_in_front()
        self.check_vips(vips, front)
        self.check_new(vips, now, front)
        self.status = "ok, %d VIP(s) found of %d named" % (len(vips), len(vip_names))
        self.last_err = None

    def load_users(self):
        users, cursor = {}, None
        for _ in range(50):                             # 200 a page; a big workspace still fits
            r = self.call("users.list", limit=200, **({"cursor": cursor} if cursor else {}))
            for m in r.get("members") or []:
                p = m.get("profile") or {}
                names = {x.strip().lower() for x in (m.get("real_name"), p.get("real_name"), p.get("display_name"), m.get("name")) if x and x.strip()}
                users[m["id"]] = {"names": names, "bot": bool(m.get("is_bot")) or m["id"] == "USLACKBOT",
                                  "name": p.get("display_name") or m.get("real_name") or m.get("name")}
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        self.users = users

    def load_ims(self):
        ims, cursor = {}, None
        for _ in range(20):
            r = self.call("users.conversations", types="im", exclude_archived="true", limit=200, **({"cursor": cursor} if cursor else {}))
            for c in r.get("channels") or []:
                if c.get("user"):
                    ims[c["user"]] = c["id"]
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        self.ims = ims

    def check_vips(self, vips, front=False):
        """A letter for each VIP DM with something unread; put away the ones you've read. With Slack in
        front it has to still be unread one poll later (you may be reading it as it arrives)."""
        watched = {self.ims[u]: u for u in vips if u in self.ims}
        for ch, uid in watched.items():
            info = self.call("conversations.info", channel=ch)["channel"]
            unread = info.get("unread_count_display")
            latest = (info.get("latest") or {}).get("ts")
            last_read = info.get("last_read") or "0"
            if unread is None:                          # no count given: is there a message after your read marker?
                msgs = self.call("conversations.history", channel=ch, oldest=last_read, limit=5).get("messages") or []
                mine = [m for m in msgs if m.get("user") == uid]
                unread, latest = len(mine), (mine[0]["ts"] if mine else latest)
            ts = latest or last_read
            if unread and front and ch not in self.buddy.letters and self.unread_once.get(ch) != ts:
                self.unread_once[ch] = ts               # give you a poll to read it
            elif unread:
                self.unread_once.pop(ch, None)
                self.buddy.slack_letter(ch, self.name(uid), ts)
            else:
                self.unread_once.pop(ch, None)
                if ch in self.buddy.letters:
                    self.buddy.slack_read(ch)
        for ch in [c for c in self.buddy.letters if c not in watched]:
            self.buddy.slack_read(ch)                   # no longer a VIP (or the DM's gone)

    def check_new(self, vips, now, front=False):
        """New DMs and @-mentions since the last poll: one plane for the lot (VIP DMs have their letter)."""
        hits = []
        for kind, q in (("DM", self.DM_QUERY), ("mention", self.MENTION_QUERY.format(me=self.me))):
            r = self.call("search.messages", query=q, sort="timestamp", sort_dir="desc", count=20)
            for m in (r.get("messages") or {}).get("matches") or []:
                hits.append((kind, m))
        newest, plane = self.seen, None
        for kind, m in hits:
            try: ts = float(m.get("ts") or 0)
            except ValueError: continue
            if ts <= self.seen:
                continue
            newest = max(newest, ts)
            uid, ch = m.get("user"), m.get("channel") or {}
            if not uid or uid == self.me or (self.users.get(uid) or {}).get("bot") or m.get("bot_id") or now - ts > PLANE_FRESH:
                continue
            if uid in vips and ch.get("is_im"):
                continue                                # a VIP's DM: that's the letter's job
            if ch.get("is_im") or ch.get("is_mpim"):
                kind = "DM"
            plane = plane or (kind, self.name(uid))
        self.seen = newest
        if plane and front:
            log.info("slack: %s from %s, no plane (you're in Slack)", plane[0], plane[1])
        elif plane:
            self.buddy.slack_plane(plane[1], plane[0])


buddy = None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if self.path == "/hook":
            self._reply(204)                      # answer first so the hook never waits on us
            try: buddy.hook(json.loads(body or b"{}"))
            except Exception as e: log.info("bad hook payload: %s", e)
        elif self.path == "/app":
            self._reply(204)
            try: buddy.app.snapshot(json.loads(body or b"{}"))
            except Exception as e: log.info("bad app snapshot: %s", e)
        elif self.path == "/usage":               # from tools/statusline.sh (status line JSON)
            self._reply(204)
            try:
                p = json.loads(body or b"{}") or {}
                buddy.set_usage(p.get("rate_limits"), account=account_of_config(p.get("transcript_path")))
            except Exception as e: log.info("bad usage payload: %s", e)
        elif self.path == "/calendar":            # re-read the calendar now
            buddy.calendar.refresh_now(); self._reply(200, b"refreshing")
        elif self.path == "/cmd":
            ok = buddy.dev.send(body.decode().strip()); self._reply(200 if ok else 503, b"ok" if ok else b"board not connected")
        elif self.path == "/play":                # an animation by name (GET /play lists them)
            ok = buddy.play(body.decode())
            if ok is None: self._reply(404, b"no such animation (GET /play lists them)")
            else: self._reply(200 if ok else 503, b"ok" if ok else b"board not connected")
        elif self.path == "/sound":               # on | off | toggle | play <moment>
            cmd = body.decode().strip().split()
            if cmd and cmd[0] in ("on", "off", "toggle"):
                on = buddy.sounds.set_enabled({"on": True, "off": False}.get(cmd[0], "toggle"))
                self._reply(200, b"on" if on else b"off")
            elif len(cmd) == 2 and cmd[0] == "play":            # a moment, or a gag as the board names it (tap_shock_idle)
                ok = (buddy.sounds.play_gag(cmd[1], force=True) if cmd[1].startswith("tap_") else buddy.sounds.play(cmd[1], force=True))
                self._reply(200 if ok else 404, b"ok" if ok else b"no such sound")
            else:
                self._reply(400, b"use: on | off | toggle | play <moment>")
        else:
            self._reply(404)

    def do_GET(self):
        if self.path == "/status":
            self._reply(200, json.dumps(buddy.status(), indent=2).encode(), "application/json")
        elif self.path == "/sound":
            self._reply(200, json.dumps(buddy.sounds.settings(), indent=2).encode(), "application/json")
        elif self.path == "/play":
            self._reply(200, json.dumps(list(ANIMATIONS)).encode(), "application/json")
        else:
            self._reply(404)


if __name__ == "__main__":
    buddy = Buddy()
    buddy.app = AppPages(buddy)
    buddy.calendar = Calendar()
    buddy.slack = SlackWatch(buddy)
    log.info("desk-buddy bridge listening on 127.0.0.1:%d", HTTP_PORT)
    http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler).serve_forever()
