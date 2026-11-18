"""单一职责：挂牌记录的物理车辆身份归并（在 vin_id 校验结果之上）。

没有完整 VIN 的记录不会被丢弃，也不会仅凭“车型相同”并为一辆车。
本模块维护：

- ``VehicleIdentity``：稳定的物理车辆对象，含别名（车源挂牌号、车牌脱敏
  片段、VIN/尾号）、成员挂牌、逐因素证据与可解释置信度；
- 三档自动结论：``confirmed``（证据足够，自动确认）、``pending``（只能待审，
  挂起为候选关联）、``new``（另立身份）；
- 冻结保护：一旦身份被自动确认，后续新线索只能追加待审，不允许无声改写其
  归属（``confirmed_*`` 结构只增不改）；
- 确定性身份 ID：由成员中最小的挂牌键派生，同一批数据无论到达顺序如何，
  回放结果一致。

完整号码的解析/校验仍是 vin_id 的职责，本模块只消费记录上的 ``_vin``。
``process(record)`` 的调用方式与 keyer 对历史键（记录 ``id``）的读取保持不变。
"""
import hashlib

from . import vin_id

# ---- 置信度阈值与证据权重 ------------------------------------------------
AUTO_CONFIRM_SCORE = 70.0      # 达到即自动确认
PENDING_SCORE = 35.0           # 达到即作为候选关联待审，低于则另立身份

W_FULL_VIN = 100.0             # 完整 VIN 一致（且校验通过）
W_PLATE = 45.0                 # 车牌脱敏片段一致
W_PARTIAL_VIN = 8.0            # 不完整 VIN 的可辨识尾号一致
W_MAKE_MODEL = 12.0            # 品牌+车型一致（仅此项不足以并车）
W_YEAR = 8.0
W_TRIM = 7.0                   # 配置一致
W_MILEAGE = 6.0                # 里程接近
W_PRICE = 4.0                  # 价格接近
W_SOURCE = 2.0                 # 同车源（换店场景下此项缺失）

MILEAGE_TOLERANCE = 3000       # km
PRICE_TOLERANCE = 0.05         # 5%
MIN_PLATE_CHARS = 4            # 车牌脱敏片段至少 4 位才视为有效证据

CONFIRMED = "confirmed"
PENDING = "pending"
NEW = "new"


def _norm_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _plate_fragment(plate):
    """车牌脱敏片段：仅保留字母数字（去掉 *、#、空格等掩码），太短则不可用。"""
    frag = "".join(ch for ch in str(plate or "").upper() if ch.isalnum())
    return frag or None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _factor(name, weight, result, detail):
    return {"factor": name, "weight": round(weight, 2),
            "result": result, "detail": detail}


def evidence_profile(record):
    """从一条挂牌记录抽取归并所需证据（不做跨记录判断）。"""
    vin_info = record.get("_vin")
    if not isinstance(vin_info, dict):
        # 保持模块可独立使用：缺 _vin 时现场调用 vin_id，不重复其职责外逻辑
        vin_info = vin_id.classify_vin(record.get("vin"))
    mileage = _num(record.get("mileage"))
    price = _num(record.get("price"))
    return {
        "listing_key": str(record.get("id")) or "",
        "source": _norm_text(record.get("src")),
        "dealer": _norm_text(record.get("dealer")),
        "vin_status": vin_info.get("status"),
        "full_vin": vin_info.get("vin"),
        "vin_check": vin_info.get("check"),
        "vin_fragment": vin_info.get("fragment"),
        "plate": _plate_fragment(record.get("plate")),
        "make": _norm_text(record.get("make")),
        "model": _norm_text(record.get("model")),
        "year": record.get("year"),
        "trim": _norm_text(record.get("trim")),
        "mileage": mileage,
        "price": price,
    }


