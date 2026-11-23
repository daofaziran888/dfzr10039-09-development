"""规则发布回执与解析认领流程。

内容抽取原来只在“草稿/生效中”之间切换，规则一旦生效会被各个作业轮流拿来
抽取页面；同一站点的两个规则版本字段口径不同时，已解析的页面很容易被当成
同一批交付。这里补上发布阶段：

- `RuleReleaseReceipt` 把“规则版本 + 目标站点 + 字段口径 + 试跑页面”锁成一张回执；
- 试跑只写解析日志，不进入候选结果批；试跑通过后进入待发布；
- 发布时记录生效时间，并把发布结论同步到解析日志台账；
- 解析端只认领“已发布且已到生效时间”的回执，日志登记实际使用的规则版本；
- 批次验收再核对日志口径，口径不同或来源不同都不签发可交付凭据。

状态只在本模块流转，路由层不做业务判断。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.models import (
    BatchAcceptance,
    CandidateBatch,
    CrawlJobSpec,
    ExtractRule,
    ParseLogEntry,
    RuleReleaseReceipt,
    as_moment,
    parse_schema,
    safe_int,
    schema_fingerprint,
    to_utc,
)
from app.store import store


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

TRIALING = "试跑中"
TRIAL_FAILED = "试跑失败"
INTERRUPTED = "已中断"
PENDING_RELEASE = "待发布"
RELEASED = "已发布"
REJECTED = "已驳回"
RETIRED = "已停用"

# 正常流转顺序：试跑中 → 待发布 → 已发布 → 已停用；
# 试跑失败/已中断/已驳回是异常分支，只能回到试跑中恢复审核。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TRIALING: {PENDING_RELEASE, TRIAL_FAILED, INTERRUPTED},
    TRIAL_FAILED: {TRIALING, INTERRUPTED},
    INTERRUPTED: {TRIALING},
    PENDING_RELEASE: {RELEASED, REJECTED},
    REJECTED: {TRIALING},
    RELEASED: {RETIRED},
    RETIRED: set(),
}

CANDIDATE_PENDING = "待验收"
CANDIDATE_PASSED = "验收通过"
CANDIDATE_REJECTED = "验收未通过"

CANDIDATE_TRANSITIONS: dict[str, set[str]] = {
    CANDIDATE_PENDING: {CANDIDATE_PASSED, CANDIDATE_REJECTED},
    CANDIDATE_REJECTED: {CANDIDATE_PENDING},
    CANDIDATE_PASSED: set(),
}


class ReleaseFlowError(ValueError):
    """发布流程中的业务约束冲突。"""


def can_transition(current: str, target: str) -> bool:
    """状态只能沿允许的边流转，不允许从试跑中直接跳到已发布。"""
    return target in ALLOWED_TRANSITIONS.get(current, set())


def _transition(receipt: RuleReleaseReceipt, target: str) -> None:
    if not can_transition(receipt.status, target):
        raise ReleaseFlowError(
            f"回执 {receipt.receipt_id} 不能从「{receipt.status}」直接流转到「{target}」"
        )
    receipt.status = target


def _now(at: Any = None) -> datetime:
    return as_moment(at)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# ---------------------------------------------------------------------------
# 回执持久化
# ---------------------------------------------------------------------------

def _schema_text(schema: dict[str, str]) -> str:
    return ",".join(f"{name}:{data_type}" for name, data_type in schema.items())


def _receipt_row(receipt: RuleReleaseReceipt) -> dict[str, Any]:
    return {
        "回执编号": receipt.receipt_id,
        "规则编号": receipt.rule_id,
        "规则版本": receipt.rule_version,
        "目标站点": receipt.target_site,
        "字段口径": _schema_text(receipt.field_schema),
        "试跑页面": ",".join(receipt.trial_pages),
        "认领页面": ",".join(receipt.claimed_pages),
        "试跑通过": receipt.trial_passed,
        "试跑失败": receipt.trial_failed,
        "来源作业": receipt.job_id,
        "发布结论": receipt.conclusion,
        "生效时间": receipt.effective_at,
        "发布时间": receipt.released_at,
        "中断原因": receipt.interrupted_reason,
        "创建时间": receipt.created_at,
        "status": receipt.status,
        "pending": receipt.status not in (RELEASED, RETIRED),
        "abnormal": receipt.status in (TRIAL_FAILED, INTERRUPTED, REJECTED),
    }


def save_receipt(receipt: RuleReleaseReceipt) -> dict[str, Any]:
    """把回执写回 store；同编号覆盖，保持一张回执只有一份当前状态。"""
    rows = store.rows("rule_release")
    for row in rows:
        if str(row.get("回执编号")) == receipt.receipt_id:
            row.update(_receipt_row(receipt))
            return row
    row = {"id": max((safe_int(item.get("id"), 0) for item in rows), default=0) + 1}
    row.update(_receipt_row(receipt))
    rows.append(row)
    return row


def save_candidate(candidate: CandidateBatch) -> dict[str, Any]:
    """候选批对外可见的写入入口，服务层不应直接碰内部函数。"""
    return _save_candidate(candidate)


def load_receipt(receipt_id: str) -> RuleReleaseReceipt | None:
    for row in store.rows("rule_release"):
        if str(row.get("回执编号")) == receipt_id:
            return RuleReleaseReceipt.from_seed_row(row)
    return None


def all_receipts() -> list[RuleReleaseReceipt]:
    return [RuleReleaseReceipt.from_seed_row(row) for row in store.rows("rule_release")]


def _receipt_for(receipt: RuleReleaseReceipt) -> RuleReleaseReceipt:
    """函数收到编号或对象都行；返回最新落库状态。"""
    if isinstance(receipt, RuleReleaseReceipt):
        return receipt
    loaded = load_receipt(str(receipt))
    if loaded is None:
        raise ReleaseFlowError(f"发布回执 {receipt} 不存在")
    return loaded


# ---------------------------------------------------------------------------
# 规则 → 回执
# ---------------------------------------------------------------------------

def create_release_receipt(
    rule: ExtractRule | dict[str, Any],
    job: CrawlJobSpec | dict[str, Any],
    *,
    pages: Iterable[str] | None = None,
    receipt_id: str = "",
    target_site: str = "",
    field_schema: dict[str, str] | None = None,
) -> RuleReleaseReceipt:
    """按“规则版本 + 本次采集作业”登记发布回执，初始状态为试跑中。"""
    if not isinstance(rule, ExtractRule):
        rule = ExtractRule.from_seed_row(rule)
    job_spec, job_id, site = _job_parts(job)
    schema = dict(field_schema or rule.field_schema)
    if not schema:
        schema = parse_schema(rule.field_mapping.get("raw", ""), rule.data_type)
    trial_pages = [str(page).strip() for page in (pages or job_spec.entry_urls) if str(page).strip()]
    version = rule.rule_version or rule.rule_id
    receipt = RuleReleaseReceipt(
        receipt_id=receipt_id or f"REL-{job_id}-{rule.rule_id}-{version}",
        rule_id=rule.rule_id,
        rule_version=version,
        target_site=target_site or site,
        field_schema=schema,
        job_id=job_id,
        trial_pages=trial_pages,
    )
    save_receipt(receipt)
    return receipt


# ---------------------------------------------------------------------------
# 试跑、恢复与发布
# ---------------------------------------------------------------------------

def run_rule_trial(
    receipt: RuleReleaseReceipt | str,
    rule: ExtractRule | dict[str, Any] | None = None,
    job: CrawlJobSpec | dict[str, Any] | None = None,
    *,
    pages: Iterable[str] | None = None,
    at: Any = None,
    interrupt_after: int | None = None,
    fail_urls: Iterable[str] = (),
) -> RuleReleaseReceipt:
    """对回执锁定的页面执行试跑。

    试跑只写日志，不产生批次；重复调用会跳过已经试跑过的页面，便于中断后恢复。
    不传规则对象时用回执锁定的规则编号/版本/字段口径构造试跑规则。
    """
    receipt = _receipt_for(receipt)
    if receipt.status != TRIALING:
        raise ReleaseFlowError(
            f"回执 {receipt.receipt_id} 当前为「{receipt.status}」，需先恢复审核才能继续试跑"
        )
    if rule is None:
        rule = ExtractRule(
            rule_id=receipt.rule_id,
            target_site=receipt.target_site,
            xpath_selector="",
            rule_version=receipt.rule_version,
            field_schema=dict(receipt.field_schema),
        )
    elif not isinstance(rule, ExtractRule):
        rule = ExtractRule.from_seed_row(rule)
    if job is not None:
        _, job_id, site = _job_parts(job)
        receipt.job_id = receipt.job_id or job_id
        receipt.target_site = receipt.target_site or site

    trial_pages = [str(page).strip() for page in (pages or receipt.trial_pages) if str(page).strip()]
    if trial_pages:
        receipt.trial_pages = trial_pages
    fail_set = set(fail_urls)
    ran = 0

    for index, url in enumerate(receipt.trial_pages, start=1):
        if _trial_succeeded(receipt.receipt_id, url):
            continue
        if interrupt_after is not None and ran >= interrupt_after:
            _recount_trial(receipt)
            receipt.interrupted_reason = "试跑中断，等待恢复审核"
            _transition(receipt, INTERRUPTED)
            save_receipt(receipt)
            return receipt

        log = _trial_log(receipt, rule, url, index, at=at, force_fail=url in fail_set)
        _persist_parse_log(log)
        ran += 1

    _recount_trial(receipt)
    if receipt.trial_finished():
        if receipt.trial_failed == 0:
            _transition(receipt, PENDING_RELEASE)
            receipt.conclusion = receipt.conclusion or "试跑通过，待发布"
        else:
            _transition(receipt, TRIAL_FAILED)
            receipt.conclusion = receipt.conclusion or "试跑未通过，需恢复审核后重试"
    save_receipt(receipt)
    return receipt


def _trial_succeeded(receipt_id: str, url: str) -> bool:
    """该页面已有成功的试跑日志；失败日志保留在台账里，恢复后允许重试。"""
    return any(
        log.parse_result == "成功"
        for log in query_parse_logs(receipt_id=receipt_id, source_url=url, mode="试跑")
    )


def _recount_trial(receipt: RuleReleaseReceipt) -> None:
    """按台账重新折算试跑计数，重复调用或恢复试跑都不会重复累加。"""
    logs = query_parse_logs(receipt_id=receipt.receipt_id, mode="试跑")
    passed = {log.source_url for log in logs if log.parse_result == "成功"}
    failed = {log.source_url for log in logs
              if log.parse_result != "成功" and log.source_url not in passed}
    receipt.trial_passed = len(passed)
    receipt.trial_failed = len(failed)


def recover_trial(receipt: RuleReleaseReceipt | str, reason: str = "") -> RuleReleaseReceipt:
    """试跑失败或中断后恢复审核：回到试跑中，已发生的解析日志保留。

    台账只增不改：失败页面的试跑日志原样保留供审计，恢复后重新试跑这些页面，
    成功的试跑页面不重复消耗采集窗口。试跑计数按成功日志重新折算。
    """
    receipt = _receipt_for(receipt)
    if receipt.status not in (TRIAL_FAILED, INTERRUPTED, REJECTED):
        raise ReleaseFlowError(f"回执 {receipt.receipt_id} 当前为「{receipt.status}」，无需恢复")
    _transition(receipt, TRIALING)
    successful = {
        log.source_url
        for log in query_parse_logs(receipt_id=receipt.receipt_id, mode="试跑")
        if log.parse_result == "成功"
    }
    receipt.trial_passed = len(successful)
    receipt.trial_failed = 0
    receipt.interrupted_reason = reason
    if reason:
        receipt.conclusion = ""
    save_receipt(receipt)
    return receipt


def submit_for_release(receipt: RuleReleaseReceipt | str) -> RuleReleaseReceipt:
    """试跑全部通过后提交发布，进入待发布。"""
    receipt = _receipt_for(receipt)
    if not receipt.trial_finished():
        raise ReleaseFlowError(f"回执 {receipt.receipt_id} 试跑尚未完成，不能提交发布")
    if receipt.trial_failed > 0:
        raise ReleaseFlowError(f"回执 {receipt.receipt_id} 存在失败页面，不能提交发布")
    if receipt.status == PENDING_RELEASE:
        return receipt
    _transition(receipt, PENDING_RELEASE)
    receipt.conclusion = receipt.conclusion or "试跑通过，待发布"
    save_receipt(receipt)
    return receipt


def release_rule(
    receipt: RuleReleaseReceipt | str,
    *,
    at: Any = None,
    effective_at: Any = None,
    conclusion: str = "",
) -> RuleReleaseReceipt:
    """发布待发布的回执：登记生效时间，并把发布结论同步到解析日志台账。"""
    receipt = _receipt_for(receipt)
    if receipt.status != PENDING_RELEASE:
        raise ReleaseFlowError(
            f"回执 {receipt.receipt_id} 当前为「{receipt.status}」，只有待发布的回执可以发布"
        )
    if not receipt.trial_finished() or receipt.trial_failed > 0:
        raise ReleaseFlowError(f"回执 {receipt.receipt_id} 试跑未通过，不能发布")

    now = _now(at)
    effective = _now(effective_at) if effective_at is not None else now
    _transition(receipt, RELEASED)
    receipt.released_at = _iso(now)
    receipt.effective_at = _iso(effective)
    if conclusion:
        receipt.conclusion = conclusion
    elif not receipt.conclusion or receipt.conclusion.startswith("试跑通过，待发布"):
        receipt.conclusion = "试跑通过，准予发布"
    save_receipt(receipt)
    _sync_release_conclusion(receipt)
    return receipt


def reject_release(receipt: RuleReleaseReceipt | str, reason: str = "") -> RuleReleaseReceipt:
    """驳回待发布回执；驳回后可通过恢复审核重新试跑。"""
    receipt = _receipt_for(receipt)
    _transition(receipt, REJECTED)
    receipt.conclusion = reason or "发布结论：驳回，待重新试跑"
    save_receipt(receipt)
    return receipt


def retire_release(receipt: RuleReleaseReceipt | str) -> RuleReleaseReceipt:
    receipt = _receipt_for(receipt)
    _transition(receipt, RETIRED)
    save_receipt(receipt)
    return receipt


def _sync_release_conclusion(receipt: RuleReleaseReceipt) -> ParseLogEntry:
    """发布结论写进解析日志台账：之后解析端和验收端都能按回执查到。"""
    detail = (
        f"发布结论：{receipt.conclusion}；规则 {receipt.rule_id} 版本 {receipt.rule_version} "
        f"自 {receipt.effective_at} 起可进入实际解析"
    )
    log = ParseLogEntry(
        record_id=f"PLOG-REL-{receipt.receipt_id}",
        source_url="",
        parse_rule_id=receipt.rule_id,
        parse_result="成功",
        error_type="",
        retry_count=0,
        error_detail=detail,
        rule_version=receipt.rule_version,
        release_receipt_id=receipt.receipt_id,
        source_job_id=receipt.job_id,
        field_schema=dict(receipt.field_schema),
        effective_at=receipt.effective_at,
        parse_mode="发布结论",
    )
    _persist_parse_log(log)
    return log


# ---------------------------------------------------------------------------
# 生效资格与解析认领
# ---------------------------------------------------------------------------

def receipt_eligibility(receipt: RuleReleaseReceipt | str, at: Any = None) -> dict[str, Any]:
    """在某个时刻回执是否具备进入实际解析的资格，以及原因。"""
    receipt = _receipt_for(receipt)
    moment = _now(at)
    eligible = receipt.is_effective(moment)
    if receipt.status != RELEASED:
        reason = f"状态为「{receipt.status}」，未发布"
    elif not receipt.effective_at:
        reason = "已发布但未登记生效时间"
    elif not eligible:
        reason = f"尚未到生效时间 {receipt.effective_at}"
    else:
        reason = "已发布且已到生效时间，可进入实际解析"
    return {
        "回执编号": receipt.receipt_id,
        "规则编号": receipt.rule_id,
        "规则版本": receipt.rule_version,
        "目标站点": receipt.target_site,
        "来源作业": receipt.job_id,
        "status": receipt.status,
        "发布结论": receipt.conclusion,
        "生效时间": receipt.effective_at,
        "发布时间": receipt.released_at,
        "试跑完成": receipt.trial_finished(),
        "可进入解析": eligible,
        "判定": reason,
    }


def list_receipt_eligibility(
    *,
    job_id: str | None = None,
    target_site: str | None = None,
    at: Any = None,
) -> list[dict[str, Any]]:
    """切换前后都按回检查看：同一站点/作业的历史回执逐条给出生效资格。"""
    result = []
    for receipt in all_receipts():
        if job_id and receipt.job_id != job_id:
            continue
        if target_site and receipt.target_site != target_site:
            continue
        result.append(receipt_eligibility(receipt, at))
    result.sort(key=lambda item: (to_utc(item["生效时间"]), item["回执编号"]))
    return result


def claim_pages(receipt: RuleReleaseReceipt | str, pages: Iterable[str]) -> RuleReleaseReceipt:
    """登记回执认领的页面范围：生效后只有这些页面归该版本解析。

    不设置认领范围时，同站点页面都按最新生效回执解析。
    """
    receipt = _receipt_for(receipt)
    receipt.claimed_pages = [str(page).strip() for page in pages if str(page).strip()]
    save_receipt(receipt)
    return receipt


def plan_job_versions(
    job: CrawlJobSpec | dict[str, Any] | str,
    *,
    at: Any = None,
) -> list[dict[str, Any]]:
    """查询一个作业逐页实际会使用的规则版本，以及尚未被任何生效回执认领的页面。

    这正是“查新作业实际使用的版本”：不真正写日志，只按当前生效回执给出计划。
    传入作业编号时从 store 里还原作业入口页面。
    """
    from urllib.parse import urlparse

    if isinstance(job, str):
        job = job_from_store(job)
    spec, job_id, site = _job_parts(job)
    receipts = effective_receipts(job, at)
    formal_urls = {
        log.source_url for log in query_parse_logs(job_id=job_id, mode="正式解析")
    }
    plan: list[dict[str, Any]] = []
    for url in spec.entry_urls:
        host = urlparse(url).netloc
        receipt = next(
            (r for r in receipts
             if (not r.target_site or not host or host == r.target_site)
             and _receipt_claims(r, url)),
            None,
        )
        plan.append({
            "来源URL": url,
            "已正式解析": url in formal_urls,
            "将使用规则": receipt.rule_id if receipt else "",
            "将使用版本": receipt.rule_version if receipt else "",
            "发布回执": receipt.receipt_id if receipt else "",
            "字段口径": _schema_text(receipt.field_schema) if receipt else "",
            "判定": (
                "已完成正式解析" if url in formal_urls
                else ("等待发布回执" if receipt is None else "可进入实际解析")
            ),
        })
    return plan


def job_from_store(job_id: str) -> CrawlJobSpec:
    """从 store 还原作业；没有登记时退化为只带编号和站点的空作业。"""
    for row in store.rows("crawl_job"):
        if str(row.get("作业编号")) == job_id:
            site_row = next(
                (item for item in store.rows("crawl_site")
                 if str(item.get("站点域名")) == str(row.get("目标站点"))),
                None,
            )
            site = SiteConfig.from_seed_row(site_row) if site_row else None
            return CrawlJobSpec.from_seed_row(row, site=site)
    return CrawlJobSpec(job_id=job_id, site=None)  # type: ignore[arg-type]


def _job_parts(job: CrawlJobSpec | dict[str, Any]) -> tuple[CrawlJobSpec, str, str]:
    if isinstance(job, CrawlJobSpec):
        spec = job
        raw_job_id = job.job_id
        site = job.site.domain if job.site else ""
    else:
        spec = CrawlJobSpec.from_seed_row(job)
        raw_job_id = str(job.get("作业编号") or spec.job_id)
        site = str(job.get("目标站点") or (spec.site.domain if spec.site else ""))
    return spec, raw_job_id, site


def effective_receipts(
    job: CrawlJobSpec | dict[str, Any],
    at: Any = None,
) -> list[RuleReleaseReceipt]:
    """当前生效的回执按优先级排序：作业绑定优先，其次站点绑定；最新生效在前。

    作业没有专属回执时，同站点已发布且在生效时间内的回执对任何新作业都有效，
    这正是“切换后新作业实际使用新版本”的入口。多张回执各自认领不同页面时，
    解析端逐页取第一张认领该页面的回执。
    """
    _, job_id, site = _job_parts(job)
    moment = _now(at)
    job_bound = [r for r in all_receipts() if r.job_id == job_id and r.is_effective(moment)]
    site_bound = [r for r in all_receipts()
                  if r.target_site == site and r.is_effective(moment)] if site else []
    pool: list[RuleReleaseReceipt] = []
    for receipt in job_bound + site_bound:
        if receipt.receipt_id not in {item.receipt_id for item in pool}:
            pool.append(receipt)
    pool.sort(key=lambda r: (to_utc(r.effective_at), to_utc(r.released_at), r.receipt_id),
              reverse=True)
    return pool


def effective_receipt(
    job: CrawlJobSpec | dict[str, Any],
    at: Any = None,
) -> RuleReleaseReceipt | None:
    """选中当前优先级最高的生效回执。"""
    pool = effective_receipts(job, at)
    return pool[0] if pool else None


def _receipt_claims(receipt: RuleReleaseReceipt, url: str) -> bool:
    """回执是否认领该页面；没有登记认领范围时视为整站页面都归它。"""
    return not receipt.claimed_pages or url in receipt.claimed_pages


def parse_claimed_pages(
    job: CrawlJobSpec | dict[str, Any],
    *,
    pages: Iterable[str] | None = None,
    at: Any = None,
) -> tuple[list[ParseLogEntry], list[tuple[str, str]]]:
    """解析端按发布回执认领网页：逐页写入实际使用的规则版本。

    回执如果登记了认领页面，只有认领范围内的页面归它解析；范围外的页面继续
    等待后续发布的回执。返回 (新写入的日志, [(跳过的页面, 原因)])，同一页面
    只会被正式解析一次。
    """
    from urllib.parse import urlparse

    spec, job_id, site = _job_parts(job)
    requested = [str(page).strip() for page in (pages or spec.entry_urls) if str(page).strip()]
    formal_urls = {
        log.source_url for log in query_parse_logs(job_id=job_id, mode="正式解析")
    }
    receipts = effective_receipts(job, at)
    parsed: list[ParseLogEntry] = []
    skipped: list[tuple[str, str]] = []

    for url in requested:
        if url in formal_urls:
            skipped.append((url, "该页面已完成正式解析"))
            continue
        host = urlparse(url).netloc
        receipt = next(
            (r for r in receipts
             if (not r.target_site or not host or host == r.target_site)
             and _receipt_claims(r, url)),
            None,
        )
        if receipt is None:
            if not receipts:
                skipped.append((url, "没有已发布且已生效的规则回执"))
            elif any(r.target_site and host and host != r.target_site for r in receipts):
                skipped.append((url, f"页面域名 {host} 与回执站点不匹配"))
            else:
                skipped.append((url, "该页面不在当前生效回执的认领范围内"))
            continue
        log = _formal_log(receipt, url, at=at)
        _persist_parse_log(log)
        parsed.append(log)
        formal_urls.add(url)

    if parsed:
        _refresh_job_progress(job_id, spec, requested)
    return parsed, skipped


def _refresh_job_progress(job_id: str, spec: CrawlJobSpec, requested: list[str]) -> None:
    """解析完成后更新作业进度，供验收判断是否还有页面未执行。"""
    job_pages = spec.entry_urls or requested
    parsed_urls = {
        log.source_url for log in query_parse_logs(job_id=job_id, mode="正式解析")
    }
    completed = len([url for url in job_pages if url in parsed_urls])
    spec.completed_pages = completed
    spec.failed_pages = sum(
        1 for log in query_parse_logs(job_id=job_id, mode="正式解析")
        if log.source_url in job_pages and log.parse_result != "成功"
    )
    if completed >= len(job_pages) and job_pages:
        spec.status = "已完成" if spec.failed_pages == 0 else "部分失败"
    else:
        spec.status = "采集执行"
    for row in store.rows("crawl_job"):
        if str(row.get("作业编号")) == job_id:
            row["完成页数"] = spec.completed_pages
            row["失败页数"] = spec.failed_pages
            row["status"] = spec.status
            row["pending"] = spec.status == "采集执行"
            row["abnormal"] = spec.status == "部分失败"
            return


def _extract_fields(url: str, field_names: list[str]) -> tuple[dict[str, str], list[str]]:
    lowered = url.lower()
    if "error" in lowered or "404" in lowered:
        return {}, ["页面不可达"]
    if "empty" in lowered:
        return {}, ["页面内容为空"]
    fields = {name: f"extracted:{name}:{abs(hash(url + name)) % 10000}" for name in field_names}
    return fields, []


def _trial_log(
    receipt: RuleReleaseReceipt,
    rule: ExtractRule,
    url: str,
    index: int,
    *,
    at: Any = None,
    force_fail: bool = False,
) -> ParseLogEntry:
    fields, errors = _extract_fields(url, rule.field_names() or list(receipt.field_schema))
    if force_fail and not errors:
        errors = ["试跑页面解析失败"]
    return ParseLogEntry(
        record_id=f"PLOG-TRIAL-{receipt.receipt_id}-{index:04d}",
        source_url=url,
        parse_rule_id=rule.rule_id,
        parse_result="成功" if not errors else "失败",
        error_type="字段缺失" if errors else "",
        retry_count=0,
        error_detail="; ".join(errors),
        recorded_at=_iso(_now(at)),
        rule_version=receipt.rule_version,
        release_receipt_id=receipt.receipt_id,
        source_job_id=receipt.job_id,
        field_schema=dict(receipt.field_schema),
        effective_at=receipt.effective_at,
        parse_mode="试跑",
    )


def _formal_log(receipt: RuleReleaseReceipt, url: str, *, at: Any = None) -> ParseLogEntry:
    fields, errors = _extract_fields(url, list(receipt.field_schema))
    return ParseLogEntry(
        record_id=_next_log_id(receipt.job_id),
        source_url=url,
        parse_rule_id=receipt.rule_id,
        parse_result="成功" if fields and not errors else "失败",
        error_type="字段缺失" if errors else "",
        retry_count=0,
        error_detail="; ".join(errors),
        recorded_at=_iso(_now(at)),
        rule_version=receipt.rule_version,
        release_receipt_id=receipt.receipt_id,
        source_job_id=receipt.job_id,
        field_schema=dict(receipt.field_schema),
        effective_at=receipt.effective_at,
        parse_mode="正式解析",
    )


def _next_log_id(job_id: str) -> str:
    prefix = f"PLOG-{job_id}-"
    existing = {str(row.get("记录编号", "")) for row in store.rows("parse_log")}
    index = 1
    while f"{prefix}{index:04d}" in existing:
        index += 1
    return f"{prefix}{index:04d}"


# ---------------------------------------------------------------------------
# 解析日志台账查询
# ---------------------------------------------------------------------------

def query_parse_logs(
    *,
    rule_version: str | None = None,
    job_id: str | None = None,
    receipt_id: str | None = None,
    mode: str | None = None,
    source_url: str | None = None,
    schema: dict[str, str] | None = None,
) -> list[ParseLogEntry]:
    """按版本、作业、回执、解析方式查台账；日志只增不改，切换前后都可查。"""
    wanted_fingerprint = schema_fingerprint(schema) if schema is not None else None
    result: list[ParseLogEntry] = []
    for row in store.rows("parse_log"):
        entry = ParseLogEntry.from_seed_row(row)
        if rule_version is not None and entry.rule_version != rule_version:
            continue
        if job_id is not None and entry.source_job_id != job_id:
            continue
        if receipt_id is not None and entry.release_receipt_id != receipt_id:
            continue
        if mode is not None and entry.parse_mode != mode:
            continue
        if source_url is not None and entry.source_url != source_url:
            continue
        if wanted_fingerprint is not None and entry.schema_fingerprint() != wanted_fingerprint:
            continue
        result.append(entry)
    return result


def logs_grouped_by_version(
    job_id: str,
    *,
    mode: str | None = None,
) -> dict[str, list[ParseLogEntry]]:
    """同一作业按实际使用的规则版本分组；默认含试跑和正式解析，发布结论单列。"""
    grouped: dict[str, list[ParseLogEntry]] = {}
    for entry in query_parse_logs(job_id=job_id):
        if entry.parse_mode == "发布结论":
            continue
        if mode is not None and entry.parse_mode != mode:
            continue
        grouped.setdefault(entry.rule_version or "未登记版本", []).append(entry)
    return grouped


def _has_log(receipt_id: str, url: str, *, parse_mode: str) -> bool:
    """台账里是否已有该回执在该页面上的某种日志（试跑/正式解析）。"""
    return any(
        log.release_receipt_id == receipt_id and log.source_url == url
        and log.parse_mode == parse_mode
        for log in query_parse_logs(receipt_id=receipt_id, source_url=url, mode=parse_mode)
    )


# ---------------------------------------------------------------------------
# 候选结果批与批次验收
# ---------------------------------------------------------------------------

def _persist_parse_log(log: ParseLogEntry) -> None:
    """台账只追加：同一记录编号重复出现时加序号，保留每一次解析痕迹。"""
    rows = store.rows("parse_log")
    existing = {str(row.get("记录编号", "")) for row in rows}
    record_id = log.record_id
    if record_id in existing:
        base, _, suffix = record_id.rpartition("-")
        if suffix.isdigit():
            index = int(suffix) + 1
            while f"{base}-{index:04d}" in existing:
                index += 1
            record_id = f"{base}-{index:04d}"
        else:
            index = 2
            while f"{record_id}-R{index}" in existing:
                index += 1
            record_id = f"{record_id}-R{index}"
    rows.append({
        "id": max((safe_int(row.get("id"), 0) for row in rows), default=0) + 1,
        **log_row(log, record_id),
    })


def log_row(log: ParseLogEntry, record_id: str | None = None) -> dict[str, Any]:
    """解析日志的对外列：台账字段单独保留，避免覆盖业务动作列。"""
    return {
        "记录编号": record_id or log.record_id,
        "来源URL": log.source_url,
        "解析规则": log.parse_rule_id,
        "解析结果": log.parse_result,
        "异常类型": log.error_type,
        "重试次数": log.retry_count,
        "错误详情": log.error_detail,
        "记录时间": log.recorded_at,
        "规则版本": log.rule_version,
        "发布回执": log.release_receipt_id,
        "来源作业": log.source_job_id,
        "字段口径": _schema_text(log.field_schema),
        "生效时间": log.effective_at,
        "解析方式": log.parse_mode,
        "status": "成功" if log.parse_result == "成功" else "格式异常",
        "pending": False,
        "abnormal": log.parse_result != "成功",
    }


def build_candidate_batch(
    *,
    job_id: str | None = None,
    urls: Iterable[str] | None = None,
    logs: Iterable[ParseLogEntry] | None = None,
    candidate_id: str = "",
) -> CandidateBatch:
    """把正式解析日志聚成候选批；候选批本身不携带任何可交付凭据。"""
    url_filter = {str(url).strip() for url in urls} if urls is not None else None
    if logs is None:
        entries = [
            log for log in query_parse_logs(job_id=job_id, mode="正式解析")
            if url_filter is None or log.source_url in url_filter
        ]
    else:
        entries = [
            log for log in logs
            if log.parse_mode == "正式解析" and (url_filter is None or log.source_url in url_filter)
        ]

    if job_id is None and entries:
        derived = {log.source_job_id for log in entries if log.source_job_id}
        if len(derived) == 1:
            job_id = derived.pop()

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for log in entries:
        fingerprint = log.schema_fingerprint()
        key = (log.source_job_id, fingerprint)
        group = groups.setdefault(key, {
            "来源作业": log.source_job_id,
            "字段口径": _schema_text(log.field_schema),
            "口径指纹": fingerprint,
            "规则版本": [],
            "页面": [],
            "数据行数": 0,
            "解析失败": 0,
        })
        if log.rule_version and log.rule_version not in group["规则版本"]:
            group["规则版本"].append(log.rule_version)
        if log.source_url and log.source_url not in group["页面"]:
            group["页面"].append(log.source_url)
        group["数据行数"] += 1
        if log.parse_result != "成功":
            group["解析失败"] += 1

    fingerprints = sorted({log.schema_fingerprint() for log in entries})
    versions = sorted({log.rule_version for log in entries if log.rule_version})
    source_jobs = sorted({log.source_job_id for log in entries if log.source_job_id})
    source_urls = sorted({log.source_url for log in entries if log.source_url})

    first_schema: dict[str, str] = {}
    if entries:
        first_schema = dict(entries[0].field_schema)

    reason = _candidate_rejection_reason(entries, fingerprints, source_jobs)
    candidate_id = candidate_id or f"CAND-{job_id or 'MIX'}-{len(store.rows('candidate_batch')) + 1}"
    candidate = CandidateBatch(
        candidate_id=candidate_id,
        job_id=job_id or "",
        source_urls=source_urls,
        rule_versions=versions,
        field_schema=first_schema if len(fingerprints) == 1 else {},
        schema_fingerprints=fingerprints,
        status=CANDIDATE_PENDING,
        deliverable=False,  # 候选批永远不直接可交付，必须经过验收
        rejection_reason=reason,
        groups=list(groups.values()),
    )
    _save_candidate(candidate)
    return candidate


def _candidate_rejection_reason(
    entries: list[ParseLogEntry],
    fingerprints: list[str],
    source_jobs: list[str],
) -> str:
    if not entries:
        return "没有可聚合的正式解析日志"
    if len(fingerprints) > 1:
        return "字段口径不兼容：候选批内存在多个字段口径指纹，不能直接作为同一个结果批"
    if len(source_jobs) > 1:
        return "来源作业不同，不能直接合并为同一个结果批"
    if any(log.parse_result != "成功" for log in entries):
        return "候选批内存在解析失败页面，需先恢复审核"
    return ""


def _candidate_row(candidate: CandidateBatch) -> dict[str, Any]:
    return {
        "候选批编号": candidate.candidate_id,
        "来源作业": candidate.job_id,
        "来源URL": ",".join(candidate.source_urls),
        "规则版本": ",".join(candidate.rule_versions),
        "字段口径": _schema_text(candidate.field_schema),
        "口径指纹": ",".join(candidate.schema_fingerprints),
        "数据行数": len(candidate.source_urls),
        "驳回原因": candidate.rejection_reason,
        "可交付": candidate.deliverable,
        "status": candidate.status,
        "pending": candidate.status != CANDIDATE_PASSED,
        "abnormal": candidate.status == CANDIDATE_REJECTED,
    }


def _save_candidate(candidate: CandidateBatch) -> dict[str, Any]:
    rows = store.rows("candidate_batch")
    for row in rows:
        if str(row.get("候选批编号")) == candidate.candidate_id:
            row.update(_candidate_row(candidate))
            return row
    row = {"id": max((safe_int(item.get("id"), 0) for item in rows), default=0) + 1}
    row.update(_candidate_row(candidate))
    rows.append(row)
    return row


def candidate_from_row(row: dict[str, Any]) -> CandidateBatch:
    return CandidateBatch(
        candidate_id=str(row.get("候选批编号", "")),
        job_id=str(row.get("来源作业", "")),
        source_urls=[part for part in str(row.get("来源URL", "")).split(",") if part],
        rule_versions=[part for part in str(row.get("规则版本", "")).split(",") if part],
        field_schema=parse_schema(row.get("字段口径", ""), "string"),
        schema_fingerprints=[part for part in str(row.get("口径指纹", "")).split(",") if part],
        status=str(row.get("status", CANDIDATE_PENDING)),
        deliverable=bool(row.get("可交付")),
        rejection_reason=str(row.get("驳回原因", "")),
    )


def accept_candidate_batch(
    candidate: CandidateBatch | str,
    *,
    logs: Iterable[ParseLogEntry] | None = None,
    job: CrawlJobSpec | dict[str, Any] | None = None,
    at: Any = None,
) -> BatchAcceptance:
    """批次验收：核对日志口径能否一起交付，全部通过才签发可交付凭据。"""
    if not isinstance(candidate, CandidateBatch):
        target = next(
            (candidate_from_row(row) for row in store.rows("candidate_batch")
             if str(row.get("候选批编号")) == str(candidate)),
            None,
        )
        if target is None:
            raise ReleaseFlowError(f"候选批 {candidate} 不存在")
        candidate = target
    if candidate.status == CANDIDATE_PASSED:
        raise ReleaseFlowError(f"候选批 {candidate.candidate_id} 已验收通过，不能重复验收")

    if logs is None:
        entries = [
            log for log in query_parse_logs(job_id=candidate.job_id or None, mode="正式解析")
            if not candidate.source_urls or log.source_url in candidate.source_urls
        ]
    else:
        entries = [log for log in logs if log.parse_mode == "正式解析"]

    fingerprints = sorted({log.schema_fingerprint() for log in entries})
    source_jobs = sorted({log.source_job_id for log in entries if log.source_job_id})
    versions = sorted({log.rule_version for log in entries if log.rule_version})
    reason = _candidate_rejection_reason(entries, fingerprints, source_jobs)

    if not reason and job is not None:
        spec, job_id, _ = _job_parts(job)
        missing = [
            url for url in spec.entry_urls
            if not any(log.source_url == url for log in entries)
        ]
        if missing:
            reason = f"作业仍有 {len(missing)} 个页面未执行，不能验收交付"
        elif spec.failed_pages > 0:
            reason = "作业存在解析失败页面，不能验收交付"

    if not reason and not entries:
        reason = "没有可交付的正式解析日志"

    deliverable = not reason
    if not can_candidate_transition(candidate.status, CANDIDATE_PASSED if deliverable else CANDIDATE_REJECTED):
        raise ReleaseFlowError(
            f"候选批 {candidate.candidate_id} 当前为「{candidate.status}」，不能给出新的验收结论"
        )

    candidate.status = CANDIDATE_PASSED if deliverable else CANDIDATE_REJECTED
    candidate.deliverable = deliverable
    candidate.rejection_reason = reason
    _save_candidate(candidate)

    acceptance = BatchAcceptance(
        acceptance_id=f"ACC-{candidate.candidate_id}-{len(store.rows('batch_acceptance')) + 1}",
        candidate_id=candidate.candidate_id,
        job_id=candidate.job_id,
        deliverable=deliverable,
        reason=reason or "日志口径一致，准予一起交付",
        schema_fingerprints=fingerprints,
        rule_versions=versions,
        row_count=len(entries),
        credential=f"DELIV-{candidate.candidate_id}" if deliverable else "",
        checked_at=_iso(_now(at)),
    )
    _save_acceptance(acceptance)
    return acceptance


def can_candidate_transition(current: str, target: str) -> bool:
    return target in CANDIDATE_TRANSITIONS.get(current, set())


def _save_acceptance(acceptance: BatchAcceptance) -> None:
    rows = store.rows("batch_acceptance")
    rows.append({
        "id": max((safe_int(row.get("id"), 0) for row in rows), default=0) + 1,
        "验收编号": acceptance.acceptance_id,
        "候选批编号": acceptance.candidate_id,
        "来源作业": acceptance.job_id,
        "可交付": acceptance.deliverable,
        "可交付凭据": acceptance.credential,
        "验收结论": acceptance.reason,
        "口径指纹": ",".join(acceptance.schema_fingerprints),
        "规则版本": ",".join(acceptance.rule_versions),
        "数据行数": acceptance.row_count,
        "验收时间": acceptance.checked_at,
        "status": "可交付" if acceptance.deliverable else "不可交付",
        "pending": False,
        "abnormal": not acceptance.deliverable,
    })


# ---------------------------------------------------------------------------
# 作业级明细查询
# ---------------------------------------------------------------------------

def inspect_job(job_id: str, *, at: Any = None) -> dict[str, Any]:
    """查一个作业的版本资格、两个版本的解析日志和候选/验收明细。"""
    grouped = logs_grouped_by_version(job_id)
    formal = logs_grouped_by_version(job_id, mode="正式解析")
    logs_detail = {
        version: [
            {
                "记录编号": entry.record_id,
                "来源URL": entry.source_url,
                "解析规则": entry.parse_rule_id,
                "解析方式": entry.parse_mode,
                "解析结果": entry.parse_result,
                "字段口径": _schema_text(entry.field_schema),
                "口径指纹": entry.schema_fingerprint(),
                "生效时间": entry.effective_at,
            }
            for entry in entries
        ]
        for version, entries in grouped.items()
    }
    candidates = [
        row for row in store.rows("candidate_batch")
        if str(row.get("来源作业")) == job_id
    ]
    acceptances = [
        row for row in store.rows("batch_acceptance")
        if str(row.get("来源作业")) == job_id
    ]
    return {
        "来源作业": job_id,
        "生效资格": list_receipt_eligibility(job_id=job_id, at=at),
        "解析日志": logs_detail,
        "解析日志条数": {version: len(entries) for version, entries in grouped.items()},
        "正式解析条数": {version: len(entries) for version, entries in formal.items()},
        "版本计划": plan_job_versions(job_id, at=at),
        "候选批": candidates,
        "批次验收": acceptances,
    }


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CANDIDATE_PENDING",
    "CANDIDATE_PASSED",
    "CANDIDATE_REJECTED",
    "INTERRUPTED",
    "PENDING_RELEASE",
    "REJECTED",
    "RELEASED",
    "RETIRED",
    "ReleaseFlowError",
    "TRIALING",
    "TRIAL_FAILED",
    "accept_candidate_batch",
    "all_receipts",
    "build_candidate_batch",
    "can_candidate_transition",
    "can_transition",
    "claim_pages",
    "create_release_receipt",
    "effective_receipt",
    "effective_receipts",
    "inspect_job",
    "job_from_store",
    "list_receipt_eligibility",
    "load_receipt",
    "logs_grouped_by_version",
    "log_row",
    "parse_claimed_pages",
    "plan_job_versions",
    "query_parse_logs",
    "receipt_eligibility",
    "recover_trial",
    "reject_release",
    "release_rule",
    "retire_release",
    "run_rule_trial",
    "save_candidate",
    "save_receipt",
    "submit_for_release",
]
