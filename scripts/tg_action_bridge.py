#!/usr/bin/env python3
"""Fallback bridge for calling tg-mcp ActionMCP tools over stdio JSON-RPC.

Use this when the current Codex thread does not expose native
`mcp__tgmcp_actions__*` tools but shell access is still available.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
DEFAULT_SERVER_NAME = "tgmcp_actions"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_RUNTIME_MODE = "copy"
DEFAULT_RUNTIME_PROFILE = "actions_bridge"


class BridgeError(RuntimeError):
    """Bridge-specific failure with user-facing context."""


@dataclass
class ServerSpec:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str | None = None


def _normalize_server_candidates(name: str) -> list[str]:
    raw = (name or DEFAULT_SERVER_NAME).strip()
    if not raw:
        raw = DEFAULT_SERVER_NAME
    variants = [raw]
    if "_" in raw:
        variants.append(raw.replace("_", "-"))
    if "-" in raw:
        variants.append(raw.replace("-", "_"))
    seen: list[str] = []
    for item in variants:
        if item and item not in seen:
            seen.append(item)
    return seen


def load_server_spec(config_path: Path, server_name: str) -> ServerSpec:
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError(f"Codex config not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BridgeError(f"Invalid TOML in config: {config_path}: {exc}") from exc

    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        raise BridgeError(f"No [mcp_servers] section in {config_path}")

    resolved_name = None
    spec = None
    for candidate in _normalize_server_candidates(server_name):
        value = servers.get(candidate)
        if isinstance(value, dict):
            resolved_name = candidate
            spec = value
            break

    if spec is None or resolved_name is None:
        tried = ", ".join(_normalize_server_candidates(server_name))
        raise BridgeError(
            f"Action server '{server_name}' not found in {config_path} (tried: {tried})"
        )

    command = str(spec.get("command") or "").strip()
    if not command:
        raise BridgeError(f"Server '{resolved_name}' has no command in {config_path}")

    args = spec.get("args") or []
    if not isinstance(args, list):
        raise BridgeError(f"Server '{resolved_name}' args must be a list")

    env = spec.get("env") or {}
    if not isinstance(env, dict):
        raise BridgeError(f"Server '{resolved_name}' env must be a table")

    cwd = spec.get("cwd")
    return ServerSpec(
        name=resolved_name,
        command=command,
        args=[str(item) for item in args],
        env={str(key): str(value) for key, value in env.items()},
        cwd=str(cwd).strip() or None if cwd is not None else None,
    )


def parse_env_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise BridgeError(f"Invalid --set-env entry '{item}'. Expected KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise BridgeError(f"Invalid --set-env entry '{item}'. Empty KEY.")
        overrides[key] = value
    return overrides


def build_server_env(
    spec_env: dict[str, str],
    env_overrides: dict[str, str],
    *,
    runtime_mode: str | None,
    runtime_profile: str | None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(spec_env)

    if runtime_mode:
        env.setdefault("TG_SESSION_RUNTIME_MODE", runtime_mode)
    if runtime_profile:
        env.setdefault("TG_SESSION_RUNTIME_PROFILE", runtime_profile)

    env.update(env_overrides)
    return env


def parse_args_payload(args_json: str | None, args_file: str | None) -> dict[str, Any]:
    if args_json and args_file:
        raise BridgeError("Use either --args-json or --args-file, not both.")

    raw = "{}"
    if args_json:
        raw = args_json
    elif args_file:
        raw = Path(args_file).read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"Invalid JSON arguments: {exc}") from exc

    if not isinstance(payload, dict):
        raise BridgeError("Tool arguments must decode to a JSON object.")
    return payload


def extract_content_payload(result: dict[str, Any]) -> Any:
    content = result.get("content")
    if isinstance(content, list) and content:
        item = content[0]
        if isinstance(item, dict) and "text" in item:
            text = str(item["text"])
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


class MCPStdioClient:
    def __init__(self, spec: ServerSpec, env: dict[str, str]):
        self.spec = spec
        self.env = env
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1

    def __enter__(self) -> "MCPStdioClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            [self.spec.command, *self.spec.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.spec.cwd,
            env=self.env,
        )
        self._request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "tg_action_bridge", "version": "0.1"},
            },
        )
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2)
        self._proc = None

    def list_tools(self) -> list[str]:
        result = self._request("tools/list", {})
        tools = result.get("tools") or []
        if not isinstance(tools, list):
            raise BridgeError("Invalid tools/list payload from server.")
        names = []
        for item in tools:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return names

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        return extract_content_payload(result)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        response = self._read_response()
        if response.get("id") != request_id:
            raise BridgeError(
                f"Unexpected MCP response id={response.get('id')} for request {request_id}"
            )
        if "error" in response:
            raise BridgeError(f"MCP error for {method}: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise BridgeError(f"Invalid MCP result for {method}: {response}")
        return result

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BridgeError("MCP process is not running.")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_response(self) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise BridgeError("MCP process is not running.")

        while True:
            line = self._proc.stdout.readline()
            if line:
                break
            returncode = self._proc.poll()
            stderr = ""
            if self._proc.stderr is not None:
                try:
                    stderr = self._proc.stderr.read().strip()
                except Exception:
                    stderr = ""
            if returncode is None:
                raise BridgeError("No MCP response received.")
            detail = f" (exit={returncode})" if returncode is not None else ""
            if stderr:
                detail += f" stderr={stderr}"
            raise BridgeError(f"ActionMCP process exited before reply{detail}")

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"Invalid JSON-RPC line from server: {line!r}") from exc

        if not isinstance(payload, dict):
            raise BridgeError(f"Unexpected JSON-RPC payload: {payload!r}")
        return payload


def run_write_call(
    client: MCPStdioClient,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    confirmation_text: str,
    approval_wait_sec: float,
) -> dict[str, Any]:
    preview_args = dict(arguments)
    preview_args["dry_run"] = True
    preview_args.pop("confirm", None)
    preview_args.pop("approval_code", None)

    preview = client.call_tool(tool_name, preview_args)
    if not isinstance(preview, dict):
        raise BridgeError(f"Expected JSON object from dry_run, got: {preview!r}")
    if preview.get("success") is False:
        return {"preview": preview, "execution": None}

    approval_code = str(preview.get("approval_code") or "").strip()
    if not approval_code:
        raise BridgeError("Dry run did not return approval_code.")

    if approval_wait_sec > 0:
        time.sleep(approval_wait_sec)

    execute_args = dict(arguments)
    execute_args["dry_run"] = False
    execute_args["confirm"] = True
    execute_args["confirmation_text"] = confirmation_text
    execute_args["approval_code"] = approval_code
    execution = client.call_tool(tool_name, execute_args)
    return {"preview": preview, "execution": execution}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="Path to Codex config.toml"
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER_NAME,
        help="ActionMCP server name in config.toml (underscore/hyphen aliases supported)",
    )
    parser.add_argument(
        "--runtime-mode",
        choices=("direct", "copy", "off"),
        default=DEFAULT_RUNTIME_MODE,
        help="Bridge-only TG_SESSION_RUNTIME_MODE override (default: copy)",
    )
    parser.add_argument(
        "--runtime-profile",
        default=DEFAULT_RUNTIME_PROFILE,
        help="Bridge-only TG_SESSION_RUNTIME_PROFILE override (default: actions_bridge)",
    )
    parser.add_argument(
        "--set-env",
        action="append",
        default=[],
        help="Extra env override for spawned ActionMCP process (KEY=VALUE)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("tools", help="List available ActionMCP tool names")

    call_parser = subparsers.add_parser(
        "call", help="Call one ActionMCP tool with raw JSON args"
    )
    call_parser.add_argument(
        "tool_name", help="Tool name, for example tg_get_actions_policy"
    )
    call_parser.add_argument("--args-json", help="Tool arguments as JSON object")
    call_parser.add_argument(
        "--args-file", help="Path to JSON file with tool arguments"
    )

    write_parser = subparsers.add_parser(
        "write-call",
        help="Run canonical dry_run -> approval_code -> confirm flow for write tools",
    )
    write_parser.add_argument(
        "tool_name", help="Write tool name, for example tg_send_message"
    )
    write_parser.add_argument("--args-json", help="Tool arguments as JSON object")
    write_parser.add_argument(
        "--args-file", help="Path to JSON file with tool arguments"
    )
    write_parser.add_argument(
        "--confirmation-text",
        help="Exact confirmation_text to use; defaults to TG_ACTIONS_CONFIRMATION_PHRASE",
    )
    write_parser.add_argument(
        "--approval-wait-sec",
        default="auto",
        help="Wait before execute: 'auto', '0', or explicit seconds",
    )
    return parser


def resolve_approval_wait(raw_value: str, env: dict[str, str]) -> float:
    value = (raw_value or "auto").strip().lower()
    if value == "auto":
        raw_env = str(env.get("TG_ACTIONS_APPROVAL_MIN_AGE_SEC", "0")).strip()
        try:
            return max(0.0, float(raw_env))
        except ValueError:
            return 0.0
    try:
        return max(0.0, float(value))
    except ValueError as exc:
        raise BridgeError(f"Invalid --approval-wait-sec value '{raw_value}'") from exc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        spec = load_server_spec(Path(args.config).expanduser().resolve(), args.server)
        env_overrides = parse_env_overrides(args.set_env)
        runtime_mode = None if args.runtime_mode == "off" else args.runtime_mode
        runtime_profile = args.runtime_profile if runtime_mode else None
        env = build_server_env(
            spec.env,
            env_overrides,
            runtime_mode=runtime_mode,
            runtime_profile=runtime_profile,
        )

        with MCPStdioClient(spec, env) as client:
            if args.command == "tools":
                payload = {"server": spec.name, "tools": client.list_tools()}
            elif args.command == "call":
                payload = client.call_tool(
                    args.tool_name,
                    parse_args_payload(
                        getattr(args, "args_json", None),
                        getattr(args, "args_file", None),
                    ),
                )
            elif args.command == "write-call":
                confirmation_text = (
                    args.confirmation_text
                    or str(
                        env.get("TG_ACTIONS_CONFIRMATION_PHRASE", "отправляй")
                    ).strip()
                )
                if not confirmation_text:
                    raise BridgeError(
                        "No confirmation text available. Set --confirmation-text or env."
                    )
                payload = run_write_call(
                    client,
                    tool_name=args.tool_name,
                    arguments=parse_args_payload(args.args_json, args.args_file),
                    confirmation_text=confirmation_text,
                    approval_wait_sec=resolve_approval_wait(
                        args.approval_wait_sec, env
                    ),
                )
            else:
                raise BridgeError(f"Unknown command: {args.command}")
    except BridgeError as exc:
        print(
            json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
