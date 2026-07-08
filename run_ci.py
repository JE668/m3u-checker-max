#!/usr/bin/env python3
"""CI 分阶段运行器：将 main.py 拆成 3 个独立步骤执行。

用法：
    python run_ci.py 1   # 阶段1：加载配置 → 抓取源 → 黑白名单过滤 → 保存状态
    python run_ci.py 2   # 阶段2：加载状态1 → 并发测速 → 保存状态
    python run_ci.py 3   # 阶段3：加载状态2 → 模板进化 → 成品输出

环境变量：
    PYTHONUNBUFFERED=1   # 建议在 CI 中设置，确保日志实时输出
"""
import sys
import main

PHASE_TITLES = {
    1: "🔍 阶段1 — 抓取直播源 & 黑白名单过滤",
    2: "🚀 阶段2 — 并发测速 & 流校验",
    3: "🧠 阶段3 — 模板进化 & 成品输出",
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python run_ci.py <1|2|3>")
        sys.exit(1)

    phase = int(sys.argv[1])
    title = PHASE_TITLES.get(phase, f"阶段{phase}")

    # GitHub Actions 日志分组
    print(f"::group::{title}", flush=True)
    main.main(ci_phase=phase, ci_state_dir="tmp")
    print("::endgroup::", flush=True)
    print(f"\n━━━ ✅ {title} 执行完毕 ━━━━━━━━━━━━━━━━━━━")
