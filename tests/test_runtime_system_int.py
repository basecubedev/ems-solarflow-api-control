import unittest

from ems.controller import EMSController


class RuntimeStateStub:
    def __init__(self, system=None):
        self.system = system or {}

    def get_system(self, key, default=None):
        return self.system.get(key, default)


class RuntimeSystemIntTest(unittest.TestCase):
    def controller(self, runtime_state):
        return EMSController(
            devices=[],
            shelly=None,
            sleep_enabled=False,
            runtime_state=runtime_state
        )

    def test_returns_default_without_runtime_state(self):
        controller = self.controller(None)

        self.assertEqual(
            controller.runtime_system_int("max_total_power", 1200),
            1200
        )

    def test_returns_default_for_missing_key(self):
        controller = self.controller(RuntimeStateStub())

        self.assertEqual(
            controller.runtime_system_int("max_total_power", 1200),
            1200
        )

    def test_returns_valid_integer(self):
        controller = self.controller(
            RuntimeStateStub({"max_total_power": 800})
        )

        self.assertEqual(
            controller.runtime_system_int("max_total_power", 1200),
            800
        )

    def test_accepts_numeric_string(self):
        controller = self.controller(
            RuntimeStateStub({"max_total_power": "900"})
        )

        self.assertEqual(
            controller.runtime_system_int("max_total_power", 1200),
            900
        )

    def test_invalid_value_falls_back_to_default(self):
        controller = self.controller(
            RuntimeStateStub({"max_total_power": "abc"})
        )

        self.assertEqual(
            controller.runtime_system_int("max_total_power", 1200),
            1200
        )

    def test_minimum_is_enforced(self):
        controller = self.controller(
            RuntimeStateStub({"max_total_power": -100})
        )

        self.assertEqual(
            controller.runtime_system_int(
                "max_total_power",
                1200,
                minimum=0
            ),
            0
        )


if __name__ == "__main__":
    unittest.main()