def score_against(candidate, profile):
    """把一条记录的证据与候选身份逐项比对，返回加权分数、证据明细和冲突。

    硬冲突（完整 VIN 不同、品牌不同、车型不同、年份不同）直接 veto：
    分数作废且禁止自动归并；其他字段冲突只把结论上限压到 pending。
    """
    factors = []
    conflicts = []
    score = 0.0

    # --- 完整 VIN：同号是最强证据，异号是硬冲突 ---------------------------
    if profile["full_vin"] and candidate.full_vin:
        if profile["full_vin"] == candidate.full_vin:
            score += W_FULL_VIN
            factors.append(_factor("full_vin", W_FULL_VIN, "match",
                                   profile["full_vin"]))
        else:
            conflicts.append("full_vin_mismatch")
            factors.append(_factor("full_vin", 0.0, "veto",
                                   f"{profile['full_vin']} != {candidate.full_vin}"))

    # --- 不完整 VIN 尾号 --------------------------------------------------
    if profile["vin_fragment"] and candidate.vin_fragments:
        if profile["vin_fragment"] in candidate.vin_fragments:
            score += W_PARTIAL_VIN
            factors.append(_factor("vin_fragment", W_PARTIAL_VIN, "match",
                                   profile["vin_fragment"]))
        else:
            factors.append(_factor("vin_fragment", 0.0, "conflict", "tail differs"))
            conflicts.append("vin_fragment_mismatch")

    # --- 车牌脱敏片段 ------------------------------------------------------
    if profile["plate"] and len(profile["plate"]) >= MIN_PLATE_CHARS:
        if profile["plate"] in candidate.plates:
            score += W_PLATE
            factors.append(_factor("plate", W_PLATE, "match", profile["plate"]))
        else:
            factors.append(_factor("plate", 0.0, "conflict", "plate differs"))
            conflicts.append("plate_mismatch")

    # --- 品牌/车型：硬冲突 -------------------------------------------------
    if profile["make"] and candidate.makes:
        if profile["make"] in candidate.makes:
            factors.append(_factor("make", 0.0, "match", profile["make"]))
        else:
            conflicts.append("make_mismatch")
            factors.append(_factor("make", 0.0, "veto",
                                   f"{profile['make']} not in {sorted(candidate.makes)}"))
    if profile["model"] and candidate.models:
        if profile["model"] in candidate.models:
            score += W_MAKE_MODEL
            factors.append(_factor("make_model", W_MAKE_MODEL, "match",
                                   profile["model"]))
        else:
            conflicts.append("model_mismatch")
            factors.append(_factor("model", 0.0, "veto",
                                   f"{profile['model']} not in {sorted(candidate.models)}"))

    # --- 年份：不一致为硬冲突 ---------------------------------------------
    if profile["year"] is not None and candidate.years:
        if profile["year"] in candidate.years:
            score += W_YEAR
            factors.append(_factor("year", W_YEAR, "match", str(profile["year"])))
        else:
            conflicts.append("year_mismatch")
            factors.append(_factor("year", 0.0, "veto",
                                   f"{profile['year']} not in {sorted(candidate.years)}"))

    # --- 配置：冲突只降级不否决 --------------------------------------------
    if profile["trim"] and candidate.trims:
        if profile["trim"] in candidate.trims:
            score += W_TRIM
            factors.append(_factor("trim", W_TRIM, "match", profile["trim"]))
        else:
            conflicts.append("trim_mismatch")
            factors.append(_factor("trim", 0.0, "conflict",
                                   f"{profile['trim']} not in {sorted(candidate.trims)}"))

    # --- 里程：接近为弱证据，差异过大为冲突 --------------------------------
    if profile["mileage"] is not None and candidate.mileages:
        nearest = min(abs(profile["mileage"] - m) for m in candidate.mileages)
        if nearest <= MILEAGE_TOLERANCE:
            score += W_MILEAGE
            factors.append(_factor("mileage", W_MILEAGE, "match",
                                   f"Δ={nearest:.0f}km"))
        else:
            conflicts.append("mileage_mismatch")
            factors.append(_factor("mileage", 0.0, "conflict",
                                   f"Δ={nearest:.0f}km"))

    # --- 价格：接近为弱证据 -------------------------------------------------
    if profile["price"] is not None and candidate.prices:
        nearest = min(abs(profile["price"] - p) for p in candidate.prices)
        base = max(1.0, min(candidate.prices))
        if nearest / base <= PRICE_TOLERANCE:
            score += W_PRICE
            factors.append(_factor("price", W_PRICE, "match",
                                   f"Δ={nearest:.0f}"))
        else:
            factors.append(_factor("price", 0.0, "neutral",
                                   f"Δ={nearest:.0f}"))

    # --- 车源/门店：同源弱证据；不同门店不惩罚（覆盖同车换店） -------------
    if profile["source"] and profile["source"] in candidate.sources:
        score += W_SOURCE
        factors.append(_factor("source", W_SOURCE, "match", profile["source"]))
    elif profile["dealer"] and candidate.dealers and profile["dealer"] not in candidate.dealers:
        factors.append(_factor("dealer", 0.0, "neutral", "different dealer (relisting allowed)"))

    veto = any(c.endswith("_mismatch") and c in
               {"full_vin_mismatch", "make_mismatch",
                "model_mismatch", "year_mismatch"} for c in conflicts)
    return {
        "score": 0.0 if veto else round(score, 2),
        "veto": veto,
        "conflicts": conflicts,
        "factors": factors,
    }


