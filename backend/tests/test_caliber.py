"""度量口径快照测试：覆盖口径建议、版本登记、可加性校验、汇总拦截与历史回查。

验证主线（对应需求验收）：
- 用每日余额（半可加）和交易额（全可加）验证可加规则；
- 提交原始度量建议 → 修订可加维度并再次试算 → 输出不允许汇总的维度组合；
- 同一粒度范围内唯一生效口径；历史口径被报表引用后可回查、同名不悄然替换。
"""
from __future__ import annotations

import pytest

from app.services.caliber import (
    FULLY_ADDITIVE,
    NON_ADDITIVE,
    SEMI_ADDITIVE,
    CaliberRegistry,
    _split_dims,
    suggest_caliber,
)
from app.services.measure_caliber_snapshot import MeasureCaliberSnapshotService
from app.store import Store

BALANCE_FACT = {
    "id": 9001, "status": "已发布", "pending": False, "abnormal": False,
    "事实名称": "账户日余额事实", "业务过程": "账户日终余额快照",
    "粒度声明": "每个账户每日一条余额",
    "关联维度": "DIM_TIME.DIM_PRODUCT.DIM_STORE",
    "度量字段": "eod_bal_amt.txn_amt", "分区字段": "dt DATE", "更新模式": "每日全量",
}
TXN_FACT = {
    "id": 9002, "status": "已发布", "pending": False, "abnormal": False,
    "事实名称": "支付交易事实", "业务过程": "支付交易流水",
    "粒度声明": "每笔支付交易明细行",
    "关联维度": "DIM_TIME.DIM_PRODUCT.DIM_STORE",
    "度量字段": "txn_amt", "分区字段": "dt DATE", "更新模式": "每日追加",
}


@pytest.fixture
def store() -> Store:
    s = Store()
    s.rows("fact_design").append(dict(BALANCE_FACT))
    s.rows("fact_design").append(dict(TXN_FACT))
    return s


@pytest.fixture
def registry(store: Store) -> CaliberRegistry:
    return CaliberRegistry(store)


# ======================================================================
# Section A: 原始度量建议（自动推断可加性）
# ======================================================================

class TestSuggestion:
    def test_balance_measure_is_semi_additive(self) -> None:
        """期末余额类度量建议为半可加，日期维度进入禁止集合。"""
        suggestion = suggest_caliber(
            "eod_bal_amt", fact_granularity="每个账户每日一条余额",
            fact_dims="DIM_TIME.DIM_PRODUCT.DIM_STORE",
        )
        assert suggestion["additivity"] == SEMI_ADDITIVE
        assert "DIM_TIME" in suggestion["blocked_dims"]
        assert "DIM_TIME" not in suggestion["additive_dims"]
        assert suggestion["rollup_agg"] == "LAST_VALUE"

    def test_transaction_amount_is_fully_additive(self) -> None:
        """交易额类流水度量建议为全可加，所有维度可 SUM。"""
        suggestion = suggest_caliber(
            "txn_amt", fact_granularity="每笔支付交易明细行",
            fact_dims="DIM_TIME.DIM_PRODUCT.DIM_STORE",
        )
        assert suggestion["additivity"] == FULLY_ADDITIVE
        assert suggestion["agg_method"] == "SUM"
        assert suggestion["blocked_dims"] == []

    def test_rate_measure_is_non_additive(self) -> None:
        suggestion = suggest_caliber("return_rate", fact_dims="DIM_TIME.DIM_STORE")
        assert suggestion["additivity"] == NON_ADDITIVE
        assert suggestion["agg_method"] == "AVG"


# ======================================================================
# Section B: 口径版本快照与生效唯一性
# ======================================================================

