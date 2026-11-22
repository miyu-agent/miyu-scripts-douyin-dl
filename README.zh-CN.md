<p align="center">
  <a href="README.md">English</a> | 简体中文
</p>

# 抖音下载

> <sub>本文档由 Anthropic 的 Claude Fable 5.2 生成。</sub>

这是 Miyu 的一个脚本软件包，提供一件工具 `douyin_dl`，用来下载抖音和 TikTok 的视频和图文，下载的内容不带水印。视频存进视频目录，图文存进图片目录（群聊这类没有这两个目录的会话存进工作区）。从手机应用里复制的整段分享文字可以直接交给它，短链接会自动展开。

## 参数

这件工具接受下面这些参数：

| 参数 | 说明 |
|---|---|
| `url` | 分享文字或者链接 |
| `action` | `download`（默认）下载作品；`info` 只查看标题、作者、时长和封面；`doctor` 进行自检 |
| `out_dir` | 视频目录或者图片目录下的子目录，默认是 `抖音`（存进工作区时默认是 `douyin`）；绝对路径和跳出这个目录的路径都会被拒绝 |
| `quality` | `best`（默认）、`1080`、`720`、`worst` |
| `cookies` | Netscape 格式的 cookies.txt，只有匿名访问被拒绝时才需要 |
| `format` | `md`（默认）或者 `json` |

## 行为

这一节说明工具怎样获取抖音和 TikTok 的作品，以及文件怎样保存。

- 抖音：工具读取手机分享页里内嵌的作品数据（请求时带上一个免费领取的 `ttwid` 设备 cookie，缓存一天），去掉水印标记，再按照 `quality` 选择清晰度下载；如果从分享页拿不到数据，就改用网页版的接口。这一部分只使用 Python 标准库。
- TikTok：工具通过 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 下载；如果没有安装 yt-dlp，工具会报错并给出安装方法。有 ffmpeg 时，工具下载音画分离的最好的一档再合并起来；没有 ffmpeg 时，下载音画合一的那一档。yt-dlp 的缓存放在脚本缓存目录里。
- 图文作品存成一个目录，里面是 `01.jpg`、`02.jpg`……
- 视频的封面、图文作品的前几张图片（最多 4 张）会附给 AI 查看（PNG/JPEG/GIF/WebP、≤5 MiB、每条边 ≤8000 像素）；如果要给人看，AI 会用 `show` 打开文件。

## 依赖

这件工具需要下面这些程序：

- 必需：Python 3
- 下载 TikTok 需要 `yt-dlp`（Arch：`pacman -S yt-dlp`）；建议再安装 `ffmpeg`

使用 `action=doctor` 时，工具会逐项检查 yt-dlp、ffmpeg，以及能不能连上抖音、能不能领取到设备 cookie。