class VehicleIdentity:
    """一辆物理车的稳定身份对象；状态历史通过 identity_id 引用它。"""

    def __init__(self, anchor_key):
        self.anchor_key = anchor_key            # 最小成员挂牌键，决定稳定 ID
        self.members = []                       # 已接受的挂牌键（有序）
        self.sources = set()
        self.dealers = set()
        self.plates = set()
        self.vin_fragments = set()
        self.makes = set()
        self.models = set()
        self.years = set()
        self.trims = set()
        self.mileages = set()
        self.prices = set()
        self.full_vin = None
        self.vin_checked_ok = False
        self.status = NEW                       # new / pending / confirmed
        self.confidence = 0.0
        self.evidence = []                      # 最近一次归并的可解释明细
        self.last_listing_key = None

    @property
    def identity_id(self):
        digest = hashlib.sha256(self.anchor_key.encode("utf-8")).hexdigest()[:12]
        return f"veh-{digest}"

    def absorb(self, profile):
        """把一条已接受记录的证据并入身份别名集合。"""
        self.last_listing_key = profile["listing_key"]
        self.members.append(profile["listing_key"])
        # 锚点始终是最小成员键：身份 ID 与到达顺序无关，可确定性回放
        self.anchor_key = min(self.members)
        if profile["source"]:
            self.sources.add(profile["source"])
        if profile["dealer"]:
            self.dealers.add(profile["dealer"])
        if profile["plate"]:
            self.plates.add(profile["plate"])
        if profile["vin_fragment"]:
            self.vin_fragments.add(profile["vin_fragment"])
        if profile["make"]:
            self.makes.add(profile["make"])
        if profile["model"]:
            self.models.add(profile["model"])
        if profile["year"] is not None:
            self.years.add(profile["year"])
        if profile["trim"]:
            self.trims.add(profile["trim"])
        if profile["mileage"] is not None:
            self.mileages.add(profile["mileage"])
        if profile["price"] is not None:
            self.prices.add(profile["price"])
        if profile["full_vin"]:
            # 完整 VIN 别名具有排他性，由 registry 保证不会并入异号身份
            self.full_vin = profile["full_vin"]
            self.vin_checked_ok = self.vin_checked_ok or profile["vin_check"] == "ok"

    def snapshot(self):
        return {
            "identity_id": self.identity_id,
            "anchor_key": self.anchor_key,
            "status": self.status,
            "confidence": self.confidence,
            "full_vin": self.full_vin,
            "aliases": {
                "sources": sorted(self.sources),
                "dealers": sorted(self.dealers),
                "plates": sorted(self.plates),
                "vin_fragments": sorted(self.vin_fragments),
                "listings": list(self.members),
            },
            "evidence": self.evidence,
        }


