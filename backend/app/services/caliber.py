"""度量口径快照引擎：把"事实发布给出的度量建议"沉淀为可版本化、可复算的口径登记。

设计要点（对应口径治理需求）：

1. 口径版本快照（CaliberSnapshot）
   - 每条快照有独立 snap_id / version_no / content_hash，登记口径的全部要素：
     度量字段、所属事实、事实粒度、聚合方式、可加性类别、可加维度、禁止汇总维度组合。
   - 同一度量在同一事实粒度范围内只允许一个"生效中"口径（effective）；口径变更不覆盖旧
     快照，旧版本转为"已停用（历史可回查）"，报表按 snap_id 绑定，杜绝同名字段悄然替换。

2. 可加性矩阵（additivity matrix）
   - fully_additive 全可加：所有关联维度均可 SUM（如交易额）。
   - semi_additive 半可加：仅在白名单维度可加，时间/日期维度不可加（如期末余额，
     跨日期只能取期末值 LAST_VALUE，不能沿日期 SUM）。
   - non_additive 不可加：任何维度组合都不允许 SUM，只能 AVG / COUNT DISTINCT 等。

3. 汇总试算（trial）
   - 按目标维度组合逐度量判定 allowed / blocked，blocked 时给出拦截原因和替代聚合方式；
     任一查询只能解析出一个适用口径，自动建议与人工规则冲突时人工口径优先且冲突被显式记录。

引擎不依赖 FastAPI，只通过 store 读写数据，便于单元测试和脚本复算。
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# 常量与口径分类
# ---------------------------------------------------------------------------

FULLY_ADDITIVE = "fully_additive"        # 全可加
SEMI_ADDITIVE = "semi_additive"          # 半可加（仅白名单维度可加）
NON_ADDITIVE = "non_additive"            # 不可加

ADDITIVITY_LABELS = {
    FULLY_ADDITIVE: "全可加",
    SEMI_ADDITIVE: "半可加",
    NON_ADDITIVE: "不可加",
}

# 事实粒度 → 该粒度下"日期/时间轴"维度（沿这些维度对余额求和一定错误）
TIME_DIMENSION_KEYS = ("DIM_TIME", "日期", "时间", "dt", "date_key", "月份", "周", "季度", "年份")

# 各聚合方式的可加性约束：SUM 要求度量在该维度组合上可加
SUM_LIKE_AGGS = {"SUM"}
LAST_VALUE_AGGS = {"LAST_VALUE", "MAX", "AVG"}
NON_SUM_AGGS = {"COUNT", "COUNT DISTINCT", "AVG", "LAST_VALUE", "MAX", "MIN"}

CALIBER_TABLE = "measure_caliber_snapshot"
REFERENCE_TABLE = "caliber_report_ref"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_dims(raw: Any) -> list[str]:
    """把 "DIM_TIME.DIM_STORE" / "DIM_TIME,DIM_STORE" / 列表统一拆成维度编码列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    for sep in (".", ",", "，", "、"):
        text = text.replace(sep, ".")
    return [d.strip() for d in text.split(".") if d.strip()]


def _is_time_dim(dim: str) -> bool:
    return any(key in dim for key in TIME_DIMENSION_KEYS)


