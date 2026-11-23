"""规则发布回执、解析认领和批次验收的端到端测试。

用题目里的 J90 场景做主线：首个页面已按 P1 解析但还没有可交付批，P2 完成
验收等待发布，两个版本字段口径冲突，后两页尚未执行；切换前后都要能查到
生效资格和已发生的解析，候选批不能直接获得可交付凭据。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import release_flow
from app.models import (
    CrawlJobSpec, ExtractRule, ParseLogEntry, SiteConfig, SiteType,
    schema_fingerprint,
)
from app.store import store


SITE_DOMAIN = "j90.example.com"
PAGE_1 = "https://j90.example.com/article/1"
PAGE_2 = "https://j90.example.com/article/2"
PAGE_3 = "https://j90.example.com/article/3"

P1_SCHEMA = {"title": "string", "body": "string", "publish_date": "string"}
P2_SCHEMA = {"title": "string", "body": "object", "author": "string"}

EARLY = "2026-11-20T09:00:00+00:00"
RELEASE_AT = "2026-11-20T12:00:00+00:00"
AFTER_RELEASE = "2026-11-20T13:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean_flow_tables():
    """只清本域新增的台账，避免影响其他模块的既有测试。"""
    for module in ("rule_release", "candidate_batch", "batch_acceptance"):
        store.rows(module).clear()
    flow_log_ids = {
        str(row.get("记录编号")) for row in store.rows("parse_log")
        if str(row.get("记录编号", "")).startswith("PLOG-")
    }
    rows = store.rows("parse_log")
    rows[:] = [row for row in rows if str(row.get("记录编号")) not in flow_log_ids]
    yield


def _site() -> SiteConfig:
    return SiteConfig(
        site_id="S-J90", domain=SITE_DOMAIN, site_type=SiteType.NEWS,
        robots_policy="allow", language="zh-CN", region="CN",
        access_method="direct", registered_at="2026-11-01", verified=True,
    )


def _job(urls: list[str]) -> CrawlJobSpec:
    return CrawlJobSpec(job_id="J90", site=_site(), entry_urls=list(urls))


def _rule(rule_id: str, version: str, schema: dict[str, str]) -> ExtractRule:
    return ExtractRule(
        rule_id=rule_id,
        target_site=SITE_DOMAIN,
        xpath_selector="//article",
        data_type="string",
        rule_version=version,
        field_schema=dict(schema),
        status="生效中",
    )


def _trial_and_submit(rule: ExtractRule, pages: list[str], *, receipt_id: str,
                     trial_at: str = EARLY) -> release_flow.RuleReleaseReceipt:
    receipt = release_flow.create_release_receipt(
        rule, _job(pages), pages=pages, receipt_id=receipt_id)
    release_flow.run_rule_trial(receipt, rule, _job(pages), at=trial_at)
    if receipt.status == release_flow.PENDING_RELEASE:
        return receipt
    raise AssertionError(f"试跑未按预期通过：{receipt.status}")


# ---------------------------------------------------------------------------
# 主场景：P1 先解析首屏，P2 待发布，后两页未执行
# ---------------------------------------------------------------------------

def test_j90_first_page_parsed_by_p1_before_p2_release() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    receipt_p1 = _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1")
    release_flow.claim_pages(receipt_p1, [PAGE_1])
    release_flow.release_rule(receipt_p1, at=EARLY, effective_at=EARLY,
                              conclusion="P1 首屏可用")
    _trial_and_submit(p2, [PAGE_1], receipt_id="REL-J90-P2")

    job = _job([PAGE_1, PAGE_2, PAGE_3])
    parsed, skipped = release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)

    # 首个页面在 P2 发布前用 P1 完成正式解析
    assert len(parsed) == 1 and not skipped
    assert parsed[0].rule_version == "1.0"
    assert parsed[0].release_receipt_id == "REL-J90-P1"
    # 后两页此时还没有生效规则可用，不被解析
    job = _job([PAGE_1, PAGE_2, PAGE_3])
    parsed_later, skipped_later = release_flow.parse_claimed_pages(
        job, pages=[PAGE_2, PAGE_3], at=EARLY)
    assert parsed_later == []
    assert {reason for _, reason in skipped_later} == {"该页面不在当前生效回执的认领范围内"}

    # P2 仍是待发布，不能进入实际解析
    p2_state = release_flow.load_receipt("REL-J90-P2")
    assert p2_state.status == release_flow.PENDING_RELEASE
    assert release_flow.receipt_eligibility("REL-J90-P2", at=EARLY)["可进入解析"] is False

    # 首个页面尚未形成可交付批：候选批存在但没有凭据
    candidate = release_flow.build_candidate_batch(job_id="J90", urls=[PAGE_1])
    assert candidate.status == release_flow.CANDIDATE_PENDING
    assert candidate.is_deliverable() is False


def test_p2_release_while_p1_pages_in_flight_then_switch_versions() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    receipt_p2 = _trial_and_submit(p2, [PAGE_1], receipt_id="REL-J90-P2")

    job = _job([PAGE_1, PAGE_2, PAGE_3])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)

    # J90 等待后两页期间发布 P2
    release_flow.release_rule(receipt_p2, at=RELEASE_AT, effective_at=RELEASE_AT,
                              conclusion="P2 验收通过，准予发布")
    parsed, skipped = release_flow.parse_claimed_pages(
        job, pages=[PAGE_2, PAGE_3], at=AFTER_RELEASE)

    assert len(parsed) == 2 and not skipped
    assert {log.rule_version for log in parsed} == {"2.0"}
    assert {log.release_receipt_id for log in parsed} == {"REL-J90-P2"}

    # 两个版本解析日志都能查到，切换前后都留痕
    grouped = release_flow.logs_grouped_by_version("J90", mode="正式解析")
    assert set(grouped) == {"1.0", "2.0"}
    assert [log.source_url for log in grouped["1.0"]] == [PAGE_1]
    assert {log.source_url for log in grouped["2.0"]} == {PAGE_2, PAGE_3}
    # 试跑日志也保留，不因切换丢失
    all_grouped = release_flow.logs_grouped_by_version("J90", mode="试跑")
    assert set(all_grouped) == {"1.0", "2.0"}

    # 生效资格也按回检查得到：P1 已生效，P2 在发布日期前不生效
    eligibility = {
        item["回执编号"]: item for item in release_flow.list_receipt_eligibility(job_id="J90")
    }
    assert eligibility["REL-J90-P1"]["可进入解析"] is True
    assert release_flow.receipt_eligibility("REL-J90-P2", at=EARLY)["可进入解析"] is False
    assert release_flow.receipt_eligibility("REL-J90-P2", at=AFTER_RELEASE)["可进入解析"] is True


def test_new_job_uses_released_version() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    release_flow.release_rule(
        _trial_and_submit(p2, [PAGE_1], receipt_id="REL-J90-P2"),
        at=RELEASE_AT, effective_at=RELEASE_AT)

    new_job = CrawlJobSpec(job_id="J91", site=_site(), entry_urls=[PAGE_2])
    parsed, _ = release_flow.parse_claimed_pages(new_job, at=AFTER_RELEASE)

    assert [log.rule_version for log in parsed] == ["2.0"]
    assert release_flow.effective_receipt(new_job, at=AFTER_RELEASE).receipt_id == "REL-J90-P2"


# ---------------------------------------------------------------------------
# 状态流转与恢复
# ---------------------------------------------------------------------------

def test_status_transitions_are_ordered() -> None:
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    receipt = _trial_and_submit(p2, [PAGE_1], receipt_id="REL-ORDER")

    with pytest.raises(release_flow.ReleaseFlowError):
        release_flow.run_rule_trial(receipt, p2, at=EARLY)  # 已过试跑阶段

    release_flow.release_rule(receipt, at=RELEASE_AT, effective_at=RELEASE_AT)
    with pytest.raises(release_flow.ReleaseFlowError):
        release_flow.release_rule(receipt, at=RELEASE_AT)  # 不能重复发布
    with pytest.raises(release_flow.ReleaseFlowError):
        release_flow.recover_trial(receipt)  # 已发布不能回到试跑


def test_cannot_release_before_trial_finishes() -> None:
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    receipt = release_flow.create_release_receipt(
        p2, _job([PAGE_1]), pages=[PAGE_1], receipt_id="REL-NOTRIAL")
    with pytest.raises(release_flow.ReleaseFlowError):
        release_flow.submit_for_release(receipt)
    with pytest.raises(release_flow.ReleaseFlowError):
        release_flow.release_rule(receipt)


def test_interrupted_trial_can_resume_review() -> None:
    p3 = _rule("CONT-P3", "3.0", P1_SCHEMA)
    receipt = release_flow.create_release_receipt(
        p3, _job([PAGE_2, PAGE_3]), pages=[PAGE_2, PAGE_3], receipt_id="REL-INTERRUPT")

    release_flow.run_rule_trial(receipt, p3, at=EARLY, interrupt_after=1)
    assert receipt.status == release_flow.INTERRUPTED
    logs_before = release_flow.query_parse_logs(receipt_id="REL-INTERRUPT", mode="试跑")
    assert len(logs_before) == 1  # 已发生的试跑解析保留

    release_flow.recover_trial(receipt, reason="采集窗口恢复")
    assert receipt.status == release_flow.TRIALING
    release_flow.run_rule_trial(receipt, p3, at=EARLY)
    assert receipt.status == release_flow.PENDING_RELEASE
    # 恢复后只补跑未完成页面，不重复写已完成页面的日志
    logs_after = release_flow.query_parse_logs(receipt_id="REL-INTERRUPT", mode="试跑")
    assert len(logs_after) == 2
    assert {log.source_url for log in logs_after} == {PAGE_2, PAGE_3}


def test_failed_trial_page_is_retried_without_duplicate_counts() -> None:
    """试跑失败后恢复审核，失败页重试成功，计数按台账折算且日志保留。"""
    rule = _rule("CONT-PF", "4.0", P1_SCHEMA)
    receipt = release_flow.create_release_receipt(
        rule, _job([PAGE_2, PAGE_3]), pages=[PAGE_2, PAGE_3], receipt_id="REL-FAIL")

    release_flow.run_rule_trial(receipt, rule, at=EARLY, fail_urls=[PAGE_3])
    assert receipt.status == release_flow.TRIAL_FAILED
    assert (receipt.trial_passed, receipt.trial_failed) == (1, 1)

    release_flow.recover_trial(receipt, reason="页面恢复")
    assert receipt.status == release_flow.TRIALING
    assert (receipt.trial_passed, receipt.trial_failed) == (1, 0)

    release_flow.run_rule_trial(receipt, rule, at=EARLY)
    assert receipt.status == release_flow.PENDING_RELEASE
    assert (receipt.trial_passed, receipt.trial_failed) == (2, 0)
    # 失败的那次试跑日志仍在台账里，可追溯
    trial_logs = release_flow.query_parse_logs(receipt_id="REL-FAIL", mode="试跑")
    assert sum(1 for log in trial_logs if log.parse_result != "成功") == 1
    assert sum(1 for log in trial_logs if log.parse_result == "成功") == 2


# ---------------------------------------------------------------------------
# 候选批与批次验收
# ---------------------------------------------------------------------------

def test_mixed_schema_candidate_cannot_be_delivered() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    release_flow.release_rule(
        _trial_and_submit(p2, [PAGE_1], receipt_id="REL-J90-P2"),
        at=RELEASE_AT, effective_at=RELEASE_AT)
    job = _job([PAGE_1, PAGE_2, PAGE_3])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)
    release_flow.parse_claimed_pages(job, pages=[PAGE_2, PAGE_3], at=AFTER_RELEASE)

    # 把两个口径的日志强行放进同一个候选批
    candidate = release_flow.build_candidate_batch(job_id="J90")
    assert len(candidate.schema_fingerprints) == 2
    assert "字段口径不兼容" in candidate.rejection_reason
    assert candidate.deliverable is False

    acceptance = release_flow.accept_candidate_batch(candidate)
    assert acceptance.deliverable is False
    assert acceptance.credential == ""
    assert candidate.is_deliverable() is False


def test_single_schema_candidate_can_be_accepted() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    job = _job([PAGE_1])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)

    candidate = release_flow.build_candidate_batch(job_id="J90")
    assert candidate.deliverable is False  # 候选批天生不带凭据

    acceptance = release_flow.accept_candidate_batch(candidate, job=job)
    assert acceptance.deliverable is True
    assert acceptance.credential
    assert candidate.status == release_flow.CANDIDATE_PASSED


def test_acceptance_records_log_schema_not_just_candidate_label() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    job = _job([PAGE_1])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)

    # 候选批登记的是旧口径，但验收要按日志实际口径核对
    candidate = release_flow.build_candidate_batch(job_id="J90")
    candidate.field_schema = dict(P2_SCHEMA)
    acceptance = release_flow.accept_candidate_batch(candidate, job=job)

    assert acceptance.schema_fingerprints == [schema_fingerprint(P1_SCHEMA)]
    assert acceptance.deliverable is True


def test_acceptance_blocks_when_job_pages_missing() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    job = _job([PAGE_1, PAGE_2, PAGE_3])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)

    candidate = release_flow.build_candidate_batch(job_id="J90")
    acceptance = release_flow.accept_candidate_batch(candidate, job=job)

    assert acceptance.deliverable is False
    assert "未执行" in acceptance.reason


def test_inspect_job_exposes_versions_logs_and_batches() -> None:
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    release_flow.release_rule(
        _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1"),
        at=EARLY, effective_at=EARLY)
    release_flow.release_rule(
        _trial_and_submit(p2, [PAGE_1], receipt_id="REL-J90-P2"),
        at=RELEASE_AT, effective_at=RELEASE_AT)
    job = _job([PAGE_1, PAGE_2, PAGE_3])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)
    release_flow.parse_claimed_pages(job, pages=[PAGE_2, PAGE_3], at=AFTER_RELEASE)
    candidate = release_flow.build_candidate_batch(job_id="J90")
    release_flow.accept_candidate_batch(candidate)

    detail = release_flow.inspect_job("J90")
    assert set(detail["解析日志"]) == {"1.0", "2.0"}
    assert detail["正式解析条数"]["1.0"] == 1
    assert detail["正式解析条数"]["2.0"] == 2
    assert {item["回执编号"] for item in detail["生效资格"]} == {"REL-J90-P1", "REL-J90-P2"}
    assert len(detail["候选批"]) == 1
    assert len(detail["批次验收"]) == 1


def test_same_field_name_different_type_is_incompatible() -> None:
    """两个版本里同名字段 body 类型不同，也属于口径不兼容，不能放进同一个候选批。"""
    p1_schema = {"title": "string", "body": "string"}
    p2_schema = {"title": "string", "body": "object"}
    assert schema_fingerprint(p1_schema) != schema_fingerprint(p2_schema)

    p1 = _rule("CONT-P1", "1.0", p1_schema)
    p2 = _rule("CONT-P2", "2.0", p2_schema)
    release_flow.claim_pages(
        release_flow.release_rule(
            _trial_and_submit(p1, [PAGE_1], receipt_id="REL-TYPE-P1"),
            at=EARLY, effective_at=EARLY),
        [PAGE_1])
    release_flow.claim_pages(
        release_flow.release_rule(
            _trial_and_submit(p2, [PAGE_1], receipt_id="REL-TYPE-P2"),
            at=RELEASE_AT, effective_at=RELEASE_AT),
        [PAGE_2])

    job = _job([PAGE_1, PAGE_2])
    release_flow.parse_claimed_pages(job, pages=[PAGE_1], at=EARLY)
    release_flow.parse_claimed_pages(job, pages=[PAGE_2], at=AFTER_RELEASE)

    candidate = release_flow.build_candidate_batch(job_id="J90", urls=[PAGE_1, PAGE_2])
    assert len(candidate.schema_fingerprints) == 2
    assert candidate.deliverable is False
    acceptance = release_flow.accept_candidate_batch(candidate)
    assert acceptance.deliverable is False
    assert acceptance.credential == ""


def test_plan_job_versions_shows_waiting_pages() -> None:
    """版本计划：已发布回执的页面显示版本，未发布页面显示等待发布回执。"""
    p1 = _rule("CONT-P1", "1.0", P1_SCHEMA)
    release_flow.claim_pages(
        release_flow.release_rule(
            _trial_and_submit(p1, [PAGE_1], receipt_id="REL-J90-P1B"),
            at=EARLY, effective_at=EARLY),
        [PAGE_1])

    plan = {item["来源URL"]: item for item in release_flow.plan_job_versions(
        _job([PAGE_1, PAGE_2]), at=AFTER_RELEASE)}
    assert plan[PAGE_1]["将使用版本"] == "1.0"
    assert plan[PAGE_1]["判定"] == "可进入实际解析"
    assert plan[PAGE_2]["将使用版本"] == ""
    assert plan[PAGE_2]["判定"] == "等待发布回执"


def test_parse_log_rejects_unreleased_receipt() -> None:
    from app.services.parse_log import ParseLogService

    service = ParseLogService()
    entry, missing = service.create_entry({
        "记录编号": "PLOG-MANUAL-1", "来源URL": PAGE_3,
        "解析规则": "CONT-P2", "解析结果": "成功",
        "规则版本": "2.0", "发布回执": "REL-UNKNOWN",
    })
    assert entry is None and missing

    p2 = _rule("CONT-P2", "2.0", P2_SCHEMA)
    _trial_and_submit(p2, [PAGE_1], receipt_id="REL-J90-P2")
    entry, missing = service.create_entry({
        "记录编号": "PLOG-MANUAL-2", "来源URL": PAGE_3,
        "解析规则": "CONT-P2", "解析结果": "成功",
        "规则版本": "2.0", "发布回执": "REL-J90-P2", "记录时间": EARLY,
    })
    assert entry is None and "尚未发布生效" in missing[0]