class IdentityRegistry:
    """跨记录的身份归并状态；同批数据回放结果与到达顺序无关。"""

    def __init__(self):
        self._identities = {}          # identity_id -> VehicleIdentity
        self._by_listing = {}          # listing_key -> identity_id（含待审挂载）
        self._by_full_vin = {}         # full_vin -> identity_id（完整 VIN 排他）
        self._reviews = []
        self._decisions = []           # 人工裁决审计，只增不改
        self._pending_profiles = {}    # 待审记录证据，供人工裁决时重放

    # -- 内部工具 -----------------------------------------------------------
    def _new_identity(self, profile, status, confidence, evidence):
        ident = VehicleIdentity(profile["listing_key"])
        ident.absorb(profile)
        ident.status = status
        ident.confidence = confidence
        ident.evidence = evidence
        self._identities[ident.identity_id] = ident
        self._by_listing[profile["listing_key"]] = ident.identity_id
        if profile["full_vin"]:
            self._by_full_vin.setdefault(profile["full_vin"], ident.identity_id)
        # 带完整 VIN 出生的身份天然确认（号码本身已由 vin_id 校验）
        if profile["full_vin"] and ident.status != CONFIRMED:
            ident.status = CONFIRMED
            ident.confidence = 1.0
        return ident

    def _open_review(self, profile, target, score_info, reason):
        review_id = "rev-" + hashlib.sha256(
            (profile["listing_key"] + "|" + target.identity_id + "|" + reason)
            .encode("utf-8")).hexdigest()[:10]
        review = {
            "review_id": review_id,
            "listing_key": profile["listing_key"],
            "candidate_identity_id": target.identity_id,
            "reason": reason,
            "score": score_info["score"],
            "conflicts": list(score_info["conflicts"]),
            "factors": score_info["factors"],
            "state": "open",
        }
        self._reviews.append(review)
        return review

    def _attach_pending(self, profile, target, score_info, reason):
        """待审挂载：记录引用候选身份，但不改变该身份的已确认归属。"""
        self._by_listing[profile["listing_key"]] = target.identity_id
        self._open_review(profile, target, score_info, reason)

    def _open_link_review(self, ident_a, ident_b, profile_a, profile_b, info):
        """批处理专用：两个分量之间的无向 link 待审（顺序无关，天然去重）。"""
        id_a, id_b = sorted((ident_a.identity_id, ident_b.identity_id))
        review_id = "rev-" + hashlib.sha256(
            (id_a + "|" + id_b + "|" + ",".join(info["conflicts"]))
            .encode("utf-8")).hexdigest()[:10]
        if any(r["review_id"] == review_id for r in self._reviews):
            return
        self._reviews.append({
            "review_id": review_id,
            "kind": "link",
            "identity_ids": [id_a, id_b],
            "listing_keys": sorted((profile_a["listing_key"],
                                    profile_b["listing_key"])),
            "candidate_identity_id": id_b,   # 兼容流式待审的字段读法
            "listing_key": profile_a["listing_key"],
            "reason": "hard_conflict" if info["conflicts"] else "below_threshold",
            "score": info["score"],
            "conflicts": list(info["conflicts"]),
            "factors": info["factors"],
            "state": "open",
        })

    def _rekey(self, ident, old_id):
        """成员变化导致锚点（最小键）变化时，同步所有身份索引与待审引用。"""
        new_id = ident.identity_id
        if new_id == old_id:
            return
        self._identities.pop(old_id, None)
        self._identities[new_id] = ident
        for key, mapped in list(self._by_listing.items()):
            if mapped == old_id:
                self._by_listing[key] = new_id
        for vin, mapped in list(self._by_full_vin.items()):
            if mapped == old_id:
                self._by_full_vin[vin] = new_id
        for review in self._reviews:
            if review["candidate_identity_id"] == old_id:
                review["candidate_identity_id"] = new_id
            if review.get("spawned_identity_id") == old_id:
                review["spawned_identity_id"] = new_id
        for decision in self._decisions:
            if decision.get("identity_id") == old_id:
                decision["identity_id"] = new_id

    @staticmethod
    def _strong_alias_disagrees(ident, profile):
        """车牌与 VIN 尾号两类强别名都存在且都对不上：判定为不同车，不待审。"""
        plate_seen = bool(profile["plate"]) and bool(ident.plates)
        vin_seen = bool(profile["vin_fragment"]) and bool(ident.vin_fragments)
        if not plate_seen and not vin_seen:
            return False
        plate_diff = plate_seen and profile["plate"] not in ident.plates
        vin_diff = vin_seen and profile["vin_fragment"] not in ident.vin_fragments
        return plate_diff and vin_diff

    # -- 主入口 -------------------------------------------------------------
    def register(self, record):
        profile = evidence_profile(record)
        key = profile["listing_key"]

        # 幂等回放：同挂牌键重复到达直接返回既有结论，不产生任何状态变更
        existing_id = self._by_listing.get(key)
        if existing_id is not None:
            return self._result_for(existing_id, key, replayed=True)

        # 1) 完整 VIN 通道（vin_id 已确认号码完整）：同号必并、异号硬隔离
        if profile["full_vin"]:
            vin_owner = self._by_full_vin.get(profile["full_vin"])
            if vin_owner is not None:
                owner = self._identities[vin_owner]
                score_info = score_against(owner, profile)
                if not score_info["veto"] and not self._strong_alias_disagrees(owner, profile):
                    return self._accept(owner, profile, score_info,
                                        reason="full_vin_match",
                                        confidence=min(1.0, owner.confidence, 1.0))
                # 同 VIN 却字段硬冲突/强别名分歧：数据质量问题，只能待审，不改归属
                self._attach_pending(profile, owner, score_info,
                                     "full_vin_conflict")
                return self._result_for(owner.identity_id, key,
                                        decision=PENDING,
                                        score_info=score_info,
                                        reason="full_vin_conflict")
            # 新 VIN：检查是否与已确认身份的其他别名冲突
            return self._route_by_evidence(profile, vin_locked=True)

        return self._route_by_evidence(profile, vin_locked=False)

    def _route_by_evidence(self, profile, vin_locked):
        """无完整 VIN（或 VIN 首次出现）时的证据加权归并。"""
        best = None
        best_info = None
        for ident in self._identities.values():
            info = score_against(ident, profile)
            if info["veto"]:
                continue
            if self._strong_alias_disagrees(ident, profile):
                # 车牌与 VIN 尾号双双对不上：不同车，连候选都不进，避免误并/噪音待审
                continue
            if best is None or info["score"] > best_info["score"]:
                best, best_info = ident, info

        if best is None:
            # 所有候选都硬冲突：另立身份；若车牌/VIN 尾号撞了已确认身份则留审
            ident = self._new_identity(profile, NEW, 0.0, [])
            self._review_alias_cross(profile, ident)
            return self._result_for(ident.identity_id, profile["listing_key"],
                                    decision=NEW)

        hard_conflict = bool(best_info["conflicts"])
        if best_info["score"] >= AUTO_CONFIRM_SCORE and not hard_conflict:
            if best.status == CONFIRMED and not vin_locked:
                # 冻结保护：已确认身份不允许被新线索无声改写，证据再强也先审
                self._attach_pending(profile, best, best_info,
                                     "confirmed_freeze_new_clue")
                return self._result_for(best.identity_id, profile["listing_key"],
                                        decision=PENDING, score_info=best_info,
                                        reason="confirmed_freeze_new_clue")
            return self._accept(best, profile, best_info,
                                reason="evidence_threshold",
                                confidence=best_info["score"] / 100.0)

        if best_info["score"] >= PENDING_SCORE or self._shares_strong_alias(best, profile):
            self._attach_pending(profile, best, best_info,
                                 "hard_conflict" if hard_conflict else "below_threshold")
            return self._result_for(best.identity_id, profile["listing_key"],
                                    decision=PENDING, score_info=best_info,
                                    reason="hard_conflict" if hard_conflict
                                    else "below_threshold")

        ident = self._new_identity(profile, NEW, 0.0, best_info["factors"])
        self._review_alias_cross(profile, ident)
        return self._result_for(ident.identity_id, profile["listing_key"],
                                decision=NEW, score_info=best_info)

    def _shares_strong_alias(self, ident, profile):
        if profile["plate"] and profile["plate"] in ident.plates:
            return True
        if profile["vin_fragment"] and profile["vin_fragment"] in ident.vin_fragments:
            return True
        return False

    def _review_alias_cross(self, profile, ident):
        """另立身份但若强别名指向其他身份，补一条待审，防止静默分裂。"""
        for other in self._identities.values():
            if other.identity_id == ident.identity_id:
                continue
            shared = False
            if profile["plate"] and profile["plate"] in other.plates:
                shared = True
            if profile["vin_fragment"] and profile["vin_fragment"] in other.vin_fragments:
                shared = True
            if profile["full_vin"] and other.full_vin == profile["full_vin"]:
                shared = True
            if shared:
                info = score_against(other, profile)
                self._open_review(profile, other, info, "alias_cross_new_identity")

    def _accept(self, ident, profile, score_info, reason, confidence):
        """接受归并：更新身份证据；确认态只升不降。"""
        old_id = ident.identity_id
        ident.absorb(profile)
        ident.evidence = score_info["factors"]
        ident.confidence = round(max(ident.confidence, confidence), 3)
        if score_info["score"] >= AUTO_CONFIRM_SCORE or profile["full_vin"]:
            ident.status = CONFIRMED
        elif ident.status == NEW:
            ident.status = PENDING
        # 锚点可能因新成员更小而变化：同步全部索引，保证身份 ID 顺序无关
        self._rekey(ident, old_id)
        self._by_listing[profile["listing_key"]] = ident.identity_id
        if profile["full_vin"]:
            self._by_full_vin.setdefault(profile["full_vin"], ident.identity_id)
        return self._result_for(ident.identity_id, profile["listing_key"],
                                decision=ident.status, score_info=score_info,
                                reason=reason)

    def _result_for(self, identity_id, listing_key, decision=None,
                    score_info=None, reason=None, replayed=False):
        ident = self._identities[identity_id]
        return {
            "identity_id": identity_id,
            "listing_key": listing_key,
            "decision": decision or ident.status,
            "confidence": ident.confidence,
            "identity_status": ident.status,
            "frozen": ident.status == CONFIRMED,
            "reason": reason,
            "replayed": replayed,
            "factors": (score_info or {}).get("factors", ident.evidence),
            "conflicts": (score_info or {}).get("conflicts", []),
        }

    # -- 人工裁决 -----------------------------------------------------------
    def resolve_review(self, review_id, approve, note=""):
        """人工处理待审；裁决记入只增审计日志。确认身份不会被自动流程改写。"""
        for review in self._reviews:
            if review["review_id"] != review_id or review["state"] != "open":
                continue
            target = self._identities[review["candidate_identity_id"]]
            key = review["listing_key"]
            review["state"] = "approved" if approve else "rejected"
            self._decisions.append({
                "review_id": review_id, "approved": approve, "note": note,
                "listing_key": key, "identity_id": target.identity_id,
            })
            if approve:
                # 重放该记录的归并：以人工授权绕过冻结/阈值限制
                profile = self._pending_profiles.pop(key, None)
                if profile is not None:
                    info = score_against(target, profile)
                    self._accept(target, profile, info,
                                 reason="manual_approval",
                                 confidence=max(target.confidence,
                                                round(info["score"] / 100.0, 3)))
            else:
                # 拒绝并入：为该挂牌另立独立身份，原候选归属保持不变
                profile = self._pending_profiles.pop(key, None)
                if profile is not None:
                    spawned = self._new_identity(profile, NEW, 0.0,
                                                 review["factors"])
                    review["spawned_identity_id"] = spawned.identity_id
            return dict(review)
        raise KeyError(f"open review not found: {review_id}")

    # -- 回放与导出 ---------------------------------------------------------
    def identities(self):
        return [self._identities[i].snapshot()
                for i in sorted(self._identities,
                                key=lambda i: self._identities[i].anchor_key)]

    def reviews(self):
        return [dict(r) for r in self._reviews]

    def decisions(self):
        return [dict(d) for d in self._decisions]

    def identity_of(self, listing_key):
        return self._by_listing.get(str(listing_key))

    # -- 批处理：顺序无关的图聚类归并 ---------------------------------------
    def build_from(self, records):
        """对一批记录做顺序无关的身份归并（两两对称评分 + 连通分量定级）。

        与流式 ``register`` 并存：本方法先看全量数据再下结论，因此来源到达
        顺序颠倒后，身份分组、置信度与待审集合完全一致；已确认分量上的新
        线索一律以 open 待审挂接，确认归属本身不被改写。
        """
        self.__init__()
        profiles = [evidence_profile(
            r if "_vin" in r else vin_id.process(r)) for r in records]
        # 批处理同样保留待审证据档案，使 resolve_review 可在批处理结果上工作
        for profile in profiles:
            self._pending_profiles[profile["listing_key"]] = profile
        keys = [p["listing_key"] for p in profiles]
        n = len(profiles)

        class _UF:
            def __init__(self, size):
                self.parent = list(range(size))

            def find(self, x):
                while self.parent[x] != x:
                    self.parent[x] = self.parent[self.parent[x]]
                    x = self.parent[x]
                return x

            def union(self, a, b):
                ra, rb = self.find(a), self.find(b)
                if ra != rb:
                    self.parent[rb] = ra

        uf = _UF(n)
        pair_infos = {}
        confirmed_seed = {i for i, p in enumerate(profiles) if p["full_vin"]}
        # 1) 两两对称评分。自动确认边规则：
        #    - 完整 VIN 同号且无冲突：天然同车，允许并边（跨店换牌同 VIN 亦然）；
        #    - 证据分达阈值且无冲突：仅在双方都不是 VIN 确认种子时并边，
        #      已确认锚点上的新线索一律降级为 link 待审（冻结保护）。
        for i in range(n):
            for j in range(i + 1, n):
                # 用一个临时身份承载 j 的证据，复用同一套加权/否决规则
                holder = VehicleIdentity(profiles[j]["listing_key"])
                holder.absorb(profiles[j])
                info = score_against(holder, profiles[i])
                pair_infos[(i, j)] = info
                same_full_vin = (profiles[i]["full_vin"]
                                 and profiles[i]["full_vin"]
                                 == profiles[j]["full_vin"])
                clean = (not info["veto"] and not info["conflicts"]
                         and not _strong_disagree(profiles[i], profiles[j]))
                auto = False
                if clean and same_full_vin:
                    auto = True
                elif (clean and info["score"] >= AUTO_CONFIRM_SCORE
                      and i not in confirmed_seed
                      and j not in confirmed_seed):
                    auto = True
                if auto:
                    uf.union(i, j)

        # 2) 连通分量 -> 身份对象（锚点=分量内最小挂牌键，确定性派生）
        groups = {}
        for idx in range(n):
            groups.setdefault(uf.find(idx), []).append(idx)
        comp_ident = {}
        for root, members in groups.items():
            anchor = min(keys[i] for i in members)
            ident = VehicleIdentity(anchor)
            for i in sorted(members, key=lambda x: keys[x]):
                ident.absorb(profiles[i])
            has_vin = any(profiles[i]["full_vin"] for i in members)
            seed_only = all(i in confirmed_seed for i in members)
            # 分量内只要还存在字段冲突，就只能停在待确认，不允许自动确认
            inner_conflict = any(
                pair_infos[tuple(sorted((a, b)))]["conflicts"]
                for a in members for b in members if a < b)
            if has_vin and seed_only:
                ident.status = CONFIRMED
                ident.confidence = 1.0
            elif len(members) > 1 and not inner_conflict:
                ident.status = CONFIRMED
                ident.confidence = round(
                    max(pair_infos[tuple(sorted((a, b)))]["score"]
                        for a in members for b in members if a < b) / 100.0, 3)
            else:
                ident.status = PENDING if len(members) > 1 else NEW
                ident.confidence = round(
                    max((pair_infos[tuple(sorted((a, b)))]["score"]
                         for a in members for b in members if a < b),
                        default=0.0) / 100.0, 3)
            comp_ident[root] = ident
            self._identities[ident.identity_id] = ident
            for i in members:
                self._by_listing[keys[i]] = ident.identity_id
                if profiles[i]["full_vin"]:
                    self._by_full_vin.setdefault(
                        profiles[i]["full_vin"], ident.identity_id)

        # 3) 跨分量的弱关联/冲突：生成无向 link 待审，确认分量的归属保持不变。
        #    无向结构 + 排序遍历保证 review 集合与到达顺序完全无关。
        results = {}
        for (i, j) in sorted(pair_infos):
            info = pair_infos[(i, j)]
            ri, rj = uf.find(i), uf.find(j)
            if ri == rj:
                continue
            if info["veto"] and not _shares_alias(profiles[i], profiles[j]):
                continue  # 硬冲突且无任何强别名交集：判定为不同车，不制造待审
            if _strong_disagree(profiles[i], profiles[j]):
                continue  # 车牌与 VIN 尾号双双对不上：不同车
            self._open_link_review(
                comp_ident[ri], comp_ident[rj], profiles[i], profiles[j], info)

        # 4) 每条记录的结论直接继承所属分量；link 待审两侧的 NEW 分量都升级
        #    为 pending（对称处理，保证乱序后每条记录结论一致）
        linked_identities = set()
        for r in self._reviews:
            linked_identities.update(r.get("identity_ids",
                                           [r["candidate_identity_id"]]))
        for root, members in groups.items():
            ident = comp_ident[root]
            if ident.status == NEW and ident.identity_id in linked_identities:
                ident.status = PENDING
            for i in members:
                key = keys[i]
                results[key] = {
                    "identity_id": ident.identity_id,
                    "listing_key": key,
                    "decision": ident.status,
                    "confidence": ident.confidence,
                    "identity_status": ident.status,
                    "frozen": ident.status == CONFIRMED,
                    "reason": "batch_resolve",
                    "replayed": False,
                    "factors": _collect_factors(i, members, pair_infos),
                    "conflicts": _collect_conflicts(i, members, pair_infos),
                }
        return results


