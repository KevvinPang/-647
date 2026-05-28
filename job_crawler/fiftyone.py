import asyncio
import html as html_lib
import json
import urllib.parse
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .browser_backend import launch_persistent_context_with_fallback, using_adspower, using_gologin
from .constants import FIFTYONE_CITY_CODE_MAP, FIFTYONE_SEARCH_URL
from .crawled_links import CrawledLinkStore
from .gologin_backend import (
    launch_gologin_browser,
    stop_gologin_api,
    using_gologin_api,
    get_gologin_executable_path,
)
from .output import build_job_record_key
from .stealth_js import STEALTH_INIT_SCRIPT
from .adspower_backend import launch_adspower_browser
from .utils import *  # noqa: F403

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


def extract_51job_detail_summary_from_html(html: str) -> str:
    """从 51job 详情页提取职位描述正文。"""
    structured_summary = extract_51job_detail_summary_from_nuxt_state(html)
    if structured_summary:
        return structured_summary

    soup = BeautifulSoup(html, "html.parser")
    selector_candidates = [
        "div.bmsg.job_msg.inbox",
        "div.job_msg",
        "div.job-detail",
        "div.jobDetail",
        "div.tCompany_main",
        "div[class*='job_msg']",
        "div[class*='job-detail']",
    ]
    for selector in selector_candidates:
        candidates = []
        for node in soup.select(selector):
            primary_node = node.find("div", recursive=False) or node
            text = clean_multiline_text(primary_node.get_text("\n", strip=True))
            if looks_like_job_summary_text(text):
                candidates.append(text)
        if candidates:
            return max(candidates, key=len)

    body_text = clean_multiline_text(soup.get_text("\n", strip=True))
    for marker in ["职位信息", "职位描述", "岗位职责", "工作职责", "任职要求"]:
        idx = body_text.find(marker)
        if idx < 0:
            continue
        snippet = clean_multiline_text(body_text[idx : idx + 6000])
        if looks_like_job_summary_text(snippet):
            return snippet
    return ""


