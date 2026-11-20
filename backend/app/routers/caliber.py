"""度量口径快照专用接口：批量登记建议、可加维度修订、汇总试算、报表绑定与历史回查。

通用资源接口负责列表/登记/动作，本路由承载需要多参数或返回结构化试算结果的口径领域操作。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.measure_caliber_snapshot import MeasureCaliberSnapshotService

router = APIRouter(prefix="/api/measure_caliber_snapshot", tags=["度量口径快照"])


def caliber_service() -> MeasureCaliberSnapshotService:
    service = service_for("measure_caliber_snapshot")
    assert isinstance(service, MeasureCaliberSnapshotService)
    return service


class FactSuggestionPayload(BaseModel):
    fact_name: str


class TrialPayload(BaseModel):
    measure_name: str
    fact_name: str
    target_dims: list[str] = Field(default_factory=list)
    agg_method: str | None = None


class QueryResolvePayload(BaseModel):
    fact_name: str
    measures: list[str]
    target_dims: list[str] = Field(default_factory=list)


class RevisePayload(BaseModel):
    snap_id: str
    additive_dims: list[str]
    remark: str = ""


class BindReportPayload(BaseModel):
    report_name: str
    measure_name: str
    fact_name: str
    snap_id: str


@router.post("/register-fact-suggestions")
def register_fact_suggestions(payload: FactSuggestionPayload) -> dict[str, Any]:
    """消费事实发布的度量建议，为事实表全部度量登记首版口径快照。"""
    return caliber_service().register_fact_suggestions(payload.fact_name)


@router.post("/trial")
def trial(payload: TrialPayload) -> dict[str, Any]:
    """按目标维度组合试算单个度量，返回放行/拦截结论与不允许汇总的维度组合。"""
    return caliber_service().trial(
        payload.measure_name, payload.fact_name, payload.target_dims, payload.agg_method
    )


@router.post("/resolve-query")
def resolve_query(payload: QueryResolvePayload) -> dict[str, Any]:
    """一次查询的口径解析：每个度量只采用一个适用口径，任一拦截则整单不可执行。"""
    return caliber_service().resolve_for_query(
        payload.measures, payload.fact_name, payload.target_dims
    )


@router.post("/revise")
def revise(payload: RevisePayload) -> dict[str, Any]:
    """修订生效口径的可加维度白名单，显式升版并停用旧快照。"""
    row, errors = caliber_service().revise(payload.snap_id, payload.additive_dims, payload.remark)
    return {"ok": row is not None and not errors, "entry": row, "errors": errors}


@router.post("/bind-report")
def bind_report(payload: BindReportPayload) -> dict[str, Any]:
    """报表按 snap_id 绑定口径快照，历史口径可随时回查，同名字段不会悄然替换。"""
    try:
        ref = caliber_service().bind_report(
            payload.report_name, payload.measure_name, payload.fact_name, payload.snap_id
        )
    except KeyError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "reference": ref}


@router.get("/report-references")
def report_references(snap_id: str | None = None) -> dict[str, Any]:
    """回查报表对口径快照的引用（含已停用的历史快照）。"""
    items = caliber_service().registry.report_references(snap_id)
    return {"items": items, "total": len(items)}


@router.get("/snapshots/{measure_name}")
def snapshots(measure_name: str, fact_name: str | None = None) -> dict[str, Any]:
    """列出某度量的全部口径版本，新老版本均可回查。"""
    items = caliber_service().registry.list_snapshots(measure_name, fact_name)
    return {"items": items, "total": len(items)}
