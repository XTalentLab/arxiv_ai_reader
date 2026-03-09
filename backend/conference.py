"""
会议论文获取器 - 从 GitHub (papercopilot/paperlists) 获取特定会议的论文列表。

数据源: https://raw.githubusercontent.com/papercopilot/paperlists/main/{conf}/{conf}{year}.json
原始网站: https://papercopilot.com/paper-list/

支持的会议:
- CVPR (每年)
- ICCV (奇数年)
- ECCV (偶数年)
- ICLR (每年)
- ICML (每年)
"""

import json
import re
from typing import List, Optional
from dataclasses import dataclass, field, asdict

import httpx


# 支持的会议配置
SUPPORTED_CONFERENCES = {
    "CVPR": {
        "slug": "cvpr",
        "name": "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
        "years": list(range(2013, 2027)),  # 每年
    },
    "ICCV": {
        "slug": "iccv",
        "name": "IEEE/CVF International Conference on Computer Vision",
        "years": [y for y in range(2013, 2027) if y % 2 == 1],  # 奇数年
    },
    "ECCV": {
        "slug": "eccv",
        "name": "European Conference on Computer Vision",
        "years": [y for y in range(2018, 2027) if y % 2 == 0],  # 偶数年
    },
    "ICLR": {
        "slug": "iclr",
        "name": "International Conference on Learning Representations",
        "years": list(range(2018, 2027)),
    },
    "ICML": {
        "slug": "icml",
        "name": "International Conference on Machine Learning",
        "years": list(range(2018, 2027)),
    },
}

# GitHub 原始数据 URL 模板
_GITHUB_RAW_URL = "https://raw.githubusercontent.com/papercopilot/paperlists/main/{slug}/{slug}{year}.json"


@dataclass
class ConferencePaper:
    """会议论文数据结构"""
    title: str
    authors: List[str] = field(default_factory=list)
    abstract: str = ""
    url: str = ""           # 论文链接 (PDF / openreview / etc.)
    arxiv_id: str = ""      # arXiv ID (如果有)
    conference: str = ""    # 会议名 (e.g., "CVPR")
    year: int = 0           # 年份
    paper_type: str = ""    # 论文类型 (Oral, Spotlight, Poster, etc.)

    def to_dict(self) -> dict:
        return asdict(self)


class ConferencePaperFetcher:
    """
    会议论文获取器。
    从 GitHub (papercopilot/paperlists) 获取结构化 JSON 数据，
    比 HTML 爬取更稳定可靠。
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=15.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def get_available_years(self, conference: str) -> List[int]:
        """获取指定会议的可用年份列表"""
        conf = SUPPORTED_CONFERENCES.get(conference.upper())
        if not conf:
            return []
        return sorted(conf["years"], reverse=True)

    def is_valid_conference(self, conference: str) -> bool:
        return conference.upper() in SUPPORTED_CONFERENCES

    def is_valid_year(self, conference: str, year: int) -> bool:
        conf = SUPPORTED_CONFERENCES.get(conference.upper())
        if not conf:
            return False
        return year in conf["years"]

    async def fetch_papers(
        self,
        conference: str,
        year: int,
        on_progress=None,
        accepted_only: bool = True,
    ) -> List[ConferencePaper]:
        """
        从 GitHub 获取指定会议和年份的论文列表（结构化 JSON）。

        Args:
            conference: 会议名 (CVPR/ICCV/ECCV/ICLR/ICML)
            year: 年份
            on_progress: 进度回调 async callable(message: str)
            accepted_only: 是否只返回 accepted 论文（过滤 Reject/Withdraw）

        Returns:
            论文列表
        """
        conf_upper = conference.upper()
        conf = SUPPORTED_CONFERENCES.get(conf_upper)
        if not conf:
            raise ValueError(f"不支持的会议: {conference}。支持: {', '.join(SUPPORTED_CONFERENCES.keys())}")

        slug = conf["slug"]
        # 从 GitHub 原始数据获取 JSON
        url = _GITHUB_RAW_URL.format(slug=slug, year=year)

        if on_progress:
            await on_progress(f"正在从 GitHub 获取 {conf_upper} {year} 论文数据...")

        client = await self._get_client()

        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise ValueError(f"{conf_upper} {year} 论文数据不存在 (GitHub 404)")
            raise RuntimeError(f"从 GitHub 获取数据失败: {e}")
        except httpx.HTTPError as e:
            raise RuntimeError(f"网络请求失败: {e}")

        if on_progress:
            await on_progress(f"正在解析 {conf_upper} {year} 论文数据...")

        # 解析 JSON 数据
        try:
            raw_papers = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSON 解析失败: {e}")

        if not isinstance(raw_papers, list):
            raise RuntimeError(f"数据格式错误: 期望 JSON 数组，实际为 {type(raw_papers).__name__}")

        # 转换为 ConferencePaper 对象
        papers = []
        # 被拒绝/撤回的状态关键词
        rejected_statuses = {"reject", "withdrawn", "withdraw", "desk reject"}

        for item in raw_papers:
            if not isinstance(item, dict):
                continue

            title = item.get("title", "").strip()
            if not title:
                continue

            # 过滤被拒绝的论文
            status = item.get("status", "").strip()
            if accepted_only and status.lower() in rejected_statuses:
                continue

            # 解析作者（分号分隔）
            author_str = item.get("author", "") or item.get("author_site", "") or ""
            authors = [a.strip() for a in author_str.split(";") if a.strip()]

            # 获取论文链接（优先 PDF > OpenAccess > OpenReview > site）
            paper_url = (
                item.get("pdf", "")
                or item.get("oa", "")
                or item.get("openreview", "")
                or item.get("site", "")
                or ""
            )

            # arXiv ID
            arxiv_id = item.get("arxiv", "").strip()
            if not arxiv_id and paper_url:
                # 尝试从 URL 提取 arXiv ID
                match = re.search(r'(\d{4}\.\d{4,5})', paper_url)
                if match:
                    arxiv_id = match.group(1)

            # 摘要
            abstract = item.get("abstract", "").strip()

            papers.append(ConferencePaper(
                title=title,
                authors=authors,
                abstract=abstract,
                url=paper_url,
                arxiv_id=arxiv_id,
                conference=conf_upper,
                year=year,
                paper_type=status,
            ))

        if on_progress:
            await on_progress(f"共获取 {len(papers)} 篇 {conf_upper} {year} accepted 论文")

        return papers
