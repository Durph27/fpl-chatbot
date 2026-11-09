"""Offline regressions for historical scope, double GWs and manager units."""
import unittest

from app.history import historical_gameweeks
from app.ingest_manager import calculate_free_transfers
from app.tools import _normalize_money


class HistoryRegressions(unittest.TestCase):
    def test_season_minimum(self):
        result = historical_gameweeks('2024–25', limit=1)['results']
        self.assertEqual([(r['player'], r['gameweek'], r['total_points']) for r in result],
                         [('Harry Clarke', 9, -5)])

    def test_double_gameweek(self):
        result = historical_gameweeks('2021-22', 'Mohamed Salah', 26)['results'][0]
        self.assertEqual(result['total_points'], 28)
        self.assertEqual(len(result['matches']), 2)

    def test_missing_scope(self):
        self.assertFalse(historical_gameweeks('2099-00', 'Salah', 1)['found'])
        self.assertFalse(historical_gameweeks('2023-24', 'Salah', 99)['found'])

    def test_money_units(self):
        self.assertEqual(_normalize_money(100.5), 100.5)

    def test_wildcard_preserves_transfers(self):
        history = [{'event': gw, 'event_transfers': 0} for gw in range(1, 5)]
        self.assertEqual(calculate_free_transfers(history, [{'event': 4, 'name': 'wildcard'}]), 4)


if __name__ == '__main__':
    unittest.main()
