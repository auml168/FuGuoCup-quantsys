import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fullgoal_quant.common import GuardError, competition_schedule, load_config, write_json


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.c = self.cfg['competition']

    def test_user_confirmed_schedule(self):
        self.assertEqual(self.c['start_date'], '2026-10-19')
        self.assertEqual(self.c['midpoint_date'], '2027-01-15')
        self.assertEqual(self.c['end_date'], '2027-04-30')
        self.assertFalse(self.c['verified'])  # Dates alone do not confirm trading rules.

    def test_inclusive_phase_boundaries(self):
        for day, phase in [('2026-10-18','赛前'), ('2026-10-19','半程赛'),
                           ('2027-01-15','半程赛'), ('2027-01-16','后半程'),
                           ('2027-04-30','后半程'), ('2027-05-01','已结束')]:
            with self.subTest(day=day):
                info = competition_schedule(self.c, date.fromisoformat(day))
                self.assertEqual(info['phase'], phase)
        self.assertEqual(competition_schedule(self.c,date(2027,1,15))['days_to_midpoint'],0)
        self.assertEqual(competition_schedule(self.c,date(2027,4,30))['days_to_end'],0)

    def test_invalid_sequence_or_date_rejected(self):
        for change in [{'midpoint_date':'2027-05-01'}, {'end_date':'2026-10-18'},
                       {'midpoint_date':'2027-02-30'}]:
            cfg = copy.deepcopy(self.cfg)
            cfg['competition'].update(change)
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                path=Path(d)/'settings.json'
                write_json(path,cfg)
                with self.assertRaises(GuardError):
                    load_config(path)


if __name__ == '__main__':
    unittest.main()
