"""End-to-end tests for taskwake (stdlib unittest; no network beyond 127.0.0.1)."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TW = os.path.join(os.path.dirname(HERE), "taskwake.py")
FAKE_KEY = "test-key-not-a-secret"


class Hook(http.server.BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Hook.received.append({"auth": self.headers.get("Authorization"),
                              "extra": self.headers.get("X-Test-Key"), "body": json.loads(body)})
        self.send_response(Hook.status)
        self.end_headers()

    def log_message(self, *a):
        pass


class TaskwakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/hook"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Hook.received.clear()
        Hook.status = 200
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TASKWAKE_")}
        self.env.update(TASKWAKE_DIR=os.path.join(self.tmp.name, "tasks"),
                        TASKWAKE_CONF_DIR=os.path.join(self.tmp.name, "conf"),
                        TASKWAKE_RETRY_DELAYS="0", HOME=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def tw(self, *args, env=None, input=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run([sys.executable, TW, *args], capture_output=True, text=True, env=e, input=input, timeout=60)

    def tid(self, out):
        return [x.split()[1] for x in out.splitlines() if x.startswith("TASK ")][0]

    def status(self, tid):
        with open(os.path.join(self.env["TASKWAKE_DIR"], tid, "status.json")) as f:
            return json.load(f)

    def wait_terminal(self, tid, secs=20):
        for _ in range(secs * 10):
            st = self.status(tid)
            if st["state"] in ("done", "failed", "cancelled", "interrupted"):
                return st
            time.sleep(0.1)
        self.fail("task did not finish")

    def set_hook(self, owner="alice"):
        r = self.tw("webhook-set", "--owner", owner, "--url", self.url, "--key-stdin", input=FAKE_KEY + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(FAKE_KEY, r.stdout + r.stderr)

    def test_owner_required(self):
        r = self.tw("start", "--sh", "true")
        self.assertEqual(r.returncode, 2)

    def test_foreground_done_and_notify(self):
        self.set_hook()
        r = self.tw("start", "--owner", "alice", "--name", "demo", "--title", "t", "--fg", "--sh", "echo hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = self.tid(r.stdout)
        st = self.status(tid)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["notify_state"], "sent")
        self.assertEqual(len(Hook.received), 1)
        got = Hook.received[0]
        self.assertEqual(got["auth"], "Bearer " + FAKE_KEY)
        self.assertEqual(got["body"], {"source": "taskwake", "id": tid, "owner": "alice", "state": "done",
                                       "exit_code": 0, "title": "t"})
        self.assertIn("hello", self.tw("log", tid).stdout)

    def test_extra_key_header(self):
        self.set_hook()
        self.tw("start", "--owner", "alice", "--fg", "--sh", "true", env={"TASKWAKE_WEBHOOK_KEY_HEADER": "X-Test-Key"})
        self.assertEqual(Hook.received[0]["extra"], FAKE_KEY)

    def test_secrets_not_passed_to_child(self):
        r = self.tw("start", "--owner", "alice", "--fg", "--sh", "env > \"$TASKWAKE_DIR/env.txt\"",
                    env={"TASKWAKE_WEBHOOK_URL": self.url, "TASKWAKE_WEBHOOK_KEY": FAKE_KEY,
                         "TASKWAKE_CHILD_OWNER_VAR": "MY_AGENT"})
        tid = self.tid(r.stdout)
        with open(os.path.join(self.env["TASKWAKE_DIR"], tid, "env.txt")) as f:
            env = f.read()
        self.assertNotIn(FAKE_KEY, env)
        self.assertNotIn("TASKWAKE_WEBHOOK_URL", env)
        self.assertIn("TASKWAKE_ID=" + tid, env)
        self.assertIn("MY_AGENT=alice", env)
        self.assertEqual(len(Hook.received), 1)  # env config still used by the runner itself

    def test_no_webhook_is_ledger_only(self):
        r = self.tw("start", "--owner", "bob", "--fg", "--sh", "exit 3")
        self.assertEqual(r.returncode, 3)
        tid = self.tid(r.stdout)
        st = self.status(tid)
        self.assertEqual((st["state"], st["exit_code"], st["notify_state"]), ("failed", 3, "no_webhook"))
        self.assertIn(tid, self.tw("pending", "--owner", "bob").stdout)

    def test_background_pending_and_ack_owner_separation(self):
        r = self.tw("start", "--owner", "alice", "--sh", "sleep 0.5; echo done > \"$TASKWAKE_DIR/result.md\"")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = self.tid(r.stdout)
        self.assertEqual(self.wait_terminal(tid)["state"], "done")
        pend = json.loads(self.tw("pending", "--owner", "alice", "--json").stdout)
        self.assertEqual([p["id"] for p in pend], [tid])
        self.assertEqual(json.loads(self.tw("pending", "--owner", "mallory", "--json").stdout), [])
        self.assertEqual(self.tw("mark-reported", tid, "--owner", "mallory").returncode, 1)
        self.assertEqual(self.tw("mark-reported", tid).returncode, 2)
        self.assertEqual(self.tw("ack", tid, "--owner", "alice").returncode, 0)
        self.assertEqual(json.loads(self.tw("pending", "--owner", "alice", "--json").stdout), [])

    def test_timeout(self):
        r = self.tw("start", "--owner", "alice", "--fg", "--timeout", "1", "--sh", "sleep 30")
        self.assertEqual(r.returncode, 124)

    def test_cancel(self):
        r = self.tw("start", "--owner", "alice", "--sh", "sleep 60")
        tid = self.tid(r.stdout)
        for _ in range(50):
            if self.status(tid)["state"] == "running":
                break
            time.sleep(0.1)
        self.assertEqual(self.tw("cancel", tid).returncode, 0)
        self.assertEqual(self.wait_terminal(tid)["state"], "cancelled")

    def test_resume_and_stdin(self):
        r = self.tw("start", "--owner", "alice", "--fg", "--stdin", "-", "--", "cat", input="from-stdin\n")
        tid = self.tid(r.stdout)
        r2 = self.tw("resume", tid, "--fg", "--sh", "echo round-$TASKWAKE_RUN")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        log = self.tw("log", tid).stdout
        self.assertIn("from-stdin", log)
        self.assertIn("round-2", log)
        self.assertEqual(len(self.status(tid)["runs"]), 2)

    def test_webhook_failure_is_recorded(self):
        self.set_hook()
        Hook.status = 401
        self.tw("start", "--owner", "alice", "--fg", "--sh", "true", env={"TASKWAKE_RETRY_DELAYS": "0,0,0"})
        self.assertEqual(len(Hook.received), 1)  # 401 is not retried
        tid = sorted(os.listdir(self.env["TASKWAKE_DIR"]))[-1]
        self.assertEqual(self.status(tid)["notify_state"], "failed")

    def test_webhook_check_never_prints_key(self):
        self.set_hook()
        r = self.tw("webhook-check", "--owner", "alice")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(FAKE_KEY, r.stdout)
        self.assertNotIn("/hook", r.stdout)
        conf = os.path.join(self.env["TASKWAKE_CONF_DIR"], "alice.env")
        self.assertEqual(os.stat(conf).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
