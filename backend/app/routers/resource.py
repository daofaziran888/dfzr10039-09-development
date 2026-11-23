"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


# 固定路径必须注册在 /{module}/{entry_id} 之前，否则会被动态段吞掉。
def _as_at(value: Any) -> str | None:
    """Query 默认值在直接调用处理函数时是对象，统一收敛成可选字符串。"""
    return value if isinstance(value, str) and value.strip() else None


@router.get("/rule-release/eligibility")
def release_eligibility(
    job_id: str | None = Query(default=None),
    target_site: str | None = Query(default=None),
    at: str | None = Query(default=None),
) -> dict[str, Any]:
    """回执生效资格：切换前后按作业或站点查，每张回执给出生效判定。"""
    from app import release_flow

    items = release_flow.list_receipt_eligibility(
        job_id=_as_at(job_id), target_site=_as_at(target_site), at=_as_at(at))
    return {"items": items, "total": len(items)}


@router.get("/rule-release/jobs/{job_id}/inspect")
def inspect_job_flow(
    job_id: str,
    at: str | None = Query(default=None),
) -> dict[str, Any]:
    """查一个作业的版本资格、各版本解析日志和候选/验收明细。"""
    from app import release_flow

    return release_flow.inspect_job(job_id, at=_as_at(at))


@router.post("/rule-release/jobs/{job_id}/parse")
def parse_job_pages(
    job_id: str,
    payload: EntryPayload,
    at: str | None = Query(default=None),
) -> ActionResult:
    """解析端按发布回执认领网页并登记实用规则版本，返回逐页结果。"""
    from app import release_flow

    values = payload.values
    pages = [
        part.strip() for part in str(values.get("页面", "")).split(",") if part.strip()
    ]
    try:
        job = release_flow.job_from_store(job_id)
        parsed, skipped = release_flow.parse_claimed_pages(
            job, pages=pages or None, at=_as_at(at))
    except release_flow.ReleaseFlowError as exc:
        return ActionResult(ok=False, message=str(exc))
    detail = {
        "已解析": [
            {"来源URL": log.source_url, "规则版本": log.rule_version,
             "发布回执": log.release_receipt_id}
            for log in parsed
        ],
        "跳过": [{"来源URL": url, "原因": reason} for url, reason in skipped],
    }
    message = f"已认领 {len(parsed)} 个页面，跳过 {len(skipped)} 个"
    return ActionResult(ok=True, message=message, entry=detail)


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, problems = service.create_entry(payload.values)
    if problems:
        if all(problem in service.REQUIRED_FIELDS for problem in problems):
            return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(problems)}")
        return ActionResult(ok=False, message=f"登记未通过业务校验：{'；'.join(problems)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
