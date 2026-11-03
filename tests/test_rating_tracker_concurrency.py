import unittest

from proavalon.rating_tracker import ProAvalonRatingTracker


class AcknowledgedTracker(ProAvalonRatingTracker):
    def __init__(self):
        super().__init__(scraper=None, bot=None)
        self.events = []

    async def _emit(self, event, *args):
        self.events.append((event, *args))
        if event != "join-room":
            return
        room_id = int(args[0])
        final_waiter = self._snapshot_waiters.get(room_id)
        if final_waiter is not None and not final_waiter.done():
            final_waiter.set_result({"roomId": room_id, "phase": "finished"})
        roster_waiter = self._roster_waiters.get(room_id)
        if roster_waiter is not None and not roster_waiter.done():
            roster_waiter.set_result([f"s5player{i}" for i in range(10)])


class ConcurrentLobbyInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_finished_rooms_are_acknowledged_independently(self):
        tracker = AcknowledgedTracker()
        self.assertTrue(await tracker._inspect_finished_room(1585))
        self.assertTrue(await tracker._inspect_finished_room(1587))
        joins = [event for event in tracker.events if event[0] == "join-room"]
        self.assertEqual(joins, [("join-room", 1585), ("join-room", 1587)])

    async def test_two_started_rooms_confirm_their_own_rosters(self):
        tracker = AcknowledgedTracker()
        self.assertTrue(await tracker._inspect_started_room(1585))
        self.assertTrue(await tracker._inspect_started_room(1587))
        self.assertNotIn(1585, tracker._roster_waiters)
        self.assertNotIn(1587, tracker._roster_waiters)


if __name__ == "__main__":
    unittest.main()
