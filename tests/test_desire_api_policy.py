from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import patch

import desire_engine as de
import desire_gateway as dg


UTC8 = timezone(timedelta(hours=8))


class DesireApiPolicyTests(unittest.TestCase):
    def test_passive_mode_does_not_lift_drift_cap(self):
        state = de.DesireState()
        state.floors["attachment"] = de.DRIVE_CONFIG["attachment"]["drift_cap"]
        for _ in range(10):
            de.tick_drives(state, separation_secs=9999, passive_mode=True)
        self.assertEqual(
            state.floors["attachment"],
            de.DRIVE_CONFIG["attachment"]["drift_cap"],
        )

    def test_conversation_can_pulse_multiple_drives(self):
        state = de.DesireState()
        before = dict(state.drives)
        with patch.object(de, "save_state"):
            dg.classify_and_pulse(state, "我想你，也好奇你为什么这么想")
        self.assertGreater(state.drives["attachment"], before["attachment"])
        self.assertGreater(state.drives["curiosity"], before["curiosity"])

    def test_libido_quiet_window_boundaries(self):
        self.assertFalse(dg.libido_silent_blocked(
            datetime(2026, 11, 9, 0, 29, tzinfo=UTC8)))
        self.assertTrue(dg.libido_silent_blocked(
            datetime(2026, 11, 9, 0, 30, tzinfo=UTC8)))
        self.assertTrue(dg.libido_silent_blocked(
            datetime(2026, 11, 9, 8, 29, tzinfo=UTC8)))
        self.assertFalse(dg.libido_silent_blocked(
            datetime(2026, 11, 9, 8, 30, tzinfo=UTC8)))

    def test_frozen_libido_drift_does_not_catch_up(self):
        state = de.DesireState()
        before = state.floors["libido"]
        de.tick_drives(
            state, separation_secs=8 * 3600,
            frozen_drifts={"libido"},
        )
        self.assertEqual(state.floors["libido"], before)

    def test_unsent_multilevel_desire_resets_on_third_partial(self):
        state = de.DesireState()
        state.drives["attachment"] = 0.50
        de.partial_satisfy(state, "attachment")
        self.assertEqual(state.silent_inject_count["attachment"], 1)
        de.partial_satisfy(state, "attachment")
        self.assertEqual(state.silent_inject_count["attachment"], 2)
        result = de.partial_satisfy(state, "attachment")
        self.assertTrue(result.get("reset"))
        self.assertEqual(state.silent_inject_count["attachment"], 0)


if __name__ == "__main__":
    unittest.main()
