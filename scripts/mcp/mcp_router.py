"""
MCP Meta-Router — on-demand activation of backend MCP servers.

Architecture
------------
- One `supergateway` process per stdio backend exposes it as HTTP/SSE
  on http://127.0.0.1:<port>/sse. Lifecycle owned by start_mcp.sh.
- For `type: sse` backends (remote), no supergateway is spawned; the
  router connects directly to the configured URL.
- This router is a single persistent Unix-socket daemon. It is the only
  entry point that Claude Code talks to. (Claude Code invokes it via
  `mcp-proxy-tool -p <socket>`, which speaks JSON-RPC over the socket
  directly to this process.) One router process handles ALL client
  connections — there is no per-connection fork.
- On startup, this router advertises ONLY control tools:
      list_servers, use_server, list_active_servers, deactivate_server
- When the LLM calls `use_server("stripe")`, this router opens an SSE
  client session to the backend, calls initialize+tools/list, and
  dynamically registers those tools under its own namespace
  (`<backend>__<tool>`). The response tells the LLM to call
  `tools/list` next so its view of available tools refreshes.
- When the LLM calls an activated tool, the router forwards `tools/call`
  to the matching backend session and relays the response.

The router keeps each backend's tools prefixed with the backend name
(e.g. `stripe__create_payment_intent`) to avoid collisions across
backends that may share tool names.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.server import Server
from mcp.shared.message import SessionMessage

log = logging.getLogger("mcp-router")

# Default config path resolves relative to this file so the router works for
# any user who cloned the repo (no hardcoded $HOME). The real config file
# (mcp_config.json) is gitignored; users copy mcp_config.example.json.
CONFIG_PATH = Path(
    os.environ.get(
        "MCP_ROUTER_CONFIG",
        str(Path(__file__).resolve().parent / "mcp_config.json"),
    )
)


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


class Backend:
    """A configured backend (one entry from mcp_config.json's `servers` map)."""

    def __init__(self, name: str, cfg: dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.url = cfg.get("url") or f"http://127.0.0.1:{cfg['port']}/sse"
        # tools registered by this backend (filled in on activation)
        self.tools: dict[str, types.Tool] = {}
        # active client session (kept open for tool-call forwarding)
        self._session: ClientSession | None = None
        self._session_cm: Any = None
        # serialise concurrent use_server() calls for the same backend
        self._activate_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"Backend({self.name}, url={self.url}, tools={len(self.tools)})"


def load_config(path: Path) -> tuple[dict[str, Backend], dict[str, Any]]:
    cfg = json.loads(path.read_text())
    servers_cfg = cfg.get("servers", {})
    backends: dict[str, Backend] = {
        name: Backend(name, scfg) for name, scfg in servers_cfg.items()
    }
    return backends, cfg


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


def _prefixed(tool_name: str, backend_name: str) -> str:
    return f"{backend_name}__{tool_name}"


def _unprefix(prefixed: str) -> tuple[str, str] | None:
    backend, sep, original = prefixed.partition("__")
    if not sep or not backend or not original:
        return None
    return backend, original


CONTROL_TOOL_SCHEMAS: dict[str, types.Tool] = {
    "list_servers": types.Tool(
        name="list_servers",
        description=(
            "List all configured MCP backends. Returns a JSON array of "
            "{name, type, port, activated, tool_count} objects."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    "use_server": types.Tool(
        name="use_server",
        description=(
            "Activate a backend MCP server. Connects to it, fetches its "
            "tools, and registers them under the namespace "
            "`<backend_name>__<tool_name>`. After this call returns, the "
            "LLM MUST call `tools/list` to see the newly registered tools. "
            "Pass `name` = the backend's name from `list_servers`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Backend name (from list_servers).",
                }
            },
            "required": ["name"],
        },
    ),
    "list_active_servers": types.Tool(
        name="list_active_servers",
        description=(
            "List backends that are currently activated and have tools "
            "registered. Returns a JSON array of "
            "{name, tool_count, tool_names} objects."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    "deactivate_server": types.Tool(
        name="deactivate_server",
        description=(
            "Disconnect from a backend and remove its tools from the "
            "router's tool list. Pass `name` = the backend's name."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Backend name."}},
            "required": ["name"],
        },
    ),
}


# ---------------------------------------------------------------------------
# Per-connection Server
# ---------------------------------------------------------------------------


def _build_server(backends: dict[str, Backend]) -> Server:
    """Create a fresh MCP Server with handlers bound to the given backends.

    Each client connection gets its own Server instance so its
    initialization state is isolated. The backends dict is shared so
    activated tools are visible across connections (Claude Code sessions
    may reconnect and expect prior activations to persist).
    """
    server = Server("mcp-router")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = list(CONTROL_TOOL_SCHEMAS.values())
        tools.extend(
            types.Tool(
                name=_prefixed(t.name, backend.name),
                description=f"[{backend.name}] {t.description or ''}".strip(),
                inputSchema=t.inputSchema,
            )
            for backend in backends.values()
            for t in backend.tools.values()
        )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.Content]:
        # Control tools
        if name == "list_servers":
            payload = [
                {
                    "name": b.name,
                    "type": b.cfg.get("type"),
                    "port": b.cfg.get("port"),
                    "url": b.url,
                    "activated": b._session is not None,
                    "tool_count": len(b.tools),
                }
                for b in backends.values()
            ]
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "use_server":
            target = arguments.get("name")
            if not isinstance(target, str) or target not in backends:
                return [
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {"ok": False, "error": f"unknown backend: {target!r}"}
                        ),
                    )
                ]
            result = await _activate(target, backends)
            if result.get("ok") and not result.get("already_active"):
                result["next_step"] = (
                    "Now call `tools/list` so the LLM can see the newly "
                    f"registered tools from `{target}` (prefixed "
                    f"with `{target}__`)."
                )
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "list_active_servers":
            payload = [
                {
                    "name": b.name,
                    "tool_count": len(b.tools),
                    "tool_names": list(b.tools.keys()),
                }
                for b in backends.values()
                if b._session is not None
            ]
            return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "deactivate_server":
            target = arguments.get("name")
            if not isinstance(target, str) or target not in backends:
                return [
                    types.TextContent(
                        type="text",
                        text=json.dumps({"ok": False, "error": "unknown backend"}),
                    )
                ]
            result = await _deactivate(target, backends)
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        # Dynamic tool: must be prefixed with backend name
        parts = _unprefix(name)
        if parts is None or parts[0] not in backends:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"tool not found: {name!r}. "
                                "Call `use_server` first, then `tools/list`."
                            ),
                        }
                    ),
                )
            ]
        backend_name, original = parts
        backend = backends[backend_name]
        if backend._session is None:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"backend {backend_name!r} is not "
                                "activated. Call `use_server` first."
                            ),
                        }
                    ),
                )
            ]
        return await _forward_call(backend, original, arguments)

    return server


