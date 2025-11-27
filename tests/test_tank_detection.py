import json
import unittest
from pathlib import Path

from enhanced_discord_bot import detect_tank_kill, _derive_keywords_from_tanks_file


class TankDetectionTests(unittest.TestCase):
    def setUp(self):
        self.keywords = {
            "cannons": ["75mm", "88mm"],
            "launchers": ["panzerschreck"]
        }

    def test_weapon_keyword_triggers_detection(self):
        payload = {
            "weapon": "75mm AP Shell",
            "killer_name": "Able Gunner",
            "killer_team": "Allies",
            "victim_name": "Axis Tank",
            "victim_team": "Axis",
            "target_vehicle": "Panther"
        }

        detection = detect_tank_kill(payload, self.keywords)
        self.assertIsNotNone(detection)
        self.assertEqual(detection.keyword_group, "cannons")
        self.assertEqual(detection.killer_name, "Able Gunner")
        self.assertEqual(detection.vehicle.lower(), "panther")

    def test_vehicle_class_falls_back_to_detection(self):
        payload = {
            "data": {
                "victim_vehicle_class": "Heavy_Tank",
                "weapon": "Satchel Charge",
                "attacker_name": "Axis Saboteur",
                "attacker_team": "Axis",
                "target_name": "Sherman Crew",
                "target_team": "Allies"
            }
        }

        detection = detect_tank_kill(payload, self.keywords)
        self.assertIsNotNone(detection)
        self.assertEqual(detection.keyword_group, "vehicle_class")
        self.assertEqual(detection.keyword_match, "Heavy_Tank")
        self.assertEqual(detection.victim_team, "Allies")

    def test_non_tank_weapon_is_ignored(self):
        payload = {
            "weapon": "M1 Garand",
            "killer_team": "Allies",
            "victim_team": "Axis"
        }

        detection = detect_tank_kill(payload, self.keywords)
        self.assertIsNone(detection)

    def test_non_dict_payload_is_ignored(self):
        self.assertIsNone(detect_tank_kill("raw string", self.keywords))
        self.assertIsNone(detect_tank_kill(None, self.keywords))

    def test_partial_fields_still_produce_detection(self):
        payload = {
            "weapon": "88mm cannon",
        }
        detection = detect_tank_kill(payload, self.keywords)
        self.assertIsNotNone(detection)
        self.assertEqual(detection.weapon, "88mm cannon")

    def test_vehicle_name_matches_without_weapon(self):
        payload = {
            "victim_vehicle": "Panther Ausf. G",
        }
        keywords = {
            "vehicles": ["panther ausf. g"],
        }
        detection = detect_tank_kill(payload, keywords)
        self.assertIsNotNone(detection)
        self.assertEqual(detection.keyword_group, "vehicles")

    def test_keywords_derived_from_tank_file(self):
        tmp_file = Path("tmp_tanks.json")
        sample = [
            {
                "vehicle": "Tiger I",
                "class": "Heavy",
                "side": "Axis",
                "country": "Germany",
                "gun": "88mm KwK 36 L/56"
            }
        ]
        tmp_file.write_text(json.dumps(sample), encoding="utf-8")
        try:
            derived = _derive_keywords_from_tanks_file(str(tmp_file))
            self.assertIn("88mm", derived.get("calibers", []))
            self.assertIn("tiger i", derived.get("vehicles", []))

            payload = {
                "weapon": "88mm shell",
                "victim_vehicle": "Tiger I",
            }
            detection = detect_tank_kill(payload, derived)
            self.assertIsNotNone(detection)
        finally:
            tmp_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
