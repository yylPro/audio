from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any

from .audio_quality_processor import AudioQualityError, QualityIssue, QualityResult, RuleHit, TranscriptSegment, join_transcript, normalize_text, scan_rule_hits


class DeepSeekQualityError(AudioQualityError):
    pass


ACTION_RULE_IDS = (
    "missing_ask",
    "missing_check",
    "missing_compare",
    "missing_calculate",
    "missing_retention_action",
    "missing_retention_success",
)

SERVICE_IDENTITY_PATTERN = re.compile(r"(?:移动|中国移动|10086).{0,12}(?:工作人员|后台|客服|专员)|(?:投诉|业务)处理专员")
SERVICE_QUESTION_PATTERN = re.compile(
    r"(?:投诉|反馈|工单|问题|宽带|套餐|机顶盒|高清|业务|费用|取消|退|不用).{0,28}"
    r"(?:什么.{0,8}(?:问题|原因|情况|需求)|怎么|是否|是不是|吗|呢|有.*问题|是吗|是吧|对吗)|"
    r"(?:看到|收到|见到).{0,24}(?:投诉|反馈|反映|工单).{0,40}(?:问题|宽带|机顶盒|移动高清|套餐|业务).{0,16}(?:吗|呢|是吗|是吧|对吗)|"
    r"(?:您|你).{0,12}(?:是要|想|需要|打算).{0,20}(?:取消|不用|退|停|拆).{0,12}(?:吗|呢|是吗|是吧)|"
    r"(?:地址|小区|位置|在哪里|是哪|哪里).{0,16}(?:吗|呢|是吗|是吧)?"
)
LOCATION_CONTEXT_PATTERN = re.compile(r"(?:地址|小区|社区|哪个区|什么地方|在哪里|附近)")
OFFICE_RESULT_PATTERN = re.compile(
    r"(?:可以去|去).{0,16}(?:营业厅|[\u4e00-\u9fa5]{1,6}厅)|"
    r"(?:搜索|搜|发送|发).{0,20}(?:营业厅|地址)|"
    r"(?:地址|路|小区|社区).{0,20}(?:搜索|搜).{0,20}(?:营业厅|厅)|"
    r"(?:附近|当地|本地|那边|地址|小区|区域).{0,35}(?:营业厅|[\u4e00-\u9fa5]{1,8}厅).{0,35}(?:取消|办理|处理|权限|地址|退|回收|申请)|"
    r"(?:营业厅|[\u4e00-\u9fa5]{1,8}厅).{0,35}(?:权限|办理|回收|取消|地址|营业|处理|退|申请|可以|能)|"
    r"(?:身份证|设备|光猫|路由器).{0,35}(?:营业厅|[\u4e00-\u9fa5]{1,8}厅|取消|退|回收|交还|办理)|"
    r"(?:权限).{0,24}(?:不一定有|有|没有|办理|取消|处理)"
)
OFFICE_QUESTION_PATTERN = re.compile(r"(?:去|到).{0,8}(?:哪个|哪家|哪里).{0,8}(?:营业厅|厅)|(?:哪个|哪家|哪里).{0,8}(?:营业厅|厅)")
CUSTOMER_OFFICE_PLAN_PATTERN = re.compile(r"(?:^|[，,。]).{0,3}(?:我|客户|下次我|之后我).{0,12}(?:去|到).{0,12}(?:营业厅|厅)")
OBSERVED_RECORD_PATTERN = re.compile(
    r"(?:我|这边|系统|后台).{0,12}(?:看到|看见|显示|查到|找到|找到了).{0,36}"
    r"(?:工单|投诉|反馈|宽带|套餐|业务|业务费|增值业务费|扣费|取消|设备|费用|流量|合约|权益|基础包|权益包|生活权益包|话费券|移动高清|中国移动APP|APP)"
)
VERIFICATION_CODE_PATTERN = re.compile(r"(?:短信|手机|动态|审核|授权|登录|业务)?(?:验证码|动态码|短信码)|看个短信|看一下10086")
VERIFICATION_LOOKUP_PATTERN = re.compile(r"找到了|查到|查到了|看到|显示|查询到|中国移动APP|APP|权益会")
VERIFICATION_BUSINESS_PATTERN = re.compile(
    r"(?:增值业务费|业务费|移动高清|基础包|权益包|生活权益包|话费券|扣费|收费|套餐|账单|费用|月租|中国移动APP|APP|权益会).{0,24}"
    r"(?:\d{1,4}\s*(?:元|块|块钱)|可以|有一个|还有一个|查询|搜索|登录|领|领取|扣费|收费)|"
    r"\d{1,4}\s*(?:元|块|块钱).{0,24}(?:增值业务|业务费|基础包|权益包|生活权益包|话费券|费用|套餐)"
)
VERIFICATION_CALCULATION_PATTERN = re.compile(
    r"(?:增值业务费|业务费|套餐费|月租|基础包|权益包|生活权益包|移动高清|话费券).{0,18}"
    r"(?:\d{1,4}\s*(?:元|块|块钱)|可以领|领取|权益|费用)|"
    r"\d{1,4}\s*(?:元|块|块钱).{0,18}(?:增值业务|业务费|基础包|权益包|生活权益包|话费券|费用)|"
    r"(?:中国移动APP|APP|权益会).{0,28}(?:查询|搜索|登录|领|领取|话费券|权益)"
)
RETENTION_OFFER_PATTERN = re.compile(
    r"(?:我|这边|我们).{0,16}(?:帮|给).{0,12}(?:申请|办理|改成|改为|改|调整|优惠|减免|保留|补退)|"
    r"(?:可以|要不|建议).{0,18}(?:申请|办理|改成|改为|优惠|减免|免费|继续使用|保留)|"
    r"(?:优惠|减免|免费|不用钱|0元|改\s*0\s*元|加\s*\d+\s*元).{0,24}(?:继续使用|继续用|保留|一年|两年|申请)|"
    r"(?:继续保留|保留使用|继续使用).{0,16}(?:宽带|套餐|业务)"
)
FINANCIAL_COMPARISON_PATTERN = re.compile(
    r"(?:原来|之前|当前|现在|套餐|资费|费用|月租).{0,32}\d{1,4}\s*(?:元|块)|"
    r"\d{1,4}\s*(?:元|块).{0,32}(?:改成|改为|优惠|减免|返还|补退|免费|不用钱)|"
    r"(?:优惠|减免|返还|补退|免费|不用钱|0元|每月加|每个月加).{0,24}(?:\d{1,4}\s*(?:元|块)|一年|两年|继续使用)"
)
BENEFIT_OUTCOME_PATTERN = re.compile(
    r"(?:优惠|减免|返还|补退|免|免费|不用钱|不收费|不会再扣|不再扣|0元|每月|每个月|一年|两年).{0,28}(?:\d{1,4}\s*多?\s*(?:元|块)|一年|两年|费用|继续使用|生效|扣费|权益)|"
    r"\d{1,4}\s*多?\s*(?:元|块).{0,28}(?:优惠|减免|返还|补退|免费|不用钱|每月|套餐|费用|一年|两年)"
)
SUCCESS_REASON_PATTERN = re.compile(r"客户.{0,10}(?:同意|接受|愿意|确认|决定|选择).{0,20}(?:优惠|方案|申请|办理|继续使用|保留)")
SUCCESS_EVIDENCE_PATTERN = re.compile(r"(?:那我先|我先).{0,12}(?:试一下|用一下|继续用|保留)|(?:嗯|哦)?好的.{0,20}(?:申请|办理|优惠)|继续保留|保留[、,， ]*保留")
REJECTION_PATTERN = re.compile(r"(?:我|我们|客户).{0,14}(?:不需要|不要|不用了|想取消|要取消|坚持取消|退掉)|不同意|不接受|拒绝|继续投诉|坚持.{0,8}(?:取消|退订|投诉)")


