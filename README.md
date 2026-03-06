# 📡 IPTV M3U Checker Max

[![GitHub Actions Status](https://img.shields.io/badge/GitHub_Actions-Auto_Update-00f3ff?style=flat-square&logo=github-actions)](https://github.com/JE668/m3u-checker-max/actions)
[![Python Version](https://img.shields.io/badge/Python-3.10-3b82f6?style=flat-square&logo=python)](#)
[![CDN Accelerated](https://img.shields.io/badge/CDN-gh.llkk.cc-f59e0b?style=flat-square)](#)

这是一个高级的全自动 IPTV 直播源验证与管理系统。通过 GitHub Actions 每天定时运行，为您提供**无死链、秒开加载、带有完整 EPG 节目单**的纯净直播源列表。

---

## 🛠️ 文件结构说明 (全新模块化架构)

```text
📦 m3u-checker-max
 ┣ 📂 config           <-- ⚙️ 配置文件目录 (你需要编辑的都在这里)
 ┃ ┣ 📜 sources.txt    (上游 M3U/TXT 直播源直链)
 ┃ ┣ 📜 epg.txt        (上游 XML/GZ 节目单链接)
 ┃ ┣ 📜 alias.txt      (频道别名智能正则映射引擎)
 ┃ ┗ 📜 demo.txt       (最终输出的分类骨架与排序模板)
 ┣ 📂 output           <-- 🚀 自动生成的成品目录
 ┃ ┣ 📜 live.m3u       (M3U 标准成品)
 ┃ ┣ 📜 live.txt       (TXT 标准成品)
 ┃ ┣ 📜 epg.xml.gz     (高压缩率纯净版 EPG)
 ┃ ┗ 📜 log.txt        (详尽的运行与清洗报告)
 ┣ 📂 .github/workflows
 ┃ ┗ 📜 update.yml     (GitHub Actions 定时任务配置)
 ┣ 📜 main.py          (核心 Python 引擎，包含 Gitee/Github 智能纠错机制)
 ┣ 📜 index.html       (科技感网页前端视图)
 ┗ 📜 README.md
