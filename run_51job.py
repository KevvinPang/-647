#!/usr/bin/env python3
"""
51job 前程无忧全量爬虫
使用 Orbita 浏览器（有头模式，内置反检测）
电商运营相关分类全量抓取
"""
import sys, os
sys.path.insert(0, r"C:\Users\24995\Desktop\新建文件夹\sybg_job_crawler\sybg_job_crawler")
os.chdir(r"C:\Users\24995\Desktop\新建文件夹\sybg_job_crawler\sybg_job_crawler")

import asyncio
import logging
import random
import time
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright
from job_crawler.browser_backend import _orbita_launch_args

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 字段（与智联格式一致）
HEADERS = [
    "序号", "招聘平台", "岗位类型一级", "岗位类型二级", "岗位名称",
    "岗位类型企业/公务员/事业单位/军队文职", "公司名称", "公司规模",
    "所在省份", "城市", "详细地址", "学历要求", "经验要求",
    "薪资范围", "福利标签", "工作内容", "任职要求", "岗位链接",
    "发布时间", "投递起始时间", "投递截止时间", "证书要求", "备注（技能要求）",
]

PROVINCE_MAP = {
    "北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市",
    "广州": "广东省", "深圳": "广东省", "杭州": "浙江省", "南京": "江苏省",
    "苏州": "江苏省", "成都": "四川省", "武汉": "湖北省", "西安": "陕西省",
    "郑州": "河南省", "长沙": "湖南省", "合肥": "安徽省", "济南": "山东省",
    "青岛": "山东省", "沈阳": "辽宁省", "厦门": "福建省", "福州": "福建省",
    "宁波": "浙江省", "无锡": "江苏省", "佛山": "广东省", "东莞": "广东省",
    "珠海": "广东省", "汕头": "广东省", "中山": "广东省", "三亚": "海南省",
    "海口": "海南省",
}

ORBITA_PATH = r"C:\Users\24995\.gologin\browser\orbita-browser-146\chrome.exe"
PROFILE_DIR = r"C:\Users\24995\Desktop\新建文件夹\sybg_job_crawler\sybg_job_crawler\auth\51job_profile"

# 电商运营相关关键词（来自上传的分类表）
KEYWORDS = [
    # 电商运营
    "电商运营", "国内电商运营", "跨境电商运营", "电商专员", "电商经理/电商主管",
    "电商总监", "网店店长", "店铺推广", "品类运营",
    # 内容运营
    "内容运营", "新媒体运营", "直播运营", "社区/社群运营", "内容审核",
    # 线下运营
    "线下运营", "线下拓展运营", "线下推广", "车辆运营",
    # 高级运营
    "运营总监", "首席运营官COO", "首席营销官CMO",
    # 市场/营销
    "市场/营销", "市场总监", "营销总监", "市场经理", "营销经理",
    "市场主管", "营销主管", "市场专员", "营销专员", "品牌经理", "品牌主管",
    "品牌专员", "市场企划经理", "市场企划专员", "市场分析", "市场调研",
    "市场助理", "SEO/SEM", "选址拓展", "互联网营销师", "市场通路",
    # 广告
    "广告创意总监", "广告创意/设计主管", "广告创意/设计专员", "广告客户总监",
    "广告客户经理", "广告客户主管", "广告客户专员", "文案/策划", "企业策划人员",
    "广告投放专员", "广告销售", "广告制作执行", "广告创意/设计经理", "广告美术指导",
    "企业/业务发展经理", "广告投放经理/主管", "广告审核",
    # 公关/媒介
    "公关总监", "公关经理", "公关主管", "公关专员", "公关/媒介助理",
    "政府事务管理", "活动策划", "活动执行", "媒介经理", "媒介主管",
    "媒介专员", "会务/会展经理", "会务/会展主管", "会务/会展专员", "媒介销售",
]


def safe_text(text, default="/"):
    """安全文本"""
    if not text:
        return default
    t = text.strip()
    return t if t else default


def save_to_excel(records, output_file):
    """保存到Excel（追加模式）"""
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    df = pd.DataFrame(records, columns=HEADERS)
    
    if output_path.exists():
        try:
            old_df = pd.read_excel(output_path, dtype=str)
            # 续号
            start_index = old_df["序号"].max() + 1
            df["序号"] = range(start_index, start_index + len(df))
            df = pd.concat([old_df, df], ignore_index=True)
        except Exception:
            pass
    
    df.to_excel(output_path, index=False, engine="openpyxl")
    return len(df)


