"""
51job 前程无忧爬虫
使用 Orbita 浏览器（有头模式）
"""
import sys, os
sys.path.insert(0, r"C:\Users\24995\Desktop\新建文件夹\sybg_job_crawler\sybg_job_crawler")
os.chdir(r"C:\Users\24995\Desktop\新建文件夹\sybg_job_crawler\sybg_job_crawler")

import asyncio
import logging
import random
import time
from pathlib import Path
from html import unescape

import pandas as pd
from playwright.async_api import async_playwright
from job_crawler.browser_backend import _orbita_launch_args

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 51job字段（与智联一致）
HEADERS = [
    "序号", "招聘平台", "岗位类型一级", "岗位类型二级", "岗位名称",
    "岗位类型企业/公务员/事业单位/军队文职", "公司名称", "公司规模",
    "所在省份", "城市", "详细地址", "学历要求", "经验要求",
    "薪资范围", "福利标签", "工作内容", "任职要求", "岗位链接",
    "发布时间", "投递起始时间", "投递截止时间", "证书要求", "备注（技能要求）",
]

# 省份映射
PROVINCE_MAP = {
    "北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市",
    "广州": "广东省", "深圳": "广东省", "杭州": "浙江省", "南京": "江苏省",
    "苏州": "江苏省", "成都": "四川省", "武汉": "湖北省", "西安": "陕西省",
    "郑州": "河南省", "长沙": "湖南省", "合肥": "安徽省", "济南": "山东省",
    "青岛": "山东省", "大连": "辽宁省", "沈阳": "辽宁省", "厦门": "福建省",
    "福州": "福建省", "宁波": "浙江省", "无锡": "江苏省", "佛山": "广东省",
    "东莞": "广东省", "珠海": "广东省", "汕头": "广东省", "中山": "广东省",
    "三亚": "海南省", "海口": "广东省", "香港": "香港特别行政区", "澳门": "澳门特别行政区",
    "台北": "台湾省",
}

ORBITA_PATH = r"C:\Users\24995\.gologin\browser\orbita-browser-146\chrome.exe"
PROFILE_DIR = r"C:\Users\24995\Desktop\新建文件夹\sybg_job_crawler\sybg_job_crawler\auth\51job_profile"


def safe_text(el, default="/"):
    """安全获取元素文本"""
    if el is None:
        return default
    try:
        text = el.strip()
        return text if text else default
    except:
        return default


async def extract_list_page(page) -> list:
    """提取列表页数据"""
    items = await page.query_selector_all(".joblist-item")
    results = []
    
    for i, item in enumerate(items):
        try:
            # 岗位名称
            name_el = await item.query_selector(".jname")
            job_name = safe_text(await name_el.inner_text() if name_el else None)
            
            # 公司名称
            company_el = await item.query_selector(".cname")
            company_name = safe_text(await company_el.inner_text() if company_el else None)
            
            # 薪资
            salary_el = await item.query_selector(".sal")
            salary = safe_text(await salary_el.inner_text() if salary_el else None)
            
            # 城市/经验/学历
            d1_el = await item.query_selector(".d1")
            d1_text = safe_text(await d1_el.inner_text() if d1_el else None)
            # 格式: "广州 经验 学历"
            parts = d1_text.split()
            city = parts[0] if parts else "/"
            exp = parts[1] if len(parts) > 1 else "/"
            edu = parts[2] if len(parts) > 2 else "/"
            
            # 发布日期
            d2_el = await item.query_selector(".d2")
            publish_time = safe_text(await d2_el.inner_text() if d2_el else None)
            
            # 福利标签
            t3_el = await item.query_selector(".t3")
            welfare = safe_text(await t3_el.inner_text() if t3_el else None)
            
            # 岗位链接
            link_el = await item.query_selector(".jname a")
            job_url = await link_el.get_attribute("href") if link_el else "/"
            
            # 公司规模（列表页没有，需要详情页）
            company_size = "/"
            
            results.append({
                "job_name": job_name,
                "company_name": company_name,
                "salary": salary,
                "city": city,
                "province": PROVINCE_MAP.get(city, ""),
                "experience": exp,
                "education": edu,
                "publish_time": publish_time,
                "welfare": welfare,
                "job_url": job_url,
                "company_size": company_size,
            })
        except Exception as e:
            logging.warning(f"解析第{i+1}条失败: {e}")
            continue
    
    return results


