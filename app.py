# app.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import datetime as dt
import re
import time

from flask import Flask, render_template, send_from_directory, abort, url_for, request

# ====== 基本配置 ======
BASE_DIR = Path(__file__).resolve().parent
IMAGES_ROOT = BASE_DIR / "images"   # 原始图片根目录
THUMBS_ROOT = BASE_DIR / "thumbs"   # 缩略图缓存目录（自动生成）
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
CACHE_TTL_SECONDS = 5  # 目录扫描缓存的生存期（秒）
THUMB_DEFAULT_W = 640  # 默认缩略图宽度

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
        for a in self.albums:
            if a.cover_relfile:
                return a.cover_relfile
        return None


# ====== 工具函数：解析标题/日期 ======
_cn_patterns = [
    re.compile(r"^(?P<title>.+?)\s*(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月(?:\s*(?P<d>\d{1,2})\s*日)?$"),
    re.compile(r"^(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月(?:\s*(?P<d>\d{1,2})\s*日)?\s*(?P<title>.+?)$"),
]
_iso_patterns = [
    re.compile(r"^(?P<title>.+?)\s*(?P<y>\d{4})[-/.](?P<m>\d{1,2})(?:[-/.](?P<d>\d{1,2}))?$"),
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


def iter_albums():
    for c in get_catalog():
        for a in c.albums:
            yield c, a


# ====== 模板上下文（给 header 的筛选下拉用） ======
@app.context_processor
def inject_globals():
    cats = get_catalog()
    years = sorted({a.date.year for c in cats for a in c.albums if a.date}, reverse=True)
    return dict(all_categories=cats, filter_years=years)


# ====== Jinja 过滤器 ======
@app.template_filter("cn_date")
def jinja_cn_date(d: Optional[dt.date]) -> str:
    if not d:
        return "未标注日期"
    return f"{d.year} 年 {d.month} 月{f' {d.day} 日' if d.day else ''}"


# ====== 缩略图服务 ======
# 说明：首次访问自动生成到 /thumbs 缓存目录；之后直接命中磁盘 + 浏览器长缓存
try:
    from PIL import Image
except Exception:
    Image = None

THUMBS_ROOT.mkdir(exist_ok=True)

@app.route("/thumb/<path:filename>")
def thumb(filename: str):
    # 可选参数 ?w=640 控制宽度
    width_arg = request.args.get("w", str(THUMB_DEFAULT_W))
    try:
        w = max(120, min(4096, int(width_arg)))
    except Exception:
        w = THUMB_DEFAULT_W

    src = (IMAGES_ROOT / filename).resolve()
    try:
        src.relative_to(IMAGES_ROOT)
    except Exception:
        abort(404)
    if not src.exists() or src.suffix.lower() not in ALLOWED_EXTS:
        abort(404)

    rel = str(src.relative_to(IMAGES_ROOT)).replace("\\", "/")
    dst = THUMBS_ROOT / f"{Path(rel).with_suffix('')}_w{w}.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 若原图更新，缩略图自动重建
    if (not dst.exists()) or (dst.stat().st_mtime < src.stat().st_mtime):
        if Image is None:
            # 没装 Pillow 就直接回源（不建议）
            resp = send_from_directory(IMAGES_ROOT, rel)
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp
        try:
            with Image.open(src) as im:
                im = im.convert('RGB')  # 统一转为 JPEG，兼容性最好
                im.thumbnail((w, w * 10000), Image.Resampling.LANCZOS)  # 按宽缩放
                im.save(dst, 'JPEG', quality=85, optimize=True, progressive=True)
        except Exception:
            resp = send_from_directory(IMAGES_ROOT, rel)
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp

    resp = send_from_directory(THUMBS_ROOT, str(dst.relative_to(THUMBS_ROOT)))
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


# ====== 页面路由 ======
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


@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    ym = request.args.get('ym', '').strip()      # 支持 YYYY 或 YYYY-MM
    cat = request.args.get('category', '').strip()

    results: List[Tuple[Category, Album]] = []

    year = month = None
    if ym:
        m = re.match(r'^(\d{4})(?:[-/\\.](\d{1,2}))?$', ym)
        if m:
            year = int(m.group(1))
            month = int(m.group(2)) if m.group(2) else None

    for c, a in iter_albums():
        if cat and c.name != cat:
            continue
        if year:
            if not a.date or a.date.year != year:
                continue
            if month and a.date.month != month:
                continue
        if q:
            text = f"{a.title} {c.name} {a.folder}".lower()
            if q.lower() not in text:
                continue
        results.append((c, a))

    results.sort(key=lambda t: (t[1].date is not None, t[1].date or dt.date(1,1,1), t[1].title), reverse=True)

    return render_template('search.html', query=q, ym=ym, selected_category=cat, results=results)


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
    resp = send_from_directory(IMAGES_ROOT, rel)
    resp.headers['Cache-Control'] = 'public, max-age=86400'  # 原图 1 天缓存
    return resp


if __name__ == "__main__":
    app.run(debug=True)