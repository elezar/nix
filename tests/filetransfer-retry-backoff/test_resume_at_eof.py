import argparse
import hashlib
import http.server
import subprocess
import threading


PAYLOAD = b"Nix EOF retry fixture\n" * 4096
EXPECTED_HASH = hashlib.sha256(PAYLOAD).hexdigest()


class ResumeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        scenario = self.path.removeprefix("/")
        range_header = self.headers.get("Range")
        self.server.ranges.append(range_header)
        if range_header:
            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            if scenario == "partial":
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
                self.send_header("Content-Length", str(len(PAYLOAD) - offset))
                self.end_headers()
                self.wfile.write(PAYLOAD[offset:])
                return
            self.send_response(416)
            sizes = {"shorter": len(PAYLOAD) - 1, "longer": len(PAYLOAD) + 1}
            if scenario != "missing":
                value = "invalid" if scenario == "malformed" else f"bytes */{sizes.get(scenario, len(PAYLOAD))}"
                self.send_header("Content-Range", value)
            if scenario == "changed-etag":
                self.send_header("ETag", '"changed"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"original"')
        if scenario == "complete":
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        body = PAYLOAD[:len(PAYLOAD) // 2] if scenario == "partial" else PAYLOAD
        if scenario == "corrupt":
            body = b"X" + body[1:]
        self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
        self.wfile.flush()
        self.close_connection = True


def fetch(args, scenario):
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), ResumeHandler) as server:
        server.ranges = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = [args.prefetch_url, "--type", "sha256", "--option", "download-attempts", "3"]
            if args.store:
                command += ["--store", args.store]
            command += [f"http://127.0.0.1:{server.server_port}/{scenario}", EXPECTED_HASH]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            return result, server.ranges
        finally:
            server.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefetch-url", default="nix-prefetch-url")
    parser.add_argument("--store")
    parser.add_argument("--expect-bug", action="store_true")
    args = parser.parse_args()
    for scenario in ("complete", "partial", "eof", "missing", "malformed", "shorter", "longer", "changed-etag", "corrupt"):
        result, ranges = fetch(args, scenario)
        expected_ranges = [None]
        if scenario != "complete":
            offset = len(PAYLOAD) // 2 if scenario == "partial" else len(PAYLOAD)
            expected_ranges.append(f"bytes={offset}-")
        assert ranges == expected_ranges, (scenario, ranges, result.stderr)
        succeeds = scenario in ("complete", "partial") or (scenario == "eof" and not args.expect_bug)
        assert (result.returncode == 0) == succeeds, (scenario, result.stderr)
        if not succeeds:
            if scenario == "corrupt" and not args.expect_bug:
                assert "hash mismatch" in result.stderr, result.stderr
            else:
                assert "HTTP error 416" in result.stderr, result.stderr
        print(f"{scenario}: exit={result.returncode}, Range={ranges}")


if __name__ == "__main__":
    main()
