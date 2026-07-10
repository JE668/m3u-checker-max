# 📡 IPTV M3U Checker Max

[![GitHub Actions Status](https://img.shields.io/badge/GitHub_Actions-Auto_Update-00f3ff?style=flat-square&logo=github-actions)](https://github.com/JE668/m3u-checker-max/actions)
[![Python Version](https://img.shields.io/badge/Python-3.10-3b82f6?style=flat-square&logo=python)](#)
[![CDN Accelerated](https://img.shields.io/badge/CDN-gh.felicity.ac.cn-f59e0b?style=flat-square)](#)

> 全自动 IPTV 直播源验证、测速、分类与管理系统。  
> GitHub Actions 每天 **4 次自动运行**，AI 辅助名称标准化与频道分类，  
> 为您提供**无死链、秒开加载、带有完整 EPG 节目单**的纯净直播源列表。

---

## 📦 模块化架构 (v2)

```
📦 m3u-checker-max
 ┣ 📂 config                 ← ⚙️ 配置文件目录
 ┃ ┣ 📜 sources.txt          (上游 M3U/TXT 直播源直链)
 ┃ ┣ 📜 epg.txt              (上游 XML/GZ 节目单链接)
 ┃ ┣ 📜 alias.txt            (频道别名映射引擎)
 ┃ ┣ 📜 demo.txt             (输出分类骨架与排序模板)
 ┃ ┣ 📜 blacklist.txt        (频道/URL 黑名单)
 ┃ ┣ 📜 whitelist.txt        (频道/URL 白名单)
 ┃ ┣ 📜 adult-sources.txt    (限制级来源 URL 列表)
 ┃ ┣ 📜 source-cat.txt       (来源→分类 映射规则)
 ┃ ┣ 📜 Channel_model.txt    (频道分类数据库)
 ┃ ┣ 📜 icons_index.txt      (图标索引)
 ┃ ┗ 📜 settings.py          (统一配置参数)
 ┣ 📂 output                 ← 🚀 自动生成成品
 ┃ ┣ 📜 live.m3u / live.txt  (常规频道)
 ┃ ┣ 📜 adult.m3u / adult.txt(限制级频道)
 ┃ ┣ 📜 epg.xml.gz           (EPG 节目单)
 ┃ ┣ 📜 log.txt              (运行报告)
 ┃ ┗ 📜 ai_cache.json        (AI 标准化缓存)
 ┣ 📂 utils                  ← 🧠 引擎模块
 ┃ ┣ 📜 config.py            (配置、会话池、线程池、工具函数)
 ┃ ┣ 📜 loaders.py           (别名/黑/白名单/频道模型加载器)
 ┃ ┣ 📜 epg.py               (EPG 下载、解析、合并)
 ┃ ┣ 📜 fetcher.py           (直播源抓取、AI 别名收集)
 ┃ ┣ 📜 categorizer.py       (分类引擎、demo.txt 自进化)
 ┃ ┣ 📜 speedtest.py         (并发测速、分辨率检测、黑白过滤)
 ┃ ┣ 📜 output.py            (M3U/TXT/日志输出)
 ┃ ┗ 📜 ai_helper.py         (AI 名称标准化与频道分类)
 ┣ 📜 main.py                (核心编排入口，仅 565 行)
 ┣ 📜 run_ci.py              (分阶段 CI 运行器)
 ┣ 📜 index.html             (科技感网页前端视图)
 ┗ 📜 README.md
```

---

## ✨ 核心特性

### ⚡ 测速引擎
- **50 线程高并发**：全量测速 + 分辨率探测，智能淘汰死链。
- **服务器级预筛**：按 host 分组，先抽检样本，整台服务器死亡则跳过所有频道。
- **带宽阈值过滤**：低于 2Mbps 的弱流自动剔除，保障播放体验。
- **MPEG-TS 同步字节校验**：0x47 同步检测，防止无效流混入。
- **分辨率识别**：ffprobe 探测分辨率，支持 4K/1080p/720p 等分类统计。

### 🤖 AI 辅助智能
- **名称标准化**：NVIDIA NIM (**Step 3.5 Flash**) 自动清洗混乱的频道名，去除质量标记、地区后缀。
- **AI 兜底匹配**：alias.txt 无法匹配时，AI 自动识别并写入新别名映射。
- **AI 频道分类**：自动判断频道归属省份/类别（覆盖 34 省 + 港澳台 + 系列频道），写入 demo.txt。
- **运行时 + 持久化双重缓存**：减少重复 API 调用，节省额度。

### 📅 EPG 多源聚合
- 自动下载 `.xml` / `.xml.gz` 多源节目单去重合并。
- Gitee/GitHub blob 链接自动纠错为 raw 直链。
- 过滤 "未提供节目表"、"精彩节目" 等垃圾条目。

### 🔤 别名映射引擎
- 精确映射 + 正则匹配双重机制。
- AI 自动发现新别名并追加到 `alias.txt`。
- 30 万+ 别名库，覆盖央视频道、各省卫视、港澳台等。

### 🧠 分类模板自进化
- 新频道自动归类并追加到 `demo.txt`。
- 从已有分类结构自学习关键词规则。
- 来源 URL 推断分类（source-cat.txt）。
- 非电视台频道（斗鱼/虎牙/YouTube 等）自动过滤。

### 🛡️ 智能过滤
- 黑名单/白名单支持频道名 + URL 双模式。
- 无效频道名自动追加黑名单（去重防膨胀）。
- 限制级来源参与测速后分离（不再跳过测速）。

### 🚀 CI/CD 流水线
- GitHub Actions 每天 **UTC 22:00 / 04:00 / 10:00 / 16:00** 自动运行。
- 三阶段流水线：抓取过滤 → 并发测速 → 分类输出。
- 状态文件跨阶段持久化，单次运行 ≤ 120 分钟。
- 自动提交变更到仓库。

---

## 🚀 如何开始使用？

1. **Fork 本仓库** 到你的 GitHub 账号。
2. 进入 **Settings → Secrets and variables → Actions**，添加：
   - `NVIDIA_API_KEY`（可选，启用 AI 功能）
3. 进入 `config/` 目录，按需修改配置。
4. 进入 **Actions** 页面，点击 **I understand my workflows...**
5. 手动触发：**Run workflow** → 开始全量检测。
6. **开启 GitHub Pages**：Settings → Pages → Source 为 `Deploy from a branch` → `main`。

---

## ⚙️ 配置说明

| 文件 | 用途 | 是否必需 |
|------|------|----------|
| `config/sources.txt` | 上游 M3U 直播源直链 | ✅ 必需 |
| `config/epg.txt` | EPG 节目单链接 | ✅ 必需 |
| `config/alias.txt` | 频道别名映射 | ✅ 必需 |
| `config/demo.txt` | 输出分类模板 | ✅ 必需 |
| `config/blacklist.txt` | 黑名单 | ❌ 可选 |
| `config/whitelist.txt` | 白名单免测 | ❌ 可选 |
| `config/adult-sources.txt` | 限制级来源分离 | ❌ 可选 |
| `config/source-cat.txt` | 来源→分类映射 | ❌ 可选 |
| `config/Channel_model.txt` | 频道分类数据库 | ❌ 可选 |
| `config/settings.py` | 统一参数配置 | ✅ 有默认值 |

### 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `NVIDIA_API_KEY` | NVIDIA NIM API Key | (禁用 AI) |
| `MAX_WORKERS` | 并发线程数 | 50 |
| `ENABLE_IPV6` | 启用 IPv6 测速 | false |
| `CDN_BASE` | CDN 加速地址 | `https://gh.felicity.ac.cn` |
| `MIN_RESOLUTION` | 最低分辨率过滤 | `1920x1080` |
| `PROBE_RESOLUTION` | 是否探测分辨率 | true |

---

## 📊 输出文件

| 文件 | 说明 |
|------|------|
| `output/live.m3u` | M3U 格式成品（含 EPG 地址、分辨率、台标） |
| `output/live.txt` | TXT 格式成品 |
| `output/adult.m3u` | 限制级频道 M3U（参与测速后分离） |
| `output/adult.txt` | 限制级频道 TXT |
| `output/epg.xml.gz` | 压缩版 EPG 节目单 |
| `output/log.txt` | 运行日志（含来源统计、失败分布、分类存活） |
| `output/ai_cache.json` | AI 标准化缓存 |
| `output/unmatched.txt` | 未匹配频道清单 |
| `output/non-tv-filtered.txt` | 非电视台频道过滤日志 |

---

## 🔧 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 完整运行
python main.py

# 分阶段运行（用于调试）
python run_ci.py 1   # 抓取源 + 过滤
python run_ci.py 2   # 并发测速
python run_ci.py 3   # 分类输出
```



---

## 🙏 致谢

本项目能够顺利运行，离不开以下开源项目与作者的贡献：

### 📡 上游直播源

| 项目 | 作者 | 说明 |
|------|------|------|
| [get-m3u](https://github.com/JE668/get-m3u) | [@JE668](https://github.com/JE668) | 直播源探针元数据 |
| [iptv-org/iptv](https://github.com/iptv-org/iptv) | [@iptv-org](https://github.com/iptv-org) | 全球 IPTV 频道集合 |
| [YanG-1989/m3u](https://github.com/YanG-1989/m3u) | [@YanG-1989](https://github.com/YanG-1989) | 国内直播源聚合 |
| [vicjl/myIPTV](https://github.com/vicjl/myIPTV) | [@vicjl](https://github.com/vicjl) | IPTV 综合源 |
| [MercuryZz/IPTVN](https://github.com/MercuryZz/IPTVN) | [@MercuryZz](https://github.com/MercuryZz) | 多分类直播源 |
| [gnodgl/IPTV](https://github.com/gnodgl/IPTV) | [@gnodgl](https://github.com/gnodgl) | CCTV 及综合直播源 |
| [cuikaipeng/IPTV](https://github.com/cuikaipeng/IPTV) | [@cuikaipeng](https://github.com/cuikaipeng) | 央视/卫视直播源 |
| [zbefine/iptv](https://github.com/zbefine/iptv) | [@zbefine](https://github.com/zbefine) | IPTV 直播源 |
| [Kimentanm/aptv](https://github.com/Kimentanm/aptv) | [@Kimentanm](https://github.com/Kimentanm) | APTV 直播源 |
| [skddyj/iptv](https://github.com/skddyj/iptv) | [@skddyj](https://github.com/skddyj) | IPTV 综合源 |
| [vamoschuck/TV](https://github.com/vamoschuck/TV) | [@vamoschuck](https://github.com/vamoschuck) | M3U 直播源 |
| [BurningC4/Chinese-IPTV](https://github.com/BurningC4/Chinese-IPTV) | [@BurningC4](https://github.com/BurningC4) | 中国 IPTV 源 |
| [mzky/checklist](https://github.com/mzky/checklist) | [@mzky](https://github.com/mzky) | itvlist 直播源 |
| [hujingguang/ChinaIPTV](https://github.com/hujingguang/ChinaIPTV) | [@hujingguang](https://github.com/hujingguang) | 中国 IPTV 自动更新 |
| [TianmuTNT/iptv](https://github.com/TianmuTNT/iptv) | [@TianmuTNT](https://github.com/TianmuTNT) | IPTV 直播源 |
| [fanmingming/live](https://github.com/fanmingming/live) | [@fanmingming](https://github.com/fanmingming) | IPv6 直播源 |
| [YueChan/Live](https://github.com/YueChan/Live) | [@YueChan](https://github.com/YueChan) | 多源聚合 |
| [best-fan/iptv-sources](https://github.com/best-fan/iptv-sources) | [@best-fan](https://github.com/best-fan) | 央视/卫视分类源 |

### 📅 EPG 节目单

| 项目 | 作者 | 说明 |
|------|------|------|
| [ChinaTelecom-GuangdongIPTV-RTP-List](https://github.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List) | [@Tzwcard](https://github.com/Tzwcard) | 广东电信 IPTV EPG |
| [51zmt EPG](http://epg.51zmt.top:8000/) | — | 公共 EPG 接口 |
| [taksssss/tv](https://gitee.com/taksssss/tv) | [@taksssss](https://gitee.com/taksssss) | 多源 EPG 聚合（亦用于频道图标回退） |

### 🔧 工具与服务

| 名称 | 说明 |
|------|------|
| [gh.felicity.ac.cn](https://gh.felicity.ac.cn) | GitHub Raw 加速 CDN（由 [@felicity](https://github.com/felicity) 提供） |
| [NVIDIA NIM API](https://build.nvidia.com) | AI 频道名称标准化与分类（Step 3.5 Flash + Gemma 4 31B） |
| [iptv-org](https://github.com/iptv-org) | 全球 IPTV 社区标准与频道数据库 |

### 🧠 频道模型与分类参考

- [taksssss/tv](https://gitee.com/taksssss/tv) — 频道图标库与 EPG 数据参考
- [YueChan/Live](https://github.com/YueChan/Live) — 频道分类思路参考


---

*免责声明：本项目及脚本仅供学习与技术交流使用，不提供、不存储任何音视频流。*
