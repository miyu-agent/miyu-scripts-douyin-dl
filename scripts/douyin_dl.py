#!/usr/bin/env python3
# Display name: Douyin downloader
# 显示名称：抖音下载
# Description: Download Douyin or TikTok videos and photo posts without watermark into the user's Videos (photo posts into Pictures), or the workspace where those are not given; the raw share text works.
# 描述：下载抖音、TikTok 的视频和图文，不带水印，视频存进用户的视频目录、图文存进图片目录（没有这两处的会话存进工作区）；整段分享文字直接给就行。
# Timeout: 300
# Clicall: yes
# Parameters:
# {"type": "object", "properties": {
#   "url": {"type": "string", "description": "Share text or link. The whole text copied from the app is fine; short links are resolved."},
#   "action": {"type": "string", "enum": ["download", "info", "doctor"], "description": "download (default) saves the post; info only reads title, author, length and cover; doctor checks yt-dlp, ffmpeg and network."},
#   "out_dir": {"type": "string", "description": "Subfolder to save into, relative to Videos or Pictures (the workspace where those are not given). Default 抖音 (douyin in the workspace)."},
#   "quality": {"type": "string", "enum": ["best", "1080", "720", "worst"], "description": "Video quality. Default best."},
#   "cookies": {"type": "string", "description": "A Netscape cookies.txt, only when the site refuses anonymous access."},
#   "format": {"type": "string", "enum": ["md", "json"], "description": "md (default) for reading; json for code, e.g. piping miyu call into jq or python."}
# }}
"""Douyin / TikTok downloader.

Douyin: the mobile share page (iesdouyin.com/share/...) embeds the whole post
as JSON once the request carries a free `ttwid` device cookie; its play link
minus the watermark flag (playwm -> play) is the clean file. The PC web detail
API is the fallback; it now wants more than ttwid and mostly answers 403.
TikTok goes through yt-dlp; photo posts are read out of the page yt-dlp saves.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
SHARE_PAGE = "https://www.iesdouyin.com/share/{kind}/{id}/"
ROUTER_DATA = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})</script>", re.S)
# The play endpoint picks the gear from `ratio`; ask for the wanted one first.
RATIOS = {"best": ["1080p", "720p"], "1080": ["1080p", "720p"], "720": ["720p", "540p"],
          "worst": ["540p", "360p", "720p"]}
DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_API = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_BODY = {
    "region": "cn", "aid": 1768, "needFid": False, "service": "www.ixigua.com",
    "migrate_info": {"ticket": "", "source": "node"}, "cbUrlProtocol": "https", "union": True,
}
ATTACH_LIMIT = 4
ATTACH_BYTES = 5 << 20
ATTACH_SIDE = 8000
YTDLP_TIMEOUT = 270
INSTALL_YTDLP = "装 yt-dlp：Arch 用 pacman -S yt-dlp，macOS 用 brew install yt-dlp，别的系统用 pipx install yt-dlp"

FMT = "md"


class Failure(Exception):
    def __init__(self, error, fix=None):
        super().__init__(error)
        self.error, self.fix = error, fix


def emit(md, data):
    if FMT == "json":
        print(json.dumps({"ok": True, **data}, ensure_ascii=False, indent=2))
    else:
        print(md)


def cache_dir():
    path = os.environ.get("MIYU_SCRIPT_CACHE_DIR")
    if path:
        os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# share text -> clean URL -> canonical URL
# --------------------------------------------------------------------------

MOJIBAKE = re.compile(r"锟斤拷|锟|斤拷|�")
PATTERNS = [
    re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+"),
    re.compile(r"https?://(?:www\.)?douyin\.com/(?:video|note|slides)/\d+"),
    re.compile(r"https?://(?:www\.)?iesdouyin\.com/share/(?:video|note|slides)/\d+"),
    re.compile(r"https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9_\-]+"),
    re.compile(r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/(?:video|photo)/\d+"),
    re.compile(r"https?://(?:www\.)?tiktok\.com/t/[A-Za-z0-9_\-]+"),
    # Captions often run straight into the link, so stop at CJK text.
    re.compile(r"https?://[^\s一-鿿　-〿，。！？、；：（）【】]+"),
]
ID_IN_PATH = re.compile(r"/(?:video|note|slides)/(\d+)")
ID_IN_QUERY = re.compile(r"(?:modal_id|aweme_id|item_ids?)=(\d{10,})")


def extract_url(text):
    cleaned = MOJIBAKE.sub("", text or "").strip()
    for pattern in PATTERNS:
        match = pattern.search(cleaned)
        if match:
            return match.group(0).rstrip(".,;:!?)\"'》」）")
    return None


def follow(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.geturl()
    except urllib.error.HTTPError as exc:
        return exc.url or url  # a 4xx still says where we landed
    except (urllib.error.URLError, OSError):
        return url


def platform_of(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if host.endswith("tiktok.com"):
        return "tiktok"
    return None


def canonicalize(url):
    """Return (canonical_url, platform, douyin_id)."""
    platform = platform_of(url)
    if platform == "douyin":
        match = ID_IN_PATH.search(url) or ID_IN_QUERY.search(url)
        if not match:
            progress("展开短链接")
            resolved = follow(url)
            match = ID_IN_PATH.search(resolved) or ID_IN_QUERY.search(resolved)
        if not match:
            raise Failure(f"认不出这条抖音链接指向哪个作品：{url}", "换一条新的分享链接再试")
        return f"https://www.douyin.com/video/{match.group(1)}", platform, match.group(1)
    if platform == "tiktok":
        if not re.search(r"/(?:video|photo)/\d+", url):
            progress("展开短链接")
            url = follow(url)
        # yt-dlp only matches /video/; a photo post answers under the same id.
        return url.split("?", 1)[0].replace("/photo/", "/video/"), platform, None
    raise Failure(f"只认抖音和 TikTok 的链接：{url}")


# --------------------------------------------------------------------------
# Douyin web API
# --------------------------------------------------------------------------

def mint_ttwid(force=False):
    base = cache_dir()
    path = os.path.join(base, "ttwid.json") if base else None
    if path and not force:
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("value") and time.time() - blob.get("at", 0) < 86400:
                return blob["value"]
        except (OSError, ValueError):
            pass
    request = urllib.request.Request(TTWID_API, data=json.dumps(TTWID_BODY).encode(),
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            for header in response.headers.get_all("Set-Cookie") or []:
                if header.startswith("ttwid="):
                    value = header.split(";", 1)[0].split("=", 1)[1]
                    if path:
                        with open(path, "w", encoding="utf-8") as fh:
                            json.dump({"value": value, "at": time.time()}, fh)
                    return value
    except (urllib.error.URLError, OSError):
        return None
    return None


def cookie_header(cookies_path, domain):
    """Netscape cookies.txt -> Cookie header for one site."""
    pairs = []
    with open(cookies_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) == 7 and fields[0].lstrip(".").endswith(domain):
                pairs.append(f"{fields[5]}={fields[6]}")
    return "; ".join(pairs) or None


def share_page_item(aweme_id, cookies):
    """The post as embedded in the mobile share page, or None."""
    for attempt in range(3):
        cookie = cookies or f"ttwid={mint_ttwid(force=attempt == 2)}"
        for kind in ("video", "note"):
            request = urllib.request.Request(SHARE_PAGE.format(kind=kind, id=aweme_id), headers={
                "User-Agent": MOBILE_UA, "Cookie": cookie})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    page = response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError):
                continue
            match = ROUTER_DATA.search(page)
            if not match:
                continue
            try:
                loader = json.loads(match.group(1)).get("loaderData") or {}
            except ValueError:
                continue
            for value in loader.values():
                items = ((value or {}).get("videoInfoRes") or {}).get("item_list") if isinstance(value, dict) else None
                if items:
                    return items[0]
        time.sleep(0.5)
    return None


def douyin_detail(aweme_id, cookies):
    """Return the post: share page first, then the PC web detail API."""
    item = share_page_item(aweme_id, cookies)
    if item:
        return item
    last = "没有回应"
    # About half of the edge nodes answer an unsigned request with 403 and the
    # rest answer fine, so a few retries are enough; re-mint the cookie late.
    for attempt in range(3):
        progress(f"读作品信息（换一条路，第 {attempt + 1} 次）")
        cookie = cookies or f"ttwid={mint_ttwid()}"
        query = urllib.parse.urlencode({"aweme_id": aweme_id, "aid": 6383,
                                        "channel": "channel_pc_web", "detail_list": 1})
        request = urllib.request.Request(f"{DETAIL_API}?{query}", headers={
            "User-Agent": UA, "Referer": "https://www.douyin.com/", "Cookie": cookie})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                blob = json.loads(response.read().decode("utf-8", "replace") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)[:160]
            time.sleep(0.8)
            continue
        if blob.get("aweme_detail"):
            return blob["aweme_detail"]
        reason = (blob.get("filter_detail") or {}).get("detail_msg")
        if reason:
            raise Failure(f"抖音不给看：{reason}", "作品可能设了私密或已删除")
        last = "空回应（被风控或作品已删除）"
        time.sleep(0.8)
    raise Failure(f"读不到这个抖音作品：{last}", "过一会儿再试；一直不行的，导出登录后的 cookies.txt 传 cookies")


def short_side(rate):
    addr = rate["play_addr"]
    sides = [s for s in (addr.get("width"), addr.get("height")) if s]
    return min(sides) if sides else 0


def pick_bitrate(detail, quality):
    rates = [r for r in (detail.get("video") or {}).get("bit_rate") or []
             if (r.get("play_addr") or {}).get("url_list")]
    if not rates:
        addr = (detail.get("video") or {}).get("play_addr") or {}
        return addr if addr.get("url_list") else None
    # Gears are labelled by the short side: a 1080p portrait clip is 1080x1920.
    rates.sort(key=lambda r: (short_side(r), r.get("bit_rate") or 0))
    if quality == "worst":
        return rates[0]["play_addr"]
    if quality in ("1080", "720"):
        under = [r for r in rates if short_side(r) <= int(quality)]
        return (under[-1] if under else rates[0])["play_addr"]
    return rates[-1]["play_addr"]


def douyin_post(detail, quality):
    aweme_id = detail.get("aweme_id")
    video = detail.get("video") or {}
    cover = (video.get("origin_cover") or video.get("cover") or {}).get("url_list") or []
    post = {
        "id": aweme_id,
        "title": (detail.get("desc") or "").strip()[:200],
        "author": (detail.get("author") or {}).get("nickname"),
        "referer": "https://www.douyin.com/",
    }
    images = [img.get("url_list") or [] for img in detail.get("images") or []]
    if images:
        post.update(type="photo", count=len(images), url=f"https://www.douyin.com/note/{aweme_id}",
                    images=[u for u in images if u], cover=[])
        return post
    duration = detail.get("duration") or video.get("duration") or 0
    if video.get("bit_rate"):
        addr = pick_bitrate(detail, quality) or {}
        urls = addr.get("url_list") or []
        width, height, size = addr.get("width"), addr.get("height"), addr.get("data_size")
    else:
        # Share page: one watermarked link; drop the mark and pick the gear.
        base = ((video.get("play_addr") or {}).get("url_list") or [""])[0].replace("/playwm/", "/play/")
        urls = [re.sub(r"ratio=[^&]*", f"ratio={r}", base) for r in RATIOS[quality]] if base else []
        width, height, size = video.get("width"), video.get("height"), None
        post["source_size"] = True  # the original's size, not the gear's
    post.update(type="video", url=f"https://www.douyin.com/video/{aweme_id}",
                duration=round(duration / 1000) or None, width=width, height=height, size=size,
                video=urls, cover=cover)
    return post


# --------------------------------------------------------------------------
# yt-dlp (TikTok)
# --------------------------------------------------------------------------

def ytdlp(argv, timeout=YTDLP_TIMEOUT, cwd=None):
    binary = shutil.which("yt-dlp")
    if not binary:
        raise Failure("TikTok 要用 yt-dlp，这台机器上没有", INSTALL_YTDLP)
    base = cache_dir()
    cache = ["--cache-dir", os.path.join(base, "yt-dlp")] if base else ["--no-cache-dir"]
    try:
        return subprocess.run([binary, *cache, *argv], capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise Failure("yt-dlp 超时了", "过一会儿再试，或者查一下网络") from None


def ytdlp_flags(cookies_path):
    flags = ["--no-warnings", "--no-playlist", "--user-agent", UA]
    if cookies_path:
        flags += ["--cookies", cookies_path]
    return flags


def ytdlp_hint(stderr):
    lower = (stderr or "").lower()
    if "ip address is blocked" in lower:
        return "TikTok 封了这个出口 IP，换个网络或代理再试"
    if "login" in lower or "cookie" in lower:
        return "从登录了的浏览器导出 cookies.txt，传 cookies"
    return "换一条新的分享链接再试；还不行的先跑 action=doctor"


REHYDRATION = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


def tiktok_item(url, cookies_path):
    """yt-dlp clears TikTok's WAF page but drops photo-mode images; read them from the saved page."""
    with tempfile.TemporaryDirectory(dir=cache_dir() or ".") as tmp:
        result = ytdlp([*ytdlp_flags(cookies_path), "--simulate", "--write-pages", url], cwd=tmp)
        for name in sorted(os.listdir(tmp)):
            if not name.endswith(".dump"):
                continue
            with open(os.path.join(tmp, name), encoding="utf-8", errors="replace") as fh:
                match = REHYDRATION.search(fh.read())
            if not match:
                continue
            try:
                scope = json.loads(match.group(1))["__DEFAULT_SCOPE__"]
            except (ValueError, KeyError):
                continue
            item = ((scope.get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct")
            if item:
                return item
    raise Failure(f"读不到 TikTok 图文：{(result.stderr or '').strip()[:300] or '页面里没有作品数据'}",
                  ytdlp_hint(result.stderr))


def tiktok_post(url, cookies_path):
    result = ytdlp([*ytdlp_flags(cookies_path), "-J", url])
    if result.returncode != 0 or not result.stdout.strip():
        raise Failure(f"yt-dlp 读不了这条 TikTok：{(result.stderr or '').strip()[:300] or '没有输出'}",
                      ytdlp_hint(result.stderr))
    try:
        meta = json.loads(result.stdout)
    except ValueError:
        raise Failure("yt-dlp 的输出不是 JSON", "先跑 action=doctor 看 yt-dlp 能不能用") from None
    formats = meta.get("formats") or []
    if formats and all(f.get("vcodec") == "none" for f in formats):
        # A photo post comes back as a lone audio track.
        item = tiktok_item(url, cookies_path)
        images = [((img.get("imageURL") or {}).get("urlList") or [])
                  for img in (item.get("imagePost") or {}).get("images") or []]
        if images:
            author = item.get("author") or {}
            return {
                "id": item.get("id"), "type": "photo",
                "title": (item.get("desc") or "").strip()[:200],
                "author": author.get("uniqueId") or author.get("nickname"),
                "count": len(images), "images": [u for u in images if u], "cover": [],
                "url": f"https://www.tiktok.com/@{author.get('uniqueId')}/photo/{item.get('id')}",
                "referer": "https://www.tiktok.com/",
            }
    return {
        "id": meta.get("id"), "type": "video",
        "title": (meta.get("title") or meta.get("description") or "").strip()[:200],
        "author": meta.get("uploader") or meta.get("creator"),
        "duration": meta.get("duration"), "width": meta.get("width"), "height": meta.get("height"),
        "size": meta.get("filesize") or meta.get("filesize_approx"),
        "url": meta.get("webpage_url") or url, "cover": [meta["thumbnail"]] if meta.get("thumbnail") else [],
        "referer": "https://www.tiktok.com/", "ytdlp": True,
    }


QUALITY_MERGED = {
    "best": "bv*+ba/b", "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b", "worst": "wv*+wa/w",
}
QUALITY_SINGLE = {"best": "b", "1080": "b[height<=1080]/b", "720": "b[height<=720]/b", "worst": "w"}


def ytdlp_download(url, folder, name, quality, cookies_path):
    table = QUALITY_MERGED if shutil.which("ffmpeg") else QUALITY_SINGLE
    progress("yt-dlp 下载中")
    result = ytdlp([*ytdlp_flags(cookies_path), "-f", table.get(quality, table["best"]),
                    "-o", os.path.join(folder, f"{name}.%(ext)s"), "--no-mtime",
                    "--print", "after_move:filepath", url])
    paths = [line for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not paths:
        raise Failure(f"yt-dlp 下载失败：{(result.stderr or '').strip()[:300] or '没有输出'}",
                      ytdlp_hint(result.stderr))
    return paths[-1] if os.path.isabs(folder) else os.path.relpath(paths[-1])


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
             "image/heic": "heic", "image/avif": "avif"}


def safe_name(post):
    stem = f"{post.get('author') or post['type']}-{post['title'] or post['id']}"
    stem = re.sub(r"[\s_]+", "_", UNSAFE.sub("_", stem))[:80].strip("_")
    return f"{stem}-{post['id']}"


def save_dir(out_dir, kind, create=True):
    """视频进 MIYU_VIDEOS_DIR、图文进 MIYU_PICTURES_DIR；本机会话以外没有这两个，退回工作区。"""
    base = os.environ.get("MIYU_VIDEOS_DIR" if kind == "video" else "MIYU_PICTURES_DIR")
    out_dir = out_dir or ("抖音" if base else "douyin")
    where = "这个目录" if base else "工作区"
    if os.path.isabs(os.path.expanduser(out_dir)):
        raise Failure(f"out_dir 只能是{where}里的相对路径：{out_dir}", "例：抖音、抖音/收藏")
    root = os.path.realpath(base or ".")
    target = os.path.realpath(os.path.join(root, out_dir))
    if target != root and not target.startswith(root + os.sep):
        raise Failure(f"out_dir 跑到{where}外面去了：{out_dir}", "例：抖音、抖音/收藏")
    if create:
        os.makedirs(target, exist_ok=True)
    return target if base else os.path.relpath(target)


def get(url, referer, timeout=60):
    agent = MOBILE_UA if "snssdk.com" in url else UA
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Referer": referer})
    return urllib.request.urlopen(request, timeout=timeout)


def progress(text):
    """进度行：Miyu 边跑边给人看，不进交给 AI 的结果。"""
    print(f"miyu-progress: {text}", file=sys.stderr, flush=True)


def stream_to(urls, referer, target):
    last = "没有能播的地址"
    for number, url in enumerate(urls):
        if number:
            progress("这个地址下不了，换下一个")
        progress("连视频服务器")
        try:
            with get(url, referer) as response, open(target, "wb") as fh:
                total = int(response.headers.get("Content-Length") or 0)
                done, shown = 0, 0.0
                while chunk := response.read(1 << 16):  # small reads keep the progress line ticking on slow links
                    fh.write(chunk)
                    done += len(chunk)
                    if time.monotonic() - shown >= 1:
                        shown = time.monotonic()
                        progress(f"视频 {human(done)}" + (f" / {human(total)}（{done * 100 // total}%）" if total else ""))
            if os.path.getsize(target) > 10240:
                return target
            last = "服务器回的文件不完整"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)[:160]
    if os.path.exists(target):
        os.remove(target)
    raise Failure(f"视频下载失败：{last}", "换一条新的分享链接再试，地址会过期")


def fetch_image(urls, referer):
    """Return (bytes, mime) of the first image that downloads."""
    for url in urls:
        try:
            with get(url, referer) as response:
                kind = response.headers.get_content_type()
                data = response.read()
        except (urllib.error.URLError, OSError):
            continue
        if len(data) >= 1024 and (kind.startswith("image/") or sniff(data)):
            info = sniff(data)
            return data, info[0] if info else kind
    return None, None


def sniff(data):
    """(mime, width, height) for the four kinds the AI can see, else None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return ("image/png",) + struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return ("image/gif",) + struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return "image/webp", w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L":
            b = data[21:25]
            return ("image/webp", 1 + (((b[1] & 0x3F) << 8) | b[0]),
                    1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
        if chunk == b"VP8X":
            return ("image/webp", 1 + int.from_bytes(data[24:27], "little"),
                    1 + int.from_bytes(data[27:30], "little"))
        return None
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "image/jpeg", w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


class Attacher:
    """Copies up to four images the AI can see into MIYU_SCRIPT_IMAGES_DIR."""

    def __init__(self):
        self.dir = os.environ.get("MIYU_SCRIPT_IMAGES_DIR")
        self.count = 0

    def add(self, data, name):
        info = sniff(data)
        if not (self.dir and info and self.count < ATTACH_LIMIT and len(data) <= ATTACH_BYTES
                and max(info[1], info[2]) <= ATTACH_SIDE):
            return False
        self.count += 1
        with open(os.path.join(self.dir, f"{self.count:02d}-{name}.{IMAGE_EXT[info[0]]}"), "wb") as fh:
            fh.write(data)
        return True


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def human(num):
    if not num:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if num < 1024:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} TiB"


def public(post):
    return {k: v for k, v in post.items() if k not in ("images", "video", "cover", "referer", "ytdlp", "source_size")}


def describe(post, platform):
    lines = [f"**{post['title'] or '（无标题）'}**", "",
             f"- 平台：{'抖音' if platform == 'douyin' else 'TikTok'}",
             f"- 作者：{post.get('author') or '?'}"]
    if post["type"] == "photo":
        lines.append(f"- 类型：图文，{post['count']} 张")
    else:
        lines.append(f"- 时长：{post.get('duration') or '?'} 秒")
        if post.get("width") and post.get("height"):
            label = "原片分辨率" if post.get("source_size") else "分辨率"
            lines.append(f"- {label}：{post['width']}×{post['height']}")
        if post.get("size"):
            lines.append(f"- 大小：{human(post['size'])}")
    lines.append(f"- 链接：{post['url']}")
    return lines


def attach_cover(post, attacher):
    if not post.get("cover"):
        return False
    data, _ = fetch_image(post["cover"], post["referer"])
    return bool(data) and attacher.add(data, "cover")


def run_info(post, platform):
    attacher = Attacher()
    if post["type"] == "photo":
        data, _ = fetch_image(post["images"][0], post["referer"]) if post["images"] else (None, None)
        attached = bool(data) and attacher.add(data, "01")
    else:
        attached = attach_cover(post, attacher)
    lines = describe(post, platform)
    if attached:
        lines.append("\n" + ("第一张图" if post["type"] == "photo" else "封面") + "附在后面。")
    emit("\n".join(lines), {"platform": platform, "attached": attacher.count, **public(post)})


def run_download(post, platform, out_dir, quality, cookies_path):
    folder = save_dir(out_dir, post["type"])
    name = safe_name(post)
    attacher = Attacher()
    if post["type"] == "photo":
        target = os.path.join(folder, name)
        os.makedirs(target, exist_ok=True)
        saved, failed = [], []
        for index, urls in enumerate(post["images"], 1):
            progress(f"图 {index}/{post['count']}")
            data, mime = fetch_image(urls, post["referer"])
            if not data:
                failed.append(index)
                continue
            path = os.path.join(target, f"{index:02d}.{IMAGE_EXT.get(mime, 'jpg')}")
            with open(path, "wb") as fh:
                fh.write(data)
            saved.append(path)
            attacher.add(data, f"{index:02d}")
        if not saved:
            raise Failure("图文的图一张都没下下来", "图片地址可能过期了，换一条新的分享链接再试")
        size = sum(os.path.getsize(p) for p in saved)
        lines = ["下载完成", "", f"- 目录：`{target}/`", f"- 图片：{len(saved)}/{post['count']} 张，{human(size)}"]
        if failed:
            lines.append(f"- 没下下来：第 {'、'.join(map(str, failed))} 张")
        data = {"path": target, "files": saved, "failed": failed, "bytes": size}
    else:
        if post.get("ytdlp"):
            path = ytdlp_download(post["url"], folder, name, quality, cookies_path)
        else:
            path = stream_to(post["video"], post["referer"], os.path.join(folder, f"{name}.mp4"))
        size = os.path.getsize(path)
        lines = ["下载完成", "", f"- 文件：`{path}`", f"- 大小：{human(size)}"]
        attach_cover(post, attacher)
        data = {"path": path, "bytes": size}
    lines += [line for line in describe(post, platform)[2:] if not line.startswith("- 大小")]
    if attacher.count:
        lines.append("\n" + ("前几张图" if post["type"] == "photo" else "封面") + "附在后面；要给用户看，用 show 打开文件。")
    emit("\n".join(lines), {"platform": platform, "attached": attacher.count, **public(post), **data})


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def doctor():
    checks = []
    binary = shutil.which("yt-dlp")
    if binary:
        version = ytdlp(["--version"], timeout=30).stdout.strip()
        checks.append(("yt-dlp", "ok", f"{binary}（{version}）"))
    else:
        checks.append(("yt-dlp", "warn", f"没有；抖音照样能下，TikTok 不行。{INSTALL_YTDLP}"))
    ffmpeg = shutil.which("ffmpeg")
    checks.append(("ffmpeg", "ok" if ffmpeg else "warn",
                   ffmpeg or "没有；TikTok 只能下音画合一的那一档，画质可能低一些"))
    try:
        with urllib.request.urlopen(urllib.request.Request("https://www.douyin.com/", headers={"User-Agent": UA}),
                                    timeout=10) as response:
            checks.append(("连得上抖音", "ok", f"HTTP {response.status}"))
    except (urllib.error.URLError, OSError) as exc:
        checks.append(("连得上抖音", "warn", str(exc)[:120]))
    ttwid = mint_ttwid()
    checks.append(("抖音设备 cookie", "ok" if ttwid else "fail", "拿到了" if ttwid else "拿不到，抖音会下不了"))
    sample = "7.88 复制打开抖音，看看【某人的作品】锟斤拷 https://v.douyin.com/iRNBho6G/ 这是后面紧贴的中文"
    got = extract_url(sample) or ""
    checks.append(("分享文字取链接", "ok" if got.rstrip("/") == "https://v.douyin.com/iRNBho6G" else "fail", got))
    worst_fail = any(state == "fail" for _name, state, _detail in checks)
    mark = {"ok": "✓", "warn": "!", "fail": "✗"}
    emit("\n".join(f"[{mark[s]}] {n}：{d}" for n, s, d in checks),
         {"checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks]})
    return 1 if worst_fail else 0


# --------------------------------------------------------------------------


def main():
    global FMT
    try:
        args = json.load(sys.stdin)
        if not isinstance(args, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        raise Failure(f"参数不是 JSON 对象：{exc}")
    FMT = "json" if args.get("format") == "json" else "md"
    action = args.get("action") or "download"
    if action not in ("download", "info", "doctor"):
        raise Failure(f"不认识的 action：{action}", "download、info 或 doctor")
    if action == "doctor":
        return doctor()
    url = extract_url(str(args.get("url") or ""))
    if not url:
        raise Failure("没在 url 里找到链接", "把抖音、TikTok 的分享文字或链接整段传进来")
    cookies_path = args.get("cookies")
    if cookies_path:
        cookies_path = os.path.expanduser(cookies_path)
        if not os.path.isfile(cookies_path):
            raise Failure(f"找不到 cookies 文件：{cookies_path}")
    quality = args.get("quality") or "best"
    if quality not in QUALITY_MERGED:
        raise Failure(f"不认识的 quality：{quality}", "best、1080、720 或 worst")
    if action == "download":
        save_dir(args.get("out_dir"), "video", create=False)  # refuse a bad folder before any network work

    canonical, platform, aweme_id = canonicalize(url)
    progress("读作品信息")
    if platform == "douyin":
        cookies = cookie_header(cookies_path, "douyin.com") if cookies_path else None
        post = douyin_post(douyin_detail(aweme_id, cookies), quality)
        if post["type"] == "video" and not post["video"]:
            raise Failure("这个抖音作品里没有能下的视频地址", "作品可能还在转码，或者已经下架")
    else:
        post = tiktok_post(canonical, cookies_path)
    if action == "info":
        run_info(post, platform)
    else:
        run_download(post, platform, args.get("out_dir"), quality, cookies_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        if FMT == "json":
            print(json.dumps({"ok": False, "error": exc.error, "fix": exc.fix}, ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc.error}" + (f"\n怎么办：{exc.fix}" if exc.fix else ""))
        sys.exit(1)
