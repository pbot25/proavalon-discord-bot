import unittest

from commands.season import select_next_waiting_lobby


class NextLobbyPingTests(unittest.TestCase):
    def test_chooses_most_populated_waiting_lobby(self) -> None:
        room_id, players = select_next_waiting_lobby([
            {
                "room_id": 10,
                "status": "Game in progress",
                "players": ["S5Playing"],
            },
            {
                "room_id": 11,
                "status": "Waiting for players",
                "players": ["S5One", "S5Two"],
            },
            {
                "room_id": 12,
                "status": "Waiting for players",
                "players": ["S5Three"],
            },
        ])
        self.assertEqual(room_id, 11)
        self.assertEqual(players, {"s5one", "s5two"})

    def test_no_known_waiting_lobby_returns_empty(self) -> None:
        room_id, players = select_next_waiting_lobby([
            {
                "room_id": 10,
                "status": "Game in progress",
                "players": ["S5Playing"],
            },
            {
                "room_id": 11,
                "status": "Waiting for players",
                "players": [],
            },
        ])
        self.assertIsNone(room_id)
        self.assertEqual(players, set())

    def test_prefers_lobby_with_more_registered_players(self) -> None:
        room_id, players = select_next_waiting_lobby(
            [
                {
                    "room_id": 20,
                    "status": "Waiting for players",
                    "players": ["Guest1", "Guest2", "RegisteredA"],
                },
                {
                    "room_id": 21,
                    "status": "Waiting for players",
                    "players": ["RegisteredA", "RegisteredB"],
                },
            ],
            {"registereda", "registeredb"},
        )
        self.assertEqual(room_id, 21)
        self.assertEqual(players, {"registereda", "registeredb"})


if __name__ == "__main__":
    unittest.main()
