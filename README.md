# FM225 人脸模组可视化调试平台

基于 **Flask + pyserial + OpenCV** 的 Web 调试工具，用于 **FM225 人脸识别模组** 的
UART 协议调试与 UVC 视频预览。把串口原始帧（TX / RX）、协议解析结果
（REPLY / NOTE / IMAGE）、命令交互、以及把人脸框叠加到实时视频流上，
全部集中在一个网页里，开箱即用、无需额外客户端。

> 适用环境：Linux（已验证 Ubuntu 22.04，Python 3.10+）。

---

## 功能特性

- **串口连接管理**：自动枚举串口（CH340 优先标记），可设波特率，连接 / 断开。
- **实时帧监控**：SSE 推送原始 TX（绿）/ RX（蓝）帧，十六进制一目了然。
- **协议解析**：自动解析 REPLY / NOTE / IMAGE，NOTE 人脸状态实时显示
  `state / 坐标 box / yaw / pitch / roll`，并把错误码翻译成中文。
- **视频预览 + 框叠加**：抓取 UVC（`/dev/video0`），把 UART NOTE 的人脸框
  叠加后以 MJPEG 推流到网页；相机被占用时自动回退到合成帧（框仍叠加）。
- **命令面板**：单脸录入、五向录入、解锁、用户列表、删除用户 / 删除全部、
  模组复位等，并实时显示模组回包。
- **人脸位置可视化**：画布实时绘制人脸框，坐标上限可校准（常见 100 / 255 / 640）。
- **用户列表**：解析并显示模组返回的用户数据。

---

## 硬件连接

FM225 通过两条通道与主机通信：

| 通道 | 接口 | Linux 设备节点 | 说明 |
|------|------|----------------|------|
| 串口 | CH340 (USB-TTL, VID `1A86` / PID `7523`) | `/dev/ttyUSB0` | 命令 / REPLY / NOTE 帧 |
| 视频 | UVC (USB Video Class) | `/dev/video0` | MJPG 320×240 预览 |

**权限**：串口属 `dialout` 组、相机属 `video` 组。若当前用户无权限，把用户加入对应组：

```bash
sudo usermod -aG dialout,video $USER
# 重新登录后生效
```

---

## 环境依赖

- Python 3.10 或更高版本
- 依赖见 `requirements.txt`：`flask` / `pyserial` / `opencv-python-headless` / `numpy`

> 若系统 `pip` 缺失，可用官方引导脚本安装：
> `curl https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && python3 /tmp/get-pip.py --user`

---

## 快速开始

```bash
# 1. 安装依赖
python3 -m pip install -r requirements.txt

# 2. 启动服务（默认监听 0.0.0.0:5000）
python3 app.py

# 3. 浏览器打开
#    http://127.0.0.1:5000
```

启动后页面分三栏：

1. **左侧 · 连接 + 命令**：刷新并选择串口 →「连接」；下方按钮发送模组命令。
2. **中间 · 视频预览 + 人脸可视化**：填 UVC 设备节点（默认 `/dev/video0`）→「开启」
   看实时画面与叠加框；下方画布按 NOTE 帧实时绘制人脸位置。
3. **右侧 · 日志 + 回包 + 用户列表**：实时帧日志、最近回包、用户数据表。

---

## 协议帧格式

所有串口帧遵循统一结构：

```
┌──────────┬────────┬──────────┬────────┬─────────┐
│ Sync     │ MsgID  │ Size     │ Data   │ Check   │
│ EF AA    │ 1B     │ 2B(BE)   │ N B    │ 1B      │
└──────────┴────────┴──────────┴────────┴─────────┘
```

- **Sync**：固定 `EF AA`（2 字节，校验时跳过）。
- **MsgID**：消息 ID（1 字节）。
- **Size**：Data 段长度（2 字节，**无符号大端**）。
- **Data**：N 字节载荷。
- **Check**：校验位 = 从 `MsgID` 开始到 `Data` 结束所有字节的 **XOR**（不含 Sync）。

消息类型（MsgType）：

| 值 | 含义 |
|----|------|
| 0  | REPLY（模组对命令的应答） |
| 1  | NOTE（主动上报，如人脸状态） |
| 2  | IMAGE（图像数据） |

**NOTE `nid=0x01`** 载荷为 8 个 `uint16`（大端）：

`state, left, top, right, bottom, yaw, pitch, roll`

其中 `left/top/right/bottom` 为归一化人脸框坐标，`yaw/pitch/roll` 为头部姿态角，
`state` 为人脸检测结果状态码（见源码 `FACE_STATE` 表）。

---

## 目录结构

```
fm225_debug_platform/
├── app.py            # Flask 后端：串口读写、协议组帧/解析、UVC 抓取与框叠加、REST/SSE 接口
├── index.html        # 前端仪表盘：连接/命令/视频预览/人脸可视化/日志
├── requirements.txt  # Python 依赖
└── README.md         # 本文件
```

---

## 常见问题

- **视频打不开 / 显示「NO CAMERA」**：`/dev/video0` 被其它程序占用（如同一设备的图形预览）
  时会自动回退到合成帧，关掉占用程序再点「开启」即可看到真实画面。
- **人脸框位置/大小不对**：右上角「坐标上限」要按模组实际量程填写
  （常见 `100` / `255` / `640`），框按比例缩放。
- **串口打不开（权限拒绝）**：把当前用户加入 `dialout` 组并重新登录（见上文）。
- **`/api/ports` 为空**：确认 FM225 的 CH340 已接入且被系统识别为 `/dev/ttyUSB*`。

---

## 开源协议

本项目以 **MIT License** 开源。欢迎提交 Issue 与 Pull Request。
