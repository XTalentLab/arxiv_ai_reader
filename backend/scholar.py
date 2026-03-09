"""
Google Scholar 作者页面爬虫 - 使用 Playwright 无头浏览器抓取。

使用真实 Chromium 内核 + playwright-stealth 绕过 WAF（如腾讯云 EdgeOne）。
TLS 指纹、JS 执行、浏览器 API 与真实浏览器完全一致。

支持:
- 从 Google Scholar 个人主页 URL 抓取论文
- 按年份范围筛选
- 提取论文标题、年份、引用数、venue
- 分页抓取（每页100条）
"""

import asyncio
import os
import re
import random
from typing import List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime

from bs4 import BeautifulSoup

# Playwright 相关导入（延迟导入避免未安装时报错）
_playwright_available = False
try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    _playwright_available = True
except ImportError:
    pass

try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None


@dataclass
class ScholarPaper:
    """Google Scholar 论文数据结构"""
    title: str
    year: int = 0
    citations: int = 0
    venue: str = ""           # 发表venue/期刊/会议
    authors: str = ""         # 作者列表（原始字符串）
    scholar_url: str = ""     # Google Scholar 论文链接
    arxiv_id: str = ""        # 如果能从链接提取到 arXiv ID
    avg_annual_citations: float = 0.0  # 平均年引用量
    is_high_impact: bool = False       # 平均年引用 > 100

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScholarAuthor:
    """Google Scholar 作者信息"""
    name: str = ""
    affiliation: str = ""
    interests: List[str] = field(default_factory=list)
    total_citations: int = 0
    h_index: int = 0
    i10_index: int = 0
    avatar_url: str = ""
    scholar_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# WAF 拦截页面检测关键词
_WAF_INDICATORS = [
    "请求已被", "Access Restricted", "验证码", "CAPTCHA",
    "安全策略", "security policy", "blocked", "拦截",
    "Tencent Cloud", "EdgeOne", "腾讯云",
]


def _is_waf_page(html: str) -> bool:
    """检测页面是否是 WAF 拦截页面"""
    return any(indicator in html for indicator in _WAF_INDICATORS)


