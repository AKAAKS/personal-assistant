from nutrition_core.adapters.base import macros_from_fields
from nutrition_core.models import Macros


class TestMacrosFromFields:
    def test_builds_macros_when_energy_present(self):
        assert macros_from_fields(165, 31, None, 3.6) == Macros(165, 31, 0.0, 3.6)

    def test_returns_none_when_energy_missing(self):
        assert macros_from_fields(None, 31, 0, 3.6) is None