class TestCaliberVersioning:
    def test_register_first_snapshot(self, registry: CaliberRegistry) -> None:
        row, errors = registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_PRODUCT", "DIM_STORE"],
        )
        assert errors == []
        assert row["version_no"] == 1
        assert row["snapshot_status"] == "生效中"
        assert _split_dims(row["禁止汇总维度"]) == ["DIM_TIME"]
        assert row["content_hash"]

    def test_duplicate_effective_caliber_rejected(self, registry: CaliberRegistry) -> None:
        """同一度量在同一事实粒度范围内只能有一个生效口径，重复登记被拒。"""
        kwargs = dict(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_PRODUCT", "DIM_STORE"],
        )
        first, errors = registry.register(**kwargs)
        assert first is not None and errors == []
        second, errors = registry.register(**kwargs)
        assert second is None
        assert any("已存在生效口径" in e for e in errors)
        # 旧快照仍在，且仍是唯一生效口径
        active = [r for r in registry.list_snapshots("eod_bal_amt") if r["snapshot_status"] == "生效中"]
        assert len(active) == 1

    def test_revise_creates_new_version_and_keeps_history(self, registry: CaliberRegistry) -> None:
        """显式修订升版：旧版本停用但保留，可按 snap_id 回查。"""
        v1, _ = registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_PRODUCT", "DIM_STORE"],
        )
        v2, errors = registry.revise_additive_dims(
            v1["snap_id"], ["DIM_PRODUCT"], remark="门店维度不再允许余额相加"
        )
        assert errors == []
        assert v2["version_no"] == 2
        assert v2["parent_snap_id"] == v1["snap_id"]
        assert registry.get_snapshot(v1["snap_id"])["snapshot_status"] == "已停用"
        assert registry.effective_snapshot("eod_bal_amt", "账户日余额事实")["snap_id"] == v2["snap_id"]
        # 历史快照仍可回查
        history = registry.list_snapshots("eod_bal_amt")
        assert {r["version_no"] for r in history} == {1, 2}

    def test_semi_additive_cannot_include_time_dim(self, registry: CaliberRegistry) -> None:
        """半可加口径把日期维度放进可加白名单必须被拒绝。"""
        row, errors = registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_TIME", "DIM_STORE"],
        )
        assert row is None
        assert any("日期/时间维度" in e for e in errors)

    def test_same_named_measure_on_other_fact_is_independent(self, registry: CaliberRegistry) -> None:
        """不同事实/粒度下的同名字段是独立口径，互不替换。"""
        registry.register(
            measure_name="txn_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=FULLY_ADDITIVE, additive_dims=["DIM_TIME", "DIM_PRODUCT", "DIM_STORE"],
        )
        row, errors = registry.register(
            measure_name="txn_amt", fact_name="支付交易事实",
            fact_granularity="每笔支付交易明细行", agg_method="SUM",
            additivity=FULLY_ADDITIVE, additive_dims=["DIM_TIME", "DIM_PRODUCT", "DIM_STORE"],
        )
        assert errors == [] and row is not None
        snap_a = registry.effective_snapshot("txn_amt", "账户日余额事实")
        snap_b = registry.effective_snapshot("txn_amt", "支付交易事实")
        assert snap_a["snap_id"] != snap_b["snap_id"]


# ======================================================================
# Section C: 报表引用与历史回查
# ======================================================================

class TestReportBinding:
    def test_report_binds_explicit_snapshot_and_keeps_it_after_revision(
        self, registry: CaliberRegistry, store: Store
    ) -> None:
        v1, _ = registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_PRODUCT", "DIM_STORE"],
        )
        ref = registry.bind_report("门店余额日报", "eod_bal_amt", "账户日余额事实", v1["snap_id"])
        assert ref["bound_snap_id"] == v1["snap_id"]

        # 口径修订后，报表仍绑定旧快照，旧版本可回查，没有被悄然替换
        registry.revise_additive_dims(v1["snap_id"], ["DIM_PRODUCT"])
        refs = registry.report_references(v1["snap_id"])
        assert len(refs) == 1 and refs[0]["报表名称"] == "门店余额日报"
        assert registry.get_snapshot(v1["snap_id"])["content_hash"] == ref["content_hash"]

    def test_bind_unknown_snapshot_rejected(self, registry: CaliberRegistry) -> None:
        with pytest.raises(KeyError):
            registry.bind_report("报表X", "eod_bal_amt", "账户日余额事实", "CAL-not-exist")


# ======================================================================
# Section D: 可加规则试算（每日余额 vs 交易额）
# ======================================================================

