from app.services.data_sources.longport_adapter import LongportAdapter
from app.services.data_sources.manager import DataSourceManager


def test_longport_symbol_convert_roundtrip():
    assert LongportAdapter._to_longport_symbol("600000") == "600000.SH"
    assert LongportAdapter._to_longport_symbol("000001") == "000001.SZ"
    assert LongportAdapter._to_longport_symbol("300750") == "300750.SZ"
    assert LongportAdapter._from_longport_symbol("600000.SH") == "600000"
    assert LongportAdapter._from_longport_symbol("000001.SZ") == "000001"


def test_longport_adapter_registered_in_manager():
    manager = DataSourceManager()
    names = [a.name for a in manager.adapters]
    assert "longport" in names

