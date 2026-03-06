# 📡 IPTV M3U Checker Max

[![GitHub Actions Status](https://img.shields.io/badge/GitHub_Actions-Auto_Update-00f3ff?style=flat-square&logo=github-actions)](https://github.com/JE668/m3u-checker-max/actions)
[![Python Version](https://img.shields.io/badge/Python-3.10-3b82f6?style=flat-square&logo=python)](#)
[![CDN Accelerated](https://img.shields.io/badge/CDN-gh.llkk.cc-f59e0b?style=flat-square)](#)

这是一个高级的全自动 IPTV 直播源验证与管理系统。通过 GitHub Actions 每天定时运行，为您提供**无死链、秒开加载、带有完整 EPG 节目单**的纯净直播源列表。

---

## ✨ 核心特性

- ⚡ **100线程高并发测速**：内置极速网络探测，准确剔除死链、卡顿流。
- ⏱️ **智能测速优选**：针对同一个频道内的多个不同链接，系统会自动按照**响应时间从短到长**重新排序。确保电视端播放器总是优先加载最快的源。
- 📅 **EPG 多源聚合与防伪清洗**：
  - 自动下载 `.xml` 与 `.xml.gz` 多源节目单进行去重整合。
  - **魔法头部校验**：完美识别并跳过伪装成 XML 的恶意/屏蔽网页，告别解析报错。
  - **垃圾信息清洗**：自动剔除包含“未提供节目表”、“精彩节目”等视觉污染数据。
  - 生成 `gh.llkk.cc` CDN 代理加速头部，电视端秒加载 EPG。
- 🔤 **别名正则映射引擎**：自带 `alias.txt`，不管是 `CCTV1` 还是 `CCTV-1综合HD`，均能利用正则表达式智能归一化为标准的 `CCTV-1`。
- 📂 **高度自定义模板**：基于 `demo.txt` 架构，输出带有 `group-title` (分类)、`tvg-logo` (台标) 等完美属性的标准 M3U。
- 🌐 **沉浸式科技感网页面板**：全自动部署至 GitHub Pages，随时随地在网页端查看、测速报告与复制直播源。

---

## 🛠️ 文件结构说明

| 文件名 | 功能描述 |
| :--- | :--- |
| `main.py` | 核心检测与构建脚本（无需修改）。 |
| `UPSTREAM_SOURCES.txt` | **【源链接配置】** 填入您收集的网上 M3U/TXT 订阅源链接，支持多行合并。 |
| `UPSTREAM_EPG.txt` | **【节目单配置】** 填入上游的 `.xml` / `.xml.gz` 节目单链接。 |
| `alias.txt` | **【别名清洗词典】** 利用精准匹配或 `re:` 正则表达式，将杂乱的频道名映射为标准名称。 |
| `demo.txt` | **【分类骨架模板】** 决定最终输出文件的频道分类和排序顺序（如“央视频道”、“卫视频道”）。 |

---

## 🚀 如何开始使用？

1. **Fork 本仓库** 到你的个人 GitHub 账号下。
2. 编辑 `UPSTREAM_SOURCES.txt`，将网上收集到的直播源直链填进去。
3. 进入 **Actions** 页面，点击绿色按钮 **I understand my workflows, go ahead and enable them**。
4. 左侧点击 **Update IPTV Links**，点击右上角 **Run workflow** 即可手动开始第一次全量检测！
5. **开启可视化网页端**：
   - 进入仓库 **Settings** (设置) -> 左侧菜单栏 **Pages**。
   - 在 **Build and deployment** 中将 Source 设置为 `Deploy from a branch`。
   - Branch 选择 `main` (或 `master`)，文件夹选择 `/ (root)`，点击 **Save**。
   - 等待 1-2 分钟，页面顶部会显示您的专属直播源网页链接（如：`https://用户名.github.io/m3u-checker-max/`）。

> **💡 定时自动更新机制**
> 配置文件 `.github/workflows/update.yml` 中默认设置了每天自动检测更新。即使您不碰它，系统也会每天默默为您筛选出最新的可用存活源。

---

## 📥 获取最终生成的直播源

如果一切运行正常，系统会自动提交并生成以下可用文件：
- **M3U 格式 (带频道图标与 EPG 接口)**：`live.m3u`
- **TXT 格式 (支持各类极简播放器)**：`live.txt`
- **节目单文件**：`epg.xml.gz`
- **详尽测速报告**：`log.txt`

将上述链接直接输入至 TiviMate、Kodi、电视家、DIYP 等播放器中即可畅享极速电视体验。

---
*免责声明：本项目及脚本仅供学习与技术交流使用，不提供、不存储任何音视频流。*
