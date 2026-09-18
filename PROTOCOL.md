# FM225 串口协议逐字段翻译

> 依据 `face.py` / `app.py` 中实现的协议（即模块手册的帧格式与状态码表）整理。
> 串口参数：**115200 8N1**，硬件为 CH340（`/dev/ttyUSB0` 或 Windows `COMx`）。
> 本工具「串口原始帧」面板里看到的每一行 `EF AA … Check` 都对应下面的一帧。

---

## 1. 帧结构（每一包都是这个样子）

| 字段 | 长度 | 含义 | 说明 |
|------|------|------|------|
| **SyncWord** | 2 B | 同步字，固定 `EF AA` | 帧头，用来在串口字节流里切包 |
| **MsgID** | 1 B | 消息 ID | 命令号 / 回复号（见 §3、§4） |
| **Size** | 2 B | Data 长度（小端 `H`，单位字节） | `N`，范围 0~65535；`N=0` 表示本消息无参数 |
| **Data** | N B | 消息体 | 命令参数或回包载荷（见各消息） |
| **Check** | 1 B | 校验码 | 整条协议**除 SyncWord 外**、其余字节按位 **XOR**（异或） |

> 校验计算：`Check = data[2] ^ data[3] ^ … ^ 最后一字节`（即从 MsgID 开始到 Data 末尾全部异或）。
> 接收方用同样算法重算，不一致即丢帧。

---

## 2. 消息大类（MsgType，出现在解析阶段，不是 MsgID）

串口读线程按帧内第一个数据字节区分三类：

| MsgType | 值 | 含义 | 载荷 |
|---------|-----|------|------|
| REPLY | `0x00` | 模块对命令的应答 | 见 §4 |
| NOTE | `0x01` | 模块主动上报的状态/事件 | 见 §5 |
| IMAGE | `0x02` | 照片数据（JPG） | Size 字节的 JPEG |

---

## 3. 主机→模块 命令（MsgID 一览）

| MsgID | 名称 | Data | 作用 |
|-------|------|------|------|
| `0x01` | reset | 无 | 模组复位 |
| `0x02` | getstatus | 无 | 查询模组状态 |
| `0x12` | unlock | `need_poweroff(1B) + timeout(1B)` | 解锁（带超时，默认 5s） |
| `0x13` | enroll_5 | `admin(1B) + name[32] + face_dir(1B) + timeout(1B)` | 五向录入（单脸=正脸 `0x01`，后续自动串联左/右/上/下） |
| `0x1d` | enroll | `admin(1B) + name[32] + face_dir(1B) + timeout(1B)` | 单脸录入（只录正脸） |
| `0x1e` | enroll_with_photo | `admin(1B) + name[32] + …` | 照片注册（本工具未实现） |
| `0x20` | deluser | `uid(2B 小端)` | 按 UID 删除用户 |
| `0x21` | delalluser | 无 | 删除全部用户 |
| `0x23` | face_reset | 无 | 清除已录入人脸（五向录入前先清） |
| `0x24` | getalluser | 无 | 获取用户列表 |

> `name[32]`：用户名 UTF-8，不足 32 字节右侧补 `0x00`。
> `face_dir`（方向掩码）：`middle=0x01, right=0x02, left=0x04, down=0x08, up=0x10`；
> 五向录入完成后模块回 **累计掩码 `0x1F`**（不是单 bit `0x08`）。

---

## 4. REPLY 回包（MsgType=0x00）

前 2 字节固定为：**`mid(1B) + result(1B)`**，其余为载荷。

### 4.1 result 错误码表（重点看超时）

| result | 含义 | 中文 |
|--------|------|------|
| `0x00` | MR_SUCCESS | 操作成功 |
| `0x01` | MR_REJECTED | 模组拒绝该命令 |
| `0x02` | MR_ABORTED | 录入/验证算法已终止 |
| `0x04` | MR_FAILED_CAMERA | **相机打开失败** |
| `0x05` | MR_FAILED_UNKNOWNREASON | 未知错误 |
| `0x06` | MR_FAILED_INVALIDPARAM | 无效参数 |
| `0x07` | MR_FAILED_NOMEMORY | 内存不足 |
| `0x08` | MR_FAILED_UNKNOWNUSER | 无已录入用户 |
| `0x09` | MR_FAILED_MAXUSER | 超过最大用户数 |
| `0x0A` | MR_FAILED_FACEENROLLED | 人脸已录入 |
| `0x0C` | MR_FAILED_LIVENESSCHECK | 活体检测失败 |
| **`0x0D` (23)** | **MR_FAILED_TIMEOUT** | **录入或解锁超时 ← 你遇到的就是它** |
| `0x0E` (14) | MR_FAILED_AUTHORIZATION | 加密芯片授权失败 |
| `0x13` (19) | MR_FAILED_READ_FILE | 读文件失败 |
| `0x14` (20) | MR_FAILED_WRITE_FILE | 写文件失败 |
| `0x15` (21) | MR_FAILED_NO_ENCRYPT | 通信协议未加密 |
| `0x17` (23) | MR_FAILED_NO_RGBIMAGE | RGB 图像未 ready |
| `0x18` (24) | MR_FAILED_JPGPHOTO_LARGE | 照片过大（照片注册） |
| `0x19` (25) | MR_FAILED_JPGPHOTO_SMALL | 照片过小（照片注册） |

