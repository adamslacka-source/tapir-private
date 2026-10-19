"""Minimal client for the Archicad JSON API and the Tapir add-on (standard library only).

Every call has a timeout: nothing may hang silently. When Archicad does not answer, the
error says so explicitly (a modal dialog is the usual cause) - the caller must stop and
report; killing Archicad is never done from here.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 19723
FORBIDDEN_ROOTS = (r"c:\archicadtemp",)          # the SK template - never write into it


class ArchicadNotResponding(RuntimeError):
    """Archicad did not answer within the timeout (modal dialog? long operation?)."""


class TapirError(RuntimeError):
    pass


class TapirClient:
    def __init__(self, port: int = DEFAULT_PORT, timeout: float = 60.0, verbose: bool = False):
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.calls = 0

    # -- transport -----------------------------------------------------------------------
    def post(self, command: str, parameters: dict | None = None, timeout: float | None = None) -> dict:
        timeout = timeout or self.timeout
        body = json.dumps({"command": command, "parameters": parameters or {}}).encode("utf-8")
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}", body, {"Content-Type": "application/json"})
        started = time.monotonic()
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                answer = json.loads(response.read())
        except (socket.timeout, TimeoutError) as exc:
            raise ArchicadNotResponding(
                f"{command}: no answer from Archicad within {timeout:.0f} s (port {self.port}). "
                "A modal dialog may be open - check Archicad, do NOT kill it.") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise ArchicadNotResponding(f"{command}: timeout after {timeout:.0f} s (port {self.port}).") from exc
            raise ArchicadNotResponding(f"{command}: Archicad JSON API not reachable on port {self.port}: {exc.reason}") from exc
        if self.verbose:
            print(f"  [{time.monotonic() - started:5.2f} s] {command}")
        return answer

    def api(self, command: str, parameters: dict | None = None, timeout: float | None = None) -> dict:
        answer = self.post(command, parameters, timeout)
        if not answer.get("succeeded"):
            raise TapirError(f"{command} failed: {json.dumps(answer.get('error'), ensure_ascii=False)}")
        return answer.get("result", {})

    def tapir(self, command: str, parameters: dict | None = None, timeout: float | None = None) -> dict:
        answer = self.post("API.ExecuteAddOnCommand", {
            "addOnCommandId": {"commandNamespace": "TapirCommand", "commandName": command},
            "addOnCommandParameters": parameters or {}}, timeout)
        if not answer.get("succeeded"):
            raise TapirError(f"{command} failed: {json.dumps(answer.get('error'), ensure_ascii=False)}")
        result = answer["result"]["addOnCommandResponse"]
        if isinstance(result, dict) and "error" in result and len(result) == 1:
            raise TapirError(f"{command} returned error: {json.dumps(result['error'], ensure_ascii=False)}")
        return result

    # -- helpers -------------------------------------------------------------------------
    def is_alive(self) -> bool:
        try:
            return bool(self.post("API.IsAlive", timeout=5).get("result", {}).get("isAlive"))
        except ArchicadNotResponding:
            return False

    def project_path(self) -> str:
        info = self.tapir("GetProjectInfo", timeout=15)
        if info.get("isUntitled"):
            return ""
        return info.get("projectPath") or info.get("projectLocation") or ""

    def guard_project(self, expected: str | Path) -> str:
        """Refuses to continue unless the open project is exactly `expected` (and not the template)."""
        path = self.project_path()
        norm = lambda p: str(p).replace("/", "\\").lower().rstrip("\\")
        if any(norm(path).startswith(root) for root in FORBIDDEN_ROOTS):
            raise TapirError(f"Refusing to write: the open project is the template ({path}).")
        if norm(path) != norm(expected):
            raise TapirError(f"Refusing to write: open project is '{path}', expected '{expected}'.")
        return path

    def addon_version(self) -> str:
        return self.tapir("GetAddOnVersion", timeout=10).get("version", "")


def results_ok(execution_results: list[dict]) -> list[str]:
    """Error texts of an ExecutionResults list (empty when everything succeeded)."""
    return [json.dumps(r.get("error", r), ensure_ascii=False) for r in execution_results if not r.get("success")]