def _content_hash(payload: dict[str, Any]) -> str:
    """口径内容指纹：同内容同指纹，任一口径要素变化即产生新版本。"""
    material = str(sorted((k, _normalize(v)) for k, v in payload.items()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _normalize(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return sorted(str(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# 自动建议：基于度量字段命名与事实粒度推断初始口径
# ---------------------------------------------------------------------------

def suggest_caliber(measure_name: str, fact_granularity: str = "",
                    fact_dims: Any = "") -> dict[str, Any]:
    """事实发布时的原始度量建议：推断聚合方式、可加性类别与可加维度。

    规则：
    - 余额/存量类（_bal / _on_hand / balance / 库存）→ 半可加，日期维度不可加；
    - 比率/率值类（_rate / 占比）→ 不可加，AVG；
    - 计数类（_cnt / count）→ 不可加 SUM，COUNT/COUNT DISTINCT；
    - 金额/数量流水类（_amt / _qty / 交易额）→ 全可加，SUM。
    """
    name = measure_name.strip()
    lower = name.lower()
    dims = _split_dims(fact_dims)

    balance_like = (
        "_bal" in lower
        or "balance" in lower or "on_hand" in lower or "onhand" in lower
        or lower.endswith("_stock") or "余额" in name or "库存" in name
    )
    rate_like = lower.endswith(("_rate", "_ratio")) or "率" in name
    count_like = lower.endswith(("_cnt", "_count")) or "次数" in name

    if balance_like:
        additive_dims = [d for d in dims if not _is_time_dim(d)]
        return {
            "measure_name": name,
            "agg_method": "SUM",
            "additivity": SEMI_ADDITIVE,
            "additive_dims": additive_dims,
            "blocked_dims": [d for d in dims if _is_time_dim(d)],
            "rollup_agg": "LAST_VALUE",
            "source": "auto_suggest",
            "reason": "期末余额/存量类度量：日期维度不可加，跨日期取期末值(LAST_VALUE)",
            "fact_granularity": fact_granularity,
        }
    if rate_like:
        return {
            "measure_name": name,
            "agg_method": "AVG",
            "additivity": NON_ADDITIVE,
            "additive_dims": [],
            "blocked_dims": dims,
            "rollup_agg": "AVG",
            "source": "auto_suggest",
            "reason": "比率类度量：任何维度组合都不能直接SUM，应按分子分母重算或AVG",
            "fact_granularity": fact_granularity,
        }
    if count_like:
        return {
            "measure_name": name,
            "agg_method": "COUNT DISTINCT",
            "additivity": NON_ADDITIVE,
            "additive_dims": [],
            "blocked_dims": dims,
            "rollup_agg": "COUNT DISTINCT",
            "source": "auto_suggest",
            "reason": "计数类度量：SUM会重复计数，应使用COUNT/COUNT DISTINCT",
            "fact_granularity": fact_granularity,
        }
    # 默认：流水金额/数量，全可加
    return {
        "measure_name": name,
        "agg_method": "SUM",
        "additivity": FULLY_ADDITIVE,
        "additive_dims": dims,
        "blocked_dims": [],
        "rollup_agg": "SUM",
        "source": "auto_suggest",
        "reason": "流水类度量：所有关联维度均可加，允许SUM汇总",
        "fact_granularity": fact_granularity,
    }


# ---------------------------------------------------------------------------
# 口径登记与版本快照
# ---------------------------------------------------------------------------

class CaliberRegistry:
    """度量口径登记表：维护版本快照、生效唯一性和报表引用回查。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ----- 基础存取 -----

    def _rows(self) -> list[dict[str, Any]]:
        return self._store.rows(CALIBER_TABLE)

    def list_snapshots(self, measure_name: str | None = None,
                       fact_name: str | None = None) -> list[dict[str, Any]]:
        rows = self._rows()
        if measure_name:
            rows = [r for r in rows if r.get("度量名称") == measure_name]
        if fact_name:
            rows = [r for r in rows if r.get("所属事实") == fact_name]
        return rows

    def get_snapshot(self, snap_id: str) -> dict[str, Any] | None:
        for row in self._rows():
            if row.get("snap_id") == snap_id:
                return row
        return None

    def effective_snapshot(self, measure_name: str, fact_name: str,
                           fact_granularity: str | None = None) -> dict[str, Any] | None:
        """取某度量在事实粒度范围内唯一的生效口径。"""
        candidates = [
            r for r in self._rows()
            if r.get("度量名称") == measure_name
            and r.get("所属事实") == fact_name
            and r.get("snapshot_status") == "生效中"
        ]
        if fact_granularity is not None:
            candidates = [r for r in candidates if r.get("事实粒度") == fact_granularity]
        if not candidates:
            return None
        # 版本号最大者即生效口径（登记时已保证唯一性，这里做防御性收敛）
        return max(candidates, key=lambda r: int(r.get("version_no", 1)))

    # ----- 登记 / 修订 -----

    def register(
        self,
        *,
        measure_name: str,
        fact_name: str,
        fact_granularity: str,
        agg_method: str,
        additivity: str,
        additive_dims: Any,
        source: str = "manual",
        origin_suggestion: dict[str, Any] | None = None,
        blocked_dims: Any | None = None,
        rollup_agg: str | None = None,
        replace: bool = False,
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """登记一条口径快照。

        唯一性规则：同一 (度量名称, 所属事实, 事实粒度) 只允许一个"生效中"口径。
        - 已存在生效口径且 replace=False → 拒绝（冲突需显式修订，不能悄然替换）；
        - replace=True → 旧生效快照转"已停用"并保留全部历史，新快照版本号 +1 生效。
        """
        errors: list[str] = []
        measure_name = measure_name.strip()
        fact_name = fact_name.strip()
        fact_granularity = fact_granularity.strip()
        if not measure_name:
            errors.append("缺少必填字段：度量名称")
        if not fact_name:
            errors.append("缺少必填字段：所属事实")
        if additivity not in ADDITIVITY_LABELS:
            errors.append(f"未知可加性类别：{additivity}")
        if agg_method not in SUM_LIKE_AGGS | NON_SUM_AGGS:
            errors.append(f"不支持的聚合方式：{agg_method}")

        additive_list = _split_dims(additive_dims)
        blocked_input = _split_dims(blocked_dims) if blocked_dims is not None else []

        # 半可加口径的日期维度必须落在禁止集合里
        if additivity == SEMI_ADDITIVE:
            time_in_additive = [d for d in additive_list if _is_time_dim(d)]
            if time_in_additive:
                errors.append(
                    "半可加度量的可加维度不能包含日期/时间维度："
                    + "、".join(time_in_additive)
                    + "（期末余额沿日期求和会重复计算）"
                )
            # 自动补全禁止维度：事实关联维度中不在可加白名单内的维度
            fact_dims = self._fact_dims(fact_name)
            blocked = sorted(set(blocked_input) | {d for d in fact_dims if d not in additive_list})
        elif additivity == NON_ADDITIVE:
            if agg_method in SUM_LIKE_AGGS:
                errors.append("不可加度量不能使用SUM聚合，请改用AVG/COUNT DISTINCT等")
            blocked = sorted(set(blocked_input) | set(self._fact_dims(fact_name)))
            additive_list = []
        else:  # 全可加
            blocked = sorted(set(blocked_input))
            if blocked:
                errors.append("全可加度量不应声明禁止汇总维度")

        existing = [
            r for r in self._rows()
            if r.get("度量名称") == measure_name
            and r.get("所属事实") == fact_name
            and r.get("事实粒度") == fact_granularity
        ]
        active = [r for r in existing if r.get("snapshot_status") == "生效中"]
        if active and not replace:
            errors.append(
                f"度量 {measure_name} 在事实 {fact_name}（{fact_granularity}）下已存在生效口径 "
                f"{active[0].get('snap_id')}，同名字段不能悄然替换；如需修订请显式 replace=True"
            )
        if errors:
            return None, errors

        # 内容指纹：与最新版本相同则不产生新版本
        payload = {
            "measure_name": measure_name,
            "fact_name": fact_name,
            "fact_granularity": fact_granularity,
            "agg_method": agg_method,
            "additivity": additivity,
            "additive_dims": sorted(additive_list),
            "blocked_dims": sorted(blocked),
            "rollup_agg": rollup_agg or ("LAST_VALUE" if additivity == SEMI_ADDITIVE else agg_method),
        }
        fingerprint = _content_hash(payload)
        if existing and existing[-1].get("content_hash") == fingerprint:
            return existing[-1], ["口径内容未变化，复用现有快照 " + existing[-1]["snap_id"]]

        if replace and active:
            for old in active:
                old["snapshot_status"] = "已停用"
                old["effective_end"] = _now()
                old["superseded_by_note"] = "被新版本显式修订，历史保留可回查"

        version_no = max((int(r.get("version_no", 1)) for r in existing), default=0) + 1
        snap_id = f"CAL-{measure_name}-v{version_no}-{uuid.uuid4().hex[:6]}"
        row = {
            "id": max((int(r.get("id", 0)) for r in self._rows()), default=0) + 1,
            "snap_id": snap_id,
            "version_no": version_no,
            "parent_snap_id": active[0]["snap_id"] if (replace and active) else None,
            "度量名称": measure_name,
            "所属事实": fact_name,
            "事实粒度": fact_granularity,
            "聚合方式": agg_method,
            "可加性类别": ADDITIVITY_LABELS[additivity],
            "additivity_code": additivity,
            "可加维度": ".".join(additive_list),
            "禁止汇总维度": ".".join(blocked),
            "替代聚合": payload["rollup_agg"],
            "口径来源": "人工规则" if source == "manual" else "自动建议",
            "content_hash": fingerprint,
            "snapshot_status": "生效中",
            "effective_start": _now(),
            "effective_end": None,
            "remark": remark,
            "conflict_with_suggestion": bool(origin_suggestion and _conflicts(origin_suggestion, payload)),
            "origin_suggestion": _compact(origin_suggestion),
            "status": "生效中",
            "pending": False,
            "abnormal": False,
        }
        self._rows().append(row)

        self._store.artifacts.register(
            CALIBER_TABLE, "measure_caliber",
            snap_id,
            {
                "measure_name": measure_name, "fact_name": fact_name,
                "version_no": version_no, "additivity": additivity,
            },
            source_modules=["fact_design", "measure_config", CALIBER_TABLE],
        )
        return row, []

    def revise_additive_dims(self, snap_id: str, additive_dims: Any,
                             remark: str = "") -> tuple[dict[str, Any] | None, list[str]]:
        """修订生效口径的可加维度（白名单），生成新版本快照，旧版本停用留痕。

        沿版本链找回首版自动建议：若人工修订后的口径与建议冲突，新版本会显式标记。
        """
        old = self.get_snapshot(snap_id)
        if old is None:
            return None, [f"快照 {snap_id} 不存在"]
        if old.get("snapshot_status") != "生效中":
            return None, [f"快照 {snap_id} 非生效中，不能修订"]

        origin = self._find_origin_suggestion(old)
        return self.register(
            measure_name=old["度量名称"],
            fact_name=old["所属事实"],
            fact_granularity=old["事实粒度"],
            agg_method=old["聚合方式"],
            additivity=old["additivity_code"],
            additive_dims=additive_dims,
            source="manual",
            origin_suggestion=origin,
            replace=True,
            remark=remark or f"修订可加维度，基于 {snap_id}",
        )

    def _find_origin_suggestion(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """沿 parent_snap_id 找到版本链中首版保存的自动建议（若有）。"""
        if row.get("origin_suggestion"):
            return row["origin_suggestion"]
        parent_id = row.get("parent_snap_id")
        if not parent_id:
            return None
        parent = self.get_snapshot(parent_id)
        return self._find_origin_suggestion(parent) if parent else None

    # ----- 报表引用：历史口径可回查 -----

    def bind_report(self, report_name: str, measure_name: str, fact_name: str,
                    snap_id: str) -> dict[str, Any]:
        """报表按 snap_id 绑定口径快照；快照必须存在，停用快照仍可绑定（回查历史口径）。"""
        snap = self.get_snapshot(snap_id)
        if snap is None:
            raise KeyError(f"口径快照 {snap_id} 不存在，报表 {report_name} 禁止绑定未登记口径")
        if snap.get("度量名称") != measure_name or snap.get("所属事实") != fact_name:
            raise KeyError(
                f"快照 {snap_id} 与度量 {measure_name}/{fact_name} 不匹配，禁止跨度量引用"
            )
        refs = self._store.rows(REFERENCE_TABLE)
        ref_id = max((int(r.get("id", 0)) for r in refs), default=0) + 1
        ref = {
            "id": ref_id,
            "报表名称": report_name,
            "度量名称": measure_name,
            "所属事实": fact_name,
            "bound_snap_id": snap_id,
            "bound_version_no": snap.get("version_no"),
            "content_hash": snap.get("content_hash"),
            "bound_at": _now(),
            "snapshot_status_at_bind": snap.get("snapshot_status"),
        }
        refs.append(ref)
        return ref

    def report_references(self, snap_id: str | None = None) -> list[dict[str, Any]]:
        refs = self._store.rows(REFERENCE_TABLE)
        if snap_id:
            refs = [r for r in refs if r.get("bound_snap_id") == snap_id]
        return refs

    # ----- 试算 / 口径解析 -----

    def trial(self, measure_name: str, fact_name: str, target_dims: Any,
              agg_method: str | None = None) -> dict[str, Any]:
        """按目标维度组合对度量做一次汇总试算，判定每个聚合请求是否被口径允许。"""
        snap = self.effective_snapshot(measure_name, fact_name)
        dims = _split_dims(target_dims)
        if snap is None:
            return {
                "measure_name": measure_name, "fact_name": fact_name,
                "target_dims": dims, "resolved_snap_id": None,
                "allowed": False,
                "blocked": True,
                "reason": "未登记生效口径快照，禁止无口径汇总",
                "blocked_dim_combos": [dims],
            }

        blocked_dims = _split_dims(snap.get("禁止汇总维度"))
        additive_dims = _split_dims(snap.get("可加维度"))
        additivity = snap.get("additivity_code")
        wanted_agg = (agg_method or snap.get("聚合方式") or "SUM").strip()

        hit_blocked = [d for d in dims if d in blocked_dims or _is_time_dim(d)
                       and additivity == SEMI_ADDITIVE]
        # 半可加：目标维度里出现任何禁止维度即拦截；不可加：任何 SUM 都拦截
        violations: list[str] = []
        if wanted_agg in SUM_LIKE_AGGS:
            if additivity == NON_ADDITIVE:
                violations.append(
                    f"度量 {measure_name} 为不可加口径，禁止SUM，替代聚合：{snap.get('替代聚合')}"
                )
            elif additivity == SEMI_ADDITIVE and hit_blocked:
                time_hits = [d for d in hit_blocked if _is_time_dim(d)]
                other_hits = [d for d in hit_blocked if not _is_time_dim(d)]
                if time_hits:
                    violations.append(
                        "期末/存量类半可加度量不能沿日期/时间维度 "
                        + "、".join(time_hits)
                        + " 求和（跨期余额会被重复累加），替代聚合："
                        + str(snap.get("替代聚合"))
                    )
                if other_hits:
                    violations.append(
                        "生效口径 " + snap["snap_id"] + " 未把 "
                        + "、".join(other_hits)
                        + " 登记为可加维度，禁止沿该维度求和，替代聚合："
                        + str(snap.get("替代聚合"))
                    )
            unknown = [d for d in dims if d not in additive_dims and d not in blocked_dims]
            if additivity == SEMI_ADDITIVE and unknown:
                violations.append("目标维度 " + "、".join(unknown) + " 未登记在可加维度白名单内")

        allowed = not violations
        return {
            "measure_name": measure_name,
            "fact_name": fact_name,
            "target_dims": dims,
            "resolved_snap_id": snap["snap_id"],
            "resolved_version_no": snap["version_no"],
            "additivity": additivity,
            "additivity_label": ADDITIVITY_LABELS[additivity],
            "requested_agg": wanted_agg,
            "effective_agg": wanted_agg if allowed else snap.get("替代聚合"),
            "allowed": allowed,
            "blocked": not allowed,
            "violations": violations,
            "blocked_dim_combos": [[d] for d in hit_blocked] if violations else [],
            "additive_dims": additive_dims,
            "blocked_dims": blocked_dims,
        }

    def resolve_for_query(self, measure_names: list[str], fact_name: str,
                          target_dims: Any) -> dict[str, Any]:
        """一次查询解析：每个度量只能采用一个适用口径；任一被拦截则整单不可执行。"""
        results = [self.trial(m, fact_name, target_dims) for m in measure_names]
        blocked = [r for r in results if r["blocked"]]
        combos = sorted({
            tuple(combo)
            for r in blocked
            for combo in r["blocked_dim_combos"]
        })
        return {
            "fact_name": fact_name,
            "target_dims": _split_dims(target_dims),
            "measure_count": len(results),
            "all_allowed": not blocked,
            "blocked_count": len(blocked),
            "trials": results,
            "blocked_dim_combos": [list(combo) for combo in combos],
        }

    # ----- 内部工具 -----

    def _fact_dims(self, fact_name: str) -> list[str]:
        for row in self._store.rows("fact_design"):
            if row.get("事实名称") == fact_name:
                return _split_dims(row.get("关联维度"))
        return []


def _conflicts(suggestion: dict[str, Any], payload: dict[str, Any]) -> bool:
    """自动建议与人工口径是否冲突（可加性类别或可加维度集合不一致即冲突）。"""
    if suggestion.get("additivity") != payload["additivity"]:
        return True
    return sorted(_split_dims(suggestion.get("additive_dims"))) != payload["additive_dims"]


def _compact(value: Any) -> Any:
    """通道消息只保留可序列化的原始建议要素，避免把运行时对象塞进快照。"""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(v) for v in value]
    return value
