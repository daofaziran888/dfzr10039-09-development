"""解析日志领域服务：台账只增不改，保留实际使用的规则版本和发布回执。"""
from __future__ import annotations

from typing import Any

from app import release_flow
from app.models import ParseLogEntry, parse_schema
from app.services.base import BaseModuleService
from app.store import store


class ParseLogService(BaseModuleService):
    MODULE = "parse_log"
    ENTITY = "解析记录"
    OPERATION_TERM = "解析"
    LIST_FIELDS = [
        "记录编号", "来源URL", "解析规则", "解析结果", "异常类型", "重试次数",
        "错误详情", "记录时间", "规则版本", "发布回执", "来源作业", "字段口径",
        "解析方式",
    ]
    REQUIRED_FIELDS = ["记录编号", "来源URL", "解析规则"]
    STATUS_ORDER = ["成功", "格式异常", "内容缺失", "重试中"]
    ACTION_RULES = {"标记重试": "格式异常", "标记审计": "内容缺失"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._ensure_ledger()

    def _ensure_ledger(self) -> None:
        """为历史日志补齐台账列，旧记录保持原样，只补空列。"""
        for row in store.rows(self.MODULE):
            row.setdefault("规则版本", "")
            row.setdefault("发布回执", "")
            row.setdefault("来源作业", "")
            row.setdefault("字段口径", "")
            row.setdefault("生效时间", "")
            row.setdefault("解析方式", "历史记录")

    def list_by_version(self, *, job_id: str | None = None,
                        rule_version: str | None = None) -> list[dict[str, Any]]:
        """按作业/版本查台账，返回原样的行，保证带 id 可继续做动作。"""
        wanted = {
            log.record_id
            for log in release_flow.query_parse_logs(job_id=job_id, rule_version=rule_version)
        }
        return [row for row in store.rows(self.MODULE)
                if str(row.get("记录编号", "")) in wanted]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """解析端登记解析记录时必须说明实际使用的规则版本和发布回执。"""
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        receipt_id = str(values.get("发布回执", "") or "").strip()
        if not receipt_id:
            return None, [*missing, "发布回执"]
        receipt = release_flow.load_receipt(receipt_id)
        if receipt is None:
            return None, ["发布回执（不存在）"]
        registry = str(values.get("记录时间", "") or "").strip()
        if not receipt.is_effective(registry or None):
            return None, [f"发布回执 {receipt_id} 尚未发布生效，不能登记正式解析"]
        rule_version = str(values.get("规则版本", "") or "").strip()
        if not rule_version:
            return None, ["规则版本"]
        if missing:
            return None, missing

        entry = {
            "id": max((int(row.get("id", 0)) for row in store.rows(self.MODULE)), default=0) + 1,
        }
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = "成功" if str(values.get("解析结果", "成功")) == "成功" else "格式异常"
        entry["pending"] = False
        entry["abnormal"] = entry["status"] == "格式异常"
        store.rows(self.MODULE).append(entry)
        return entry, []

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        snapshot["by_version"] = {}
        for row in rows:
            version = str(row.get("规则版本", "") or "未登记版本")
            snapshot["by_version"][version] = snapshot["by_version"].get(version, 0) + 1
        snapshot["by_mode"] = {}
        for row in rows:
            mode = str(row.get("解析方式", "") or "历史记录")
            snapshot["by_mode"][mode] = snapshot["by_mode"].get(mode, 0) + 1
        return snapshot


__all__ = ["ParseLogService", "ParseLogEntry", "parse_schema"]
