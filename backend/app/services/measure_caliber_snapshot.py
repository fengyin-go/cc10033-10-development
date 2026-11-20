"""度量口径快照服务：把度量建议登记为带版本的口径快照，治理可加性与生效唯一性。

职责：
- 消费 measure.suggested / measure.confirmed 通道，把自动建议沉淀为口径版本快照；
- 同一度量在同一事实粒度范围内只保留一个"生效中"口径，修订显式升版、旧版停用留痕；
- 提供可加维度修订、汇总试算、查询口径解析、报表快照绑定等领域动作；
- domain_snapshot 输出版本/冲突/引用统计，供跨模块消费。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.caliber import (
    ADDITIVITY_LABELS,
    CALIBER_TABLE,
    CaliberRegistry,
    _split_dims,
    suggest_caliber,
)
from app.store import store

# 页面可填的中文类别 → 引擎内部分类码
ADDITIVITY_CODE_BY_LABEL = {label: code for code, label in ADDITIVITY_LABELS.items()}


class MeasureCaliberSnapshotService(BaseModuleService):
    MODULE = CALIBER_TABLE
    ENTITY = "口径快照"
    OPERATION_TERM = "登记"
    LIST_FIELDS = [
        "度量名称", "所属事实", "事实粒度", "聚合方式", "可加性类别",
        "可加维度", "禁止汇总维度", "替代聚合", "口径来源",
    ]
    REQUIRED_FIELDS = ["度量名称", "所属事实", "事实粒度", "聚合方式", "可加性类别"]
    STATUS_ORDER = ["生效中", "已停用"]
    ACTION_RULES = {"停用口径": "已停用", "重新生效": "生效中"}
    NEGATIVE_ACTIONS = ["停用口径"]

    def __init__(self, bound_store: Any | None = None) -> None:
        # 默认挂到全局 store；测试可注入独立 Store 实例
        self._bound_store = bound_store or store
        self.registry = CaliberRegistry(self._bound_store)

    # ---------- 登记 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """通用登记入口：手工口径走这里；自动建议走 register_suggestion。"""
        label = str(values.get("可加性类别", "")).strip()
        additivity = ADDITIVITY_CODE_BY_LABEL.get(label)
        if additivity is None and label:
            return None, [f"可加性类别必须是：{'、'.join(ADDITIVITY_LABELS.values())}"]

        fact_name = str(values.get("所属事实", "")).strip()
        measure_name = str(values.get("度量名称", "")).strip()

        # 未显式给类别时，回退到自动建议推断，保证"事实发布→建议→快照"链路不断
        origin = self._find_suggestion(measure_name, fact_name)
        if additivity is None:
            if origin is None:
                return None, ["缺少必填字段：可加性类别（且通道中无该度量的自动建议可推断）"]
            additivity = origin["additivity"]
            values["聚合方式"] = values.get("聚合方式") or origin["agg_method"]
            values["可加维度"] = values.get("可加维度") or origin["additive_dims"]

        fact_granularity = str(values.get("事实粒度", "")).strip() or self._fact_granularity(fact_name)
        row, errors = self.registry.register(
            measure_name=measure_name,
            fact_name=fact_name,
            fact_granularity=fact_granularity,
            agg_method=str(values.get("聚合方式", "SUM")).strip(),
            additivity=additivity,
            additive_dims=values.get("可加维度", ""),
            blocked_dims=values.get("禁止汇总维度"),
            rollup_agg=values.get("替代聚合"),
            source="manual" if values.get("口径来源") != "自动建议" else "auto",
            origin_suggestion=origin,
            replace=str(values.get("强制修订", "")).strip() == "是",
            remark=str(values.get("remark", "")).strip(),
        )
        if row is None:
            return None, errors
        if errors:  # 内容未变化、复用快照的提示
            return row, errors

        self._bound_store.channel.publish("caliber.registered", {
            "snap_id": row["snap_id"], "version_no": row["version_no"],
            "measure_name": measure_name, "fact_name": fact_name,
            "additivity": additivity, "additive_dims": _split_dims(row.get("可加维度")),
        })
        return row, []

    def register_suggestion(self, measure_name: str, fact_name: str) -> tuple[dict[str, Any] | None, list[str]]:
        """消费自动建议通道，为单个度量登记首版口径快照。"""
        suggestion = self._find_suggestion(measure_name, fact_name, consume=True)
        fact = self._find_fact(fact_name)
        if fact is None:
            return None, [f"事实表 {fact_name} 不存在，无法登记口径"]
        if suggestion is None:
            # 通道里没有建议时，用命名规则现场推断（仍标记为自动建议来源）
            suggestion = suggest_caliber(
                measure_name,
                fact_granularity=str(fact.get("粒度声明", "")),
                fact_dims=fact.get("关联维度", ""),
            )
        row, errors = self.registry.register(
            measure_name=measure_name,
            fact_name=fact_name,
            fact_granularity=str(fact.get("粒度声明", "")),
            agg_method=suggestion["agg_method"],
            additivity=suggestion["additivity"],
            additive_dims=suggestion["additive_dims"],
            blocked_dims=suggestion["blocked_dims"],
            rollup_agg=suggestion["rollup_agg"],
            source="auto",
            origin_suggestion=suggestion,
            remark=suggestion.get("reason", ""),
        )
        if row and not errors:
            self._bound_store.channel.publish("caliber.registered", {
                "snap_id": row["snap_id"], "version_no": row["version_no"],
                "measure_name": measure_name, "fact_name": fact_name,
                "additivity": suggestion["additivity"],
                "additive_dims": suggestion["additive_dims"],
                "source": "auto_suggest",
            })
        return row, errors

    def register_fact_suggestions(self, fact_name: str) -> dict[str, Any]:
        """为事实表的全部度量批量登记建议口径（事实发布后的标准入口）。"""
        fact = self._find_fact(fact_name)
        if fact is None:
            return {"ok": False, "message": f"事实表 {fact_name} 不存在", "items": []}
        measures = _split_dims(fact.get("度量字段"))
        items, failed = [], []
        for measure in measures:
            row, errors = self.register_suggestion(measure, fact_name)
            if row is None:
                failed.append({"measure_name": measure, "errors": errors})
            else:
                items.append({"snap_id": row["snap_id"], "measure_name": measure,
                              "note": errors[0] if errors else "registered"})
        return {"ok": not failed, "fact_name": fact_name, "items": items, "failed": failed}

    # ---------- 动作 ----------

    def list_entries(self, *, filters=None, keyword=None, status=None,
                     page: int = 1, size: int = 20):
        """覆盖基类：读写绑定的 store，保证测试注入实例与全局单例行为一致。"""
        rows = list(self._bound_store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [r for r in rows
                        if expected.casefold() in str(r.get(field, "")).casefold()]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [r for r in rows
                    if keyword.casefold() in str(r.get(field, "")).casefold()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        for row in self._bound_store.rows(self.MODULE):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "停用口径":
            entry["snapshot_status"] = "已停用"
            entry["status"] = "已停用"
            entry["effective_end"] = datetime.now(timezone.utc).isoformat()
            entry["pending"] = True
            entry["abnormal"] = True
            self._bound_store.channel.publish("caliber.deactivated", {
                "snap_id": entry["snap_id"], "measure_name": entry["度量名称"],
            })
            return entry, "口径快照已停用，历史版本保留可回查"
        if action == "重新生效":
            active = self.registry.effective_snapshot(
                entry["度量名称"], entry["所属事实"], entry.get("事实粒度"))
            if active is not None:
                return None, (
                    f"已存在生效口径 {active['snap_id']}，同一度量在事实粒度 "
                    f"{entry.get('事实粒度')} 范围内只能有一个生效口径"
                )
            entry["snapshot_status"] = "生效中"
            entry["status"] = "生效中"
            entry["effective_end"] = None
            entry["pending"] = False
            entry["abnormal"] = False
            self._bound_store.channel.publish("caliber.reactivated", {
                "snap_id": entry["snap_id"], "measure_name": entry["度量名称"],
            })
            return entry, "口径快照已重新生效"
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    # ---------- 领域查询 ----------

    def trial(self, measure_name: str, fact_name: str, target_dims: Any,
              agg_method: str | None = None) -> dict[str, Any]:
        return self.registry.trial(measure_name, fact_name, target_dims, agg_method)

    def resolve_for_query(self, measures: list[str], fact_name: str, target_dims: Any) -> dict[str, Any]:
        return self.registry.resolve_for_query(measures, fact_name, target_dims)

    def revise(self, snap_id: str, additive_dims: Any, remark: str = "") -> tuple[dict[str, Any] | None, list[str]]:
        row, errors = self.registry.revise_additive_dims(snap_id, additive_dims, remark)
        if row is not None and not errors:
            self._bound_store.channel.publish("caliber.revised", {
                "snap_id": row["snap_id"], "parent": row.get("parent_snap_id"),
                "measure_name": row["度量名称"], "additive_dims": _split_dims(row.get("可加维度")),
            })
        return row, errors

    def bind_report(self, report_name: str, measure_name: str, fact_name: str, snap_id: str) -> dict[str, Any]:
        ref = self.registry.bind_report(report_name, measure_name, fact_name, snap_id)
        self._bound_store.channel.publish("caliber.bound", {
            "report_name": report_name, "snap_id": snap_id, "measure_name": measure_name,
        })
        return ref

    def domain_snapshot(self) -> dict[str, Any]:
        # 不调用 super()：基类读全局 store，这里完全基于绑定 store 统计
        rows = self.registry.list_snapshots()
        by_status: dict[str, int] = {}
        by_additivity: dict[str, int] = {}
        active = 0
        conflicts = 0
        for row in rows:
            status = row.get("snapshot_status", "未知")
            by_status[status] = by_status.get(status, 0) + 1
            label = row.get("可加性类别", "未指定")
            by_additivity[label] = by_additivity.get(label, 0) + 1
            if row.get("snapshot_status") == "生效中":
                active += 1
            if row.get("conflict_with_suggestion"):
                conflicts += 1
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for r in rows if r.get("abnormal")),
            "additivity_distribution": by_additivity,
            "active_calibers": active,
            "historical_calibers": len(rows) - active,
            "conflict_with_suggestion": conflicts,
            "report_references": len(self._bound_store.rows("caliber_report_ref")),
        }

    # ---------- 内部工具 ----------

    def _find_fact(self, fact_name: str) -> dict[str, Any] | None:
        for row in self._bound_store.rows("fact_design"):
            if row.get("事实名称") == fact_name:
                return row
        return None

    def _fact_granularity(self, fact_name: str) -> str:
        fact = self._find_fact(fact_name)
        return str(fact.get("粒度声明", "")) if fact else ""

    def _find_suggestion(self, measure_name: str, fact_name: str,
                         consume: bool = False) -> dict[str, Any] | None:
        """从 measure.suggested 通道找回该度量的原始自动建议。"""
        messages = self._bound_store.channel.topic_snapshot("measure.suggested")
        if consume:
            self._bound_store.channel.consume("measure.suggested", self.MODULE)
        for msg in reversed(messages):
            payload = msg["payload"]
            if payload.get("measure_name") == measure_name and payload.get("fact_name") == fact_name:
                fact = self._find_fact(fact_name)
                return suggest_caliber(
                    measure_name,
                    fact_granularity=str(fact.get("粒度声明", "")) if fact else "",
                    fact_dims=fact.get("关联维度", "") if fact else "",
                )
        return None