> `result=0x00` 且各 mid 的载荷：
> - `0x12` unlock：`uid(2B) + name[32] + isadmin(1B) + unlockstatus(1B)`（成功）
> - `0x13`/`0x1d` enroll：`uid(2B) + face_dir(1B)`（成功，face_dir 见 §3）
> - `0x24` getalluser：用户列表原始数据

---

## 5. NOTE 状态上报（MsgType=0x01）

### 5.1 nid=0x00 / 0x02
- `0x00`：设备 ready
- `0x02`：设备 error

### 5.2 nid=0x01 人脸状态帧（最重要，录入时实时看它）

Data 共 17 字节：`state(2B 小端) + left(2B) + top(2B) + right(2B) + bottom(2B) + yaw(2B) + pitch(2B) + roll(2B)`

| 字段 | 含义 | 说明 |
|------|------|------|
| **state** | 人脸状态码 | 见下表；`0x00` 才表示「检测OK，可录入」 |
| left/top/right/bottom | 人脸框坐标 | 相对 `coordMax`（量程上限，常见 100/255/640）归一化 |
| yaw / pitch / roll | 头部姿态角 | 偏航/俯仰/翻滚，数值越小越正对 |

### 5.3 state 状态码表

| state | 含义 | 录入时含义 |
|-------|------|-----------|
| `0x00` | ok 检测到人脸 | ✅ 正对、在范围内，可录 |
| `0x01` | 未检测到人脸 | 镜头里没人 / 太暗 |
| `0x02` | 太靠上 | 脸往上移 |
| `0x03` | 太靠下 | 脸往下移 |
| `0x04` | 太靠左 | 脸往左移 |
| `0x05` | 太靠右 | 脸往右移 |
| `0x06` | 太靠远 | 凑近一点 |
| `0x07` | 太靠近 | 退远一点 |
| `0x08` | 眉毛遮挡 | 抬头/整理刘海 |
| `0x09` | 眼睛遮挡 | 睁眼 |
| `0x0A` | 脸部遮挡 | 别挡脸 |
| `0x0B` | 录入方向错误 | 当前方向不对（五向序列顺序错） |
| `0x0C` | 闭眼检测到睁眼 | — |
| `0x0D` | 闭眼 | 睁眼 |
| `0x0E` | 闭眼模式无法判断 | — |

---

## 6. 录入超时的排查顺序（结合本工具）

1. 点「五向录入」后，**盯「人脸状态」badge**：必须持续出现 `0x00 检测到人脸` 才不超时；
   若出现 `0x01~0x0E`（太靠远/未检测到/遮挡…），模块收不到合格人脸 → 超时（result `0x0D`）。
2. 五向顺序：正脸 → 左 → 右 → 上 → 下，每向需在超时内（默认 20s）完成；
   任一时段状态不是 `0x00` 都会超时。
3. **相机本身没图 = 必超时**：FM225 的 UVC 相机（`/dev/video0`）若被占用/卡死，
   模块内部取不到画面 → 每次录入都 `MR_FAILED_TIMEOUT`。本工具「开启视频」若报
   `busy: Device or resource busy`，即为此根因，需在 VMware 主机「可移动设备」中断开重连摄像头或重启 VM。
4. 排查用日志：本工具已加**持久协议日志** `/tmp/fm225_proto.log`（VM 上），
   浏览器点「导出协议日志」可下载；搜索 `result=0x0d` 看超时回包，搜索 `NOTE state=0x`
   看录入时人脸状态变化。

---

## 7. 本工具「串口原始帧」怎么读

- `▶ TX …` = 你点按钮发出的一帧（如 `ef aa 13 23 00 61 64 6d 69 6e …` 是五向录入命令）
- `◀ RX …` = 模块返回的一帧（REPLY/NOTE/IMAGE 按 MsgType 区分）
- 想确认一帧对不对：把 `EF AA` 后的字节按 §1 拆开，对照 §3/§4/§5 即可逐字段翻译。