async def extract_detail_page(page) -> dict:
    """提取详情页数据"""
    data = {
        "company_size": "/",
        "work_content": "/",
        "requirements": "/",
        "address": "/",
    }
    
    try:
        # 公司规模
        for sel in [".company .mt10", ".company-info .dc", ".msg", ".in .ltype"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text:
                    data["company_size"] = text.strip()
                    break
        
        # 工作内容/任职要求
        for sel in [".job_msg .description", ".job-detail .detail-panel", ".job_msg", ".detail.in"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and len(text) > 10:
                    data["work_content"] = text.strip()[:3000]
                    break
        
        # 公司地址
        for sel in [".in .add", ".company .add", ".address"]:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text:
                    data["address"] = text.strip()
                    break
    except Exception as e:
        logging.warning(f"解析详情页失败: {e}")
    
    return data


def save_to_excel(data_list, output_file, job_type_1, job_type_2):
    """保存到Excel"""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    records = []
    for i, item in enumerate(data_list, 1):
        record = {
            "序号": i,
            "招聘平台": "前程无忧51job",
            "岗位类型一级": job_type_1,
            "岗位类型二级": job_type_2,
            "岗位名称": item.get("job_name", "/"),
            "岗位类型企业/公务员/事业单位/军队文职": "企业",
            "公司名称": item.get("company_name", "/"),
            "公司规模": item.get("company_size", "/"),
            "所在省份": item.get("province", "/"),
            "城市": item.get("city", "/"),
            "详细地址": item.get("address", "/"),
            "学历要求": item.get("education", "/"),
            "经验要求": item.get("experience", "/"),
            "薪资范围": item.get("salary", "/"),
            "福利标签": item.get("welfare", "/"),
            "工作内容": item.get("work_content", "/"),
            "任职要求": item.get("requirements", "/"),
            "岗位链接": item.get("job_url", "/"),
            "发布时间": item.get("publish_time", "/"),
            "投递起始时间": "/",
            "投递截止时间": "/",
            "证书要求": "/",
            "备注（技能要求）": "/",
        }
        records.append(record)
    
    df = pd.DataFrame(records, columns=HEADERS)
    
    if output_path.exists():
        old_df = pd.read_excel(output_path, dtype=str)
        df = pd.concat([old_df, df], ignore_index=True)
    
    df.to_excel(output_path, index=False, engine="openpyxl")
    return len(data_list)


async def crawl_keyword(keyword, job_type_1="电商运营", job_type_2="", 
                       max_pages=10, output_file="output/51job_电商运营.xlsx"):
    """抓取单个关键词"""
    all_data = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            executable_path=ORBITA_PATH,
            args=_orbita_launch_args(),
            locale="zh-CN",
            viewport={"width": 1400, "height": 900},
        )
        
        page = browser.pages[0] if browser.pages else await browser.new_page()
        
        for page_num in range(1, max_pages + 1):
            # 构建URL
            url = f"https://we.51job.com/pc/search?jobArea=000000&keyword={keyword}&searchType=2&sortType=0&curr={page_num}"
            
            logging.info(f"[{keyword}] 第{page_num}页: {url}")
            
            try:
                await page.goto(url, wait_until="load", timeout=60000)
                await asyncio.sleep(random.uniform(3, 6))
                
                # 检查是否还有数据
                items = await page.query_selector_all(".joblist-item")
                if not items:
                    logging.info(f"[{keyword}] 第{page_num}页无数据，停止")
                    break
                
                # 提取列表页数据
                page_data = await extract_list_page(page)
                logging.info(f"[{keyword}] 第{page_num}页: 获取{len(page_data)}条")
                
                # 提取详情页（每个职位点进去获取更多信息）
                detail_count = 0
                for j, item in enumerate(page_data[:5]):  # 只抓前5个的详情
                    if item.get("job_url") and item["job_url"] != "/":
                        try:
                            detail_page = await browser.new_page()
                            await detail_page.goto(item["job_url"], wait_until="load", timeout=30000)
                            await asyncio.sleep(2)
                            
                            detail_data = await extract_detail_page(detail_page)
                            item.update(detail_data)
                            detail_count += 1
                            
                            await detail_page.close()
                            await asyncio.sleep(1)
                        except Exception as e:
                            logging.warning(f"详情页失败: {e}")
                
                logging.info(f"[{keyword}] 第{page_num}页: 详情页{detail_count}条")
                
                all_data.extend(page_data)
                
                # 翻页间隔
                await asyncio.sleep(random.uniform(2, 4))
                
            except Exception as e:
                logging.error(f"[{keyword}] 第{page_num}页失败: {e}")
                continue
        
        await browser.close()
    
    # 保存
    if all_data:
        count = save_to_excel(all_data, output_file, job_type_1, job_type_2 or keyword)
        logging.info(f"[{keyword}] 完成: {count}条 -> {output_file}")
        return count
    return 0


async def main():
    """主入口"""
    # 测试单个关键词
    keywords = ["电商运营", "直播运营", "内容运营"]
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print("51job 爬虫启动（Orbita有头模式）")
    print(f"关键词: {keywords}")
    print("=" * 60)
    
    total = 0
    for kw in keywords:
        output_file = output_dir / f"51job_{kw}.xlsx"
        count = await crawl_keyword(
            kw, 
            job_type_1="电商运营", 
            job_type_2=kw,
            max_pages=5,
            output_file=output_file
        )
        total += count
        print(f">>> {kw}: {count}条")
        
        # 关键词间隔
        await asyncio.sleep(5)
    
    print("=" * 60)
    print(f"完成！总计: {total}条")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
