"""Console settings: bounds, overrides on top of the config file, all-or-nothing updates."""
import unittest

from solar01 import config, settings


class ValidateTest(unittest.TestCase):
    def test_bounds_and_types(self):
        self.assertEqual(settings.validate('core.jkbms.poll_s', 1, 5.0), 1.0)
        self.assertIsInstance(settings.validate('core.jkbms.poll_s', 2, 5.0), float)
        for bad in (0.5, 11, float('nan'), '2', True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.validate('core.jkbms.poll_s', bad, 5.0)
        with self.assertRaises(ValueError):
            settings.validate('core.inverter.poll_s', 2, 5.0)        # the FlexBoss21 needs 5 s or more
        with self.assertRaises(ValueError):
            settings.validate('hub.planner.qc_arm_min', 30.5, 30)
        self.assertIs(settings.validate('hub.planner.dry_run', True, False), True)
        with self.assertRaises(ValueError):
            settings.validate('core.emulator.chg_max_a', 50, 100.0)  # battery limits stay in the file
        with self.assertRaises(ValueError):
            settings.validate('core.inverter.ct_cal_min_write_s', 10, 60.0)   # EEPROM wear

    def test_windows_and_holidays(self):
        self.assertEqual(settings.validate('hub.planner.weekday_sop', ' 10-14, 0-6 ', ''), '0-6,10-14')
        for bad in ('0-25', '6-2', '0-6,5-8', 'x', ''):
            with self.assertRaises(ValueError, msg=bad):
                settings.validate('hub.planner.weekday_sop', bad, '')
        self.assertEqual(settings.validate('hub.planner.on_peak', '', ''), '')
        self.assertEqual(settings.validate('hub.planner.extra_holidays', ['2026-12-24', '2026-12-24', '2026-11-27'], []),
                         ['2026-11-27', '2026-12-24'])
        with self.assertRaises(ValueError):
            settings.validate('hub.planner.extra_holidays', ['24/12/2026'], [])


class OverridesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.from_dict({'hub': {'planner': {'reserve_soc': 25.0}}})
        self.o = settings.Overrides(self.cfg, 'hub')

    def test_override_and_clear(self):
        changes = self.o.update({'hub.planner.reserve_soc': 30, 'hub.planner.weekday_sop': '0-6'})
        self.assertEqual(changes['hub.planner.reserve_soc'], (25.0, 30.0))
        self.assertEqual((self.cfg.hub.planner.reserve_soc, self.cfg.hub.planner.weekday_sop), (30.0, '0-6'))
        st = self.o.state()
        self.assertEqual(st['file']['hub.planner.reserve_soc'], 25.0)
        self.assertEqual(st['overrides'], ['hub.planner.reserve_soc', 'hub.planner.weekday_sop'])
        self.o.update({'hub.planner.reserve_soc': None})
        self.assertEqual(self.cfg.hub.planner.reserve_soc, 25.0, 'clearing brings the file value back')
        self.assertEqual(self.o.values, {'hub.planner.weekday_sop': '0-6'})

    def test_all_or_nothing(self):
        with self.assertRaises(ValueError):
            self.o.update({'hub.planner.reserve_soc': 30, 'hub.planner.floor_soc': 99})
        with self.assertRaises(ValueError) as cm:
            self.o.update({'hub.planner.reserve_soc': 10})             # below the 15 % floor
        self.assertIn('floor', str(cm.exception))
        with self.assertRaises(ValueError):
            self.o.update({'hub.planner.qc_rearm_below': 30})          # not below the 30 min countdown
        with self.assertRaises(ValueError):
            self.o.update({'core.jkbms.poll_s': 1})                    # the core's, not the hub's
        self.assertEqual((self.cfg.hub.planner.reserve_soc, self.cfg.hub.planner.floor_soc), (25.0, 15.0))
        self.assertEqual(self.o.values, {})
        self.assertEqual(self.o.update({'hub.planner.reserve_soc': 30}, dry_run=True),
                         {'hub.planner.reserve_soc': (25.0, 30.0)})
        self.assertEqual(self.cfg.hub.planner.reserve_soc, 25.0, 'a dry run changes nothing')

    def test_undervoltage_cutoff_and_restore(self):
        core = settings.Overrides(config.Config(), 'core')
        core.update({'core.emulator.cell_uv_mv': 2750, 'core.emulator.cell_uv_restore_mv': 3050})
        self.assertEqual((core.cfg.core.emulator.cell_uv_mv, core.cfg.core.emulator.cell_uv_restore_mv), (2750, 3050))
        with self.assertRaises(ValueError):
            core.update({'core.emulator.cell_uv_restore_mv': 2750})     # must be above the cutoff
        with self.assertRaises(ValueError):
            core.update({'core.emulator.cell_uv_mv': 2400})             # below the allowed range

    def test_load_skips_what_no_longer_validates(self):
        skipped = self.o.load({'hub.planner.reserve_soc': 35, 'hub.planner.hyst_soc': 99, 'hub.gone': 1})
        self.assertEqual(self.cfg.hub.planner.reserve_soc, 35.0)
        self.assertEqual(len(skipped), 2)
        o = settings.Overrides(config.Config(), 'hub')
        self.assertTrue(o.load({'hub.planner.floor_soc': 40}), 'floor above the reserve: nothing applied')
        self.assertEqual(o.cfg.hub.planner.floor_soc, 15.0)

    def test_items_for_the_console(self):
        items = settings.items({'hub': self.o.state(), 'core': None})
        by_key = {i['key']: i for i in items}
        self.assertEqual(len(items), len(settings.SETTINGS))
        self.assertEqual(by_key['hub.planner.reserve_soc']['value'], 25.0)
        self.assertEqual(by_key['hub.planner.extra_holidays']['type'], 'list')
        self.assertEqual(by_key['core.jkbms.poll_s']['type'], 'float')
        self.assertFalse(by_key['core.jkbms.poll_s']['available'], 'core settings unknown until the core reports them')

    def test_every_setting_exists_with_a_default_inside_its_bounds(self):
        defaults = config.Config()
        for s in settings.SETTINGS:
            value = settings.get(defaults, s.key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.assertTrue(s.min <= value <= s.max, s.key)
            settings.validate(s.key, value, value)


if __name__ == '__main__':
    unittest.main()
