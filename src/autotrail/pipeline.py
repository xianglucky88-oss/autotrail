"""薄编排：loader → normalizer → vin_id → fetch → identity → statusline → dedup → keyer → store。

只做顺序编排，不含业务判断。vin_id 产出的身份键由 keyer 读取；store 按
vin_id 声明的列表字段合并多来源/里程观测，老格式记录不受影响。
"""
import importlib
from . import store as _store
from . import loader as _loader
from . import vin_id as _vin_id
from .keyer import key_of
from .models import Record

CHAIN_NAMES = ['normalizer', 'vin_id', 'fetch', 'identity', 'statusline', 'dedup']

def run(con=None):
    own = con is None
    con = con or _store.connect()
    try:
        chain = []
        for n in CHAIN_NAMES:
            mod = importlib.import_module("." + n, __package__)
            chain.append(mod.process)
        fed = 0
        for raw in _loader.load():
            rec = raw
            for fn in chain:
                rec = fn(rec)
            _store.upsert(con, Record(key=key_of(rec), data=rec),
                          merge_list_fields=_vin_id.MERGE_LIST_FIELDS)
            fed += 1
        _store.commit(con)
        return {"fed": fed, "rows": _store.count(con)}
    finally:
        if own:
            con.close()
