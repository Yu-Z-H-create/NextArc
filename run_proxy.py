#!/usr/bin/env python3
"""
USTC Young 代理服务 - 独立启动入口

用法:
    python run_proxy.py              # 默认 0.0.0.0:8899
    python run_proxy.py --port 8080  # 自定义端口
    python run_proxy.py --debug      # 调试模式

启动后在浏览器访问 http://服务器IP:8899 即可使用。
"""

import argparse
import logging
import sys


def main():
    parser = argparse.ArgumentParser(
        description="USTC Young 前端覆写代理服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_proxy.py                    # 启动在 0.0.0.0:8899
  python run_proxy.py --port 8080        # 自定义端口
  python run_proxy.py --host 192.168.1.100 --port 8080
  python run_proxy.py --debug            # 开启详细日志
        """,
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="监听地址 (默认 0.0.0.0，允许所有设备访问)",
    )
    parser.add_argument(
        "--port", type=int, default=8899,
        help="监听端口 (默认 8899)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="开启 DEBUG 级别日志",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print()
    print("=" * 50)
    print("  🎓 USTC Young 代理服务")
    print("=" * 50)
    print(f"  监听地址: {args.host}:{args.port}")
    print(f"  代理目标: young.ustc.edu.cn")
    print("=" * 50)
    print()
    print("  📱 手机使用:")
    print(f"     1. 浏览器打开 http://<服务器IP>:{args.port}")
    print("     2. 点击页面上的链接进入目标网站")
    print("     3. 登录后签到按钮将自动显示")
    print()
    print("  按 Ctrl+C 停止服务")
    print()

    from src.ustc_proxy.server import run

    try:
        run(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\n\n✅ 服务已停止")


if __name__ == "__main__":
    main()
