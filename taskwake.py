#!/usr/bin/env python3
"""taskwake - a background-task ledger for AI agents, with a webhook that wakes the
agent that dispatched the task once it finishes.

Adapted from the ideas in pi-task.mjs of robbin/grokbot-local-agent
(MIT, Copyright (c) 2026 Robbin Fan; see LICENSE.upstream-MIT): numbered tasks, state on
disk, resume, cancel, metadata-only completion notifications, pending/ack catch-up.
This version wraps *any* command (codex exec, claude -p, python scripts, shell) and uses
only the Python standard library.

Ledger:   $TASKWAKE_DIR (default: $XDG_STATE_HOME/taskwake/tasks or ~/.local/state/taskwake/tasks)/<ID>/
  status.json   current state (written atomically)
  events.jsonl  event log, including every notification attempt
  output.log    stdout+stderr of every run (appended, with a separator per run)
  cmd-N.json    the command of run N
  input-N.txt   stdin of run N (optional)
  result.md     optional: the command may write its own result to $TASKWAKE_DIR/result.md

Webhook config: $TASKWAKE_CONF_DIR (default ~/.config/taskwake)/<owner>.env (chmod 600):
  TASKWAKE_WEBHOOK_URL=https://...
  TASKWAKE_WEBHOOK_KEY=...          # raw key, without "Bearer "
The env vars TASKWAKE_WEBHOOK_URL / TASKWAKE_WEBHOOK_KEY take precedence.
TASKWAKE_NOTIFY_OFF=1 disables notifications temporarily.
"""
import fcntl
import getpass
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

__version__ = "1.0.0"


def _default_root():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "taskwake", "tasks")


ROOT = os.path.abspath(os.path.expanduser(os.environ.get("TASKWAKE_DIR") or _default_root()))
CONF_DIR = os.path.expanduser(os.environ.get("TASKWAKE_CONF_DIR") or "~/.config/taskwake")
TERMINAL = {"done", "failed", "cancelled", "interrupted"}
# seconds between notification attempts; default = 5 attempts
RETRY_DELAYS = [int(x) for x in os.environ.get("TASKWAKE_RETRY_DELAYS", "0,5,20,60,180").split(",") if x.strip()]
# Optional extra header that also carries the key (some webhook receivers want e.g. X-Automation-Key).
EXTRA_KEY_HEADER = os.environ.get("TASKWAKE_WEBHOOK_KEY_HEADER", "").strip()
# Optional: also export the owner to child processes under this variable name (e.g. for usage accounting).
CHILD_OWNER_VAR = os.environ.get("TASKWAKE_CHILD_OWNER_VAR", "").strip()
# Never passed to child processes.
SECRET_ENV = ("TASKWAKE_WEBHOOK_URL", "TASKWAKE_WEBHOOK_KEY")


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def die(msg, code=1):
    print(f"taskwake: {msg}", file=sys.stderr)
    sys.exit(code)


def tdir(tid):
    if not re.fullmatch(r"[0-9]{4,}-[A-Za-z0-9._\-]+", tid or "") or ".." in tid:
        die(f"invalid task id: {tid!r}", 2)
    d = os.path.join(ROOT, tid)
    if not os.path.isdir(d):
        die(f"task not found: {tid} (TASKWAKE_DIR={ROOT})")
    return d


def read_status(tid):
    with open(os.path.join(tdir(tid), "status.json")) as f:
        return json.load(f)


def write_status(st):
    d = os.path.join(ROOT, st["id"])
    tmp = os.path.join(d, f".status.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(d, "status.json"))


class Lock:
    """Cross-process lock protecting read-modify-write of status.json."""

    def __init__(self, tid=None):
        self.p = os.path.join(ROOT, tid, ".lock") if tid else os.path.join(ROOT, ".seq.lock")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.p), exist_ok=True)
        self.f = open(self.p, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def update(tid, **kw):
    with Lock(tid):
        st = read_status(tid)
        st.update(kw)
        write_status(st)
        return st


def event(tid, typ, text=""):
    text = re.sub(r"\s+", " ", str(text))[:500]
    with open(os.path.join(ROOT, tid, "events.jsonl"), "a") as f:
        f.write(json.dumps({"ts": now(), "type": typ, "text": text}, ensure_ascii=False) + "\n")


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie counts as dead (Linux /proc only; elsewhere assume alive)
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def all_ids():
    if not os.path.isdir(ROOT):
        return []
    return sorted(x for x in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, x, "status.json")))


