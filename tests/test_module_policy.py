"""Pure policy tests: no archive, credential or network access."""
import copy
import unittest

from dashboard.modules import ModuleRegistry, ModuleDisabledError, ModulePolicyError


class MemorySettings:
    def __init__(self):
        self.settings = {}

    def module_settings(self):
        return copy.deepcopy(self.settings)

    def update_module(self, key, enabled, version, *, expected_settings=None):
        if self.settings != expected_settings:
            raise AssertionError("Policy must pass the exact concurrency snapshot")
        self.settings[key] = {"enabled": enabled, "version": version + 1}
        return self.settings[key]

    def health(self):
        return {"schema_version": 1}


class ModulePolicyTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySettings()
        self.registry = ModuleRegistry(self.store)

    def test_all_optional_modules_default_enabled(self):
        payload = self.registry.snapshot()
        self.assertEqual({m['id'] for m in payload['modules']}, {'analysis','media','voice','sync','backup'})
        self.assertTrue(all(m['enabled'] and m['version'] == 0 for m in payload['modules']))

    def test_dependencies_are_explicit_and_disable_is_not_cascading(self):
        with self.assertRaises(ModulePolicyError):
            self.registry.update({'id':'media','enabled':False,'expected_version':0})
        self.registry.update({'id':'voice','enabled':False,'expected_version':0})
        self.registry.update({'id':'media','enabled':False,'expected_version':0})
        with self.assertRaises(ModuleDisabledError):
            self.registry.require('media')
        with self.assertRaises(ModulePolicyError):
            self.registry.update({'id':'voice','enabled':True,'expected_version':1})

    def test_unknown_fields_and_non_boolean_values_rejected(self):
        for payload in ({'id':'core','enabled':False,'expected_version':0},
                        {'id':'sync','enabled':0,'expected_version':0},
                        {'id':'sync','enabled':False,'expected_version':True},
                        {'id':'sync','enabled':False,'expected_version':0,'path':'elsewhere'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.registry.update(payload)

    def test_corrupted_stored_settings_fail_closed(self):
        self.store.settings = {'analysis': {'enabled':'false','version':1}}
        with self.assertRaises(RuntimeError):
            self.registry.require('analysis')

    def test_stored_dependency_violation_fails_closed(self):
        self.store.settings = {'media': {'enabled':False,'version':1}}
        with self.assertRaises(RuntimeError):
            self.registry.require('voice')


if __name__ == '__main__':
    unittest.main()