class TestTrial:
    @pytest.fixture
    def registered(self, registry: CaliberRegistry) -> CaliberRegistry:
        registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_PRODUCT", "DIM_STORE"],
        )
        registry.register(
            measure_name="txn_amt", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=FULLY_ADDITIVE,
            additive_dims=["DIM_TIME", "DIM_PRODUCT", "DIM_STORE"],
        )
        return registry

    def test_balance_sum_along_date_blocked(self, registered: CaliberRegistry) -> None:
        """期末余额沿日期求和必须被拦截，并给出不允许的维度组合。"""
        trial = registered.trial("eod_bal_amt", "账户日余额事实", ["DIM_TIME", "DIM_STORE"])
        assert trial["blocked"] is True
        assert ["DIM_TIME"] in trial["blocked_dim_combos"]
        assert trial["effective_agg"] == "LAST_VALUE"
        assert any("DIM_TIME" in v for v in trial["violations"])

    def test_balance_sum_without_date_allowed(self, registered: CaliberRegistry) -> None:
        """同一余额度量在门店/产品维度（白名单内）允许汇总。"""
        trial = registered.trial("eod_bal_amt", "账户日余额事实", ["DIM_STORE", "DIM_PRODUCT"])
        assert trial["allowed"] is True
        assert trial["resolved_snap_id"].startswith("CAL-eod_bal_amt")

    def test_transaction_amount_sum_always_allowed(self, registered: CaliberRegistry) -> None:
        """交易额沿日期求和允许（流水可加）。"""
        trial = registered.trial("txn_amt", "账户日余额事实", ["DIM_TIME", "DIM_STORE"])
        assert trial["allowed"] is True
        assert trial["additivity"] == FULLY_ADDITIVE

    def test_trial_without_caliber_blocked(self, registry: CaliberRegistry) -> None:
        trial = registry.trial("ghost_measure", "账户日余额事实", ["DIM_TIME"])
        assert trial["blocked"] is True
        assert "未登记生效口径" in trial["reason"]

    def test_revise_then_retrial_outputs_blocked_combos(self, registered: CaliberRegistry) -> None:
        """修订可加维度后再次试算：原先允许的门店组合变为不允许并输出组合。"""
        snap = registered.effective_snapshot("eod_bal_amt", "账户日余额事实")
        # 修订：可加白名单从 产品+门店 收缩为仅产品
        v2, errors = registered.revise_additive_dims(snap["snap_id"], ["DIM_PRODUCT"])
        assert errors == []
        trial = registered.trial("eod_bal_amt", "账户日余额事实", ["DIM_STORE", "DIM_PRODUCT"])
        assert trial["blocked"] is True
        assert trial["resolved_version_no"] == v2["version_no"]
        flat = [c for combo in trial["blocked_dim_combos"] for c in combo]
        assert "DIM_STORE" in flat

    def test_revise_flags_conflict_against_origin_suggestion(
        self, store: Store
    ) -> None:
        """人工修订与首版自动建议不一致时，新生效快照显式标记冲突。"""
        service = MeasureCaliberSnapshotService(store)
        result = service.register_fact_suggestions("账户日余额事实")
        assert result["ok"]
        v1 = service.registry.effective_snapshot("eod_bal_amt", "账户日余额事实")
        assert v1["conflict_with_suggestion"] is False
        # 自动建议可加维度含 DIM_STORE，人工修订将其移除 → 冲突
        v2, errors = service.registry.revise_additive_dims(v1["snap_id"], ["DIM_PRODUCT"])
        assert errors == []
        assert v2["conflict_with_suggestion"] is True
        assert v2["origin_suggestion"] is not None
        assert v2["origin_suggestion"]["additivity"] == SEMI_ADDITIVE

    def test_resolve_query_uses_one_caliber_per_measure(self, registered: CaliberRegistry) -> None:
        """一次查询中每个度量只解析出一个适用口径，存在拦截则整单不可执行。"""
        resolution = registered.resolve_for_query(
            ["eod_bal_amt", "txn_amt"], "账户日余额事实", ["DIM_TIME", "DIM_STORE"]
        )
        assert resolution["all_allowed"] is False
        assert resolution["blocked_count"] == 1
        snap_ids = [t["resolved_snap_id"] for t in resolution["trials"]]
        assert len([s for s in snap_ids if s]) == 2 and len(set(snap_ids)) == 2
        assert ["DIM_TIME"] in resolution["blocked_dim_combos"]

    def test_non_additive_measure_blocked_on_every_sum(self, registry: CaliberRegistry) -> None:
        """不可加度量在任何维度组合下 SUM 都被拦截。"""
        registry.register(
            measure_name="return_rate", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="AVG",
            additivity=NON_ADDITIVE, additive_dims=[],
        )
        trial = registry.trial(
            "return_rate", "账户日余额事实", ["DIM_PRODUCT"], agg_method="SUM")
        assert trial["blocked"] is True
        assert "不可加口径" in trial["violations"][0]

    def test_non_additive_register_rejects_sum(self, registry: CaliberRegistry) -> None:
        """不可加口径登记时若聚合方式写 SUM，直接拒绝。"""
        row, errors = registry.register(
            measure_name="bad_rate", fact_name="账户日余额事实",
            fact_granularity="每个账户每日一条余额", agg_method="SUM",
            additivity=NON_ADDITIVE, additive_dims=[],
        )
        assert row is None
        assert any("不可加度量不能使用SUM" in e for e in errors)


