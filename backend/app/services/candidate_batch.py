"""候选结果批领域服务：把解析日志聚合成候选批，候选批本身不具备交付资格。"""
from __future__ import annotations

from typing import Any

from app import release_flow
from app.models import CandidateBatch
from app.services.base import BaseModuleService
from app.store import store


class CandidateBatchService(BaseModuleService):
    MODULE = "candidate_batch"
    ENTITY = "候选结果批"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = [
        "候选批编号", "来源作业", "来源URL", "规则版本", "字段口径",
        "口径指纹", "数据行数", "驳回原因",
    ]
    REQUIRED_FIELDS = ["候选批编号", "来源作业"]
    STATUS_ORDER = [
        release_flow.CANDIDATE_PENDING,
        release_flow.CANDIDATE_PASSED,
        release_flow.CANDIDATE_REJECTED,
    ]
    ACTION_RULES = {
        "重建候选批": release_flow.CANDIDATE_PENDING,
        "验收候选批": release_flow.CANDIDATE_PASSED,
    }
    NEGATIVE_ACTIONS = [release_flow.CANDIDATE_REJECTED]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        candidate_id = str(entry.get("候选批编号", ""))
        try:
            if action == "重建候选批":
                candidate = release_flow.build_candidate_batch(
                    job_id=str(entry.get("来源作业", "") or "") or None,
                    candidate_id=candidate_id,
                )
                updated = release_flow.save_candidate(candidate)
                return updated, f"已重建{self.ENTITY}，当前状态：{candidate.status}"
            acceptance = release_flow.accept_candidate_batch(candidate_id)
        except release_flow.ReleaseFlowError as exc:
            return None, str(exc)
        updated = store.find(self.MODULE, entry_id) or entry
        verdict = "可一起交付" if acceptance.deliverable else f"不可交付：{acceptance.reason}"
        return updated, f"{self.ENTITY}验收完成，{verdict}"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        urls = [part.strip() for part in str(values.get("来源URL", "")).split(",") if part.strip()]
        candidate: CandidateBatch = release_flow.build_candidate_batch(
            job_id=str(values.get("来源作业", "")).strip() or None,
            urls=urls or None,
            candidate_id=str(values.get("候选批编号", "")).strip(),
        )
        return release_flow.save_candidate(candidate), []

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["deliverable"] = sum(
            1 for row in store.rows(self.MODULE) if row.get("可交付"))
        snapshot["blocked"] = sum(
            1 for row in store.rows(self.MODULE) if not row.get("可交付"))
        return snapshot