@dataclass(frozen=True)
class DeepSeekAssessment:
    actions: dict[str, dict[str, Any]]
    summary: str
    uncertain_parts: str
    raw_response: str


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise DeepSeekQualityError("DeepSeek 未返回可解析的 JSON 对象")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise DeepSeekQualityError(f"DeepSeek JSON 解析失败：{exc}") from exc
    if not isinstance(value, dict):
        raise DeepSeekQualityError("DeepSeek 返回的 JSON 根节点不是对象")
    return value


def _valid_evidence(value: Any, transcript: str) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized_transcript = normalize_text(transcript)
    result: list[str] = []
    for item in value:
        quote = normalize_text(item)
        # Require the model to cite an actual ASR phrase, rather than inventing a summary.
        if quote and quote in normalized_transcript and quote not in result:
            result.append(quote)
    return result


def validate_assessment(payload: dict[str, Any], transcript: str, raw_response: str = "") -> DeepSeekAssessment:
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, dict):
        raise DeepSeekQualityError("DeepSeek 返回缺少 actions 对象")
    actions: dict[str, dict[str, Any]] = {}
    for rule_id in ACTION_RULE_IDS:
        item = raw_actions.get(rule_id, {})
        if not isinstance(item, dict):
            item = {}
        evidence = _valid_evidence(item.get("evidence"), transcript)
        present = bool(item.get("present", False)) and bool(evidence)
        actions[rule_id] = {
            "present": present,
            "evidence": evidence,
            "reason": normalize_text(item.get("reason")),
        }
    return DeepSeekAssessment(actions, normalize_text(payload.get("summary")), normalize_text(payload.get("uncertain_parts")), raw_response)