# ---------------------------------------------------------------------------
# Activation / deactivation (shared across connections)
# ---------------------------------------------------------------------------


async def _activate(name: str, backends: dict[str, Backend]) -> dict[str, Any]:
    backend = backends[name]
    async with backend._activate_lock:
        if backend._session is not None:
            return {
                "ok": True,
                "already_active": True,
                "name": name,
                "tool_count": len(backend.tools),
            }
        try:
            cm = sse_client(backend.url)
            read_stream, write_stream = await cm.__aenter__()
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            await session.initialize()
            tools_result = await session.list_tools()
            backend.tools = {t.name: t for t in tools_result.tools}
            backend._session = session
            backend._session_cm = cm
        except Exception as exc:
            log.exception("Failed to activate backend %s", name)
            return {"ok": False, "name": name, "error": str(exc)}
    return {
        "ok": True,
        "name": name,
        "tool_count": len(backend.tools),
        "tool_names": list(backend.tools.keys()),
    }


async def _deactivate(name: str, backends: dict[str, Backend]) -> dict[str, Any]:
    backend = backends[name]
    async with backend._activate_lock:
        if backend._session is None:
            return {"ok": True, "already_inactive": True, "name": name}
        try:
            await backend._session.__aexit__(None, None, None)
        except Exception:
            log.exception("Error closing session for %s", name)
        try:
            await backend._session_cm.__aexit__(None, None, None)
        except Exception:
            log.exception("Error closing SSE stream for %s", name)
        backend._session = None
        backend._session_cm = None
        backend.tools.clear()
    return {"ok": True, "name": name}


async def _forward_call(
    backend: Backend, original_tool: str, arguments: dict[str, Any]
) -> list[types.Content]:
    assert backend._session is not None
    try:
        result = await backend._session.call_tool(original_tool, arguments=arguments)
        return list(result.content)
    except Exception as exc:
        log.exception("Backend %s call_tool failed", backend.name)
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"ok": False, "error": str(exc)}),
            )
        ]


# ---------------------------------------------------------------------------
# Unix-socket server
# ---------------------------------------------------------------------------

_conn_counter = 0


def _next_conn_id() -> str:
    global _conn_counter
    _conn_counter += 1
    return f"c{_conn_counter}"


