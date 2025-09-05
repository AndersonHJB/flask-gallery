# app.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import datetime as dt
import re
import time

from flask import Flask, render_template, send_from_directory, abort, url_for
from werkzeug.utils import safe_join

# ====== 基本配置 ======
BASE_DIR = Path(__file__).resolve().parent
IMAGES_ROOT = BASE_DIR / "images"  # 你的图片根目录
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
CACHE_TTL_SECONDS = 5  # 扫描缓存的生存期（秒）

app = Flask(__name__)


# ====== 数据结构 ======
@dataclass
class Album:
    category: str            # 分类文件夹名（中文可）
    folder: str              # 相册文件夹名（原始）
    title: str               # 从 folder 解析的“标题”
    date: Optional[dt.date]  # 从 folder 解析的“日期”（可空）
    rel_dir: str             # 相册相对 images 的路径（用于拼 URL）
    images: List[str]        # 相册内图片文件名列表（不含路径）

    @property
    def cover_relfile(self) -> Optional[str]:
        return f"{self.rel_dir}/{self.images[0]}" if self.images else None


@dataclass
class Category:
    name: str                 # 分类文件夹名
    rel_dir: str              # 相对 images 的路径（就是 name）
    albums: List[Album]

    @property
    def total_albums(self) -> int:
        return len(self.albums)

    @property
    def total_images(self) -> int:
        return sum(len(a.images) for a in self.albums)

    @property
    def cover_relfile(self) -> Optional[str]:
        # 选第一个有封面的相册
        for a in self.albums:
            if a.cover_relfile:
                return a.cover_relfile
        return None


# ====== 工具函数 ======
_cn_patterns = [
    # 标题在前：天安门 2025 年 8 月 24 日
    re.compile(r"^(?P<title>.+?)\s*(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月(?:\s*(?P<d>\d{1,2})\s*日)?$"),
    # 日期在前：2025 年 8 月 24 日 天安门
    re.compile(r"^(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月(?:\s*(?P<d>\d{1,2})\s*日)?\s*(?P<title>.+?)$"),
]

_iso_patterns = [
    # 标题在前：天安门 2025-08-24 / 2025/08/24 / 2025.08.24 / 2025-08
    re.compile(r"^(?P<title>.+?)\s*(?P<y>\d{4})[-/.](?P<m>\d{1,2})(?:[-/.](?P<d>\d{1,2}))?$"),
    # 日期在前：2025-08-24 天安门
    re.compile(r"^(?P<y>\d{4})[-/.](?P<m>\d{1,2})(?:[-/.](?P<d>\d{1,2}))?\s*(?P<title>.+?)$"),
]


def _to_date(y: str, m: str, d: Optional[str]) -> Optional[dt.date]:
    try:
        year = int(y)
        month = int(m)
        day = int(d) if d else 1  # 无日，默认 1 号
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_title_date(folder_name: str) -> Tuple[str, Optional[dt.date]]:
    name = folder_name.strip()
    for pat in _cn_patterns + _iso_patterns:
        m = pat.match(name)
        if m:
            gd = m.groupdict()
            title = (gd.get("title") or "").strip()
            date = _to_date(gd["y"], gd["m"], gd.get("d"))
            # 若标题空，就把日期从原名中抹掉后作为标题
            if not title:
                title = pat.sub("", name).strip("-_ ·，, ") or name
            return title, date
    # 没匹配到日期，整串当标题
    return name, None


def allowed_image(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in ALLOWED_EXTS


# ====== 扫描与缓存 ======
_cache = {"at": 0.0, "data": []}


def _scan_once() -> List[Category]:
    if not IMAGES_ROOT.exists():
        return []

    categories: List[Category] = []

    for cat_dir in sorted([p for p in IMAGES_ROOT.iterdir() if p.is_dir()]):
        albums: List[Album] = []
        for album_dir in sorted([p for p in cat_dir.iterdir() if p.is_dir()]):
            imgs = sorted([f.name for f in album_dir.iterdir() if allowed_image(f)])
            if not imgs:
                continue  # 空相册不展示
            title, date = parse_title_date(album_dir.name)
            rel_dir = str(album_dir.relative_to(IMAGES_ROOT)).replace("\\", "/")
            albums.append(Album(
                category=cat_dir.name,
                folder=album_dir.name,
                title=title,
                date=date,
                rel_dir=rel_dir,
                images=imgs,
            ))

        # 相册按日期倒序（无日期靠后再按名称）
        albums.sort(key=lambda a: (a.date is not None, a.date or dt.date(1, 1, 1), a.title), reverse=True)
        if albums:
            categories.append(Category(name=cat_dir.name, rel_dir=cat_dir.name, albums=albums))

    # 分类按名称排序（中文 OK）
    categories.sort(key=lambda c: c.name)
    return categories


def get_catalog() -> List[Category]:
    now = time.time()
    if now - _cache["at"] > CACHE_TTL_SECONDS or not _cache["data"]:
        _cache["data"] = _scan_once()
        _cache["at"] = now
    return _cache["data"]


# ====== Jinja 过滤器 ======
@app.template_filter("cn_date")
def jinja_cn_date(d: Optional[dt.date]) -> str:
    if not d:
        return "未标注日期"
    return f"{d.year} 年 {d.month} 月{f' {d.day} 日' if d.day else ''}"


# ====== 路由 ======
@app.route("/")
def index():
    cats = get_catalog()
    return render_template("index.html", categories=cats)


@app.route("/c/<category>/")
def view_category(category: str):
    cats = get_catalog()
    cat = next((c for c in cats if c.name == category), None)
    if not cat:
        abort(404)
    return render_template("category.html", category=cat)


@app.route("/c/<category>/<album>/")
def view_album(category: str, album: str):
    cats = get_catalog()
    cat = next((c for c in cats if c.name == category), None)
    if not cat:
        abort(404)
    alb = next((a for a in cat.albums if a.folder == album), None)
    if not alb:
        abort(404)
    return render_template("album.html", category=cat, album=alb)


@app.route("/media/<path:filename>")
def media(filename: str):
    # 限制仅服务图片文件
    p = (IMAGES_ROOT / filename).resolve()
    try:
        p.relative_to(IMAGES_ROOT)
    except Exception:
        abort(404)
    if p.suffix.lower() not in ALLOWED_EXTS or not p.exists():
        abort(404)
    rel = str(p.relative_to(IMAGES_ROOT)).replace("\\", "/")
    return send_from_directory(IMAGES_ROOT, rel)


if __name__ == "__main__":
    app.run(debug=True)