"""结构检查：验证核心文件均已就位。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CORE_FILES = [
    "app/main.py",
    "app/lifespan.py",
    "app/config.py",
    "app/api/health.py",
    "app/infrastructure/dashscope.py",
    "app/infrastructure/redis.py",
    "app/infrastructure/logging.py",
    "app/infrastructure/metrics.py",
    "app/services/cache_service.py",
]


def main() -> int:
    missing = [f for f in CORE_FILES if not (ROOT / f).exists()]
    if missing:
        print("缺失文件：")
        for f in missing:
            print(f"  - {f}")
        return 1
    print(f"结构检查通过：{len(CORE_FILES)} 个核心文件均存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