def extract_51job_detail_summary_from_nuxt_state(html: str) -> str:
    """Prefer structured detail text embedded in 51job's Nuxt state."""
    summary = ""
    patterns = [
        r'jobDescribe:"((?:\\.|[^"])*)"',
        r'"jobDescribe":"((?:\\.|[^"])*)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.S)
        if not match:
            continue
        try:
            candidate = clean_multiline_text(json.loads(f'"{match.group(1)}"'))
        except Exception:
            candidate = clean_multiline_text(match.group(1).replace("\\u002F", "/"))
        if looks_like_job_summary_text(candidate) or len(candidate) >= 80:
            if len(candidate) > len(summary):
                summary = candidate
    return summary


def extract_51job_detail_extras_from_html(html: str) -> dict[str, str]:
    """Best-effort extraction for extra fields visible on 51job detail pages."""
    soup = BeautifulSoup(html, "html.parser")
    body_text = clean_multiline_text(soup.get_text("\n", strip=True))

    def dedupe_texts(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = clean_text(value)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result

    def decode_js_fragment(value: str) -> str:
        try:
            return clean_text(json.loads(f'"{value}"'))
        except Exception:
            return clean_text(value.replace("\\u002F", "/"))

    def extract_script_string(field_name: str) -> str:
        match = re.search(rf'{re.escape(field_name)}:"((?:\\.|[^"])*)"', html)
        if not match:
            return ""
        return decode_js_fragment(match.group(1))

    lines = [clean_text(line) for line in body_text.split("\n") if clean_text(line)]

    header_node = soup.select_one(".tHeader.tHjob")
    header_lines = (
        [clean_text(span.get_text(" ", strip=True)) for span in header_node.select("p.msg.ltype span")]
        if header_node
        else []
    )
    header_lines = [line for line in header_lines if line]

    address = ""
    address_node = soup.select_one(".job-address .bmsg .fp")
    if address_node:
        address = clean_text(address_node.get_text(" ", strip=True))
    if not address:
        address = extract_script_string("address")
    if not address:
        for marker in ["工作地址", "上班地址", "地址"]:
            idx = body_text.find(marker)
            if idx < 0:
                continue
            snippet = clean_multiline_text(body_text[idx : idx + 240])
            snippet_lines = [line for line in snippet.split("\n") if clean_text(line)]
            for candidate in snippet_lines[1:4]:
                candidate_text = clean_text(candidate)
                if candidate_text and candidate_text not in {"点击查看地图", "地图"}:
                    address = candidate_text
                    break
            if address:
                break

    degree = ""
    experience = ""
    degree_tokens = ["初中", "高中", "中专", "大专", "本科", "硕士", "博士", "学历"]
    experience_tokens = [
        "应届",
        "在校生",
        "经验",
        "实习生",
        "1年",
        "2年",
        "3年",
        "4年",
        "5年",
        "10年",
        "无需经验",
        "无经验",
    ]
    for line in header_lines + lines[:30]:
        if not degree and any(token in line for token in degree_tokens):
            degree = line
        if not experience and any(token in line for token in experience_tokens):
            experience = line
        if degree and experience:
            break
    if not degree:
        degree = extract_script_string("degreeString")
    if not experience:
        experience = extract_script_string("workYearString")

    tag_nodes = soup.select(".job-detail .mt10 p.fp")
    tags: list[str] = []
    for node in tag_nodes:
        label_node = node.select_one(".label")
        label = clean_text(label_node.get_text(" ", strip=True)) if label_node else ""
        if "关键" not in label:
            continue
        tags.extend(
            clean_text(anchor.get_text(" ", strip=True))
            for anchor in node.select("a")
            if clean_text(anchor.get_text(" ", strip=True))
        )
    if not tags:
        script_keywords = extract_script_string("jobKeywordString")
        if script_keywords:
            tags.extend(re.split(r"[,，/、\s]+", script_keywords))
    if not tags:
        for marker in ["关键词：", "关键词", "关键字：", "关键字"]:
            idx = body_text.find(marker)
            if idx < 0:
                continue
            snippet = clean_multiline_text(body_text[idx : idx + 200])
            snippet_lines = [clean_text(line) for line in snippet.split("\n") if clean_text(line)]
            if len(snippet_lines) >= 2:
                tags.extend(re.split(r"[ /、,，]+", clean_text(" ".join(snippet_lines[1:3]))))
                if tags:
                    break
    tags = dedupe_texts(tags)

    benefits = [
        clean_text(node.get_text(" ", strip=True))
        for node in soup.select(".job-detail .tags .tag")
        if clean_text(node.get_text(" ", strip=True))
    ]
    if not benefits:
        script_welfare = extract_script_string("welfare")
        if script_welfare:
            benefits.extend(re.split(r"[,，/、]+", script_welfare))
    if not benefits:
        for line in lines:
            if line.startswith("·") or line.startswith("-"):
                clean_line = clean_text(line.lstrip("·- "))
                if clean_line and len(clean_line) <= 40:
                    benefits.append(clean_line)
            if len(benefits) >= 6:
                break
    benefits = dedupe_texts(benefits)

    remark_parts = []
    if tags:
        remark_parts.append("关键词：" + " / ".join(tags))

    return {
        "详细地址": address,
        "学历要求": degree,
        "经验要求": experience,
        "福利标签": " / ".join(benefits),
        "备注": " / ".join(remark_parts),
    }


def build_51job_detail_url(job_id: str) -> str:
    """根据 51job 列表页 jobId 构造详情页链接。"""
    text = clean_text(job_id)
    if not text:
        return ""
    return f"https://jobs.51job.com/all/{urllib.parse.quote(text)}.html"


def build_51job_search_url(keyword: str, city_name: str) -> str:
    """构造 51job 搜索 URL。常用城市使用 jobArea 编码。"""
    normalized_city = normalize_city_name(city_name)
    params = {
        "keyword": keyword,
        "searchType": "2",
        "sortType": "0",
    }
    city_code = FIFTYONE_CITY_CODE_MAP.get(normalized_city)
    if city_code:
        params["jobArea"] = city_code
    return f"{FIFTYONE_SEARCH_URL}?{urllib.parse.urlencode(params)}"


def parse_51job_jobs_from_dom(html: str) -> list[dict[str, Any]]:
    """从 51job 搜索结果页 DOM 解析岗位列表。"""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.joblist-item")
    use_new_layout = False
    if not cards:
        cards = soup.select("div.job-item")
        use_new_layout = bool(cards)
    if use_new_layout:
        converted_cards: list[str] = []
        for card in cards:
            anchor_node = card.select_one("a[href]")
            detail_url = normalize_absolute_url(
                anchor_node.get("href", "") if anchor_node else "",
                "https://jobs.51job.com",
            )
            job_name_node = card.select_one(".job-name")
            salary_node = card.select_one(".salary")
            company_node = card.select_one(".company")
            location_node = card.select_one(".location")

            job_name = clean_text(job_name_node.get_text(" ", strip=True)) if job_name_node else ""
            salary = clean_text(salary_node.get_text(" ", strip=True)) if salary_node else ""
            company_name = clean_text(company_node.get_text(" ", strip=True)) if company_node else ""
            area = clean_text(location_node.get_text(" ", strip=True)) if location_node else ""
            if not job_name and not company_name:
                continue

            converted_cards.append(
                "<div class='joblist-item'>"
                f"<div class='jname'>{html_lib.escape(job_name)}</div>"
                f"<div class='sal'>{html_lib.escape(salary)}</div>"
                f"<div class='area'>{html_lib.escape(area)}</div>"
                f"<a class='comp' href='{html_lib.escape(detail_url, quote=True)}'>"
                f"<span class='cname'>{html_lib.escape(company_name)}</span>"
                "</a>"
                "</div>"
            )
        if converted_cards:
            return parse_51job_jobs_from_dom("<html><body>" + "".join(converted_cards) + "</body></html>")
        return []
    jobs: list[dict[str, Any]] = []

    for card in cards:
        job_node = card.select_one("div.joblist-item-job")
        sensors_data = {}
        if job_node and job_node.get("sensorsdata"):
            try:
                sensors_data = json.loads(job_node.get("sensorsdata", "{}"))
            except json.JSONDecodeError:
                sensors_data = {}

        job_name = clean_text(
            sensors_data.get("jobTitle")
            or (card.select_one(".jname").get_text() if card.select_one(".jname") else "")
        )
        salary = clean_text(
            sensors_data.get("jobSalary")
            or (card.select_one(".sal").get_text() if card.select_one(".sal") else "")
        )
        area = clean_text(sensors_data.get("jobArea", ""))
        if not area:
            area_node = card.select_one(".area")
            area = clean_text(area_node.get_text(" ", strip=True)) if area_node else ""

        company_node = card.select_one("a.comp .cname")
        company_name = clean_text(company_node.get_text()) if company_node else ""
        company_link_node = card.select_one("a.comp")

        company_meta = [
            clean_text(node.get_text())
            for node in card.select("a.comp .bc .dc")
            if clean_text(node.get_text())
        ]
        company_size = ""
        for value in reversed(company_meta):
            if any(token in value for token in ["人", "少于", "以上"]):
                company_size = value
                break

        tags = [
            clean_text(node.get_text())
            for node in card.select(".joblist-item-tags .tag")
            if clean_text(node.get_text())
        ]

        job_id = clean_text(str(sensors_data.get("jobId", "")))
        detail_url = build_51job_detail_url(job_id)
        if not detail_url and company_link_node:
            detail_url = normalize_absolute_url(company_link_node.get("href", ""), "https://jobs.51job.com")

        city = normalize_city_name(area)
        job_time = normalize_publish_time_text(sensors_data.get("jobTime", ""))

        if not job_name and not company_name:
            continue

        jobs.append(
            {
                "招聘平台": "51job",
                "岗位类型一级": "",
                "岗位类型二级": "",
                "岗位名称": job_name or "未知岗位",
                "岗位类型企业/公务员/事业单位/军队文职": "企业",
                "公司名称": company_name or "未知单位",
                "公司规模": company_size,
                "所在省份": infer_province(city),
                "城市": city,
                "详细地址": area,
                "学历要求": clean_text(sensors_data.get("jobDegree", "")),
                "经验要求": clean_text(sensors_data.get("jobYear", "")),
                "薪资范围": salary,
                "福利标签": format_tags(tags),
                "工作内容": "",
                "任职要求": "",
                "岗位链接": detail_url,
                "发布时间": job_time,
                "投递起始时间": job_time,
                "投递截止时间": "",
                "证书要求": "",
                "备注": "；".join([x for x in ["公司信息：" + " / ".join(company_meta) if company_meta else ""] if x]),
                "__工作城市": area,
                "__详情链接": detail_url,
                "__岗位摘要": "",
            }
        )

    return jobs


def looks_like_51job_verification_page(html: str) -> bool:
    """判断 51job 是否进入滑块验证页。"""
    text = clean_text(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    return any(token in text for token in ["访问验证", "滑动滑块", "拖动到最右边", "请按住滑块"])


async def search_51job_keyword(
    page,
    keyword: str,
    city_name: str,
    settings: dict[str, Any],
) -> None:
    """打开 51job 搜索页并触发列表加载。"""
    direct_url = build_51job_search_url(keyword, city_name)
    try:
        await page.goto(direct_url, wait_until="domcontentloaded", timeout=90000)
    except Exception as exc:
        print(f"打开 51job 搜索页异常：{exc}，正在重试...")
        await human_sleep(*settings["delays"]["retry_reload"])
        await page.goto(direct_url, wait_until="domcontentloaded", timeout=90000)

    await human_sleep(*settings["delays"]["after_open_search"])
    if await page.locator(".joblist-item, .job-item").count() > 0:
        return

    html = await page.content()
    visible_input_count = await page.locator("input:visible").count()
    if settings["manual_auth"] and (
        looks_like_51job_verification_page(html) or visible_input_count == 0
    ):
        await wait_for_manual_51job_auth(
            context=page.context,
            page=page,
            settings=settings,
            reason="搜索页需要登录/验证后才显示搜索框",
        )
        try:
            await page.goto(direct_url, wait_until="domcontentloaded", timeout=90000)
            await human_sleep(*settings["delays"]["after_open_search"])
            if await page.locator(".joblist-item, .job-item").count() > 0:
                return
        except Exception:
            pass

    try:
        search_input = page.locator("input:visible").first
        await search_input.fill(keyword)
        await search_input.press("Enter")
    except Exception as exc:
        print(f"51job 输入关键词失败：{exc}")
        return

    await human_sleep(*settings["delays"]["after_open_search"])

    # 51job 的城市筛选偶尔会保留默认城市；这里尽量点击目标城市，再由后续解析做严格过滤。
    if city_name:
        try:
            await page.get_by_text(city_name, exact=True).first.click(timeout=5000)
            await human_sleep(*settings["delays"]["after_open_search"])
        except Exception:
            pass


async def wait_for_manual_51job_auth(context, page, settings: dict[str, Any], reason: str) -> None:
    """等待用户人工完成 51job 登录。持久化 Profile 会自动保存会话。"""
    wait_seconds = int(settings["auth_wait_seconds"])
    print(
        f"51job 需要人工处理：{reason}。请在打开的浏览器中使用手机号/短信验证码登录，"
        f"程序将在 {wait_seconds} 秒后继续。"
    )
    while True:
        try:
            await asyncio.sleep(2)
            current_html = await page.content()
            if not looks_like_51job_verification_page(current_html):
                break
        except Exception:
            await asyncio.sleep(1)
    print(f"51job 真实浏览器会话已保存在：{settings['user_data_dir']}")

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await human_sleep(*settings["delays"]["after_open_search"])


async def login_51job_profile(settings: dict[str, Any]) -> None:
    """打开持久化浏览器 Profile，让用户真实登录 51job。"""
    if async_playwright is None:
        raise RuntimeError(
            "缺少 Playwright Python 依赖，请先运行：pip install -r requirements.txt。"
            "orbita_cdp 主流程不需要安装 Playwright 自带 Chromium。"
        )

    user_data_dir = Path(settings["user_data_dir"])
    user_data_dir.mkdir(parents=True, exist_ok=True)
    wait_seconds = int(settings["auth_wait_seconds"])

    async with async_playwright() as p:
        gl = None
        if using_adspower(settings):
            browser, context, gl = await launch_adspower_browser(p, settings)
        elif using_gologin_api(settings):
            # API 模式
            browser, context, gl = await launch_gologin_browser(p, settings)
        elif using_gologin(settings):
            # 本地模式：Orbita 引擎（不传额外 args，Orbita 内置反检测）
            user_data_dir = Path(settings["user_data_dir"])
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = await launch_persistent_context_with_fallback(
                p.chromium,
                user_data_dir=user_data_dir,
                headless=False,
                executable_path=get_gologin_executable_path(),
                ignore_https_errors=True,
                user_agent=settings["user_agent"],
                viewport=settings["viewport"],
                locale="zh-CN",
            )
            await context.add_init_script(
                STEALTH_INIT_SCRIPT,
            )
        else:
            user_data_dir = Path(settings["user_data_dir"])
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = await launch_persistent_context_with_fallback(
                p.chromium,
                user_data_dir=user_data_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_https_errors=True,
                user_agent=settings["user_agent"],
                viewport=settings["viewport"],
                locale="zh-CN",
            )
            await context.add_init_script(
                STEALTH_INIT_SCRIPT,
            )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://we.51job.com/pc/login", wait_until="domcontentloaded", timeout=90000)
        print(
            f"已打开 51job 登录页。请用手机号和短信验证码完成真实登录，"
            f"程序将在 {wait_seconds} 秒后保存 Profile 并退出。"
        )
        await asyncio.sleep(wait_seconds)
        if using_gologin(settings):
            print(f"51job Gologin session will be saved on stop.")
        else:
            print(f"51job 登录 Profile 已保存：{user_data_dir}")
        await context.close()
        if gl is not None:
            stop_gologin_api(gl)


async def fetch_51job_summary_from_detail_page(
    detail_page,
    context,
    detail_url: str,
    settings: dict[str, Any],
) -> tuple[str, dict[str, str]]:
    """访问 51job 详情页，尽量提取工作内容/任职要求正文。"""
    if not detail_url:
        return "", {}

    max_retries = settings["max_detail_retries"]
    for attempt in range(1, max_retries + 2):
        try:
            await human_sleep(*settings["delays"]["before_open_detail"])
            await detail_page.goto(
                detail_url,
                wait_until="domcontentloaded",
                timeout=settings["detail_page_timeout_ms"],
            )
            await human_sleep(*settings["delays"]["after_open_detail"])
            html = await detail_page.content()

            if looks_like_51job_verification_page(html):
                if settings["manual_auth"]:
                    await wait_for_manual_51job_auth(
                        context=context,
                        page=detail_page,
                        settings=settings,
                        reason="详情页触发验证",
                    )
                    html = await detail_page.content()
                else:
                    emit_task_log(settings, f"51job 详情页触发验证，已跳过：{detail_url}")
                    return "", {}

            summary = extract_51job_detail_summary_from_html(html)
            extras = extract_51job_detail_extras_from_html(html)
            if summary:
                return summary, extras

            if attempt > max_retries:
                return "", extras
            await human_sleep(*settings["delays"]["detail_retry"])
        except Exception as exc:
            if attempt > max_retries:
                emit_task_log(settings, f"51job 详情页抓取失败，已放弃：{detail_url}，原因：{exc}")
                return "", {}
            await human_sleep(*settings["delays"]["detail_retry"])

    return "", {}


async def enrich_51job_jobs_with_detail_summaries(
    context,
    jobs: list[dict[str, Any]],
    settings: dict[str, Any],
    crawled_link_store: CrawledLinkStore | None = None,
    item_callback=None,
) -> int:
    """补全 51job 岗位详情。详情页可能需要人工验证。"""
    if not jobs:
        return 0

    updated_count = 0
    detail_page = await context.new_page()
    try:
        total_jobs = len(jobs)
        for index, item in enumerate(jobs, start=1):
            detail_url = clean_text(str(item.get("__详情链接", "")))
            if not detail_url:
                if callable(item_callback):
                    item_callback(item, index)
                if is_cancel_requested(settings):
                    emit_cancel_log_once(settings, "收到中止请求，当前详情已处理完成，停止继续分析剩余详情。")
                    break
                continue
            if crawled_link_store is not None and crawled_link_store.contains(detail_url):
                emit_task_log(settings, f"51job 详情链接已抓取过，跳过 ({index}/{total_jobs})：{detail_url}")
                if callable(item_callback):
                    item_callback(item, index)
                if is_cancel_requested(settings):
                    emit_cancel_log_once(settings, "收到中止请求，当前详情已处理完成，停止继续分析剩余详情。")
                    break
                continue
            if crawled_link_store is not None:
                if not crawled_link_store.add(detail_url):
                    emit_task_log(settings, f"51job 详情链接被其他任务记录，跳过 ({index}/{total_jobs})：{detail_url}")
                    if callable(item_callback):
                        item_callback(item, index)
                    if is_cancel_requested(settings):
                        emit_cancel_log_once(settings, "收到中止请求，当前详情已处理完成，停止继续分析剩余详情。")
                        break
                    continue
                crawled_link_store.save()
            update_task_progress(settings, current_detail_url=detail_url, detail_index=index, detail_total=total_jobs)
            emit_task_log(settings, f"51job 正在分析详情链接 ({index}/{total_jobs})：{detail_url}")
            summary, extras = await fetch_51job_summary_from_detail_page(
                detail_page=detail_page,
                context=context,
                detail_url=detail_url,
                settings=settings,
            )
            if not summary:
                if callable(item_callback):
                    item_callback(item, index)
                if is_cancel_requested(settings):
                    emit_cancel_log_once(settings, "鏀跺埌涓璇锋眰锛屽綋鍓嶈鎯呭凡澶勭悊瀹屾垚锛屽仠姝㈢户缁垎鏋愬墿浣欒鎯呫€?")
                    break
                continue
            work_content, requirement = split_job_summary(summary)
            changed = False
            for field in ["详细地址", "学历要求", "经验要求", "福利标签"]:
                extra_value = clean_text(extras.get(field, ""))
                if extra_value and clean_text(str(item.get(field, ""))) != extra_value:
                    item[field] = extra_value
                    changed = True
            extra_remark = clean_text(extras.get("备注", ""))
            if extra_remark:
                current_remark = clean_text(str(item.get("备注", "")))
                merged_remark = merge_distinct_text(current_remark, extra_remark)
                if merged_remark and merged_remark != current_remark:
                    item["备注"] = merged_remark
                    changed = True
            if work_content and item.get("工作内容") != work_content:
                item["工作内容"] = work_content
                changed = True
            if requirement and item.get("任职要求") != requirement:
                item["任职要求"] = requirement
                changed = True
            if changed:
                updated_count += 1
            await human_sleep(*settings["delays"]["between_details"])
            if callable(item_callback):
                item_callback(item, index)
            if is_cancel_requested(settings):
                emit_cancel_log_once(settings, "收到中止请求，当前详情已处理完成，停止继续分析剩余详情。")
                break
    finally:
        await detail_page.close()
    return updated_count


async def crawl_51job(
    keyword: str,
    city: str,
    settings: dict[str, Any],
    crawled_link_store: CrawledLinkStore | None = None,
) -> list[dict]:
    """爬取 51job 搜索列表数据并返回岗位记录。"""
    if async_playwright is None:
        raise RuntimeError(
            "缺少 Playwright Python 依赖，请先运行：pip install -r requirements.txt。"
            "orbita_cdp 主流程不需要安装 Playwright 自带 Chromium。"
        )

    jobs: list[dict[str, Any]] = []
    seen = set()

    async with async_playwright() as p:
        gl = None
        if using_adspower(settings):
            browser, context, gl = await launch_adspower_browser(p, settings)
            profile_ready = True
        elif using_gologin_api(settings):
            # API 模式
            browser, context, gl = await launch_gologin_browser(p, settings)
            profile_ready = True
        elif using_gologin(settings):
            # 本地模式：Orbita 引擎（不传额外 args，Orbita 内置反检测）
            user_data_dir = Path(settings["user_data_dir"])
            profile_ready = user_data_dir.exists() and any(user_data_dir.rglob("Cookies"))
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = await launch_persistent_context_with_fallback(
                p.chromium,
                user_data_dir=user_data_dir,
                headless=settings["headless"],
                executable_path=get_gologin_executable_path(),
                ignore_https_errors=True,
                user_agent=settings["user_agent"],
                viewport=settings["viewport"],
                locale="zh-CN",
            )
            await context.add_init_script(
                STEALTH_INIT_SCRIPT,
            )
        else:
            user_data_dir = Path(settings["user_data_dir"])
            profile_ready = user_data_dir.exists() and any(user_data_dir.rglob("Cookies"))
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = await launch_persistent_context_with_fallback(
                p.chromium,
                user_data_dir=user_data_dir,
                headless=settings["headless"],
                args=["--disable-blink-features=AutomationControlled"],
                ignore_https_errors=True,
                user_agent=settings["user_agent"],
                viewport=settings["viewport"],
                locale="zh-CN",
            )
            await context.add_init_script(
                STEALTH_INIT_SCRIPT,
            )
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            region_label = city or "不限地区"
            update_task_progress(
                settings,
                platform="51job",
                keyword=keyword,
                region=region_label,
                page=1,
                total_pages=settings["max_pages_per_region"],
                cumulative_count=0,
                current_detail_url="",
            )
            emit_task_log(settings, f"开始抓取 51job：关键词={keyword}，地区={region_label}")
            await search_51job_keyword(
                page=page,
                keyword=keyword,
                city_name=city,
                settings=settings,
            )
            html = await page.content()
            if looks_like_51job_verification_page(html) and settings["manual_auth"]:
                await wait_for_manual_51job_auth(
                    context=context,
                    page=page,
                    settings=settings,
                    reason="搜索页触发验证",
                )
                await search_51job_keyword(
                    page=page,
                    keyword=keyword,
                    city_name=city,
                    settings=settings,
                )

            current_page = 1
            while current_page <= settings["max_pages_per_region"]:
                if is_cancel_requested(settings):
                    emit_cancel_log_once(settings, "收到中止请求，停止读取新的页面。")
                    break
                update_task_progress(
                    settings,
                    platform="51job",
                    keyword=keyword,
                    region=region_label,
                    page=current_page,
                    total_pages=settings["max_pages_per_region"],
                    current_detail_url="",
                )
                emit_task_log(
                    settings,
                    f"51job 正在读取第 {current_page}/{settings['max_pages_per_region']} 页："
                    f"关键词={keyword}，地区={region_label}",
                )
                await human_sleep(*settings["delays"]["between_pages"])
                html = await page.content()
                raw_page_jobs = parse_51job_jobs_from_dom(html)

                if not raw_page_jobs:
                    emit_task_log(settings, f"51job 第 {current_page} 页未解析到岗位，停止当前搜索。")
                    break

                page_jobs = [
                    item
                    for item in raw_page_jobs
                    if is_job_in_target_city(str(item.get("__工作城市", "")), city)
                ]
                filtered_count = len(raw_page_jobs) - len(page_jobs)
                existing_filtered_count = 0
                existing_output_record_keys = settings.get("_current_output_record_keys")
                if settings.get("filter_existing_output_early") and isinstance(existing_output_record_keys, set):
                    filtered_page_jobs = []
                    for item in page_jobs:
                        if build_job_record_key(item) in existing_output_record_keys:
                            existing_filtered_count += 1
                            continue
                        filtered_page_jobs.append(item)
                    page_jobs = filtered_page_jobs
                filter_text = "未进行地区过滤" if not city else f"过滤非目标地区 {filtered_count} 条"
                if existing_filtered_count:
                    filter_text += f"，过滤 Excel 已有岗位 {existing_filtered_count} 条"
                update_task_progress(
                    settings,
                    parsed_count=len(raw_page_jobs),
                    kept_count=len(page_jobs),
                    filtered_count=filtered_count,
                    existing_filtered_count=existing_filtered_count,
                    cumulative_count=len(jobs),
                )
                emit_task_log(
                    settings,
                    f"51job 第 {current_page} 页解析到 {len(raw_page_jobs)} 条，"
                    f"{filter_text}，准备处理 {len(page_jobs)} 条。",
                )

                if not page_jobs:
                    emit_task_log(
                        settings,
                        f"51job 第 {current_page} 页：解析 {len(raw_page_jobs)} 条，"
                        f"{filter_text}，但剩余岗位均已存在于当前 Excel 或已被地区过滤，已跳过。",
                    )
                    if is_cancel_requested(settings):
                        emit_cancel_log_once(settings, "收到中止请求，当前页已处理完成，停止继续翻页。")
                        break
                    if current_page >= settings["max_pages_per_region"]:
                        break
                    next_buttons = page.get_by_text("下一页", exact=True)
                    if await next_buttons.count() == 0:
                        emit_task_log(settings, "51job 已到最后一页。")
                        break
                    try:
                        await next_buttons.first.click(timeout=5000)
                    except Exception:
                        emit_task_log(settings, "51job 点击下一页失败，结束当前搜索。")
                        break
                    await human_sleep(*settings["delays"]["after_next_page"])
                    current_page += 1
                    continue

                new_count = 0
                page_result_callback = settings.get("page_result_callback")

                def handle_processed_item(item, detail_index=None):
                    nonlocal new_count
                    if not clean_text(str(item.get("岗位类别/大类", ""))):
                        item["岗位类型一级"] = keyword
                    key = build_job_record_key(item)
                    if key in seen:
                        return
                    seen.add(key)
                    if isinstance(existing_output_record_keys, set):
                        existing_output_record_keys.add(key)
                    jobs.append(item)
                    new_count += 1
                    if callable(page_result_callback):
                        page_result_callback(
                            keyword,
                            [item],
                            {
                                "platform": "51job",
                                "region": region_label,
                                "page": current_page,
                                "total_pages": settings["max_pages_per_region"],
                                "detail_index": detail_index,
                            },
                        )

                if profile_ready and not settings.get("skip_detail_fetch"):
                    detail_updated = await enrich_51job_jobs_with_detail_summaries(
                        context=context,
                        jobs=page_jobs,
                        settings=settings,
                        crawled_link_store=crawled_link_store,
                        item_callback=handle_processed_item,
                    )
                else:
                    detail_updated = 0
                    for index, item in enumerate(page_jobs, start=1):
                        handle_processed_item(item, index)
                        if is_cancel_requested(settings):
                            emit_cancel_log_once(settings, "收到中止请求，当前岗位已写入，停止继续处理剩余岗位。")
                            break

                emit_task_log(
                    settings,
                    f"51job 第 {current_page} 页：解析 {len(raw_page_jobs)} 条，"
                    f"{filter_text}，详情补全 {detail_updated} 条，"
                    f"新增 {new_count} 条（累计 {len(jobs)} 条）"
                )
                update_task_progress(settings, cumulative_count=len(jobs), current_detail_url="")

                if is_cancel_requested(settings):
                    emit_cancel_log_once(settings, "收到中止请求，当前页已处理完成，停止继续翻页。")
                    break

                if current_page >= settings["max_pages_per_region"]:
                    break

                next_buttons = page.get_by_text("下一页", exact=True)
                if await next_buttons.count() == 0:
                    emit_task_log(settings, "51job 已到最后一页。")
                    break
                try:
                    await next_buttons.first.click(timeout=5000)
                except Exception:
                    emit_task_log(settings, "51job 点击下一页失败，结束当前搜索。")
                    break
                await human_sleep(*settings["delays"]["after_next_page"])
                current_page += 1

        finally:
            await context.close()
            if gl is not None:
                stop_gologin_api(gl)

    return jobs
