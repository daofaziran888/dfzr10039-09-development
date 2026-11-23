"""批次验收领域服务：核对解析日志口径，签发或拒绝可交付凭据。"""
from __future__ import annotations

from typing import Any

from app import release_flow
from app.services.base import BaseModuleService
from app.store import store


class BatchAcceptanceService(BaseModuleService):
    MODULE = "batch_acceptance"
    ENTITY = "批次验收"
    OPERATION_TERM = "验收"
    LIST_FIELDS = [
        "验收编号", "候选批编号", "来源作业", "可交付凭据", "验收结论",
        "口径指纹", "规则版本", "数据行数", "验收时间",
    ]
    REQUIRED_FIELDS = ["候选批编号"]
    STATUS_ORDER = ["可交付", "不可交付", "待复核"]
    ACTION_RULES = {"复核验收": "待复核", "重新验收": "可交付"}
    NEGATIVE_ACTIONS = ["确认不可交付"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "复核验收":
            entry["status"] = "待复核"
            entry["pending"] = True
            entry["abnormal"] = False
            return entry, f"{self.ENTITY}已复核验收"
        if action == "重新验收":
            candidate_id = str(entry.get("候选批编号", ""))
            try:
                acceptance = release_flow.accept_candidate_batch(candidate_id)
            except release_flow.ReleaseFlowError as exc:
                return None, str(exc)
            verdict = "可一起交付" if acceptance.deliverable else f"不可交付：{acceptance.reason}"
            latest = _latest_row(acceptance.acceptance_id) or entry
            return latest, f"{self.ENTITY}已重新核对，{verdict}"
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        try:
            acceptance = release_flow.accept_candidate_batch(
                str(values.get("候选批编号")).strip())
        except release_flow.ReleaseFlowError as exc:
            return None, [str(exc)]
        row = _latest_row(acceptance.acceptance_id)
        return (row, []) if row else (None, ["验收结论未能落库"])

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        snapshot["deliverable"] = sum(1 for row in rows if row.get("可交付"))
        snapshot["blocked"] = sum(1 for row in rows if not row.get("可交付"))
        return snapshot


def _latest_row(acceptance_id: str) -> dict[str, Any] | None:
    for row in store.rows("batch_acceptance"):
        if str(row.get("验收编号")) == acceptance_id:
            return row
    return None
