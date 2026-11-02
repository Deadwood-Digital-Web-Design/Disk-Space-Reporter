import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import disk_space_reporter as m


def make(path, size, days_old=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = time.time() - days_old * 86400
    os.utime(path, (ts, ts))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make(self.root / "movies" / "a.mp4", 5000)
        make(self.root / "movies" / "sub" / "b.mkv", 3000)
        make(self.root / "docs" / "c.pdf", 1000)
        make(self.root / "notes.txt", 10)
        make(self.root / "weird.xyz", 50)

    def tearDown(self):
        self._tmp.cleanup()

    def scan(self, top=25):
        s = m.Scan(self.root, top)
        s.run()
        return s.result()

    def test_totals(self):
        r = self.scan()
        self.assertEqual(r["files"], 5)
        self.assertEqual(r["bytes"], 9060)

    def test_folder_sizes_roll_up(self):
        r = self.scan()
        sizes = {c["name"]: c["size"] for c in r["children"]}
        self.assertEqual(sizes, {"movies": 8000, "docs": 1000})
        self.assertEqual(r["children"][0]["name"], "movies")  # biggest first
        self.assertEqual(r["deeper"][0]["size"], 3000)         # movies/sub

    def test_biggest_files_sorted_and_limited(self):
        r = self.scan(top=2)
        self.assertEqual([f["size"] for f in r["biggest"]], [5000, 3000])

    def test_space_by_type(self):
        kinds = {k["name"]: (k["size"], k["count"]) for k in self.scan()["kinds"]}
        self.assertEqual(kinds["Video"], (8000, 2))
        self.assertEqual(kinds["Documents"], (1010, 2))
        self.assertEqual(kinds["Other"], (50, 1))

    def test_old_big_files(self):
        old = m.BIG_BYTES
        m.BIG_BYTES = 2000
        try:
            make(self.root / "old_big.iso", 4000, days_old=500)
            make(self.root / "new_big.iso", 4000, days_old=1)
            make(self.root / "old_small.iso", 100, days_old=500)
            r = self.scan()
        finally:
            m.BIG_BYTES = old
        self.assertEqual([os.path.basename(f["path"]) for f in r["old"]], ["old_big.iso"])
        self.assertGreaterEqual(r["old"][0]["age_days"], 499)

    @unittest.skipIf(os.name == "nt", "symlinks need admin on Windows")
    def test_symlinks_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        try:
            make(Path(outside.name) / "huge.bin", 99999)
            (self.root / "link_dir").symlink_to(outside.name, target_is_directory=True)
            (self.root / "link_file").symlink_to(Path(outside.name) / "huge.bin")
            self.assertEqual(self.scan()["bytes"], 9060)
        finally:
            outside.cleanup()

    def test_cancel_stops_early(self):
        s = m.Scan(self.root)
        s.cancelled = True
        s.run()
        self.assertEqual(s.files, 0)
        self.assertTrue(s.result()["cancelled"])

    def test_never_changes_anything(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        self.scan()
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_csv_and_text_output(self):
        r = self.scan()
        lines = m.csv_text(r).strip().splitlines()
        self.assertEqual(lines[0], "size_bytes,size,days_since_changed,path")
        self.assertEqual(len(lines), 6)
        m.print_text(r)  # just must not crash

    def test_human_size(self):
        self.assertEqual(m.human_size(500), "500 B")
        self.assertEqual(m.human_size(2048), "2.0 KB")


class LocationTests(unittest.TestCase):
    def test_system_drive_first_then_user_then_others(self):
        locs = m.list_locations()
        self.assertEqual(locs[0]["path"].upper(), m.default_drive().upper())
        self.assertTrue(locs[0]["label"].startswith("Whole "))
        self.assertEqual(locs[1]["path"], str(Path.home()))
        self.assertTrue(locs[1]["label"].startswith("User folder"))
        self.assertEqual(len({l["path"] for l in locs}), len(locs))  # no repeats
        for l in locs:
            self.assertTrue(os.path.isdir(l["path"]), l)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make(self.root / "a.bin", 1234)
        self.state = {"token": "tok", "port": 0, "start_path": str(self.root), "top": 25,
                      "scan": None, "thread": None, "result": None, "error": None}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), m.make_handler(self.state))
        self.state["port"] = self.srv.server_address[1]
        self.state["server"] = self.srv
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.state['port']}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self._tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    def status(self):
        return json.loads(urllib.request.urlopen(self.base + "/api/status?token=tok").read())

    def test_page_loads_with_start_path(self):
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn("Disk Space Reporter", page)
        self.assertIn(json.dumps(str(self.root)), page)
        self.assertNotIn("__TOKEN__", page)
        self.assertNotIn("__LOCATIONS__", page)
        self.assertIn(json.dumps(str(Path.home())), page)  # the User folder choice is in the dropdown data

    def test_full_scan_flow_and_csv(self):
        self.assertTrue(self.post("/api/start", {"token": "tok", "path": str(self.root)})["ok"])
        for _ in range(100):
            s = self.status()
            if s["state"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(s["state"], "done")
        self.assertEqual(s["result"]["bytes"], 1234)
        csv = urllib.request.urlopen(self.base + "/api/csv?token=tok").read().decode()
        self.assertIn("a.bin", csv)

    def test_bad_folder_rejected(self):
        r = self.post("/api/start", {"token": "tok", "path": str(self.root / "nope")})
        self.assertFalse(r["ok"])

    def test_wrong_token_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/start", {"token": "bad", "path": str(self.root)})
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/api/status?token=bad")
        self.assertEqual(cm.exception.code, 403)

    def test_wrong_host_header_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.state["port"])
        c.request("GET", "/", headers={"Host": "evil.example.com"})
        self.assertEqual(c.getresponse().status, 403)


if __name__ == "__main__":
    unittest.main()
