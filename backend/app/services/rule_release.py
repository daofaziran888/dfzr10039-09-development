"""规则发布回执领域服务：试跑、恢复审核、发布和停用的状态流转都在这里。"""
from __future__ import annotations

from typing import Any

from app import release_flow
from app.models import CrawlJobSpec, ExtractRule, SiteConfig, SiteType, parse_schema
from app.services.base import BaseModuleService
from app.store import store


def _blank_site(domain: str = "") -> SiteConfig:
    return SiteConfig(
        site_id="", domain=domain, site_type=SiteType.NEWS, robots_policy="allow",
        language="zh-CN", region="CN", access_method="direct", registered_at="",
    )


class RuleReleaseService(BaseModuleService):
    MODULE = "rule_release"
    ENTITY = "发布回执"
    OPERATION_TERM = "发布"
    LIST_FIELDS = [
        "回执编号", "规则编号", "规则版本", "目标站点", "字段口径", "试跑页面",
        "来源作业", "发布结论", "生效时间", "发布时间",
    ]
    REQUIRED_FIELDS = ["回执编号", "规则编号", "规则版本", "目标站点"]
    STATUS_ORDER = [
        release_flow.TRIALING,
        release_flow.PENDING_RELEASE,
        release_flow.RELEASED,
        release_flow.RETIRED,
        release_flow.TRIAL_FAILED,
        release_flow.INTERRUPTED,
        release_flow.REJECTED,
    ]
    ACTION_RULES = {
        "试跑规则": release_flow.TRIALING,
        "恢复审核": release_flow.TRIALING,
        "提交发布": release_flow.PENDING_RELEASE,
        "发布规则": release_flow.RELEASED,
        "驳回发布": release_flow.REJECTED,
        "停用回执": release_flow.RETIRED,
    }
    NEGATIVE_ACTIONS = [release_flow.TRIAL_FAILED, release_flow.INTERRUPTED, release_flow.REJECTED]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        receipt_id = str(entry.get("回执编号", ""))
        try:
            if action == "试跑规则":
                receipt = release_flow.run_rule_trial(receipt_id)
            elif action == "恢复审核":
                receipt = release_flow.recover_trial(receipt_id, reason="人工恢复审核")
            elif action == "提交发布":
                receipt = release_flow.submit_for_release(receipt_id)
            elif action == "发布规则":
                receipt = release_flow.release_rule(receipt_id)
            elif action == "驳回发布":
                receipt = release_flow.reject_release(receipt_id, reason="发布结论：人工驳回")
            else:
                receipt = release_flow.retire_release(receipt_id)
        except release_flow.ReleaseFlowError as exc:
            return None, str(exc)
        updated = release_flow.save_receipt(receipt)
        updated["id"] = entry.get("id")
        return updated, f"{self.ENTITY}已{action}，当前状态：{receipt.status}"

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        receipt_id = str(values.get("回执编号")).strip()
        if release_flow.load_receipt(receipt_id) is not None:
            return None, ["回执编号（已存在）"]
        target_site = str(values.get("目标站点", "")).strip()
        schema = parse_schema(values.get("字段口径", ""), "string")
        rule = ExtractRule(
            rule_id=str(values.get("规则编号", "")).strip(),
            target_site=target_site,
            xpath_selector=str(values.get("XPath选择器", "")),
            data_type="string",
            rule_version=str(values.get("规则版本", "")).strip(),
            field_schema=schema,
        )
        job = CrawlJobSpec(
            job_id=str(values.get("来源作业", "") or "").strip(),
            site=_blank_site(target_site),
            entry_urls=[part.strip() for part in str(values.get("试跑页面", "")).split(",") if part.strip()],
        )
        receipt = release_flow.create_release_receipt(
            rule, job,
            pages=job.entry_urls,
            receipt_id=receipt_id,
            target_site=target_site,
            field_schema=schema,
        )
        return release_flow.save_receipt(receipt), []

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        items = release_flow.list_receipt_eligibility()
        snapshot["eligible"] = sum(1 for item in items if item["可进入解析"])
        snapshot["pending_release"] = sum(
            1 for item in items if item["status"] == release_flow.PENDING_RELEASE)
        return snapshot


__all__ = ["RuleReleaseService"]