def reinforce_ask_evidence(assessment: DeepSeekAssessment, segments: list[TranscriptSegment]) -> DeepSeekAssessment:
    """Keep a direct service-question from the transcript from being lost in model evidence ranking."""
    current_ask = assessment.actions["missing_ask"]
    if current_ask.get("present"):
        return assessment
    previous_text = ""
    direct_questions: list[str] = []
    for segment in segments:
        text = normalize_text(segment.text)
        staff_context = bool(SERVICE_IDENTITY_PATTERN.search(text) or SERVICE_IDENTITY_PATTERN.search(previous_text))
        if staff_context and SERVICE_QUESTION_PATTERN.search(text) and text not in direct_questions:
            direct_questions.append(text)
        previous_text = text
    if not direct_questions:
        return assessment
    actions = dict(assessment.actions)
    ask = dict(actions["missing_ask"])
    existing = [item for item in ask.get("evidence", []) if item not in direct_questions]
    ask["present"] = True
    ask["evidence"] = direct_questions + existing
    ask["reason"] = "客服已询问投诉、业务问题或客户具体诉求。"
    actions["missing_ask"] = ask
    return replace(assessment, actions=actions)


def reinforce_location_lookup_evidence(assessment: DeepSeekAssessment, segments: list[TranscriptSegment]) -> DeepSeekAssessment:
    """Preserve evidence that staff used the customer's location to identify a service office."""
    if not any(LOCATION_CONTEXT_PATTERN.search(normalize_text(segment.text)) for segment in segments):
        return assessment
    results: list[str] = []
    for segment in segments:
        text = normalize_text(segment.text)
        if OFFICE_RESULT_PATTERN.search(text) and not OFFICE_QUESTION_PATTERN.search(text) and not CUSTOMER_OFFICE_PLAN_PATTERN.search(text):
            if text not in results:
                results.append(text)
    if not results:
        return assessment
    actions = dict(assessment.actions)
    check = dict(actions["missing_check"])
    merged: list[str] = []
    for item in results + list(check.get("evidence", [])):
        if item and not any(item in kept or kept in item for kept in merged):
            merged.append(item)
    check["present"] = True
    check["evidence"] = merged
    check["reason"] = "客服根据客户地址或小区信息查询并告知了具体营业厅、地址、权限或办理条件。"
    actions["missing_check"] = check
    return replace(assessment, actions=actions)


