# USTC Young 代理服务

> 在服务器端拦截并修改 `young.ustc.edu.cn` 的前端行为，**手机浏览器无需安装任何扩展**。

等效于电脑端 Chrome DevTools Override 的效果。

## 功能

| 功能 | 说明 |
|------|------|
| ✅ 签到按钮强制显示 | `showSignBtn()` → 始终返回 `true` |
| ✅ 签退按钮强制显示 | `showSignOutBtn()` → 始终返回 `true` |
| ✅ 所有功能按钮解锁 | getButton API result 扩展为 `[1,7,2,3,4,5,6,8]` |
| ✅ 二维码自动展示 | 点击"签到二维码"后直接弹出图片 |

## 快速开始

### 1. 安装依赖（仅需 aiohttp）

```bash
pip install aiohttp
```

### 2. 启动服务

```bash
# 默认监听 0.0.0.0:8899
python run_proxy.py

# 自定义端口
python run_proxy.py --port 8080

# 调试模式（查看详细日志）
python run_proxy.py --debug
```

### 3. 手机访问

1. **确认网络互通**：手机和服务器在同一局域网，或服务器有公网 IP
2. 手机浏览器打开：`http://<服务器IP>:8899`
3. 页面会显示使用说明和快捷链接
4. 点击链接进入目标网站 → 登录 → 进入项目详情页
5. 签到/签退按钮自动显示，点击签到二维码可直接查看

## 工作原理

```
手机浏览器 (Kiwi/Chrome)
    │
    ▼
你的服务器 (0.0.0.0:8899)  ←── HTTP 反向代理
    │
    ├── HTML 响应 → 注入 JS 补丁脚本（Vue 组件覆写 + API 拦截）
    ├── /mobile/item/getButton/* API → 修改返回的 result 数组
    └── /mobile/item/createWxaCodeUnlimit API → 记录二维码数据 + 触发页面展示
    │
    ▼
young.ustc.edu.cn (原始服务器)
```

## 与 NextArc 的关系

本代理服务**独立运行**，不影响 NextArc 主程序。两者可以同时启动：

```bash
# 终端 1：运行 NextArc 飞书机器人
python -m src.main

# 终端 2：运行 USTC 代理服务
python run_proxy.py
```

## 配置说明

所有配置在 `src/ustc_proxy/server.py` 顶部：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TARGET_HOST` | `young.ustc.edu.cn` | 代理目标域名 |
| `FULL_BUTTON_RESULT` | `["1","7","2","3","4","5","6","8"]` | 强制启用的按钮 ID |

修改后重启即可生效。

## 故障排查

### 服务无法启动
- 检查端口是否被占用：`lsof -i :8899` 或 `netstat -tlnp | grep 8899`
- 换个端口：`python run_proxy.py --port 9000`

### 手机无法访问服务器
- 确认防火墙放行对应端口
- 确认服务器绑定地址是 `0.0.0.0` 而不是 `127.0.0.1`
- 同局域网时确认手机 WiFi 和服务器在同一网段

### 页面打开但按钮没出现
- 打开浏览器控制台查看 `[USTC-Proxy]` 开头的日志
- 确认目标网站已登录
- 尝试刷新页面（清除缓存）

### 日志开启调试模式
```bash
python run_proxy.py --debug
```
