from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionState(str, Enum):
    WAITING = "waiting"
    RECORDING_PREGAME = "recording_pregame"
    RECORDING_GAME = "recording_game"
    POSTGAME = "postgame"
    COMPLETED = "completed"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    ABANDONED = "abandoned"


@dataclass
class RecordingLifecycle:
    """Pure state machine for one selected ten-player ProAvalon room."""

    postgame_seconds: float = 30.0
    abandonment_grace_seconds: float = 120.0
    abandonment_player_threshold: int = 4
    state: SessionState = SessionState.WAITING
    game_started: bool = False
    postgame_deadline: float | None = None
    invalid_since: float | None = None

    def start(self) -> None:
        if self.state is not SessionState.WAITING:
            raise RuntimeError("Recording lifecycle has already started.")
        self.state = SessionState.RECORDING_PREGAME

    def update(
        self,
        *,
        now: float,
        room_present: bool,
        player_count: int | None,
        game_in_progress: bool,
        game_finished: bool,
    ) -> SessionState:
        if self.state in {
            SessionState.COMPLETED,
            SessionState.CANCELLED_BEFORE_START,
            SessionState.ABANDONED,
        }:
            return self.state

        if self.state is SessionState.POSTGAME:
            if self.postgame_deadline is not None and now >= self.postgame_deadline:
                self.state = SessionState.COMPLETED
            return self.state

        if game_finished:
            self.state = SessionState.POSTGAME
            self.postgame_deadline = now + self.postgame_seconds
            self.invalid_since = None
            return self.state

        if not self.game_started:
            if game_in_progress:
                self.game_started = True
                self.state = SessionState.RECORDING_GAME
                self.invalid_since = None
                return self.state

            # Before the game starts, ten players must remain continuously.
            if not room_present or player_count != 10:
                self.state = SessionState.CANCELLED_BEFORE_START
            return self.state

        # Once play has started, ordinary disconnects are tolerated. A room is
        # abandoned only when it disappears or a majority has left (four or
        # fewer remain) for the full grace period.
        invalid = (
            not room_present
            or player_count is None
            or player_count <= self.abandonment_player_threshold
        )
        if not invalid:
            self.invalid_since = None
            return self.state

        if self.invalid_since is None:
            self.invalid_since = now
        elif now - self.invalid_since >= self.abandonment_grace_seconds:
            self.state = SessionState.ABANDONED

        return self.state
