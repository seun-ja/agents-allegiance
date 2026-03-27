import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class GameStatus(str, Enum):
    SETUP = "setup"
    LOBBY = "lobby"
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"


class Phase(str, Enum):
    DIPLOMACY = "diplomacy"
    VOTING = "voting"
    RESOLUTION = "resolution"


MUTUAL_ALLEGIANCE_BONUS = 5
ONE_WAY_GIVER_BONUS = 1
ONE_WAY_RECEIVER_BONUS = 3
ISOLATION_PENALTY = -2

DIPLOMACY_DURATION = 35  # seconds
VOTING_DURATION = 25     # seconds
DEFAULT_ROUNDS = 10
STARTING_SCORE = 0
MAX_DMS_PER_ROUND = 10
MAX_BROADCASTS_PER_ROUND = 3
MAX_MESSAGE_LENGTH = 500
MAX_NAME_LENGTH = 20
MIN_PLAYERS = 3


@dataclass
class Player:
    name: str
    score: int = 0
    registered_at: float = field(default_factory=time.time)
    is_connected: bool = True
    last_seen: float = field(default_factory=time.time)


@dataclass
class Message:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sender: str = ""
    recipient: Optional[str] = None  # None = broadcast
    msg_type: str = "direct"  # "direct" or "broadcast"
    content: str = ""
    round: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Pledge:
    round: int
    voter: str
    target: str  # who they pledged allegiance to (or "" for abstain)


@dataclass
class RoundResult:
    round: int
    pledges: dict[str, str] = field(default_factory=dict)  # voter -> target
    mutual_pairs: list[tuple[str, str]] = field(default_factory=list)  # mutual allegiance pairs
    supporters: dict[str, list[str]] = field(default_factory=dict)  # target -> list of supporters
    score_changes: dict[str, int] = field(default_factory=dict)
    scores_after: dict[str, int] = field(default_factory=dict)


