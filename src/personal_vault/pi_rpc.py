"""Small synchronous command client with a continuously drained Pi event stream."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from collections import deque


class RpcError(RuntimeError):
    pass


class PiRpc:
    def __init__(self, command, *, session=None, cwd=None, env=None):
        self.command = list(command)
        self.session = session
        self.cwd, self.env = cwd, env
        self.events = queue.Queue()
        self.diagnostics = deque(maxlen=100)
        self._pending = {}
        self._lock = threading.Lock()
        self.process = None

    def start(self):
        if self.process is not None:
            raise RpcError("close the existing connection before starting again")
        args = self.command + ["--mode", "rpc"]
        if self.session:
            args += ["--session", str(self.session)]
        self.events = queue.Queue()
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=self.cwd, env=self.env)
        process = self.process
        self._reader = threading.Thread(target=self._read, args=(process,), daemon=True)
        self._stderr = threading.Thread(target=self._read_stderr, args=(process,), daemon=True)
        self._reader.start(); self._stderr.start()
        try:
            state = self.request("get_state")
        except Exception:
            self.close()
            raise
        self.session = state.get("sessionFile") or self.session
        return state

    def _read(self, process):
        try:
            for line in iter(process.stdout.readline, b""):
                record = json.loads(line)
                if record.get("type") == "response":
                    with self._lock:
                        target = self._pending.get(record.get("id"))
                    if target:
                        target.put(record)
                    else:
                        self.events.put(record)
                else:
                    self.events.put(record)
        except (ValueError, OSError) as error:
            self.diagnostics.append(str(error))
        finally:
            failure = RpcError("Pi connection closed")
            with self._lock:
                for target in self._pending.values():
                    target.put(failure)
            self.events.put({"type": "connection_closed"})

    def _read_stderr(self, process):
        for line in iter(process.stderr.readline, b""):
            self.diagnostics.append(line.decode("utf-8", errors="replace").rstrip())

    def request(self, command, *, timeout=30, **fields):
        request_id = str(uuid.uuid4())
        target = queue.Queue()
        with self._lock:
            if not self.process or self.process.poll() is not None:
                raise RpcError("Pi is not connected")
            self._pending[request_id] = target
            try:
                self.process.stdin.write(json.dumps({**fields, "id": request_id, "type": command}, ensure_ascii=False).encode() + b"\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._pending.pop(request_id, None)
                raise RpcError("Pi connection closed") from None
        try:
            response = target.get(timeout=timeout)
            if isinstance(response, Exception):
                raise response
            if not response.get("success"):
                raise RpcError(response.get("error", "Pi command failed"))
            data = response.get("data", {})
            if command == "get_state":
                self.session = data.get("sessionFile") or self.session
            return data
        except queue.Empty:
            raise TimeoutError(f"Pi command timed out: {command}") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def wait_settled(self, *, timeout=180, on_event=None):
        deadline = time.monotonic() + timeout
        records = []
        while True:
            try:
                event = self.events.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                raise TimeoutError("Pi did not settle before the deadline") from None
            if event.get("type") == "connection_closed":
                raise RpcError("Pi disconnected; reconnect to the saved session without resending the prompt")
            records.append(event)
            if on_event:
                on_event(event)
            if event.get("type") == "agent_settled":
                return records

    def prompt(self, message, *, timeout=180, on_event=None):
        response = self.request("prompt", message=message)
        if response.get("disposition") == "handled":
            return []
        return self.wait_settled(timeout=timeout, on_event=on_event)

    def cancel(self):
        queued = self.request("clear_queue")
        self.request("abort")
        return queued

    def reconnect(self):
        self.close()
        return self.start()

    def close(self):
        process = self.process
        if not process:
            return
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        self._reader.join(timeout=2); self._stderr.join(timeout=2)
        process.stdout.close(); process.stderr.close()
        self.process = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
