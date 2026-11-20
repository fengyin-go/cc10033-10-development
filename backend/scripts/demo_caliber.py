#!/usr/bin/env python3
"""口径快照端到端演示（可复算脚本）。

场景：
  事实「账户日余额事实」含两个度量
    - eod_bal_amt 每日期末余额（半可加：日期维度不可加）
    - txn_amt     交易额流水（全可加：日期维度可加）

流程：
  1. 事实发布 → 提交原始度量建议并登记首版口径快照；
  2. 用每日余额和交易额分别试算，验证可加规则；
  3. 人工修订可加维度（把 DIM_STORE 移出白名单）后再次试算；
  4. 输出不允许汇总的维度组合；
  5. 报表按 snap_id 绑定历史口径，验证同名不被悄然替换、历史可回查。

运行：python3 scripts/demo_caliber.py
"""
from __future__ import annotations

import json
import os
import sys

# -I 隔离模式下 PYTHONPATH 不生效，显式把 backend 根目录加入模块搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.measure_caliber_snapshot import MeasureCaliberSnapshotService
from app.store import Store


def show(title: str, payload: object) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    store = Store()

    # --- 事实登记：每个账户每日一条余额，关联 时间/产品/门店 三个维度 ---
    store.rows("fact_design").append({
        "id": 1, "status": "已发布", "pending": False, "abnormal": False,
        "事实名称": "账户日余额事实", "业务过程": "账户日终余额快照",
        "粒度声明": "每个账户每日一条余额",
        "关联维度": "DIM_TIME.DIM_PRODUCT.DIM_STORE",
        "度量字段": "eod_bal_amt.txn_amt", "分区字段": "dt DATE", "更新模式": "每日全量",
    })

    service = MeasureCaliberSnapshotService(store)

    # 1) 事实发布 → 批量提交原始度量建议，沉淀为首版口径快照
    registered = service.register_fact_suggestions("账户日余额事实")
    show("① 原始度量建议登记结果（首版口径快照）", registered)

    snapshots = service.registry.list_snapshots()
    show("① 口径快照登记（含事实粒度 / 可加维度 / 禁止维度 / 内容指纹）", [
        {
            "snap_id": s["snap_id"], "版本": s["version_no"],
            "度量": s["度量名称"], "事实粒度": s["事实粒度"],
            "可加性": s["可加性类别"], "聚合方式": s["聚合方式"],
            "可加维度": s["可加维度"], "禁止汇总维度": s["禁止汇总维度"],
            "替代聚合": s["替代聚合"], "状态": s["snapshot_status"],
            "content_hash": s["content_hash"],
        }
        for s in snapshots
    ])

    # 2) 可加规则验证：同一维度组合 DIM_TIME + DIM_STORE 下，余额被拦、交易额放行
    balance_trial = service.trial(
        "eod_bal_amt", "账户日余额事实", ["DIM_TIME", "DIM_STORE"])
    txn_trial = service.trial(
        "txn_amt", "账户日余额事实", ["DIM_TIME", "DIM_STORE"])
    show("② 试算【每日期末余额】沿日期+门店汇总", {
        "allowed": balance_trial["allowed"],
        "生效口径": balance_trial["resolved_snap_id"],
        "请求聚合": balance_trial["requested_agg"],
        "实际可用聚合": balance_trial["effective_agg"],
        "拦截原因": balance_trial["violations"],
        "不允许汇总的维度组合": balance_trial["blocked_dim_combos"],
    })
    show("② 试算【交易额】沿日期+门店汇总（流水可加，应放行）", {
        "allowed": txn_trial["allowed"],
        "生效口径": txn_trial["resolved_snap_id"],
        "实际可用聚合": txn_trial["effective_agg"],
    })

    # 3) 报表先绑定 v1 口径，再人工修订可加维度（门店移出白名单）
    v1 = service.registry.effective_snapshot("eod_bal_amt", "账户日余额事实")
    ref = service.bind_report("门店余额日报", "eod_bal_amt", "账户日余额事实", v1["snap_id"])
    print(f"\n③ 报表「门店余额日报」已绑定口径 {ref['bound_snap_id']}（v{ref['bound_version_no']}）")

    v2, errors = service.revise(
        v1["snap_id"], ["DIM_PRODUCT"], remark="人工修订：门店维度不再允许余额相加")
    show("③ 修订可加维度后生成的新生效快照", {
        "new_snap_id": v2["snap_id"], "版本": v2["version_no"],
        "parent": v2["parent_snap_id"], "可加维度": v2["可加维度"],
        "禁止汇总维度": v2["禁止汇总维度"],
        "与自动建议冲突": v2["conflict_with_suggestion"],
        "首版自动建议": v2["origin_suggestion"],
        "errors": errors,
    })

    # 4) 修订后再次试算：原门店组合现在也被拦截
    retrial = service.trial(
        "eod_bal_amt", "账户日余额事实", ["DIM_STORE", "DIM_PRODUCT"])
    show("④ 修订后再次试算（门店+产品）", {
        "allowed": retrial["allowed"],
        "生效口径版本": retrial["resolved_version_no"],
        "不允许汇总的维度组合": retrial["blocked_dim_combos"],
        "拦截原因": retrial["violations"],
    })

    # 5) 一次查询只能采用一个适用口径
    resolution = service.resolve_for_query(
        ["eod_bal_amt", "txn_amt"], "账户日余额事实", ["DIM_TIME", "DIM_STORE"])
    show("⑤ 查询级口径解析（每度量一个口径，任一拦截则整单不可执行）", {
        "all_allowed": resolution["all_allowed"],
        "blocked_count": resolution["blocked_count"],
        "各度量采用口径": [
            {"度量": t["measure_name"], "snap_id": t["resolved_snap_id"],
             "allowed": t["allowed"]}
            for t in resolution["trials"]
        ],
        "不允许汇总的维度组合": resolution["blocked_dim_combos"],
    })

    # 6) 历史口径回查：报表仍指向 v1，同名快照没有被悄然替换
    history = service.registry.list_snapshots("eod_bal_amt", "账户日余额事实")
    refs = service.registry.report_references(v1["snap_id"])
    show("⑥ 历史版本与报表引用回查", {
        "全部版本": [
            {"snap_id": s["snap_id"], "版本": s["version_no"], "状态": s["snapshot_status"],
             "可加维度": s["可加维度"], "content_hash": s["content_hash"]}
            for s in history
        ],
        "报表仍绑定的历史快照": refs,
        "当前生效快照": service.registry.effective_snapshot(
            "eod_bal_amt", "账户日余额事实")["snap_id"],
    })

    print("\n演示完成。")


if __name__ == "__main__":
    main()
