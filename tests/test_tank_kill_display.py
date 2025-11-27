import datetime
import unittest

from enhanced_discord_bot import ClockState, build_tank_kill_field


class TankKillDisplayTests(unittest.TestCase):
    def test_format_tank_scoreline_uses_live_counts(self):
        clock = ClockState()
        clock.tank_kill_counts = {'allied': 3, 'axis': 1}

        summary = clock.format_tank_scoreline()

        self.assertIn('Allies: 3', summary)
        self.assertIn('Axis: 1', summary)

    def test_build_field_with_last_event(self):
        clock = ClockState()
        event_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        clock.tank_kill_events.append({
            'timestamp': event_time,
            'killer': 'Able Gunner',
            'victim': 'Tiger Crew',
            'keyword_match': '75mm',
            'vehicle': 'Tiger',
        })

        field = build_tank_kill_field(clock, include_last=True)

        self.assertIn('Last:', field)
        self.assertIn('Able Gunner', field)
        self.assertIn('Tiger Crew', field)

    def test_build_field_with_no_events(self):
        clock = ClockState()
        field = build_tank_kill_field(clock, include_last=True)
        self.assertIn('No tank kills yet', field)


if __name__ == '__main__':
    unittest.main()