# ======================================================================
# Section E: 模块服务与通道集成
# ======================================================================

class TestServiceIntegration:
    """跨服务集成走全局 store（agg_summary/star_query 单例绑定全局 store），
    事实名唯一，重复执行因内容指纹复用而保持幂等。"""

    def _ensure_facts(self) -> None:
        from app.store import store as global_store
        existing = {r.get("事实名称") for r in global_store.rows("fact_design")}
        for fact in (BALANCE_FACT, TXN_FACT):
            if fact["事实名称"] not in existing:
                global_store.rows("fact_design").append(dict(fact))

    def test_register_fact_suggestions(self) -> None:
        """事实发布链路：批量把度量建议登记为首版口径快照。"""
        self._ensure_facts()
        service = MeasureCaliberSnapshotService()
        before = len(global_channel_snapshot("caliber.registered"))
        result = service.register_fact_suggestions("账户日余额事实")
        assert result["ok"] is True
        snap = service.registry.effective_snapshot("eod_bal_amt", "账户日余额事实")
        assert snap is not None and snap["additivity_code"] == SEMI_ADDITIVE
        txn = service.registry.effective_snapshot("txn_amt", "账户日余额事实")
        assert txn["additivity_code"] == FULLY_ADDITIVE
        # 首次登记发布两条事件；重复执行因指纹复用不再产生新事件
        after = global_channel_snapshot("caliber.registered")
        assert len(after) - before in (0, 2)

    def test_manual_rule_conflict_flags_snapshot(self) -> None:
        """人工规则与自动建议冲突时，生效快照显式标明冲突并记录事实粒度/可加维度。"""
        self._ensure_facts()
        service = MeasureCaliberSnapshotService()
        service.register_fact_suggestions("账户日余额事实")
        snap = service.registry.effective_snapshot("eod_bal_amt", "账户日余额事实")
        # 人工把余额改为全可加（与自动建议的半可加冲突）
        row, errors = service.registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity=snap["事实粒度"], agg_method="SUM",
            additivity=FULLY_ADDITIVE,
            additive_dims=["DIM_TIME", "DIM_PRODUCT", "DIM_STORE"],
            source="manual", origin_suggestion={"additivity": SEMI_ADDITIVE, "additive_dims": []},
            replace=True,
        )
        assert errors == []
        assert row["conflict_with_suggestion"] is True
        assert row["事实粒度"] == "每个账户每日一条余额"

        # 恢复为自动建议的半可加口径，保证后续测试的可加规则不被人工覆盖污染
        restored, rest_errors = service.registry.register(
            measure_name="eod_bal_amt", fact_name="账户日余额事实",
            fact_granularity=row["事实粒度"], agg_method="SUM",
            additivity=SEMI_ADDITIVE, additive_dims=["DIM_PRODUCT", "DIM_STORE"],
            source="manual", replace=True, remark="测试后恢复半可加口径",
        )
        assert rest_errors == [] and restored["additivity_code"] == SEMI_ADDITIVE

    def test_agg_summary_blocks_balance_sum_over_date(self) -> None:
        """汇总构建集成：期末余额沿日期求和的配置在执行构建时被拦下。"""
        from app.services import SERVICES

        self._ensure_facts()
        MeasureCaliberSnapshotService().register_fact_suggestions("账户日余额事实")
        agg_service = SERVICES["agg_summary"]
        entry, missing = agg_service.create_entry({
            "汇总名称": "余额沿日期求和汇总", "源事实表": "账户日余额事实",
            "汇总维度": "DIM_TIME.DIM_STORE", "汇总度量": "eod_bal_amt",
            "汇总粒度": "每日",
        })
        assert missing == []
        assert entry["口径校验结果"] == "拦截"
        result, message = agg_service.run_action(entry["id"], "执行构建")
        assert result["status"] == "构建失败"
        assert "口径校验拦截" in message
        blocked = global_channel_snapshot("agg.blocked_by_caliber")
        assert blocked and blocked[-1]["payload"]["blocked_dim_combos"] == [["DIM_TIME"]]

    def test_agg_summary_allows_transaction_sum(self) -> None:
        """同一汇总维度下，交易额求和正常放行并构建成功。"""
        from app.services import SERVICES

        self._ensure_facts()
        MeasureCaliberSnapshotService().register_fact_suggestions("支付交易事实")
        agg_service = SERVICES["agg_summary"]
        entry, _ = agg_service.create_entry({
            "汇总名称": "交易额日汇总", "源事实表": "支付交易事实",
            "汇总维度": "DIM_TIME.DIM_STORE", "汇总度量": "txn_amt",
            "汇总粒度": "每日",
        })
        assert entry["口径校验结果"] == "通过"
        result, message = agg_service.run_action(entry["id"], "执行构建")
        assert result["status"] == "已构建"

    def test_star_query_blocked_by_caliber(self) -> None:
        """星型查询集成：含不可汇总维度组合的查询验证失败、不可发布。"""
        from app.services import SERVICES

        self._ensure_facts()
        MeasureCaliberSnapshotService().register_fact_suggestions("账户日余额事实")
        query_service = SERVICES["star_query"]
        entry, _ = query_service.create_entry({
            "查询名称": "余额按日期查询", "事实表": "账户日余额事实",
            "关联维度": "DIM_TIME.DIM_STORE", "度量选择": "eod_bal_amt",
            "分组维度": "DIM_TIME.DIM_STORE",
        })
        result, message = query_service.run_action(entry["id"], "验证查询")
        assert result is None
        assert "口径校验未通过" in message
        assert global_channel_snapshot("query.blocked_by_caliber")

    def test_domain_snapshot_reports_versions_and_refs(self) -> None:
        self._ensure_facts()
        service = MeasureCaliberSnapshotService()
        service.register_fact_suggestions("账户日余额事实")
        snap = service.registry.effective_snapshot("eod_bal_amt", "账户日余额事实")
        assert snap is not None
        service.bind_report("余额日报", "eod_bal_amt", "账户日余额事实", snap["snap_id"])
        summary = service.domain_snapshot()
        assert summary["active_calibers"] >= 2
        assert summary["report_references"] >= 1
        assert "半可加" in summary["additivity_distribution"]


class TestBoundStoreIsolation:
    """注入独立 Store 的服务实例：list/get/统计都只读写该实例，不污染全局。"""

    def test_service_uses_injected_store_end_to_end(self, store: Store) -> None:
        from app.store import store as global_store
        before = len(global_store.rows("measure_caliber_snapshot"))
        service = MeasureCaliberSnapshotService(store)
        result = service.register_fact_suggestions("账户日余额事实")
        assert result["ok"] is True
        # 列表与单条查询走绑定 store
        items, total = service.list_entries(page=1, size=10)
        assert total == 2 and len(items) == 2
        assert service.get_entry(items[0]["id"]) is not None
        # 注入实例不向全局 store 写入
        assert len(global_store.rows("measure_caliber_snapshot")) == before
        # 统计同样基于绑定 store
        assert service.domain_snapshot()["count"] == 2


def global_channel_snapshot(topic: str):
    from app.store import store as global_store
    return global_store.channel.topic_snapshot(topic)
