import pytest

def test_vin_id_is_open_gap():
    from src.autotrail import vin_id
    with pytest.raises(NotImplementedError):
        vin_id.process({"id": "x"})

def test_snapshot_is_open_gap():
    from src.autotrail import snapshot
    with pytest.raises(NotImplementedError):
        snapshot.process({"id": "x"})