def orphan_check():
    """Tasks recorded as running/queued whose runner is gone (reboot, kill -9) -> interrupted."""
    for tid in all_ids():
        try:
            st = read_status(tid)
        except Exception:
            continue
        if st.get("state") in ("running", "queued") and not alive(st.get("pid")):
            age = time.time() - os.path.getmtime(os.path.join(ROOT, tid, "status.json"))
            if st.get("state") == "queued" and age < 30:
                continue  # just created; the runner may not have written its pid yet
            with Lock(tid):
                st = read_status(tid)
                if st.get("state") in ("running", "queued") and not alive(st.get("pid")):
                    st.update(state="interrupted", pid=None, ended_at=now(), error="runner process disappeared")
                    write_status(st)
                    event(tid, "orphan_detected", "runner process no longer exists; marked interrupted")


# ------------------------------------------------------------------ webhook

def safe_owner(owner):
    return re.sub(r"[^\w.\-]", "_", owner or "")


def conf_path(owner):
    return os.path.join(CONF_DIR, safe_owner(owner) + ".env")


def webhook_conf(owner):
    url = os.environ.get("TASKWAKE_WEBHOOK_URL")
    key = os.environ.get("TASKWAKE_WEBHOOK_KEY")
    src = "env"
    if not (url and key) and owner:
        p = conf_path(owner)
        if os.path.isfile(p):
            src = p
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if k.strip() == "TASKWAKE_WEBHOOK_URL" and not url:
                        url = v
                    elif k.strip() == "TASKWAKE_WEBHOOK_KEY" and not key:
                        key = v
    if key and key.lower().startswith("bearer "):
        key = key[7:].strip()
    return url, key, src


def post_once(url, key, payload, timeout=15):
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key,
               "User-Agent": f"taskwake/{__version__}"}
    if EXTRA_KEY_HEADER:
        headers[EXTRA_KEY_HEADER] = key
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # never log the URL or the key
        return None, type(e).__name__ + ": " + str(getattr(e, "reason", e))[:150]


def notify(tid, retry=True):
    """Completion notification: sends only {source,id,owner,state,exit_code,title}; never the command or output."""
    st = read_status(tid)
    if os.environ.get("TASKWAKE_NOTIFY_OFF") == "1":
        event(tid, "notify", "skipped: TASKWAKE_NOTIFY_OFF=1")
        return False
    url, key, _ = webhook_conf(st.get("owner"))
    if not (url and key):
        event(tid, "notify", f"skipped: no webhook configured for owner={st.get('owner')}; "
                             "ledger only (catch up with `pending`)")
        update(tid, notify_state="no_webhook")
        return False
    payload = {"source": "taskwake", "id": tid, "owner": st.get("owner"), "state": st.get("state"),
               "exit_code": st.get("exit_code"), "title": st.get("title") or ""}
    delays = RETRY_DELAYS if retry else [0]
    for i, d in enumerate(delays, 1):
        if d:
            time.sleep(d)
        code, err = post_once(url, key, payload)
        ok = code is not None and 200 <= code < 300
        event(tid, "notify", f"attempt {i}: " + (f"http {code}" if code is not None else f"error {err}"))
        if ok:
            update(tid, notify_state="sent", notified_at=now())
            return True
        if code is not None and code in (400, 401, 403, 404):
            break  # configuration error; retrying will not help
    update(tid, notify_state="failed")
    return False


# ------------------------------------------------------------------ runner