def _extract_user_id(url: str) -> Optional[str]:
    """从 Google Scholar URL 提取 user ID"""
    match = re.search(r'user=([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None


def _extract_arxiv_id(url: str) -> str:
    """尝试从 URL 中提取 arXiv ID"""
    if not url:
        return ""
    match = re.search(r'(\d{4}\.\d{4,5})', url)
    return match.group(1) if match else ""


def _calculate_avg_annual_citations(citations: int, year: int) -> float:
    """计算平均年引用量"""
    if year <= 0 or citations <= 0:
        return 0.0
    current_year = datetime.now().year
    years_since = max(1, current_year - year)
    return round(citations / years_since, 1)


class GoogleScholarScraper:
    """
    Google Scholar 个人主页爬虫 - 基于 Playwright 无头浏览器。

    核心原理: 使用真实 Chromium 内核执行请求，TLS 指纹 (JA3/JA4)、
    HTTP/2 指纹、JS 执行环境与真实浏览器完全一致，从根本上绕过
    WAF（如腾讯云 EdgeOne）的 Bot 检测。

    反检测策略:
    - 真实 Chromium 内核（非 httpx/requests 的 Python TLS）
    - playwright-stealth 插件屏蔽 navigator.webdriver 等指纹
    - 随机延迟模拟人类浏览行为
    - 先访问首页获取 Cookie，再访问目标页
    - 指数退避重试
    """

    # 重试配置
    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 5.0

    def __init__(self, proxy: str = None):
        """
        Args:
            proxy: 代理地址，如 'http://127.0.0.1:7890'。
                   如果不指定，自动从环境变量 HTTPS_PROXY / HTTP_PROXY 读取。
        """
        self._proxy = (
            proxy
            or os.environ.get('HTTPS_PROXY')
            or os.environ.get('HTTP_PROXY')
            or os.environ.get('https_proxy')
            or os.environ.get('http_proxy')
        )
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initialized = False

    async def _ensure_browser(self):
        """确保 Playwright 浏览器已启动"""
        if self._initialized and self._page and not self._page.is_closed():
            return

        if not _playwright_available:
            raise RuntimeError(
                "Playwright 未安装。请运行:\n"
                "  pip install playwright playwright-stealth\n"
                "  playwright install chromium"
            )

        print("[Scholar] 启动 Playwright Chromium 浏览器...")
        self._playwright = await async_playwright().start()

        # 浏览器启动参数 - 模拟真实用户环境
        launch_args = [
            "--disable-blink-features=AutomationControlled",  # 隐藏自动化标志
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--window-size=1920,1080",
        ]

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
        )

        # 创建浏览器上下文（含代理、语言、时区等环境配置）
        context_kwargs = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.6943.53 Safari/537.36"
            ),
        }
        if self._proxy:
            context_kwargs["proxy"] = {"server": self._proxy}
            print(f"[Scholar] 使用代理: {self._proxy}")

        self._context = await self._browser.new_context(**context_kwargs)

        # 预设 Google Consent Cookie，避免同意弹窗
        await self._context.add_cookies([{
            "name": "CONSENT",
            "value": "YES+cb.20231201-00-p0.en+FX+999",
            "domain": ".google.com",
            "path": "/",
        }])

        self._page = await self._context.new_page()

        # 应用 stealth 插件（屏蔽 navigator.webdriver 等自动化指纹）
        if stealth_async:
            await stealth_async(self._page)
            print("[Scholar] playwright-stealth 已应用")
        else:
            print("[Scholar] 警告: playwright-stealth 未安装，反检测能力降低")

        # 预热: 先访问 Google Scholar 首页获取 Cookie
        print("[Scholar] 预热Session - 访问Scholar首页获取Cookie...")
        try:
            await self._page.goto(
                "https://scholar.google.com/?hl=en",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(random.uniform(2.0, 4.0))
            cookies = await self._context.cookies()
            print(f"[Scholar] Session预热完成，Cookie数: {len(cookies)}")
        except Exception as e:
            print(f"[Scholar] Session预热失败(不影响后续请求): {e}")

        self._initialized = True

    async def _navigate_with_retry(self, url: str) -> str:
        """
        导航到指定 URL 并返回页面 HTML。
        带指数退避重试，检测 WAF 拦截。
        """
        await self._ensure_browser()
        last_error = None

        for attempt in range(self._MAX_RETRIES):
            try:
                # 请求间随机延迟，模拟人类浏览
                if attempt > 0:
                    delay = self._RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(3.0, 8.0)
                    print(f"[Scholar] 重试 {attempt+1}/{self._MAX_RETRIES}，等待 {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(random.uniform(1.5, 3.5))

                resp = await self._page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

                # 等待页面渲染完成
                await asyncio.sleep(random.uniform(0.5, 1.5))
                html = await self._page.content()

                # 检查 WAF 拦截
                if _is_waf_page(html):
                    print(f"[Scholar] 检测到WAF拦截页面，等待重试...")
                    last_error = RuntimeError("Google Scholar WAF拦截")
                    continue

                # 检查 HTTP 状态码
                if resp and resp.status in (429, 403):
                    print(f"[Scholar] HTTP {resp.status}，等待重试...")
                    last_error = RuntimeError(f"Google Scholar HTTP {resp.status}")
                    continue

                return html

            except Exception as e:
                last_error = e
                print(f"[Scholar] 导航失败 (attempt {attempt+1}): {e}")

        raise RuntimeError(
            f"Google Scholar 请求失败（{self._MAX_RETRIES}次重试后）: {last_error}"
        )

    async def close(self):
        """关闭浏览器和 Playwright"""
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            print(f"[Scholar] 关闭浏览器异常: {e}")
        finally:
            self._initialized = False
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    async def fetch_author_info(self, scholar_url: str) -> ScholarAuthor:
        """抓取作者基本信息（名字、机构、研究兴趣、引用指标）"""
        user_id = _extract_user_id(scholar_url)
        if not user_id:
            raise ValueError(f"无法从URL提取用户ID: {scholar_url}")

        url = f"https://scholar.google.com/citations?user={user_id}&hl=en"

        try:
            html = await self._navigate_with_retry(url)
        except Exception as e:
            raise RuntimeError(f"请求 Google Scholar 失败: {e}")

        soup = BeautifulSoup(html, "lxml")
        author = ScholarAuthor(scholar_url=scholar_url)

        # 作者名
        name_el = soup.select_one("#gsc_prf_in")
        if name_el:
            author.name = name_el.get_text(strip=True)

        # 机构
        affil_el = soup.select_one(".gsc_prf_il")
        if affil_el:
            author.affiliation = affil_el.get_text(strip=True)

        # 研究兴趣
        interest_els = soup.select("#gsc_prf_int a")
        author.interests = [el.get_text(strip=True) for el in interest_els]

        # 引用指标（总引用、h-index、i10-index）
        index_els = soup.select("#gsc_rsb_st td.gsc_rsb_std")
        if len(index_els) >= 3:
            try:
                author.total_citations = int(index_els[0].get_text(strip=True).replace(",", ""))
            except (ValueError, IndexError):
                pass
            try:
                author.h_index = int(index_els[2].get_text(strip=True).replace(",", ""))
            except (ValueError, IndexError):
                pass
            try:
                author.i10_index = int(index_els[4].get_text(strip=True).replace(",", ""))
            except (ValueError, IndexError):
                pass

        # 头像
        avatar_el = soup.select_one("#gsc_prf_pup-img")
        if avatar_el and avatar_el.get("src"):
            src = avatar_el["src"]
            if src.startswith("/"):
                src = "https://scholar.google.com" + src
            author.avatar_url = src

        return author

    async def fetch_papers(
        self,
        scholar_url: str,
        year_from: int = 0,
        year_to: int = 9999,
        on_progress=None,
    ) -> Tuple[ScholarAuthor, List[ScholarPaper]]:
        """
        抓取作者的论文列表，支持年份筛选。

        Args:
            scholar_url: Google Scholar 个人主页 URL
            year_from: 起始年份
            year_to: 结束年份
            on_progress: 进度回调 async callable(message: str)

        Returns:
            (author_info, papers_list)
        """
        user_id = _extract_user_id(scholar_url)
        if not user_id:
            raise ValueError(f"无法从URL提取用户ID: {scholar_url}")

        if on_progress:
            await on_progress("正在启动浏览器并获取作者信息...")

        # 获取作者信息
        author = await self.fetch_author_info(scholar_url)

        if on_progress:
            await on_progress(f"作者: {author.name} ({author.affiliation})")

        # 抓取论文列表（分页，每页100条）
        papers: List[ScholarPaper] = []
        cstart = 0
        page_size = 100
        max_pages = 20  # 最多抓取2000篇

        for page in range(max_pages):
            url = (
                f"https://scholar.google.com/citations?user={user_id}&hl=en"
                f"&cstart={cstart}&pagesize={page_size}&sortby=pubdate"
            )

            if on_progress:
                await on_progress(f"正在抓取第 {page + 1} 页论文...")

            try:
                # 页面间随机延迟，模拟人类浏览
                await asyncio.sleep(random.uniform(2.0, 5.0))
                html = await self._navigate_with_retry(url)
            except Exception as e:
                print(f"[Scholar] 抓取第 {page + 1} 页失败: {e}")
                break

            soup = BeautifulSoup(html, "lxml")
            rows = soup.select("tr.gsc_a_tr")

            if not rows:
                break

            page_papers = []
            for row in rows:
                paper = self._parse_paper_row(row)
                if paper:
                    # 年份筛选
                    if paper.year and year_from <= paper.year <= year_to:
                        paper.avg_annual_citations = _calculate_avg_annual_citations(
                            paper.citations, paper.year
                        )
                        paper.is_high_impact = paper.avg_annual_citations > 100
                        page_papers.append(paper)
                    # 如果论文年份小于起始年份，后面的更早，提前终止
                    elif paper.year and paper.year < year_from:
                        if on_progress:
                            await on_progress(f"已超出年份范围，停止抓取")
                        papers.extend(page_papers)
                        return author, papers

            papers.extend(page_papers)

            # 检查是否还有下一页
            next_btn = soup.select_one("#gsc_bpf_more")
            if next_btn and next_btn.get("disabled") is not None:
                break

            cstart += page_size

        if on_progress:
            await on_progress(f"共获取 {len(papers)} 篇论文 ({year_from}-{year_to})")

        return author, papers

    def _parse_paper_row(self, row) -> Optional[ScholarPaper]:
        """解析单行论文数据"""
        try:
            # 标题和链接
            title_el = row.select_one(".gsc_a_at")
            if not title_el:
                return None

            title = title_el.get_text(strip=True)
            scholar_link = title_el.get("href", "")
            if scholar_link and scholar_link.startswith("/"):
                scholar_link = "https://scholar.google.com" + scholar_link

            # 作者和venue
            gray_els = row.select(".gs_gray")
            authors_str = gray_els[0].get_text(strip=True) if len(gray_els) > 0 else ""
            venue_str = gray_els[1].get_text(strip=True) if len(gray_els) > 1 else ""

            # 引用数
            cite_el = row.select_one(".gsc_a_ac")
            citations = 0
            if cite_el:
                cite_text = cite_el.get_text(strip=True)
                if cite_text.isdigit():
                    citations = int(cite_text)

            # 年份
            year_el = row.select_one(".gsc_a_y span")
            year = 0
            if year_el:
                year_text = year_el.get_text(strip=True)
                if year_text.isdigit():
                    year = int(year_text)

            # 尝试提取 arXiv ID
            arxiv_id = _extract_arxiv_id(scholar_link)

            return ScholarPaper(
                title=title,
                year=year,
                citations=citations,
                venue=venue_str,
                authors=authors_str,
                scholar_url=scholar_link,
                arxiv_id=arxiv_id,
            )
        except Exception as e:
            print(f"[Scholar] 解析论文行失败: {e}")
            return None
