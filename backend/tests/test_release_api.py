"""发布回执相关接口的契约测试：只看路由和服务的对外行为，不依赖 TestClient。"""
from __future__ import annotations

from app import release_flow
from app.models import CrawlJobSpec, ExtractRule, SiteConfig, SiteType
from app.routers import resource
from app.schemas import ActionPayload, EntryPayload
from app.store import store

SITE = "api.example.com"
P1 = "https://api.example.com/a"
P2 = "https://api.example.com/b"
P3 = "https://api.example.com/c"


def _job(urls: list[str]) -> CrawlJobSpec:
    site = SiteConfig(site_id="S", domain=SITE, site_type=SiteType.NEWS,
                      robots_policy="allow", language="zh-CN", region="CN",
                      access_method="direct", registered_at="", verified=True)
    return CrawlJobSpec(job_id="J90-API", site=site, entry_urls=urls)


def _rule(rule_id: str, version: str, schema: dict[str, str]) -> ExtractRule:
    return ExtractRule(rule_id=rule_id, target_site=SITE, xpath_selector="//a",
                       rule_version=version, field_schema=dict(schema))


def _clean() -> None:
    for module in ("rule_release", "candidate_batch", "batch_acceptance"):
        store.rows(module).clear()
    rows = store.rows("parse_log")
    rows[:] = [row for row in rows
               if not str(row.get("记录编号", "")).startswith(("PLOG-J90", "PLOG-TRIAL", "PLOG-REL"))]


def test_release_action_endpoint_flow() -> None:
    """通过资源动作端点完成 试跑→提交→发布，每步状态可查。"""
    _clean()
    receipt = release_flow.create_release_receipt(
        _rule("CONT-API-1", "1.0", {"title": "string"}),
        _job([P1]), pages=[P1], receipt_id="REL-API-1")
    row = release_flow.save_receipt(receipt)

    trialled = resource.run_action("rule_release", row["id"], ActionPayload(action="试跑规则"))
    assert trialled.ok and trialled.entry["status"] == "待发布"

    submitted = resource.run_action("rule_release", row["id"], ActionPayload(action="提交发布"))
    assert submitted.ok and submitted.entry["status"] == "待发布"

    released = resource.run_action("rule_release", row["id"], ActionPayload(action="发布规则"))
    assert released.ok and released.entry["status"] == "已发布"
    assert release_flow.receipt_eligibility("REL-API-1")["可进入解析"] is True

    blocked = resource.run_action("rule_release", row["id"], ActionPayload(action="试跑规则"))
    assert blocked.ok is False and "已发布" in blocked.message


def test_parse_claim_endpoint_returns_per_page_versions() -> None:
    """解析端点逐页返回实际使用的规则版本和跳过原因。"""
    _clean()
    p1 = _rule("CONT-API-1", "1.0", {"title": "string", "body": "string"})
    p2 = _rule("CONT-API-2", "2.0", {"title": "string", "body": "object"})
    for rule, receipt_id, page in ((p1, "REL-API-1", P1), (p2, "REL-API-2", P2)):
        receipt = release_flow.create_release_receipt(
            rule, _job([page]), pages=[page], receipt_id=receipt_id)
        release_flow.run_rule_trial(receipt, rule)
        release_flow.claim_pages(receipt, [page])
        release_flow.release_rule(receipt)

    job = _job([P1, P2, P3])
    first, _ = release_flow.parse_claimed_pages(job, pages=[P1, P2])
    assert {log.rule_version for log in first} == {"1.0", "2.0"}

    second, skipped = release_flow.parse_claimed_pages(job, pages=[P1, P2, P3])
    assert second == []
    reasons = {url: reason for url, reason in skipped}
    assert reasons[P1] == "该页面已完成正式解析"
    assert reasons[P3] == "该页面不在当前生效回执的认领范围内"


def test_candidate_requires_acceptance_before_delivery() -> None:
    """候选批登记后仍不可交付，验收端点才给出可交付凭据。"""
    _clean()
    rule = _rule("CONT-API-1", "1.0", {"title": "string"})
    receipt = release_flow.create_release_receipt(
        rule, _job([P1]), pages=[P1], receipt_id="REL-API-1")
    release_flow.run_rule_trial(receipt, rule)
    release_flow.claim_pages(receipt, [P1])
    release_flow.release_rule(receipt)
    release_flow.parse_claimed_pages(_job([P1]), pages=[P1])

    created = resource.create_entry("candidate_batch", EntryPayload(values={
        "候选批编号": "CAND-API-1", "来源作业": "J90-API"}))
    assert created.ok
    assert created.entry["可交付"] is False
    assert created.entry["status"] == "待验收"

    accepted = resource.create_entry("batch_acceptance", EntryPayload(values={
        "候选批编号": "CAND-API-1"}))
    assert accepted.ok
    assert accepted.entry["可交付"] is True
    assert accepted.entry["可交付凭据"].startswith("DELIV-")

    again = resource.create_entry("batch_acceptance", EntryPayload(values={
        "候选批编号": "CAND-API-1"}))
    assert again.ok is False


def test_router_handlers_work_when_called_directly() -> None:
    """直接调用路由处理函数时 Query 默认值不能污染业务参数。"""
    _clean()
    rule = _rule("CONT-API-1", "1.0", {"title": "string", "body": "string"})
    receipt = release_flow.create_release_receipt(
        rule, _job([P1]), pages=[P1], receipt_id="REL-API-1")
    release_flow.run_rule_trial(receipt, rule)
    release_flow.claim_pages(receipt, [P1])
    release_flow.release_rule(receipt)
    store.rows("crawl_job").append({
        "id": 999, "作业编号": "J90-API", "目标站点": SITE,
        "入口URL": f"{P1},{P2}", "status": "采集执行",
        "pending": True, "abnormal": False,
    })

    claimed = resource.parse_job_pages("J90-API", EntryPayload(values={"页面": P1}))
    assert claimed.ok and len(claimed.entry["已解析"]) == 1

    detail = resource.inspect_job_flow("J90-API")
    assert detail["正式解析条数"] == {"1.0": 1}

    eligibility = resource.release_eligibility(job_id="J90-API")
    assert eligibility["total"] == 1
    assert eligibility["items"][0]["可进入解析"] is True

    future = resource.inspect_job_flow("J90-API", at="2020-01-01T00:00:00+00:00")
    assert future["生效资格"][0]["可进入解析"] is False


def test_parse_log_create_requires_effective_receipt() -> None:
    """手工登记解析日志必须带已发布生效的回执，否则拒绝。"""
    _clean()
    service = resource.require_service("parse_log")
    entry, problems = service.create_entry({
        "记录编号": "PLOG-MANUAL-API", "来源URL": P1, "解析规则": "CONT-API-1",
        "规则版本": "1.0", "发布回执": "REL-UNKNOWN",
    })
    assert entry is None and "发布回执" in problems[0]

    rule = _rule("CONT-API-1", "1.0", {"title": "string"})
    receipt = release_flow.create_release_receipt(
        rule, _job([P1]), pages=[P1], receipt_id="REL-API-1")
    release_flow.run_rule_trial(receipt, rule)
    release_flow.claim_pages(receipt, [P1])
    release_flow.release_rule(receipt)

    entry, problems = service.create_entry({
        "记录编号": "PLOG-MANUAL-API", "来源URL": P2, "解析规则": "CONT-API-1",
        "规则版本": "1.0", "发布回执": "REL-API-1",
    })
    assert not problems and entry["status"] == "成功"