def new_id(name):
    name = re.sub(r"[^A-Za-z0-9._\-]+", "-", name or "task").strip("-.")[:40] or "task"
    with Lock():
        p = os.path.join(ROOT, ".seq")
        n = 1
        if os.path.exists(p):
            with open(p) as f:
                n = int(f.read().strip() or 0) + 1
        with open(p, "w") as f:
            f.write(str(n))
    return f"{n:04d}-{name}"


def child_env(st, tid, d, runno):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    owner = st.get("owner") or ""
    env.update(TASKWAKE_ID=tid, TASKWAKE_DIR=d, TASKWAKE_RUN=str(runno), TASKWAKE_OWNER=owner)
    if owner and CHILD_OWNER_VAR and not env.get(CHILD_OWNER_VAR):
        env[CHILD_OWNER_VAR] = owner
    return env


def run_task(tid, runno):
    """Execute run number `runno` (inside the runner process). Returns the exit code."""
    d = os.path.join(ROOT, tid)
    st = read_status(tid)
    with open(os.path.join(d, f"cmd-{runno}.json")) as f:
        spec = json.load(f)
    timeout = spec.get("timeout") or 0
    env = child_env(st, tid, d, runno)
    stdin_p = os.path.join(d, f"input-{runno}.txt")
    log = open(os.path.join(d, "output.log"), "ab")
    log.write(f"\n===== run {runno} @ {now()} : {spec['display']} =====\n".encode())
    log.flush()
    update(tid, state="running", pid=os.getpid(), started_at=st.get("started_at") or now(), ended_at=None,
           exit_code=None, error=None, reported=False, notify_state=None, current_run=runno)
    event(tid, "start", f"run {runno}: {spec['display']}")
    stdin = open(stdin_p, "rb") if os.path.exists(stdin_p) else subprocess.DEVNULL
    try:
        child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=stdin, start_new_session=True)
    except Exception as e:
        log.close()
        return finish(tid, runno, "failed", 127, f"failed to start: {e}")
    update(tid, child_pid=child.pid)
    reason = {"v": None}

    def on_sig(signum, frame):
        if reason["v"]:  # second signal: kill immediately
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except Exception:
                pass
        reason["v"] = "cancelled"
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except Exception:
            pass
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # survive the parent terminal going away

    t0 = time.time()
    kill_at = None
    while True:
        try:
            rc = child.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        if timeout and not reason["v"] and time.time() - t0 > timeout:
            reason["v"] = "timeout"
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except Exception:
                pass
        if reason["v"] and kill_at is None:
            kill_at = time.time() + 10
        if kill_at and time.time() > kill_at:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except Exception:
                pass
            kill_at = float("inf")
    log.close()
    if reason["v"] == "cancelled":
        return finish(tid, runno, "cancelled", 130, "cancelled")
    if reason["v"] == "timeout":
        return finish(tid, runno, "failed", 124, f"timeout after {timeout}s")
    return finish(tid, runno, "done" if rc == 0 else "failed", 0 if rc == 0 else rc, None if rc == 0 else f"exit {rc}")