def _shares_alias(a, b):
    if a["plate"] and a["plate"] == b["plate"]:
        return True
    if a["vin_fragment"] and a["vin_fragment"] == b["vin_fragment"]:
        return True
    if a["full_vin"] and a["full_vin"] == b["full_vin"]:
        return True
    return False


def _strong_disagree(a, b):
    plate_seen = bool(a["plate"]) and bool(b["plate"])
    vin_seen = bool(a["vin_fragment"]) and bool(b["vin_fragment"])
    if not plate_seen and not vin_seen:
        return False
    plate_diff = plate_seen and a["plate"] != b["plate"]
    vin_diff = vin_seen and a["vin_fragment"] != b["vin_fragment"]
    return plate_diff and vin_diff


def _collect_factors(idx, members, pair_infos):
    factors = []
    for other in members:
        if other == idx:
            continue
        info = pair_infos[tuple(sorted((idx, other)))]
        factors.extend(info["factors"])
    # 去重，保留可解释性
    seen, uniq = set(), []
    for f in factors:
        token = (f["factor"], f["result"], f["detail"])
        if token not in seen:
            seen.add(token)
            uniq.append(f)
    return uniq


def _collect_conflicts(idx, members, pair_infos):
    conflicts = []
    for other in members:
        if other == idx:
            continue
        conflicts.extend(pair_infos[tuple(sorted((idx, other)))]["conflicts"])
    return sorted(set(conflicts))