def reinforce_observed_record_evidence(assessment: DeepSeekAssessment, segments: list[TranscriptSegment]) -> DeepSeekAssessment:
    """Preserve explicit system or backend observations of customer records as check evidence."""
    observed = [normalize_text(segment.text) for segment in segments if OBSERVED_RECORD_PATTERN.search(normalize_text(segment.text))]
    if not observed:
        return assessment
    actions = dict(assessment.actions)
    check = dict(actions["missing_check"])
    merged: list[str] = []
    for item in observed + list(check.get("evidence", [])):
        if item and not any(item in kept or kept in item for kept in merged):
            merged.append(item)
    check["present"] = True
    check["evidence"] = merged
    check["reason"] = "客服明确读取并说明了系统或后台中的客户工单、投诉或业务记录。"
    actions["missing_check"] = check
    return replace(assessment, actions=actions)


def reinforce_verification_lookup_evidence(assessment: DeepSeekAssessment, segments: list[TranscriptSegment]) -> DeepSeekAssessment:
    """Treat SMS-code-gated lookup followed by concrete business details as check evidence."""
    texts = [normalize_text(segment.text) for segment in segments]
    code_indexes = [index for index, text in enumerate(texts) if VERIFICATION_CODE_PATTERN.search(text)]
    if not code_indexes:
        return assessment

    check_evidence: list[str] = []
    calculation_evidence: list[str] = []
    for index in code_indexes:
        window = texts[index:min(len(texts), index + 7)]
        window_text = " ".join(window)
        if not (VERIFICATION_LOOKUP_PATTERN.search(window_text) and VERIFICATION_BUSINESS_PATTERN.search(window_text)):
            continue
        for text in window:
            if text and (
                VERIFICATION_CODE_PATTERN.search(text)
                or VERIFICATION_LOOKUP_PATTERN.search(text)
                or VERIFICATION_BUSINESS_PATTERN.search(text)
            ):
                if text not in check_evidence:
                    check_evidence.append(text)
            if text and VERIFICATION_CALCULATION_PATTERN.search(text) and text not in calculation_evidence:
                calculation_evidence.append(text)

    if not check_evidence:
        return assessment

    actions = dict(assessment.actions)

    def merge(rule_id: str, evidence: list[str], reason: str) -> None:
        item = dict(actions[rule_id])
        merged: list[str] = []
        for quote in evidence + list(item.get("evidence", [])):
            if quote and not any(quote in kept or kept in quote for kept in merged):
                merged.append(quote)
        item["present"] = True
        item["evidence"] = merged
        item["reason"] = reason
        actions[rule_id] = item

    merge("missing_check", check_evidence, "客服通过短信验证码或动态码核验后，查询并说明了客户实际业务、费用或权益信息。")
    if calculation_evidence:
        merge("missing_calculate", calculation_evidence, "客服拆解说明了增值业务费、基础包、权益包、APP权益或话费券等费用/权益内容。")
    return replace(assessment, actions=actions)


