"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app import release_flow
from app.services.base import BaseModuleService
from app.store import store


class ContentExtractService(BaseModuleService):
    MODULE = "content_extract"
    ENTITY = "抽取规则"
    OPERATION_TERM = "清洗"
    LIST_FIELDS = ["规则编号", "目标站点", "XPath选择器", "字段映射", "数据类型", "清洗正则", "抽取顺序", "版本号", "规则状态"]
    REQUIRED_FIELDS = ["规则编号", "目标站点", "XPath选择器"]
    STATUS_ORDER = ["草稿", "生效中", "抽取异常", "已废弃"]
    ACTION_RULES = {"启效规则": "生效中", "标记异常": "抽取异常", "废弃规则": "已废弃"}
    NEGATIVE_ACTIONS = []

    def domain_snapshot(self) -> dict[str, Any]:
        """抽取规则快照里带上版本，便于和发布回执对账。"""
        snapshot = super().domain_snapshot()
        snapshot["versions"] = sorted({
            str(row.get("版本号", "")) for row in store.rows(self.MODULE)
            if str(row.get("版本号", "")).strip()
        })
        snapshot["released_rules"] = sum(
            1 for row in release_flow.all_receipts()
            if row.rule_id in {str(item.get("规则编号")) for item in store.rows(self.MODULE)}
            and row.status == release_flow.RELEASED
        )
        return snapshot