def resolve_batch(records, registry=None):
    """顺序无关批处理入口：返回 {listing_key: 归并结论}，并填充 registry 状态。"""
    reg = registry or IdentityRegistry()
    prepared = []
    for rec in records:
        r = dict(rec)
        if "_vin" not in r:
            r = vin_id.process(r)
        prepared.append(r)
    return reg.build_from(prepared)


# 模块级默认注册表：process(record) 签名保持不变，pipeline 无需改动。
# 测试通过 reset_registry() / 新 Registry 实例获得干净回放环境。
_default = IdentityRegistry()


def get_registry():
    return _default


def reset_registry():
    global _default
    _default = IdentityRegistry()
    return _default


def process(record, registry=None):
    """流水线阶段：归并挂牌身份，在记录副本上挂 ``_identity`` 块。

    历史键仍由 keyer 按记录 ``id`` 读取；状态历史可通过
    ``_identity.identity_id`` 引用稳定的物理车辆对象。
    """
    reg = registry or _default
    rec = dict(record)
    if "_vin" not in rec:
        rec = vin_id.process(rec)
    profile = evidence_profile(rec)
    # 暂存证据，供人工批准/驳回待审时重放归并
    reg._pending_profiles[profile["listing_key"]] = profile
    result = reg.register(rec)
    rec["_identity"] = result
    rec["_stage_identity"] = result["decision"]
    return rec
