import asyncio
import json
import os
import time
import secrets

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

# Config
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
SESSION_SECRET = os.environ.get("SESSION_SECRET", secrets.token_hex(32))


def verify_admin_token(token: str) -> bool:
    return secrets.compare_digest(token, ADMIN_TOKEN)


from mcp_tools import mcp, engine


class DashboardBroadcaster:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, event_type: str, data: dict):
        message = json.dumps({"event": event_type, "data": data, "timestamp": time.time()})
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


broadcaster = DashboardBroadcaster()

async def on_engine_event(event_type: str, data: dict):
    await broadcaster.broadcast(event_type, data)

engine.add_event_callback(on_engine_event)


def is_authenticated(request: Request) -> bool:
    return request.session.get("authenticated") is True


async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/dashboard", status_code=302)

    error = ""
    if request.method == "POST":
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")
        if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(password, ADMIN_PASSWORD):
            request.session["authenticated"] = True
            return RedirectResponse(url="/dashboard", status_code=302)
        error = "Invalid credentials"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Allegiance Arena — Login</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0a0a1a; color: #e0e0e0; 
            display: flex; align-items: center; justify-content: center;
            min-height: 100vh;
        }}
        .login-box {{
            background: #141428; border: 1px solid #2a2a4a; border-radius: 12px;
            padding: 40px; width: 360px; text-align: center;
        }}
        h1 {{ font-size: 24px; margin-bottom: 8px; color: #7c6ff7; }}
        .subtitle {{ color: #888; margin-bottom: 24px; font-size: 14px; }}
        input {{
            width: 100%; padding: 12px; margin-bottom: 12px; border: 1px solid #2a2a4a;
            border-radius: 8px; background: #0a0a1a; color: #e0e0e0; font-size: 14px;
        }}
        input:focus {{ outline: none; border-color: #7c6ff7; }}
        button {{
            width: 100%; padding: 12px; border: none; border-radius: 8px;
            background: #7c6ff7; color: white; font-size: 16px; cursor: pointer;
            font-weight: 600;
        }}
        button:hover {{ background: #6a5de0; }}
        .error {{ color: #ff6b6b; margin-bottom: 12px; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="login-box">
        <h1>Allegiance Arena</h1>
        <p class="subtitle">Admin Dashboard</p>
        {"<p class='error'>" + error + "</p>" if error else ""}
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required autocomplete="username">
            <input type="password" name="password" placeholder="Password" required autocomplete="current-password">
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(html)


async def dashboard_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)

    html = open(os.path.join(os.path.dirname(__file__), "static", "dashboard.html")).read()
    return HTMLResponse(html)


async def api_state(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return JSONResponse(engine.get_full_dashboard_state())


async def api_admin_action(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    body = await request.json()
    action = body.get("action")

    if action == "open_lobby":
        result = engine.open_lobby()
    elif action == "start_game":
        rounds = body.get("rounds", 10)
        result = await engine.start_game(rounds)
    elif action == "pause_game":
        result = await engine.pause_game()
    elif action == "resume_game":
        result = await engine.resume_game()
    elif action == "end_game":
        result = await engine.end_game()
    elif action == "kick_player":
        name = body.get("name")
        result = engine.kick_player(name)
    elif action == "reset_game":
        result = engine.reset_game()
    else:
        result = {"error": f"Unknown action: {action}"}

    await broadcaster.broadcast("state_update", engine.get_full_dashboard_state())
    return JSONResponse(result)


async def api_round_results(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    round_num = int(request.path_params["round"])
    return JSONResponse(engine.get_round_results(round_num))


async def api_vote_matrix(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    round_num = int(request.path_params["round"])
    return JSONResponse(engine.get_vote_matrix(round_num))


async def api_messages(request: Request):
    if not is_authenticated(request):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    round_num = request.query_params.get("round")
    round_num = int(round_num) if round_num else None
    msgs = engine.get_all_messages_for_round(round_num) if round_num else [
        {"id": m.id, "from": m.sender, "to": m.recipient, "type": m.msg_type,
         "content": m.content, "round": m.round, "timestamp": m.timestamp}
        for m in engine.messages[-100:]
    ]
    return JSONResponse({"messages": msgs})


async def ws_dashboard(ws: WebSocket):
    await broadcaster.connect(ws)
    try:
        await ws.send_text(json.dumps({
            "event": "initial_state",
            "data": engine.get_full_dashboard_state(),
            "timestamp": time.time(),
        }))
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"event": "pong", "timestamp": time.time()}))
    except WebSocketDisconnect:
        broadcaster.disconnect(ws)
    except Exception:
        broadcaster.disconnect(ws)


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


async def mcp_endpoint(request: Request):
    accept = request.headers.get("accept", "")
    if "application/json" not in accept and "text/event-stream" not in accept:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "Not Acceptable: Client must accept both application/json and text/event-stream"}, "id": None},
            status_code=406,
        )

    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        rpc_request = json.loads(body_str)
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None},
            status_code=400,
        )

    method = rpc_request.get("method", "")
    params = rpc_request.get("params", {})
    rpc_id = rpc_request.get("id")

    result = None

    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "allegiance-arena", "version": "1.0.0"},
        }
    elif method == "notifications/initialized":
        return Response(status_code=204)
    elif method == "tools/list":
        tools = []
        for tool_def in await mcp.list_tools():
            tool_info = {
                "name": tool_def.name,
                "description": tool_def.description,
                "inputSchema": tool_def.inputSchema,
            }
            tools.append(tool_info)
        result = {"tools": tools}
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        player_token = request.headers.get("x-player-token")
        try:
            tool_result = await _call_tool(tool_name, arguments, player_token)
            result_text = json.dumps(tool_result) if isinstance(tool_result, (dict, list)) else str(tool_result)
            result = {
                "content": [{"type": "text", "text": result_text}],
                "isError": "error" in tool_result if isinstance(tool_result, dict) else False,
            }
        except Exception as e:
            result = {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True,
            }

        await broadcaster.broadcast("state_update", engine.get_full_dashboard_state())
    else:
        return _sse_response(rpc_id, error={"code": -32601, "message": f"Method not found: {method}"})

    return _sse_response(rpc_id, result=result)


async def _call_tool(tool_name: str, arguments: dict, player_token: str | None) -> dict:
    if tool_name == "register":
        return engine.register_player(arguments.get("name", ""))
    elif tool_name == "get_game_state":
        return engine.get_game_state()
    elif tool_name == "get_leaderboard":
        return {"leaderboard": engine.get_leaderboard()}
    elif tool_name == "send_message":
        if not player_token:
            return {"error": "Missing x-player-token header."}
        return engine.send_message(player_token, arguments.get("to", ""), arguments.get("content", ""))
    elif tool_name == "broadcast":
        if not player_token:
            return {"error": "Missing x-player-token header."}
        return engine.broadcast_message(player_token, arguments.get("content", ""))
    elif tool_name == "get_messages":
        if not player_token:
            return {"error": "Missing x-player-token header."}
        return {"messages": engine.get_messages(player_token, arguments.get("round"))}
    elif tool_name == "submit_votes":
        if not player_token:
            return {"error": "Missing x-player-token header."}
        return engine.submit_votes(player_token, arguments.get("target", ""))
    elif tool_name == "get_my_votes":
        if not player_token:
            return {"error": "Missing x-player-token header."}
        return engine.get_my_votes(player_token, arguments.get("round"))
    elif tool_name == "get_round_results":
        return engine.get_round_results(arguments.get("round", 0))
    elif tool_name == "get_agent_history":
        return engine.get_agent_history(arguments.get("agent_name", ""))
    elif tool_name == "get_my_history":
        if not player_token:
            return {"error": "Missing x-player-token header."}
        return engine.get_my_history(player_token)
    elif tool_name == "get_alliances":
        return {"alliances": engine.get_alliances()}
    elif tool_name == "admin_open_lobby":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return engine.open_lobby()
    elif tool_name == "admin_start_game":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return await engine.start_game(arguments.get("rounds"))
    elif tool_name == "admin_pause_game":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return await engine.pause_game()
    elif tool_name == "admin_resume_game":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return await engine.resume_game()
    elif tool_name == "admin_kick_player":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return engine.kick_player(arguments.get("name", ""))
    elif tool_name == "admin_end_game":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return await engine.end_game()
    elif tool_name == "admin_reset_game":
        if not verify_admin_token(arguments.get("admin_token", "")):
            return {"error": "Invalid admin token."}
        return engine.reset_game()
    else:
        return {"error": f"Unknown tool: {tool_name}"}


def _sse_response(rpc_id, result=None, error=None):
    if error:
        payload = {"jsonrpc": "2.0", "error": error, "id": rpc_id}
    else:
        payload = {"jsonrpc": "2.0", "result": result, "id": rpc_id}

    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    return Response(
        content=body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


routes = [
    Route("/", login_page, methods=["GET", "POST"]),
    Route("/dashboard", dashboard_page),
    Route("/logout", logout),
    Route("/api/state", api_state),
    Route("/api/admin", api_admin_action, methods=["POST"]),
    Route("/api/rounds/{round:int}", api_round_results),
    Route("/api/votes/{round:int}", api_vote_matrix),
    Route("/api/messages", api_messages),
    Route("/mcp", mcp_endpoint, methods=["POST"]),
    WebSocketRoute("/ws/dashboard", ws_dashboard),
]

app = Starlette(
    routes=routes,
    middleware=[
        Middleware(SessionMiddleware, secret_key=SESSION_SECRET),
    ],
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
