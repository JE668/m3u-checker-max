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
```

## ✨ 核心特性

- ⚡ **100线程高并发测速**：内置极速网络探测，准确剔除死链、卡顿流。
- ⏱️ **智能测速优选**：针对同一个频道内的多个不同链接，系统会自动按照**响应时间从短到长**重新排序。确保电视端播放器总是优先加载最快的源。
- 📅 **EPG 多源聚合与防伪清洗**：
  - 自动下载 `.xml` 与 `.xml.gz` 多源节目单进行去重整合。
  - **魔法头部校验**：完美识别并跳过伪装成 XML 的恶意/屏蔽网页，告别解析报错。
  - **防呆设计**：自动将 Gitee/GitHub 的误填网页链接 (blob) 纠错修正为真实的底层数据流 (raw)。
  - **垃圾信息清洗**：自动剔除包含“未提供节目表”、“精彩节目”等视觉污染数据。
- 🔤 **别名正则映射引擎**：自带 `alias.txt`，将杂乱名归一化。
- 🌐 **沉浸式科技感网页面板**：全自动部署至 GitHub Pages，所有分发链路强制挂载 `gh.llkk.cc` 节点 CDN 代理加速，国内直连秒开。

## 🚀 如何开始使用？

1. **Fork 本仓库** 到你的个人 GitHub 账号下。
2. 进入 `config` 文件夹，根据需要修改你的配置。
3. 进入 **Actions** 页面，点击绿色按钮 **I understand my workflows...**
4. 左侧点击 **Update IPTV Links**，点击右上角 **Run workflow** 手动开始全量检测。
5. **开启可视化网页端**：进入仓库 **Settings** -> 左侧 **Pages** -> Source 设置为 `Deploy from a branch` -> 选定 `main` 并保存。

---
*免责声明：本项目及脚本仅供学习与技术交流使用，不提供、不存储任何音视频流。*
```