def reconcile_retention_semantics(assessment: DeepSeekAssessment, segments: list[TranscriptSegment]) -> DeepSeekAssessment:
    """Enforce consistency between a retention offer, its economics, and customer acceptance."""
    texts = [normalize_text(segment.text) for segment in segments]
    offer_indexes = [index for index, text in enumerate(texts) if RETENTION_OFFER_PATTERN.search(text)]
    if not offer_indexes:
        return assessment

    actions = dict(assessment.actions)

    def merge(rule_id: str, evidence: list[str], reason: str) -> None:
        item = dict(actions[rule_id])
        merged: list[str] = []
        for quote in evidence + list(item.get("evidence", [])):
            if quote and not any(quote in kept or kept in quote for kept in merged):
                merged.append(quote)
        item["present"] = True
        item["evidence"] = merged
        item["reason"] = reason
        actions[rule_id] = item

    offer_evidence = [texts[index] for index in offer_indexes]
    merge("missing_retention_action", offer_evidence, "客服提出优惠、减免、免费、改档或继续使用方案以争取客户保留业务。")

    context_indexes: set[int] = set()
    for index in offer_indexes:
        context_indexes.update(range(max(0, index - 2), min(len(texts), index + 3)))
    comparison_evidence = [texts[index] for index in sorted(context_indexes) if FINANCIAL_COMPARISON_PATTERN.search(texts[index])]
    calculation_evidence = [texts[index] for index in sorted(context_indexes) if BENEFIT_OUTCOME_PATTERN.search(texts[index])]
    if comparison_evidence:
        merge("missing_compare", offer_evidence + comparison_evidence, "挽留方案明确体现了优惠前后、金额、资费或资源差异。")
    if calculation_evidence:
        merge("missing_calculate", offer_evidence + calculation_evidence, "挽留方案明确说明了客户可获得的费用、期限、减免或权益结果。")

    success = actions["missing_retention_success"]
    success_reason = normalize_text(success.get("reason"))
    if not success.get("present"):
        success_evidence = [text for text in texts if SUCCESS_EVIDENCE_PATTERN.search(text)]
        post_offer_text = " ".join(texts[min(offer_indexes):])
        reason_accepts = bool(SUCCESS_REASON_PATTERN.search(success_reason) and not re.search(r"未明确|未同意|拒绝|坚持|最终.*(?:取消|退订|投诉)", success_reason))
        behavior_accepts = bool(success_evidence and not REJECTION_PATTERN.search(post_offer_text))
        if reason_accepts or behavior_accepts:
            merge("missing_retention_success", success_evidence or offer_evidence, "客户明确同意、允许办理或按客服提出的挽留方案继续处理。")

    return replace(assessment, actions=actions)