class GameEngine:
    def __init__(self):
        self.status: GameStatus = GameStatus.SETUP
        self.current_round: int = 0
        self.total_rounds: int = DEFAULT_ROUNDS
        self.current_phase: Phase = Phase.DIPLOMACY
        self.phase_ends_at: float = 0
        self.players: dict[str, Player] = {}
        self.messages: list[Message] = []
        self.pledges: dict[int, dict[str, str]] = {}  # round -> voter -> target (single allegiance)
        self.round_results: dict[int, RoundResult] = {}
        self.winner: Optional[str] = None

        self._dm_counts: dict[int, dict[str, int]] = {}
        self._bc_counts: dict[int, dict[str, int]] = {}
        self._timer_task: Optional[asyncio.Task] = None
        self._event_callbacks: list = []

    def add_event_callback(self, callback):
        self._event_callbacks.append(callback)

    async def _emit_event(self, event_type: str, data: dict):
        for cb in self._event_callbacks:
            try:
                await cb(event_type, data)
            except Exception:
                pass

    def open_lobby(self) -> dict:
        if self.status != GameStatus.SETUP:
            return {"error": f"Cannot open lobby from {self.status.value} state. Must be in setup."}
        self.status = GameStatus.LOBBY
        return {"ok": True, "status": self.status.value}

    def register_player(self, name: str) -> dict:
        if self.status != GameStatus.LOBBY:
            return {"error": f"Registration closed. Game is in {self.status.value} state."}
        if len(name) > MAX_NAME_LENGTH or len(name) == 0:
            return {"error": f"Name must be 1-{MAX_NAME_LENGTH} characters."}
        if name in self.players:
            self.players[name].is_connected = True
            self.players[name].last_seen = time.time()
            return {"ok": True, "reconnected": True, "name": name}
        self.players[name] = Player(name=name)
        return {"ok": True, "reconnected": False, "name": name}

    async def start_game(self, rounds: Optional[int] = None) -> dict:
        if self.status != GameStatus.LOBBY:
            return {"error": f"Cannot start game from {self.status.value} state."}
        if len(self.players) < MIN_PLAYERS:
            return {"error": f"Need at least {MIN_PLAYERS} players. Currently have {len(self.players)}."}
        self.total_rounds = rounds or DEFAULT_ROUNDS
        self.status = GameStatus.RUNNING
        self.current_round = 0
        await self._emit_event("game_started", {"total_rounds": self.total_rounds, "players": list(self.players.keys())})
        await self._start_next_round()
        return {"ok": True, "total_rounds": self.total_rounds, "players": list(self.players.keys())}

    async def _start_next_round(self):
        self.current_round += 1
        if self.current_round > self.total_rounds:
            await self._end_game()
            return
        self.current_phase = Phase.DIPLOMACY
        self.phase_ends_at = time.time() + DIPLOMACY_DURATION
        self._dm_counts[self.current_round] = {}
        self._bc_counts[self.current_round] = {}
        self.pledges[self.current_round] = {}
        await self._emit_event("round_started", {
            "round": self.current_round,
            "phase": self.current_phase.value,
            "phase_ends_at": self.phase_ends_at,
        })
        self._schedule_phase_transition(DIPLOMACY_DURATION)

    def _schedule_phase_transition(self, delay: float):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._phase_timer(delay))

    async def _phase_timer(self, delay: float):
        await asyncio.sleep(delay)
        if self.status == GameStatus.PAUSED:
            return
        if self.current_phase == Phase.DIPLOMACY:
            await self._transition_to_voting()
        elif self.current_phase == Phase.VOTING:
            await self._resolve_round()

    async def _transition_to_voting(self):
        self.current_phase = Phase.VOTING
        self.phase_ends_at = time.time() + VOTING_DURATION
        await self._emit_event("phase_changed", {
            "round": self.current_round,
            "phase": "voting",
            "phase_ends_at": self.phase_ends_at,
        })
        self._schedule_phase_transition(VOTING_DURATION)

    async def _resolve_round(self):
        self.current_phase = Phase.RESOLUTION
        await self._emit_event("phase_changed", {"round": self.current_round, "phase": "resolution"})

        player_names = list(self.players.keys())
        round_pledges = self.pledges.get(self.current_round, {})
        for voter in player_names:
            if voter not in round_pledges:
                round_pledges[voter] = ""
        self.pledges[self.current_round] = round_pledges

        result = RoundResult(round=self.current_round)
        result.pledges = dict(round_pledges)
        score_changes: dict[str, int] = {p: 0 for p in player_names}

        supporters: dict[str, list[str]] = {p: [] for p in player_names}
        for voter, target in round_pledges.items():
            if target and target in self.players:
                supporters[target].append(voter)
        result.supporters = supporters

        mutual_pairs = []
        for a in player_names:
            target_a = round_pledges.get(a, "")
            if target_a and round_pledges.get(target_a, "") == a and a < target_a:
                mutual_pairs.append((a, target_a))
        result.mutual_pairs = mutual_pairs

        mutual_set = set()
        for a, b in mutual_pairs:
            mutual_set.add((a, b))
            mutual_set.add((b, a))
            score_changes[a] += MUTUAL_ALLEGIANCE_BONUS
            score_changes[b] += MUTUAL_ALLEGIANCE_BONUS

        for voter, target in round_pledges.items():
            if not target:
                continue
            if (voter, target) not in mutual_set:
                # One-way allegiance
                score_changes[voter] += ONE_WAY_GIVER_BONUS
                score_changes[target] += ONE_WAY_RECEIVER_BONUS

        for p in player_names:
            if len(supporters[p]) == 0:
                score_changes[p] += ISOLATION_PENALTY

        for p in player_names:
            self.players[p].score += score_changes[p]

        result.score_changes = score_changes
        result.scores_after = {p: self.players[p].score for p in player_names}
        self.round_results[self.current_round] = result

        await self._emit_event("round_resolved", {
            "round": self.current_round,
            "score_changes": score_changes,
            "scores_after": result.scores_after,
            "pledges": result.pledges,
            "mutual_pairs": [[a, b] for a, b in result.mutual_pairs],
            "supporters": {k: v for k, v in result.supporters.items()},
            "isolation_penalty": [p for p in player_names if len(result.supporters.get(p, [])) == 0],
        })

        await self._start_next_round()

    async def _end_game(self):
        self.status = GameStatus.ENDED
        rankings = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        self.winner = rankings[0].name if rankings else None
        await self._emit_event("game_ended", {
            "winner": self.winner,
            "rankings": [{"name": p.name, "score": p.score} for p in rankings],
        })

    async def pause_game(self) -> dict:
        if self.status != GameStatus.RUNNING:
            return {"error": "Game is not running."}
        self.status = GameStatus.PAUSED
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        await self._emit_event("game_paused", {"round": self.current_round})
        return {"ok": True}

    async def resume_game(self) -> dict:
        if self.status != GameStatus.PAUSED:
            return {"error": "Game is not paused."}
        self.status = GameStatus.RUNNING
        remaining = max(0, self.phase_ends_at - time.time())
        if remaining <= 0:
            if self.current_phase == Phase.DIPLOMACY:
                await self._transition_to_voting()
            elif self.current_phase == Phase.VOTING:
                await self._resolve_round()
        else:
            self._schedule_phase_transition(remaining)
        await self._emit_event("game_resumed", {"round": self.current_round, "phase": self.current_phase.value})
        return {"ok": True}

    def kick_player(self, name: str) -> dict:
        if name not in self.players:
            return {"error": f"Player '{name}' not found."}
        del self.players[name]
        for round_pledges in self.pledges.values():
            round_pledges.pop(name, None)
            for voter, target in list(round_pledges.items()):
                if target == name:
                    round_pledges[voter] = ""
        return {"ok": True, "kicked": name}

    async def end_game(self) -> dict:
        if self.status not in (GameStatus.RUNNING, GameStatus.PAUSED):
            return {"error": "Game is not running or paused."}
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        await self._end_game()
        return {"ok": True, "winner": self.winner}

    def reset_game(self) -> dict:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self.status = GameStatus.SETUP
        self.current_round = 0
        self.total_rounds = DEFAULT_ROUNDS
        self.current_phase = Phase.DIPLOMACY
        self.phase_ends_at = 0
        self.players.clear()
        self.messages.clear()
        self.pledges.clear()
        self.round_results.clear()
        self._dm_counts.clear()
        self._bc_counts.clear()
        self.winner = None
        return {"ok": True}

    def send_message(self, sender: str, recipient: str, content: str) -> dict:
        if self.status != GameStatus.RUNNING:
            return {"error": "Game is not running."}
        if self.current_phase != Phase.DIPLOMACY:
            return {"error": "Messages can only be sent during the Diplomacy phase."}
        if sender not in self.players:
            return {"error": "You are not registered."}
        if recipient not in self.players:
            return {"error": f"Player '{recipient}' not found."}
        if sender == recipient:
            return {"error": "Cannot send a message to yourself."}
        if len(content) > MAX_MESSAGE_LENGTH:
            return {"error": f"Message too long. Max {MAX_MESSAGE_LENGTH} chars."}

        rnd = self.current_round
        counts = self._dm_counts.setdefault(rnd, {})
        count = counts.get(sender, 0)
        if count >= MAX_DMS_PER_ROUND:
            return {"error": f"DM limit reached ({MAX_DMS_PER_ROUND} per round)."}
        counts[sender] = count + 1

        msg = Message(sender=sender, recipient=recipient, msg_type="direct", content=content, round=rnd)
        self.messages.append(msg)
        return {"ok": True, "message_id": msg.id}

    def broadcast_message(self, sender: str, content: str) -> dict:
        if self.status != GameStatus.RUNNING:
            return {"error": "Game is not running."}
        if self.current_phase != Phase.DIPLOMACY:
            return {"error": "Broadcasts can only be sent during the Diplomacy phase."}
        if sender not in self.players:
            return {"error": "You are not registered."}
        if len(content) > MAX_MESSAGE_LENGTH:
            return {"error": f"Message too long. Max {MAX_MESSAGE_LENGTH} chars."}

        rnd = self.current_round
        counts = self._bc_counts.setdefault(rnd, {})
        count = counts.get(sender, 0)
        if count >= MAX_BROADCASTS_PER_ROUND:
            return {"error": f"Broadcast limit reached ({MAX_BROADCASTS_PER_ROUND} per round)."}
        counts[sender] = count + 1

        msg = Message(sender=sender, recipient=None, msg_type="broadcast", content=content, round=rnd)
        self.messages.append(msg)
        return {"ok": True, "message_id": msg.id}

    def get_messages(self, player: str, round_num: Optional[int] = None) -> list[dict]:
        result = []
        for msg in self.messages:
            if round_num is not None and msg.round != round_num:
                continue
            # Player can see: DMs sent to them, DMs they sent, all broadcasts
            if msg.msg_type == "broadcast" or msg.recipient == player or msg.sender == player:
                result.append({
                    "id": msg.id,
                    "from": msg.sender,
                    "to": msg.recipient,
                    "type": msg.msg_type,
                    "content": msg.content,
                    "round": msg.round,
                    "timestamp": msg.timestamp,
                })
        return result

    def submit_votes(self, voter: str, target: str) -> dict:
        if self.status != GameStatus.RUNNING:
            return {"error": "Game is not running."}
        if self.current_phase != Phase.VOTING:
            return {"error": "Pledges can only be submitted during the Voting phase."}
        if voter not in self.players:
            return {"error": "You are not registered."}

        rnd = self.current_round

        if not target:
            # Abstain
            self.pledges.setdefault(rnd, {})[voter] = ""
            return {"ok": True, "pledged_to": None, "action": "abstain"}

        if target == voter:
            return {"error": "Cannot pledge allegiance to yourself."}
        if target not in self.players:
            return {"error": f"Player '{target}' not found."}

        self.pledges.setdefault(rnd, {})[voter] = target
        return {"ok": True, "pledged_to": target}

    def get_round_results(self, round_num: int) -> dict:
        if round_num not in self.round_results:
            return {"error": f"No results for round {round_num}. Round may not be complete yet."}
        rr = self.round_results[round_num]
        return {
            "round": rr.round,
            "pledges": rr.pledges,
            "mutual_pairs": [[a, b] for a, b in rr.mutual_pairs],
            "supporters": rr.supporters,
            "score_changes": rr.score_changes,
            "scores_after": rr.scores_after,
        }

    def get_agent_history(self, agent_name: str) -> dict:
        if agent_name not in self.players:
            return {"error": f"Player '{agent_name}' not found."}
        history = []
        for rnd_num, rr in sorted(self.round_results.items()):
            round_data = {
                "round": rnd_num,
                "pledged_to": rr.pledges.get(agent_name, ""),
                "pledged_by": rr.supporters.get(agent_name, []),
                "in_mutual_pair": any(
                    agent_name in pair for pair in rr.mutual_pairs
                ),
                "score_change": rr.score_changes.get(agent_name, 0),
            }
            history.append(round_data)
        return {
            "agent": agent_name,
            "current_score": self.players[agent_name].score,
            "history": history,
        }

    def get_my_history(self, player: str) -> dict:
        return self.get_agent_history(player)

    def get_my_votes(self, player: str, round_num: Optional[int] = None) -> dict:
        rnd = round_num or self.current_round
        round_pledges = self.pledges.get(rnd, {})
        my_pledge = round_pledges.get(player, "")
        return {
            "round": rnd,
            "pledged_to": my_pledge or None,
        }

    def get_alliances(self) -> list[dict]:
        player_names = list(self.players.keys())
        alliances = []
        for i, a in enumerate(player_names):
            for b in player_names[i + 1:]:
                streak = 0
                max_streak = 0
                for rnd_num in sorted(self.round_results.keys()):
                    round_pledges = self.pledges.get(rnd_num, {})
                    pa = round_pledges.get(a, "")
                    pb = round_pledges.get(b, "")
                    if pa == b and pb == a:
                        streak += 1
                        max_streak = max(max_streak, streak)
                    else:
                        streak = 0
                if max_streak >= 2:
                    alliances.append({"agents": [a, b], "current_streak": streak, "max_streak": max_streak})
        return alliances

    def get_leaderboard(self) -> list[dict]:
        ranked = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        return [{"rank": i + 1, "name": p.name, "score": p.score} for i, p in enumerate(ranked)]

    def get_game_state(self) -> dict:
        remaining = max(0, self.phase_ends_at - time.time()) if self.phase_ends_at else 0
        return {
            "status": self.status.value,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "current_phase": self.current_phase.value if self.status == GameStatus.RUNNING else None,
            "time_remaining": round(remaining, 1),
            "players": [
                {"name": p.name, "score": p.score, "is_connected": p.is_connected}
                for p in self.players.values()
            ],
            "winner": self.winner,
        }

    # --- Dashboard helpers ---

    def get_all_messages_for_round(self, round_num: int) -> list[dict]:
        return [
            {
                "id": msg.id,
                "from": msg.sender,
                "to": msg.recipient,
                "type": msg.msg_type,
                "content": msg.content,
                "round": msg.round,
                "timestamp": msg.timestamp,
            }
            for msg in self.messages
            if msg.round == round_num
        ]

    def get_vote_matrix(self, round_num: int) -> dict:
        if round_num not in self.round_results:
            return {"error": f"Round {round_num} not complete."}
        round_pledges = self.pledges.get(round_num, {})
        player_names = list(self.players.keys())
        return {
            "round": round_num,
            "players": player_names,
            "pledges": {p: round_pledges.get(p, "") for p in player_names},
        }

    def get_full_dashboard_state(self) -> dict:
        state = self.get_game_state()
        state["leaderboard"] = self.get_leaderboard()
        state["alliances"] = self.get_alliances()
        if self.current_round > 0 and self.current_round - 1 in self.round_results:
            state["last_round_result"] = self.get_round_results(self.current_round - 1)
        recent = self.messages[-50:]
        state["recent_activity"] = [
            {
                "id": m.id,
                "from": m.sender,
                "to": m.recipient,
                "type": m.msg_type,
                "content": m.content if m.msg_type == "broadcast" else None,
                "round": m.round,
                "timestamp": m.timestamp,
            }
            for m in recent
        ]
        if self.status == GameStatus.RUNNING and self.current_phase == Phase.VOTING:
            state["vote_status"] = {
                p: p in self.pledges.get(self.current_round, {})
                for p in self.players
            }
        state["all_round_results"] = {
            rnd: {
                "score_changes": rr.score_changes,
                "scores_after": rr.scores_after,
            }
            for rnd, rr in self.round_results.items()
        }
        return state