async def extract_list_page(page) -> list:
    """提取列表页"""
    items = await page.query_selector_all(".joblist-item")
    results = []
    
    for item in items:
        try:
            name_el = await item.query_selector(".jname")
            company_el = await item.query_selector(".cname")
            salary_el = await item.query_selector(".sal")
            d1_el = await item.query_selector(".d1")
            d2_el = await item.query_selector(".d2")
            t3_el = await item.query_selector(".t3")
            link_el = await item.query_selector(".jname a")
            
            name = safe_text(await name_el.inner_text() if name_el else None)
            company = safe_text(await company_el.inner_text() if company_el else None)
            salary = safe_text(await salary_el.inner_text() if salary_el else None)
            d1_text = safe_text(await d1_el.inner_text() if d1_el else None)
            publish_time = safe_text(await d2_el.inner_text() if d2_el else None)
            welfare = safe_text(await t3_el.inner_text() if t3_el else None)
            job_url = await link_el.get_attribute("href") if link_el else "/"
            
            parts = d1_text.split()
            city = parts[0] if parts else "/"
            exp = parts[1] if len(parts) > 1 else "/"
            edu = parts[2] if len(parts) > 2 else "/"
            
            results.append({
                "job_name": name,
                "company_name": company,
                "salary": salary,
                "city": city,
                "province": PROVINCE_MAP.get(city, "/"),
                "experience": exp,
                "education": edu,
                "publish_time": publish_time,
                "welfare": welfare,
                "job_url": job_url,
            })
        except Exception as e:
            logging.warning(f"解析条目失败: {e}")
            continue
    
    return results


async def crawl_keyword(browser, keyword, job_type_1, job_type_2, max_pages=50):
    """抓取单个关键词"""
    page = browser.pages[0] if browser.pages else await browser.new_page()
    all_records = []
    
    for page_num in range(1, max_pages + 1):
        url = f"https://we.51job.com/pc/search?jobArea=000000&keyword={keyword}&searchType=2&sortType=0&curr={page_num}"
        
        try:
            await page.goto(url, wait_until="load", timeout=60000)
            await asyncio.sleep(random.uniform(2, 4))
            
            items = await page.query_selector_all(".joblist-item")
            if not items:
                logging.info(f"[{keyword}] 第{page_num}页无数据，停止")
                break
            
            page_data = await extract_list_page(page)
            
            # 转换格式
            for item in page_data:
                record = {
                    "序号": 0,  # 后续重编
                    "招聘平台": "前程无忧51job",
                    "岗位类型一级": job_type_1,
                    "岗位类型二级": job_type_2,
                    "岗位名称": item["job_name"],
                    "岗位类型企业/公务员/事业单位/军队文职": "企业",
                    "公司名称": item["company_name"],
                    "公司规模": "/",
                    "所在省份": item["province"],
                    "城市": item["city"],
                    "详细地址": "/",
                    "学历要求": item["education"],
                    "经验要求": item["experience"],
                    "薪资范围": item["salary"],
                    "福利标签": item["welfare"],
                    "工作内容": "/",
                    "任职要求": "/",
                    "岗位链接": item["job_url"],
                    "发布时间": item["publish_time"],
                    "投递起始时间": "/",
                    "投递截止时间": "/",
                    "证书要求": "/",
                    "备注（技能要求）": "/",
                }
                all_records.append(record)
            
            logging.info(f"[{keyword}] 第{page_num}页: +{len(page_data)}条 (累计{len(all_records)}条)")
            
            # 翻页间隔
            await asyncio.sleep(random.uniform(1, 3))
            
        except Exception as e:
            logging.error(f"[{keyword}] 第{page_num}页失败: {e}")
            try:
                await asyncio.sleep(5)
                await page.reload(wait_until="load", timeout=30000)
                await asyncio.sleep(3)
            except:
                break
    
    if page != browser.pages[0]:
        await page.close()
    
    return all_records


async def main():
    """主入口"""
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "51job_电商运营_全量.xlsx"
    
    print("=" * 60)
    print("51job 前程无忧全量爬虫")
    print(f"关键词数: {len(KEYWORDS)}")
    print(f"输出文件: {output_file}")
    print("=" * 60)
    
    # 清理 profile
    import shutil
    if Path(PROFILE_DIR).exists():
        shutil.rmtree(PROFILE_DIR)
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            executable_path=ORBITA_PATH,
            args=_orbita_launch_args(),
            locale="zh-CN",
            viewport={"width": 1400, "height": 900},
        )
        
        total_records = 0
        failed_keywords = []
        
        for i, keyword in enumerate(KEYWORDS):
            print(f"\n[{i+1}/{len(KEYWORDS)}] >>> {keyword}")
            
            try:
                records = await crawl_keyword(
                    browser, keyword,
                    job_type_1="电商运营",
                    job_type_2=keyword,
                    max_pages=50,
                )
                
                if records:
                    count = save_to_excel(records, output_file)
                    total_records += len(records)
                    print(f"    完成: {keyword} = +{len(records)}条 (累计{total_records}条)")
                else:
                    print(f"    完成: {keyword} = 0条")
                
            except Exception as e:
                print(f"    失败: {keyword} - {e}")
                failed_keywords.append(keyword)
            
            # 关键词间隔
            await asyncio.sleep(random.uniform(3, 6))
    
    print("\n" + "=" * 60)
    print(f"✅ 全部完成！总计: {total_records}条")
    print(f"📁 文件: {output_file}")
    if failed_keywords:
        print(f"⚠️ 失败: {len(failed_keywords)}个")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