def finish(tid, runno, state, code, error):
    d = os.path.join(ROOT, tid)
    extra = {}
    try:  # remember the Codex CLI session id so the task can be resumed
        with open(os.path.join(d, "output.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400000))
            m = re.findall(rb"session id:\s*([0-9a-f]{8}-[0-9a-f-]{27,})", f.read())
            if m:
                extra["codex_session"] = m[-1].decode()
    except OSError:
        pass
    with Lock(tid):
        st = read_status(tid)
        for r in st["runs"]:
            if r["n"] == runno:
                r.update(ended_at=now(), exit_code=code, state=state)
        st.update(state=state, pid=None, child_pid=None, ended_at=now(), exit_code=code, error=error,
                  reported=False, **extra)
        write_status(st)
    event(tid, "end", f"run {runno}: {state} exit={code}" + (f" ({error})" if error else ""))
    print(f"STATE {state}\nEXIT {code}\nLOG {os.path.join(d, 'output.log')}", flush=True)
    notify(tid)
    return code


def prepare_run(tid, runno, argv, cwd, timeout, stdin_file, display):
    d = os.path.join(ROOT, tid)
    with open(os.path.join(d, f"cmd-{runno}.json"), "w") as f:
        json.dump({"argv": argv, "cwd": cwd, "timeout": timeout, "display": display}, f, ensure_ascii=False, indent=2)
    if stdin_file:
        if stdin_file == "-":
            data = sys.stdin.buffer.read()
        else:
            with open(stdin_file, "rb") as f:
                data = f.read()
        with open(os.path.join(d, f"input-{runno}.txt"), "wb") as f:
            f.write(data)


def launch(tid, runno, detach):
    if detach:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_runner", tid, str(runno)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        update(tid, pid=p.pid)
        print(f"TASK {tid}\nPID {p.pid}\nDIR {os.path.join(ROOT, tid)}", flush=True)
        return 0
    print(f"TASK {tid}\nDIR {os.path.join(ROOT, tid)}", flush=True)
    return run_task(tid, runno)


# ------------------------------------------------------------------ CLI

USAGE = """usage: taskwake <command> ...
  start  --owner AGENT [--name N] [--title T] [--cwd D] [--timeout S] [--stdin FILE|-] [--fg]
         (--sh "shell string" | -- cmd args...)
                     create a task and run it in the background (--fg: run in the foreground)
  resume ID [--timeout S] [--stdin FILE|-] [--fg] [--sh "..." | -- cmd ...]
                     run another round in the same task (no command = rerun the previous one)
  status ID          show state
  list [--all] [--owner AGENT|none]       running and recent tasks
  log ID [-n 40]     tail of the output
  cancel ID          cancel (SIGTERM, then SIGKILL after 10 s)
  pending [--owner AGENT|none] [--json]   finished but not yet reported tasks (catch-up)
  mark-reported ID [ID...] --owner AGENT  mark as reported (alias: ack; refuses other owners' tasks)
  notify ID          resend the completion notification once
  webhook-check --owner AGENT             is a webhook configured? (never prints the key)
  webhook-set --owner AGENT [--url URL --key-file F | --url URL --key-stdin]
                     store URL and key in $TASKWAKE_CONF_DIR/AGENT.env (mode 600); never prints the key
  webhook-test --owner AGENT              send {"source":"taskwake","event":"test"} (really wakes the agent)
  version
exit codes: 0 done, 1 failed/refused, 2 usage error, 124 timeout, 130 cancelled"""


def parse(argv, flags_with_val, bool_flags):
    opts, pos, cmd = {}, [], None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            cmd = argv[i + 1:]
            break
        if a in flags_with_val:
            if i + 1 >= len(argv):
                die(f"{a} needs a value", 2)
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a in bool_flags:
            opts[a] = True
            i += 1
            continue
        if a.startswith("--"):
            die(f"unknown option {a}\n{USAGE}", 2)
        pos.append(a)
        i += 1
    return opts, pos, cmd


def get_owner(opts, required=True):
    o = opts.get("--owner") or os.environ.get("TASKWAKE_OWNER")
    if not o:
        if required:
            die("--owner is required (or set TASKWAKE_OWNER)", 2)
        return None
    if re.search(r"\s", o) or o == "none":
        die("owner must not contain whitespace and must not be 'none'", 2)
    return o


def cmd_spec(opts, cmd):
    if opts.get("--sh"):
        return ["bash", "-lc", opts["--sh"]], opts["--sh"]
    if cmd:
        return cmd, shlex.join(cmd)
    return None, None


def c_start(argv):
    opts, pos, cmd = parse(argv, {"--owner", "--name", "--title", "--cwd", "--timeout", "--stdin", "--sh"}, {"--fg"})
    owner = get_owner(opts)
    argv2, display = cmd_spec(opts, cmd)
    if not argv2:
        die('missing command (--sh "..." or -- cmd ...)', 2)
    cwd = os.path.abspath(opts.get("--cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        die(f"working directory does not exist: {cwd}", 2)
    timeout = int(opts.get("--timeout") or 0)
    os.makedirs(ROOT, exist_ok=True)
    tid = new_id(opts.get("--name") or os.path.basename(argv2[0] if argv2[0] != "bash" else "sh"))
    d = os.path.join(ROOT, tid)
    os.makedirs(d)
    st = {"id": tid, "owner": owner, "title": opts.get("--title") or "", "cwd": cwd, "state": "queued",
          "pid": None, "child_pid": None, "created_at": now(), "started_at": None, "ended_at": None,
          "exit_code": None, "error": None, "reported": False, "reported_at": None, "notify_state": None,
          "current_run": 1, "runs": [{"n": 1, "cmd": display, "started_at": now(), "ended_at": None, "exit_code": None}]}
    write_status(st)
    event(tid, "created", f"owner={owner} cmd={display}")
    prepare_run(tid, 1, argv2, cwd, timeout, opts.get("--stdin"), display)
    return launch(tid, 1, not opts.get("--fg"))


def c_resume(argv):
    opts, pos, cmd = parse(argv, {"--timeout", "--stdin", "--sh"}, {"--fg"})
    if len(pos) != 1:
        die("usage: taskwake resume ID [--sh ... | -- cmd ...]", 2)
    tid = pos[0]
    tdir(tid)
    orphan_check()
    with Lock(tid):
        st = read_status(tid)
        if st["state"] in ("running", "queued") and alive(st.get("pid")):
            die(f"{tid} is still running; cannot resume")
        n = max(r["n"] for r in st["runs"]) + 1
        argv2, display = cmd_spec(opts, cmd)
        with open(os.path.join(ROOT, tid, f"cmd-{n - 1}.json")) as f:
            prev = json.load(f)
        if not argv2:
            argv2, display = prev["argv"], prev["display"]
        timeout = int(opts["--timeout"]) if "--timeout" in opts else prev.get("timeout", 0)
        st["runs"].append({"n": n, "cmd": display, "started_at": now(), "ended_at": None, "exit_code": None})
        st.update(state="queued", current_run=n, reported=False, reported_at=None)
        write_status(st)
    prepare_run(tid, n, argv2, st["cwd"], timeout, opts.get("--stdin"), display)
    event(tid, "resume", f"run {n}: {display}")
    return launch(tid, n, not opts.get("--fg"))


def ago(ts):
    if not ts:
        return "-"
    s = int(time.time() - datetime.fromisoformat(ts).timestamp())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def line(st):
    return (f"{st['id']}  owner={st.get('owner') or '-'}  {st['state']}  exit={st.get('exit_code')}  "
            f"runs={len(st['runs'])}  reported={'yes' if st.get('reported') else 'no'}  "
            f"notify={st.get('notify_state') or '-'}  ended={ago(st.get('ended_at'))}  {st.get('title') or ''}").rstrip()


def owner_match(st, f):
    if f is None:
        return True
    return (st.get("owner") is None) if f == "none" else st.get("owner") == f


def c_status(argv):
    if len(argv) != 1:
        die("usage: taskwake status ID", 2)
    orphan_check()
    st = read_status(argv[0])
    d = tdir(argv[0])
    print(line(st))
    for k in ("title", "cwd", "created_at", "started_at", "ended_at", "error", "codex_session", "reported_at", "notified_at"):
        if st.get(k):
            print(f"  {k}: {st[k]}")
    for r in st["runs"]:
        print(f"  run {r['n']}: exit={r.get('exit_code')} {r.get('started_at')} -> {r.get('ended_at') or '...'}  {r['cmd'][:200]}")
    print(f"  dir: {d}")
    for f in ("output.log", "result.md"):
        p = os.path.join(d, f)
        if os.path.exists(p):
            print(f"  {f}: {p} ({os.path.getsize(p)} bytes)")
    print("  recent events:")
    with open(os.path.join(d, "events.jsonl")) as f:
        for ln in f.read().splitlines()[-6:]:
            e = json.loads(ln)
            print(f"    {e['ts']} {e['type']} {e['text'][:160]}")


def c_list(argv):
    opts, _, _ = parse(argv, {"--owner"}, {"--all"})
    orphan_check()
    rows = [read_status(t) for t in all_ids()]
    rows = [s for s in rows if owner_match(s, opts.get("--owner"))]
    if not opts.get("--all"):
        cut = time.time() - 3 * 86400
        rows = [s for s in rows if s["state"] in ("running", "queued") or not s.get("reported")
                or datetime.fromisoformat(s.get("ended_at") or s["created_at"]).timestamp() > cut]
    for s in rows:
        print(line(s))
    if not rows:
        print("(none)")


def c_log(argv):
    opts, pos, _ = parse(argv, {"-n"}, set())
    if len(pos) != 1:
        die("usage: taskwake log ID [-n 40]", 2)
    p = os.path.join(tdir(pos[0]), "output.log")
    if not os.path.exists(p):
        print("(no output yet)")
        return
    n = int(opts.get("-n") or 40)
    with open(p, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 200000))
        print("\n".join(f.read().decode("utf-8", "replace").splitlines()[-n:]))


def c_cancel(argv):
    if len(argv) != 1:
        die("usage: taskwake cancel ID", 2)
    tid = argv[0]
    orphan_check()
    st = read_status(tid)
    if st["state"] not in ("running", "queued"):
        print(f"{tid} is already {st['state']}; nothing to cancel")
        return
    pid = st.get("pid")
    event(tid, "cancel_requested", f"signal runner pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as e:
        die(f"failed to signal runner: {e}")
    for _ in range(150):  # wait up to 15 s
        time.sleep(0.1)
        if read_status(tid)["state"] in TERMINAL:
            print(f"{tid} cancelled")
            return
    for p in (st.get("child_pid"), pid):
        try:
            if p == st.get("child_pid"):
                os.killpg(p, signal.SIGKILL)
            else:
                os.kill(p, signal.SIGKILL)
        except Exception:
            pass
    update(tid, state="cancelled", pid=None, child_pid=None, ended_at=now(), exit_code=130, error="forced cancel")
    event(tid, "cancel_forced", "SIGKILL")
    print(f"{tid} cancelled (forced)")


def c_pending(argv):
    opts, _, _ = parse(argv, {"--owner"}, {"--json"})
    orphan_check()
    rows = [read_status(t) for t in all_ids()]
    rows = [s for s in rows if s["state"] in TERMINAL and not s.get("reported") and owner_match(s, opts.get("--owner"))]
    if opts.get("--json"):
        keys = ("id", "owner", "title", "state", "exit_code", "ended_at", "notify_state")
        print(json.dumps([{k: s.get(k) for k in keys} for s in rows], ensure_ascii=False, indent=1))
        return
    for s in rows:
        print(line(s))
    if not rows:
        print("(no pending tasks)")


def c_ack(argv):
    opts, pos, _ = parse(argv, {"--owner"}, {"--force"})
    if not pos:
        die("usage: taskwake mark-reported ID [ID...] --owner AGENT", 2)
    f = opts.get("--owner")
    if not f and not opts.get("--force"):
        die("--owner is required (so one agent cannot ack another's tasks); use --force to override", 2)
    bad = 0
    for tid in pos:
        tdir(tid)
        with Lock(tid):
            st = read_status(tid)
            if f and not owner_match(st, f):
                print(f"refused: {tid} belongs to {st.get('owner') or '-'}, not {f}", file=sys.stderr)
                bad = 1
                continue
            if st["state"] not in TERMINAL:
                print(f"refused: {tid} is still {st['state']}", file=sys.stderr)
                bad = 1
                continue
            st.update(reported=True, reported_at=now())
            write_status(st)
        event(tid, "reported", f"by {f or 'force'}")
        print(f"{tid} marked as reported")
    sys.exit(bad)


def c_notify(argv):
    if len(argv) != 1:
        die("usage: taskwake notify ID", 2)
    tdir(argv[0])
    ok = notify(argv[0], retry=False)
    print("sent" if ok else f"not sent / failed; see: taskwake status {argv[0]}")
    sys.exit(0 if ok else 1)


def c_webhook_check(argv):
    opts, _, _ = parse(argv, {"--owner"}, set())
    owner = get_owner(opts)
    url, key, src = webhook_conf(owner)
    p = conf_path(owner)
    mode = oct(os.stat(p).st_mode & 0o777) if os.path.exists(p) else "-"
    host = re.sub(r"^(https?://[^/]+).*$", r"\1/...", url) if url else "-"
    print(f"owner={owner} config={p} mode={mode} source={src if url and key else '-'}")
    print(f"URL: {'configured ' + host if url else 'not configured'}  "
          f"KEY: {'configured (length %d)' % len(key) if key else 'not configured'}")
    sys.exit(0 if url and key else 1)


def c_webhook_set(argv):
    """Store the webhook. The key is never echoed or printed:
    1) interactive terminal: taskwake webhook-set --owner AGENT
    2) from a file:          taskwake webhook-set --owner AGENT --url URL --key-file PATH
                             (plain key, or JSON with a "key"/"value"/"secret" field)
    3) from a pipe:          ... | taskwake webhook-set --owner AGENT --url URL --key-stdin
    """
    opts, _, _ = parse(argv, {"--owner", "--url", "--key-file"}, {"--key-stdin"})
    owner = get_owner(opts)
    url = opts.get("--url")
    if opts.get("--key-file"):
        with open(os.path.expanduser(opts["--key-file"])) as f:
            raw = f.read().strip()
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                raw = j.get("key") or j.get("value") or j.get("secret")
        except ValueError:
            pass
        key = (raw or "").strip()
    elif opts.get("--key-stdin"):
        key = sys.stdin.readline().strip()
    else:
        if not sys.stdin.isatty():
            die("non-interactive: use --url with --key-file or --key-stdin", 2)
        url = url or input("Webhook URL: ").strip()
        key = getpass.getpass("Webhook key (hidden): ").strip()
    if key.lower().startswith("authorization:"):
        key = key.split(":", 1)[1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not url or not re.match(r"^https?://", url) or not key or re.search(r"\s", key):
        die("URL must start with http(s):// and the key must be non-empty without whitespace", 2)
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    p = conf_path(owner)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# taskwake webhook for {owner}\nTASKWAKE_WEBHOOK_URL={url}\nTASKWAKE_WEBHOOK_KEY={key}\n")
    os.chmod(p, 0o600)
    print(f"wrote {p} (600), key length {len(key)}")


def c_webhook_test(argv):
    opts, _, _ = parse(argv, {"--owner"}, set())
    owner = get_owner(opts)
    url, key, _ = webhook_conf(owner)
    if not (url and key):
        die(f"no webhook configured for owner={owner}")
    code, err = post_once(url, key, {"source": "taskwake", "event": "test", "owner": owner, "id": None})
    print(f"http {code}" if code is not None else f"error {err}")
    sys.exit(0 if code and 200 <= code < 300 else 1)


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    c, rest = a[0], a[1:]
    if c in ("version", "--version"):
        print(f"taskwake {__version__}")
        return 0
    if c == "_runner":
        return run_task(rest[0], int(rest[1]))
    table = {"start": c_start, "run": c_start, "resume": c_resume, "status": c_status, "list": c_list, "log": c_log,
             "cancel": c_cancel, "pending": c_pending, "mark-reported": c_ack, "ack": c_ack, "notify": c_notify,
             "webhook-check": c_webhook_check, "webhook-test": c_webhook_test, "webhook-set": c_webhook_set}
    if c not in table:
        die(f"unknown command {c}\n{USAGE}", 2)
    return table[c](rest) or 0


if __name__ == "__main__":
    sys.exit(main())