def _short_msg(msg: types.JSONRPCMessage) -> str:
    """Render a JSON-RPC message as a one-line summary for logging."""
    root = msg.root
    if isinstance(root, types.JSONRPCRequest):
        return f"REQ id={root.id} method={root.method}"
    if isinstance(root, types.JSONRPCNotification):
        return f"NOT method={root.method}"
    if isinstance(root, types.JSONRPCResponse):
        return f"RES id={root.id}"
    if isinstance(root, types.JSONRPCError):
        return f"ERR id={root.id} code={root.error.code}"
    return repr(root)[:120]


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    backends: dict[str, Backend],
) -> None:
    """Handle one MCP client connection on a Unix socket.

    Bridges the socket (newline-delimited JSON) to the MCP SDK's
    MemoryObjectStream interface, and runs a fresh Server per connection.
    Every message in both directions is logged with a per-connection ID
    so the full wire traffic is reconstructable from the log file.
    """
    conn_id = _next_conn_id()
    # Create memory streams compatible with the MCP SDK.
    read_stream_writer, read_stream = anyio.create_memory_object_stream(1000)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(1000)
    server = _build_server(backends)

    async def socket_to_mcp() -> None:
        """Read JSON-RPC lines from the socket and feed them to the SDK.

        mcp SDK 1.27+ expects each item on the read stream to be a
        ``SessionMessage`` (a wrapper carrying the ``JSONRPCMessage``
        plus transport metadata). Without this wrap, the SDK's
        ``BaseSession._receive_loop`` raises ``AttributeError`` on the
        first inbound message and the per-connection ``Server.run`` dies.
        """
        buffer = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    log.info("[%s] socket EOF", conn_id)
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = types.JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:
                        log.warning(
                            "[%s] C->R INVALID JSON: %r (%s)", conn_id, line, exc
                        )
                        continue
                    log.info("[%s] C->R %s", conn_id, _short_msg(msg))
                    await read_stream_writer.send(SessionMessage(message=msg))
        except anyio.ClosedResourceError:
            pass
        except Exception:
            log.exception("[%s] socket_to_mcp failed", conn_id)
        finally:
            await read_stream_writer.aclose()

    async def mcp_to_socket() -> None:
        """Read SDK messages and write them to the socket as JSON lines.

        The MCP SDK 1.27+ emits ``SessionMessage`` objects on the write
        stream; unwrap ``.message`` to get the raw ``JSONRPCMessage``
        for serialization to the socket.
        """
        try:
            async for session_msg in write_stream_reader:
                summary = _short_msg(session_msg.message)
                log.info("[%s] S->R %s", conn_id, summary)
                data = (
                    session_msg.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    + "\n"
                )
                writer.write(data.encode("utf-8"))
                await writer.drain()
                log.info("[%s] R->C %s (sent)", conn_id, summary)
        except anyio.ClosedResourceError:
            pass
        except Exception:
            log.exception("[%s] mcp_to_socket failed", conn_id)
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    log.info("[%s] client connected", conn_id)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(socket_to_mcp)
            tg.start_soon(mcp_to_socket)
            # ``stateless=True`` tells the MCP SDK's ``ServerSession`` to
            # not require the client to send ``initialize`` +
            # ``notifications/initialized`` before processing requests.
            # fcc-claude (via mcp-proxy-tool) sends ``tools/list`` as the
            # first message with no initialization handshake, so without
            # this flag every request is rejected with "Received request
            # before initialization was complete" (-32602) and fcc-claude
            # shows "tools fetch failed". The router doesn't depend on
            # per-connection init state — it just routes requests — so
            # stateless mode is the correct fit.
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
                stateless=True,
            )
    except Exception:
        log.exception("[%s] Server.run failed", conn_id)
    finally:
        log.info("[%s] client disconnected", conn_id)


async def serve_unix_socket(socket_path: str, backends: dict[str, Backend]) -> None:
    """Listen on a Unix socket and accept MCP client connections forever.

    Implementation note: ``anyio.create_unix_server`` was removed in
    anyio 4.x — its replacement ``create_unix_listener`` has a different
    API (async-iterator based, no callback, no ``serve_forever``). Since
    ``_handle_client`` is already written in terms of asyncio's
    ``StreamReader``/``StreamWriter`` (which is what the MCP SDK's
    ``Server.run`` integrates with via anyio memory streams), the
    minimal fix is to use ``asyncio.start_unix_server`` directly. This
    works transparently inside ``anyio.run()`` because anyio's default
    asyncio backend *is* asyncio.
    """
    # Remove any stale socket file.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(socket_path)
    parent_dir = os.path.dirname(socket_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    os.chmod(parent_dir or ".", 0o700)

    async def on_connect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _handle_client(reader, writer, backends)

    server = await asyncio.start_unix_server(on_connect, path=socket_path)
    # Restrict socket permissions.
    with contextlib.suppress(OSError):
        os.chmod(socket_path, 0o600)
    log.info("listening on unix://%s", socket_path)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP meta-router (Unix-socket daemon)")
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to mcp_config.json (default: %(default)s)",
    )
    parser.add_argument(
        "--socket",
        required=True,
        help="Unix socket path to listen on (e.g. ~/.mcp-router/sockets/router.sock).",
    )
    parser.add_argument(
        "--log",
        default=os.environ.get("MCP_ROUTER_LOG"),
        help="Optional path to write logs to (in addition to stderr).",
    )
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log:
        handlers.append(logging.FileHandler(args.log))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    if not Path(args.config).exists():
        log.error("Config not found: %s", args.config)
        return 2

    backends, _raw_cfg = load_config(Path(args.config))
    log.info(
        "Loaded %d backends from %s: %s",
        len(backends),
        args.config,
        ", ".join(b.name for b in backends.values()),
    )

    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(serve_unix_socket, args.socket, backends)
    return 0


if __name__ == "__main__":
    sys.exit(main())
