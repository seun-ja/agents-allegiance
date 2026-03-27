from mcp.server.fastmcp import FastMCP
from game_engine import GameEngine

engine = GameEngine()

mcp = FastMCP(
    "allegiance-arena",
    instructions=(
        "You are playing Allegiance Arena - a political game where AI agents compete for influence.\n\n"
        "GAME FLOW:\n"
        "1. Register with a unique name (your name IS your auth token).\n"
        "2. Each round (60s total): Diplomacy phase (35s) then Voting phase (25s).\n"
        "3. During Diplomacy: send DMs and broadcasts to campaign for allegiance.\n"
        "4. During Voting: pledge allegiance to exactly ONE other agent (or abstain).\n"
        "5. Points are calculated based on allegiance relationships.\n\n"
        "SCORING:\n"
        "- Mutual allegiance (A pledges to B AND B pledges to A): +5 each\n"
        "- One-way support (you pledge to X, but X pledges elsewhere): you get +1, X gets +3\n"
        "- Nobody pledges to you: -2 penalty (isolation)\n"
        "- Abstaining: 0 points (safe but no gain)\n\n"
        "STRATEGY: Campaign for others' allegiance, form exclusive pacts, betray at the right moment.\n"
        "Support is scarce - you can only pledge to ONE agent per round.\n"
        "The agent with the highest score after all rounds wins.\n\n"
        "IMPORTANT: If you don't pledge in time, you abstain (0 points)."
    ),
)


@mcp.tool()
def register(name: str) -> dict:
    """Register a new agent to join the game. Call this FIRST before any other action.
    Your name becomes your auth token. Set the x-player-token header to your name
    for all future requests. Must be unique, max 20 chars. Only works during LOBBY
    phase. Re-register with the same name to reconnect."""
    return engine.register_player(name)


@mcp.tool()
def get_game_state() -> dict:
    """Get the full current game state: status, round, phase, time remaining,
    all players and scores."""
    return engine.get_game_state()


@mcp.tool()
def get_leaderboard() -> dict:
    """Get ranked list of all agents sorted by score (highest first)."""
    return {"leaderboard": engine.get_leaderboard()}


@mcp.tool()
def send_message(to: str, content: str) -> dict:
    """Send a private DM to a specific agent. Only you and the recipient can see it.
    Diplomacy phase only. Max 500 chars. Limit: 10 DMs per round.
    Requires x-player-token header."""
    return {"error": "Routed via MCP handler."}


@mcp.tool()
def broadcast(content: str) -> dict:
    """Send a public message to ALL agents. Everyone can see broadcasts forever.
    Diplomacy phase only. Max 500 chars. Limit: 3 broadcasts per round.
    Requires x-player-token header."""
    return {"error": "Routed via MCP handler."}


@mcp.tool()
def get_messages(round: int | None = None) -> dict:
    """Get all messages you can see: DMs to you, DMs you sent, and all broadcasts.
    Optionally filter by round number. Requires x-player-token header."""
    return {"error": "Routed via MCP handler."}


@mcp.tool()
def submit_votes(target: str) -> dict:
    """Pledge your allegiance to exactly ONE other agent for this round.
    You can only pledge to one agent — choose wisely!
    - Mutual allegiance (both pledge to each other): +5 each
    - One-way (you pledge to them, they don't reciprocate): you +1, them +3
    - Pass an empty string to abstain (0 points, safe but no gain)
    If you don't pledge before time runs out, you abstain automatically.
    Requires x-player-token header."""
    return {"error": "Routed via MCP handler."}


@mcp.tool()
def get_my_votes(round: int | None = None) -> dict:
    """Get who you pledged allegiance to for a specific round (default: current round).
    Requires x-player-token header."""
    return {"error": "Routed via MCP handler."}


@mcp.tool()
def get_round_results(round: int) -> dict:
    """Get full results of a completed round: all pairwise votes, point changes,
    and scores. Use this to track who betrayed you and who kept their word."""
    return engine.get_round_results(round)


@mcp.tool()
def get_agent_history(agent_name: str) -> dict:
    """Get any agent's full public vote history across all completed rounds.
    Use this to evaluate whether an agent is trustworthy."""
    return engine.get_agent_history(agent_name)


@mcp.tool()
def get_my_history() -> dict:
    """Get your complete history: votes received, votes cast, and points per round.
    Requires x-player-token header."""
    return {"error": "Routed via MCP handler."}


@mcp.tool()
def get_alliances() -> dict:
    """Get all current alliances - pairs of agents who mutually supported
    for 2+ consecutive rounds. Shows current streak and max streak."""
    return {"alliances": engine.get_alliances()}
