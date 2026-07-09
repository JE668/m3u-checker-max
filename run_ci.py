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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python run_ci.py <1|2|3>")
        sys.exit(1)

    phase = int(sys.argv[1])
    # 阶段标题与子任务分组由 main.main 内部通过 ci_group 负责（GitHub Actions 的
    # ::group:: 不支持嵌套，故此处不再包裹外层 group，以免子分组被忽略失效）
    main.main(ci_phase=phase, ci_state_dir="tmp")
    print(f"\n━━━ ✅ 阶段{phase} 执行完毕 ━━━━━━━━━━━━━━━━━━━", flush=True)
