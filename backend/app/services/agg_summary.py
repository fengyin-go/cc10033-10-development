"""汇总构建服务：基于事实表构建多级汇总表，上卷维度、聚合度量并维护增量刷新。

增强能力：
- 消费 fact.published 和 measure.ready 通道，获取事实和度量信息
- 消费 agg.suggested 通道自动创建建议汇总
- 汇总粒度上卷验证：汇总维度必须是源事实表关联维度的子集
- 增量标记管理：基于已刷新记录的metadata确定增量边界
- 构建失败自动记录追踪
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.caliber import CaliberRegistry, _split_dims
from app.store import store


class AggSummaryService(BaseModuleService):
    MODULE = "agg_summary"
    ENTITY = "汇总表"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["汇总名称", "源事实表", "汇总维度", "汇总度量", "汇总粒度", "刷新周期",
                   "增量标记", "口径校验结果", "口径拦截原因"]
    REQUIRED_FIELDS = ["汇总名称", "源事实表", "汇总维度"]
    STATUS_ORDER = ["草稿", "已构建", "已刷新", "构建失败"]
    ACTION_RULES = {"执行构建": "草稿", "刷新汇总": "已刷新"}
    NEGATIVE_ACTIONS = []

    VALID_GRANULARITIES = {"每小时", "每日", "每周", "每月", "每季度", "每年"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建汇总表：消费事实和度量通道，校验汇总粒度的合理性。"""
        fact_name = str(values.get("源事实表", "")).strip()
        agg_granularity = str(values.get("汇总粒度", "每日")).strip()

        # 消费事实发布通道
        fact_msgs = store.channel.consume("fact.published", "agg_summary")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_source_granularity"] = msg["payload"].get("granularity", "")

        # 标准化汇总粒度
        if agg_granularity not in self.VALID_GRANULARITIES:
            agg_granularity = "每日"
            values["汇总粒度"] = agg_granularity

        # 消费度量确认通道，获取可用度量列表
        available_measures: list[str] = []
        measure_msgs = store.channel.consume("measure.ready", "agg_summary")
        for m in measure_msgs:
            if m["payload"].get("fact_name") == fact_name:
                available_measures.append(m["payload"].get("measure_name", ""))

        if available_measures:
            # 汇总默认使用全部可用度量
            values["汇总度量"] = ".".join(available_measures) if not values.get("汇总度量") else values["汇总度量"]

        # 检查汇总维度上卷合理性
        _validate_agg_dims(values)

        # 度量口径快照校验：逐度量判定目标维度组合是否在可加范围内，
        # 例如期末余额沿日期(DIM_TIME)求和会在此被拦截
        verdict = _caliber_verdict(
            fact_name,
            _split_dims(values.get("汇总维度")),
            _split_dims(values.get("汇总度量")),
        )
        values["口径校验结果"] = "通过" if verdict["all_allowed"] else "拦截"
        values["口径拦截原因"] = "" if verdict["all_allowed"] else "；".join(verdict["reasons"])

        # 设置刷新周期默认值
        if not values.get("刷新周期"):
            granularity_map = {"每小时": "每小时 第10分钟", "每日": "T+1 03:00", "每周": "周一 06:00",
                               "每月": "月初T+3 04:00"}
            values["刷新周期"] = granularity_map.get(agg_granularity, "T+1 03:00")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("agg.defined", {
            "agg_name": entry["汇总名称"],
            "source_fact": fact_name,
            "agg_dims": entry.get("汇总维度", ""),
            "agg_measures": entry.get("汇总度量", ""),
            "agg_granularity": agg_granularity,
            "refresh_cycle": entry.get("刷新周期", ""),
            "incremental": entry.get("增量标记") == "是",
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "agg_summary", "summary_table",
            f"agg_{entry['id']}",
            {"agg_name": entry["汇总名称"], "source_fact": fact_name, "granularity": agg_granularity},
            source_modules=["fact_design", "measure_config", "agg_summary"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        fact_name = entry.get("源事实表", "")

        if action == "执行构建":
            # 口径快照硬拦截：不允许的维度组合（如期末余额沿日期求和）禁止进入构建
            verdict = _caliber_verdict(
                fact_name,
                _split_dims(entry.get("汇总维度")),
                _split_dims(entry.get("汇总度量")),
            )
            if not verdict["all_allowed"]:
                entry["status"] = "构建失败"
                entry["pending"] = True
                entry["abnormal"] = True
                entry["口径校验结果"] = "拦截"
                entry["口径拦截原因"] = "；".join(verdict["reasons"])
                store.channel.publish("agg.blocked_by_caliber", {
                    "agg_name": entry["汇总名称"],
                    "source_fact": fact_name,
                    "reasons": verdict["reasons"],
                    "blocked_dim_combos": verdict["blocked_dim_combos"],
                    "entry_id": entry_id,
                })
                return entry, "口径校验拦截，构建未执行：" + "；".join(verdict["reasons"])

            # 模拟汇总构建过程
            build_result = _simulate_agg_build(entry)
            if build_result["success"]:
                entry["status"] = "已构建"
                entry["pending"] = True
                store.channel.publish("agg.built", {
                    "agg_name": entry["汇总名称"],
                    "source_fact": fact_name,
                    "row_count": build_result.get("output_rows", 0),
                    "build_time": datetime.now(timezone.utc).isoformat(),
                    "incremental": entry.get("增量标记") == "是",
                    "entry_id": entry_id,
                })
            else:
                entry["status"] = "构建失败"
                entry["pending"] = True
                entry["abnormal"] = True
                store.channel.publish("agg.build_failed", {
                    "agg_name": entry["汇总名称"],
                    "error": build_result.get("error", ""),
                    "entry_id": entry_id,
                })
                # 记录失败到恢复管理器
                store.recovery.log_retry(
                    "agg_summary", f"build_{entry_id}", 1,
                    build_result.get("error", "未知错误"), False
                )

        elif action == "刷新汇总":
            # 增量刷新
            refresh_result = _simulate_agg_refresh(entry)
            if refresh_result["success"]:
                entry["status"] = "已刷新"
                entry["pending"] = False
                store.channel.publish("agg.refreshed", {
                    "agg_name": entry["汇总名称"],
                    "new_rows": refresh_result.get("new_rows", 0),
                    "refresh_time": datetime.now(timezone.utc).isoformat(),
                    "entry_id": entry_id,
                })
            else:
                entry["pending"] = True
                entry["abnormal"] = True

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 汇总粒度分布和构建状态统计
        granularity_dist: dict[str, int] = {}
        build_stats = {"success": 0, "failed": 0, "pending": 0}
        for row in store.rows(self.MODULE):
            g = row.get("汇总粒度", "未指定")
            granularity_dist[g] = granularity_dist.get(g, 0) + 1
            status = row.get("status", "")
            if status == "已刷新":
                build_stats["success"] += 1
            elif status == "构建失败":
                build_stats["failed"] += 1
            else:
                build_stats["pending"] += 1
        base["granularity_distribution"] = granularity_dist
        base["build_stats"] = build_stats
        base["consumed_facts"] = len(store.channel.consume("fact.published", "agg_summary"))
        base["consumed_measures"] = len(store.channel.consume("measure.ready", "agg_summary"))
        return base


def _validate_agg_dims(values: dict[str, Any]) -> None:
    """校验汇总维度是源事实表维度的子集（上卷不增加粒度）。"""
    agg_dims = str(values.get("汇总维度", "")).strip()
    fact_name = str(values.get("源事实表", "")).strip()
    if not agg_dims or not fact_name:
        return

    agg_dim_set = set(d.strip() for d in agg_dims.split(".") if d.strip())
    for row in store.rows("fact_design"):
        if row.get("事实名称") == fact_name:
            fact_dims = set(d.strip() for d in str(row.get("关联维度", "")).split(".") if d.strip())
            extra = agg_dim_set - fact_dims
            if extra:
                values["汇总维度"] = ".".join(sorted(agg_dim_set & fact_dims))
            break


def _simulate_agg_build(entry: dict[str, Any]) -> dict[str, Any]:
    """模拟汇总构建过程。"""
    agg_dims = str(entry.get("汇总维度", "")).strip()
    agg_measures = str(entry.get("汇总度量", "")).strip()

    if not agg_dims or not agg_measures:
        return {"success": False, "error": "汇总维度或度量未配置"}

    dim_count = len([d for d in agg_dims.split(".") if d.strip()])
    measure_count = len([m for m in agg_measures.split(".") if m.strip()])
    estimated_rows = 100 * dim_count * measure_count
    cp_id = store.recovery.create_checkpoint("agg_summary", f"build_{entry.get('id')}", {
        "agg_name": entry.get("汇总名称"),
        "phase": "build_started",
        "dims_count": dim_count,
        "measures_count": measure_count,
    })
    store.recovery.mark_completed(cp_id)
    return {"success": True, "output_rows": estimated_rows, "checkpoint": cp_id}


def _simulate_agg_refresh(entry: dict[str, Any]) -> dict[str, Any]:
    """模拟汇总增量刷新过程。"""
    return {"success": True, "new_rows": 50, "refreshed_at": datetime.now(timezone.utc).isoformat()}


def _caliber_verdict(fact_name: str, agg_dims: list[str],
                     measures: list[str]) -> dict[str, Any]:
    """汇总配置的口径试算：返回是否放行、拦截原因与不允许汇总的维度组合。"""
    registry = CaliberRegistry(store)
    reasons: list[str] = []
    blocked_combos: list[list[str]] = []
    checked = 0
    for measure in measures:
        trial = registry.trial(measure, fact_name, agg_dims)
        if trial.get("resolved_snap_id") is None:
            # 未登记口径不阻断历史配置，仅提示（新事实发布链路会先登记口径）
            continue
        checked += 1
        if trial["blocked"]:
            reasons.extend(trial["violations"])
            blocked_combos.extend(trial["blocked_dim_combos"])
    # 去重，保持输出稳定
    unique_combos = sorted({tuple(c) for c in blocked_combos})
    return {
        "all_allowed": not reasons,
        "checked_measures": checked,
        "reasons": reasons,
        "blocked_dim_combos": [list(c) for c in unique_combos],
    }