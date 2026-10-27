# 抖音下载

Miyu 的脚本包，带一件工具 `douyin_dl`：下载抖音、TikTok 的视频和图文，不带水印，视频存进视频目录、图文存进图片目录（群聊这类没有这两处的会话存进工作区）。从 App 里复制的整段分享文字直接给就行，短链接会自己展开。

## 参数

| 参数 | 说明 |
|---|---|
| `url` | 分享文字或链接 |
| `action` | `download`（默认）下载；`info` 只看标题、作者、时长、封面；`doctor` 自检 |
| `out_dir` | 视频目录或图片目录下的子目录，默认 `抖音`（存工作区时默认 `douyin`）；绝对路径、跑出去的都拒绝 |
| `quality` | `best`（默认）、`1080`、`720`、`worst` |
| `cookies` | Netscape 格式的 cookies.txt，匿名被拒时才要 |
| `format` | `md`（默认）或 `json` |

## 行为

- 抖音：读手机分享页里内嵌的作品数据（带一个免费领的 `ttwid` 设备 cookie，缓存一天），去掉水印标记、照 `quality` 挑档下载；分享页拿不到时退到网页版接口。只用 Python 标准库。
- TikTok：经 [yt-dlp](https://github.com/yt-dlp/yt-dlp)；没装的会报错并给装法。有 ffmpeg 时下音画分离的最好一档再合起来，没有的下音画合一的那一档。yt-dlp 的缓存放在脚本缓存目录里。
- 图文存成一个目录 `01.jpg`、`02.jpg`……
- 视频的封面、图文的前至多 4 张附给 AI 看（PNG/JPEG/GIF/WebP、≤5 MiB、每边 ≤8000 像素）；要给人看，AI 用 `show` 打开文件。

## 依赖

- 必需：Python 3
- TikTok：`yt-dlp`（Arch：`pacman -S yt-dlp`）；建议再装 `ffmpeg`

`action=doctor` 逐项检查 yt-dlp、ffmpeg、能不能连上抖音、能不能领到设备 cookie。