class DeepSeekQualityScorer:
    def __init__(self, config: dict[str, Any]):
        self.enabled = bool(config.get("enabled", True))
        self.api_key_env = str(config.get("api_key_env", "DEEPSEEK_API_KEY"))
        self.base_url = str(config.get("base_url", "https://api.deepseek.com/chat/completions"))
        self.model = str(config.get("model", "deepseek-chat"))
        self.timeout_seconds = int(config.get("timeout_seconds", 120))
        self.max_input_chars = int(config.get("max_input_chars", 18000))

    def assess(self, segments: list[TranscriptSegment]) -> DeepSeekAssessment:
        if not self.enabled:
            raise DeepSeekQualityError("DeepSeek 语义评分未启用")
        api_key = os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            raise DeepSeekQualityError(f"环境变量 {self.api_key_env} 未配置")
        transcript = join_transcript(segments)
        if len(transcript) > self.max_input_chars:
            transcript = transcript[:self.max_input_chars] + "\n[后续转录因长度限制未发送]"
        body = {
            "model": self.model,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是中国移动降挽录音质检审核员。仅依据给出的转录原文判断，不得补写、猜测或改写证据。只返回 JSON。"},
                {"role": "user", "content": self._prompt(transcript)},
            ],
        }
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise DeepSeekQualityError(f"DeepSeek 请求失败：HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise DeepSeekQualityError(f"DeepSeek 请求失败：{exc}") from exc
        content = str((((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""))
        return validate_assessment(_extract_json(content), transcript, content)

    @staticmethod
    def _prompt(transcript: str) -> str:
        return f'''请审核以下录音转录。说话人标签可能不可靠，不要因标签而忽略明显是客服的话。

定义：
1. 问（missing_ask）：客服对客户的投诉、反馈、工单、故障、扣费、业务异常、取消、套餐、宽带或设备情况提出任何追问、确认或核对，均成立；不只限于问“为什么取消”。例如“投诉的移动高清有什么问题吗”“投诉的问题是什么原因呢”“宽带不用了是吗”“这个工单是投诉宽带问题吗”“我这里看到你有一个工单是投诉宽带问题的是吗”“之前打过10086投诉宽带问题吗”“您是在什么地方”“在哪个小区”“宽带地址是哪里”“附近有营业厅吗”“哪天方便”“是否继续使用”“设备是否还在”都必须判为已体现。客户回答不要求紧挨在问题之后，可结合整个对话的前后文判断。
2. 查（missing_check）：客服查询并给出客户实际情况，例如套餐、流量、账单、合约、宽带、权限。客户提供/查看短信验证码、动态码、短信码，是客服登录、核验或查询客户资料/业务状态的重要前置证据；若随后客服说“找到了/查到/看到/显示”并说明增值业务费、基础包、权益包、APP内业务、话费券、扣费来源或收费明细，必须判定为查。客服明确说“我这里看到”“从这里看到”“系统看到/显示”“后台看到/查到”客户的工单、投诉、反馈、业务记录或处理状态，并说明内容，也属于查。客服询问地址、小区或所在区域后，根据这些信息定位并告知具体营业厅、网点地址、权限或办理条件，也属于查；只有说“我查一下”但没有查到结果，不成立。
3. 比（missing_compare）：客服为挽留而推荐替代套餐/方案，并比较前后方案的价格、资源、优惠或多得到的内容；“优惠15元”“优惠一年、原本二十多元/28元的费用”“返还15元”等优惠金额和优惠前后的费用说明都成立；“原来/当前88元，改成78元”成立；为挽留提出“改成0元、不用钱、免费继续使用”“每月加1元继续使用”也属于比。前后信息可以在相隔多句中，分别引用两句证据。
4. 算（missing_calculate）：客服说明推荐方案的收益、成本、权益、扣费/生效后果；套餐内项目、增值业务费、移动高清基础包、生活权益包、话费券、APP/权益会领取路径等费用和内容拆解也成立。“改成0元、不用钱、免费继续使用”“每月加1元可继续使用”“续约可改1元套餐”“优惠一年、返还15元”都明确属于算。
5. 挽留动作（missing_retention_action）：客服提出让客户继续使用、保留、改套餐、优惠、减免、免费、0元/1元继续使用、申请、上单、补退或其他可执行替代处理。比如“帮你申请做个优惠”“给你改0元宽带不用钱继续使用”“每月加1元继续使用”“继续保留使用”“帮您上单补退”都成立。
6. 挽留成功（missing_retention_success）：客户明确同意、接受、办理或继续使用；仅客服提出方案不成立。

语义一致性要求：
1. 客服为了让客户继续使用业务而提出优惠、减免、免费、改档、保留、补退或其他替代方案时，“挽留动作”必须为 true。不能出现理由写“客服提出优惠方案”，present 却为 false。
2. 挽留方案说明优惠前后、原价与新价、优惠金额、免费、套餐/资源差异时，“比”必须为 true。
3. 挽留方案说明客户最终少付多少、每月成本、优惠期限、返还/补退金额、免费结果或可获得权益时，“算”必须为 true。
4. 客户明确同意、表示“好的/可以”、允许客服申请办理、按方案继续处理且后续没有拒绝，也属于挽留成功；不要求必须逐字说“我同意”。客户明确拒绝、坚持取消、仍表示不需要或继续投诉时才为 false。

每项的 present 只能在有原文证据时为 true。evidence 必须逐字摘录原文中连续出现的一小段；找不到则 []。每一项都要通读完整转录，列出所有明确、相关且不重复的客服原文证据，不要只挑一两句代表句。reason 用简短中文说明。不要把普通业务回访硬判为挽留。

证据选择优先级：对“问”，必须优先引用客服直接询问投诉、业务、套餐、宽带或移动高清“有什么问题/什么情况/什么原因/是否需要”的原句；这类主问题证据优先级高于后续询问地点、营业厅和时间。例如“我想请问一下，你投诉的那个移动高清是有什么问题吗”必须作为“问”的首条证据，不能被后续“你什么时候方便”替代。

补充规则：
1. 客服为了定位营业厅或制定处理方案而询问客户地址、小区、所在区域时，该问句属于“问”；随后根据这些信息给出具体营业厅、地址、权限或办理条件，属于“查”。
2. “我这里看到”“从这里看到”“系统看到/显示”“后台看到/查到”后面说明了客户工单、投诉、套餐、费用、用量、合约或业务状态，属于“查”。
3. “您方便看个短信验证码吗/看一下动态码”之后，客服说“找到了”并解释增值业务费、移动高清基础包、生活权益包、话费券、中国移动APP或权益会等内容，必须列入“查”；其中费用金额、权益包和话费券说明也必须列入“算”。
4. 客服确认“收到/看到/见到客户反馈、投诉或工单”，并问“是不是宽带/机顶盒/移动高清/套餐问题”，属于“问”；同一句如果还体现“我这里看到工单/后台看到记录”，同时属于“查”。
5. 符合定义的客服证据都要列出，不限制条数；客户自己的提问、反问和陈述不能作为客服的“问”或“查”证据。

返回格式：
{{"actions":{{"missing_ask":{{"present":true,"evidence":["原文短句"],"reason":""}},"missing_check":{{"present":false,"evidence":[],"reason":""}},"missing_compare":{{"present":false,"evidence":[],"reason":""}},"missing_calculate":{{"present":false,"evidence":[],"reason":""}},"missing_retention_action":{{"present":false,"evidence":[],"reason":""}},"missing_retention_success":{{"present":false,"evidence":[],"reason":""}}}},"summary":"中文摘要","uncertain_parts":"没有则留空"}}

转录原文：
{transcript}'''


def assessment_to_quality_result(assessment: DeepSeekAssessment, segments: list[TranscriptSegment], rules: dict[str, Any]) -> QualityResult:
    deductions = {normalize_text(item.get("id")): max(0, int(item.get("deduction", 0))) for item in rules.get("required_actions", []) if isinstance(item, dict)}
    hits: list[RuleHit] = []
    for rule_id in ACTION_RULE_IDS:
        action = assessment.actions[rule_id]
        if action["present"]:
            continue
        label = next((normalize_text(x.get("label")) for x in rules.get("required_actions", []) if normalize_text(x.get("id")) == rule_id), rule_id)
        hits.append(RuleHit(rule_id, 0.0, "未找到明确证据", action["reason"] or f"未体现{label}", deductions.get(rule_id, 5)))
    # Deterministic conduct-risk checks remain local and are not delegated to the model.
    hits.extend(scan_rule_hits(segments, None, rules))
    score = max(0, min(100, 100 - sum(hit.deduction for hit in hits)))
    issues = [QualityIssue(hit.rule_id, hit.start_seconds, hit.quote, hit.description, "请结合原始转录复核后完善沟通话术。", 1.0) for hit in hits]
    evidence_summary = "；".join(
        f"{rule_id.replace('missing_', '')}：{'已体现' if action['present'] else '未体现'}"
        for rule_id, action in assessment.actions.items()
    )
    analysis = "；".join(issue.reason for issue in issues) if issues else "未发现需要扣分的明确问题。"
    if assessment.uncertain_parts:
        analysis += f"；需人工复核：{assessment.uncertain_parts}"
    return QualityResult(
        "DeepSeek语义审核", 1.0, {"综合质检": score}, score,
        score >= int(rules.get("qualified_score", 80)) and not bool(assessment.uncertain_parts),
        assessment.summary or evidence_summary, analysis,
        "请以转录原文及证据片段为准复核。", issues, bool(assessment.uncertain_parts),
    )
