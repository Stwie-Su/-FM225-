#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FM225 可视化调试平台 —— 后端 (Flask + pyserial + OpenCV UVC 预览)
==================================================================
Linux 环境 (已验证 Ubuntu 22.04, Python 3.10+)。启动后浏览器打开 http://127.0.0.1:5000
视频预览: 抓取 UVC(/dev/video0) 并把 UART NOTE 人脸框叠加后 MJPEG 推流。

前置:
    pip install flask pyserial opencv-python-headless
运行:
    python app.py
    # 浏览器 http://127.0.0.1:5000  → 连接 → 开启视频
注意: /dev/video0 被占用(如 LVGL app 实机运行)时预览会失败, 关掉占用程序再开。

协议帧: Sync(EF AA) + MsgID(1) + Size(2, unsigned) + Data(N) + Check(1, XOR)
"""
import os
import sys
import json
import time
import struct
import threading
import queue

from flask import Flask, request, Response, send_file, jsonify

try:
    import serial
    from serial.tools import list_ports
except Exception as e:
    serial = None
    list_ports = None
    _import_err = e
else:
    _import_err = None

try:
    import cv2
    import numpy as np
    HAVE_CV2 = True
except Exception as e:
    cv2 = None
    np = None
    HAVE_CV2 = False
    _cv_err = e

app = Flask(__name__)

# ===================== 全局状态 =====================
ser = None
ser_lock = threading.Lock()
event_q = queue.Queue()
reader_thread = None
running = False
ctx = {'username': '', 'enroll5_active': False}

# 视频/叠加
video_cap = None
video_thread = None
video_running = False
latest_frame = None
frame_lock = threading.Lock()
last_box = None
box_lock = threading.Lock()
coord_max = 100
video_device = '/dev/video0'
video_err = ''

# 会话内 NOTE 状态直方图（排查"是不是一次都没检测到脸"）
note_stats = {}

SYNC_WORD = 0xEFAA
CH340_VID, CH340_PID = 0x1A86, 0x7523

MR_REUSLTS = {
    0: 'MR_SUCCESS 操作成功', 1: 'MR_REJECTED 模组拒绝', 2: 'MR_ABORTED 算法终止',
    4: 'MR_FAILED_CAMERA 相机打开失败', 5: 'MR_FAILED_UNKNOWNREASON 未知错误',
    6: 'MR_FAILED_INVALIDPARAM 无效参数', 7: 'MR_FAILED_NOMEMORY 内存不足',
    8: 'MR_FAILED_UNKNOWNUSER 无已录入用户', 9: 'MR_FAILED_MAXUSER 超过最大用户',
    10: 'MR_FAILED_FACEENROLLED 人脸已录入', 12: 'MR_FAILED_LIVENESSCHECK 活体失败',
    13: 'MR_FAILED_TIMEOUT 超时', 14: 'MR_FAILED_AUTHORIZATION 授权失败',
    19: 'MR_FAILED_READ_FILE 读文件失败', 20: 'MR_FAILED_WRITE_FILE 写文件失败',
    21: 'MR_FAILED_NO_ENCRYPT 协议未加密', 23: 'MR_FAILED_NO_RGBIMAGE 无RGB图',
    24: 'MR_FAILED_JPGPHOTO_LARGE 照片过大', 25: 'MR_FAILED_JPGPHOTO_SMALL 照片过小',
}

FACE_STATE = {
    0x00: 'ok 检测到人脸', 0x01: '未检测到人脸', 0x02: '太靠上', 0x03: '太靠下',
    0x04: '太靠左', 0x05: '太靠右', 0x06: '太靠远', 0x07: '太靠近',
    0x08: '眉毛遮挡', 0x09: '眼睛遮挡', 0x0a: '脸部遮挡', 0x0b: '录入方向错误',
    0x0c: '闭眼检测到睁眼', 0x0d: '闭眼', 0x0e: '闭眼模式无法判断',
}

enroll_direction = {'middle': 0x01, 'right': 0x02, 'left': 0x04, 'down': 0x08, 'up': 0x10}
ALL_DIRS = 0x1F
no_data_cmd = {'reset': 0x01, 'getstatus': 0x02, 'delalluser': 0x21,
               'getalluser': 0x24, 'face_reset': 0x23, 'enroll_with_photo': 0x1e,
               'version': 0x30, 'sn': 0x93}

# 模组全部命令 MsgID → 中文名（回包翻译用，对齐手册 V1.7 命令表）
MID_NAMES = {
    0x01: '模组复位(RESET)', 0x02: '状态查询(GET STATUS)', 0x12: '解锁(UNLOCK)',
    0x13: '五向录入(ENROLL_5)', 0x1d: '单脸录入(ENROLL_SINGLE)', 0x1e: '照片录入',
    0x20: '删除用户(DELUSER)', 0x21: '删除全部(DELALL)', 0x22: '查询用户(GETUSERINFO)',
    0x23: '清除录入状态(FACE_RESET)', 0x24: '获取用户列表(GET_ALL_USERID)',
    0x26: '交互录入(ENROLL_ITG)', 0x30: '获取版本(GET VERSION)', 0x50: '初始化加密',
    0x93: '获取序列号(GET SN)', 0xb0: '读UVC参数', 0xb1: '设置UVC参数',
    0xf6: '固件升级', 0xf7: '照片注册(ENROLL_WITH_PHOTO)',
}

# 典型失败原因的可操作提示
RESULT_HINT = {
    0x01: '（模组正忙：录入/识别会话进行中会拒绝复位/查询，可改用「清除录入状态」或等会话超时后再试）',
    0x04: '（相机打开失败：检查双摄+灯板排线是否接好、UVC 是否被上位机占用）',
    0x0d: '（整个会话期间模组未检测到人脸：本平台 NOTE 一直报 state=0x01。'
          '请检查 ①红外/可见光双摄与灯板排线 ②镜头是否贴膜遮挡 ③人脸距离 0.5~1m、光照正常）',
}


# ===================== 协议组帧 =====================
def get_parity_code(packet: bytes) -> int:
    p = 0
    for b in packet[2:]:
        p ^= b
    return p


def _pack(msgid: int, data: bytes) -> bytes:
    head = struct.pack('>H B H', SYNC_WORD, msgid, len(data))
    body = head + data
    return body + get_parity_code(body).to_bytes(1, 'big')


def _name32(s: str) -> bytes:
    b = s.encode('utf-8', 'ignore')[:32]
    return b.ljust(32, b'\x00')


def make_unlock_data(need_poweroff=0, timeout=5):
    return _pack(0x12, struct.pack('>B B', need_poweroff & 0xFF, timeout & 0xFF))


def make_deluser_data(uid):
    return _pack(0x20, struct.pack('>H', uid & 0xFFFF))


def make_enroll_data(username, admin=0, face_dir=0, timeout=0):
    return _pack(0x1d, struct.pack('>B 32s B B', admin & 0xFF, _name32(username), face_dir & 0xFF, timeout & 0xFF))


def make_enroll_5_data(username, direction, admin=0, timeout=20):
    return _pack(0x13, struct.pack('>B 32s B B', admin & 0xFF, _name32(username), direction & 0xFF, timeout & 0xFF))


def make_cmd_with_no_data(cmd):
    return _pack(cmd, b'')


def make_get_user_data():
    return _pack(0x24, b'')


def make_read_uvc_data():
    """0xB0 READ_USB_UVC_PARAMETERS: 读 usb类型/旋转180+镜像/JPEG质量"""
    return _pack(0xb0, b'')


def make_set_uvc_data(usb_type=0x20, rotate180=0, mirror=0, jpeg_q=80):
    """0xB1 SET_USB_UVC_PARAMETERS
    usb_type: 0x11=USB1.1 / 0x20=USB2.0
    第2字节 BIT0 旋转180, BIT1 镜像翻转
    """
    flag = (0x01 if rotate180 else 0) | (0x02 if mirror else 0)
    return _pack(0xb1, struct.pack('BBB', usb_type & 0xFF, flag, max(10, min(99, int(jpeg_q)))))


# ===================== 事件推送 =====================
def ts():
    return time.strftime('%H:%M:%S')


def push(ev: dict):
    ev.setdefault('ts', ts())
    event_q.put(ev)
    proto_log(ev)


# ===================== 协议持久日志（排查录入超时等） =====================
PROTO_LOG = '/tmp/fm225_proto.log'
proto_lock = threading.Lock()

def fmt_proto(ev: dict) -> str:
    t = ev.get('type', '')
    if t == 'raw_tx':
        return 'TX  ' + (('[' + ev.get('label', '') + '] ') if ev.get('label') else '') + ev.get('hex', '')
    if t == 'raw_rx':
        return 'RX  ' + (('[' + ev.get('kind', '') + '] ') if ev.get('kind') else '') + ev.get('hex', '')
    if t == 'note':
        if ev.get('nid') == 0x01:
            b = ev.get('box', {})
            return ('NOTE state=0x%02x(%s) box L%d T%d R%d B%d yaw%d pitch%d roll%d'
                    % (ev.get('state', 0), ev.get('state_text', ''), b.get('left', 0), b.get('top', 0),
                       b.get('right', 0), b.get('bottom', 0), ev.get('yaw', 0), ev.get('pitch', 0), ev.get('roll', 0)))
        return 'NOTE ' + ev.get('text', '')
    if t == 'reply':
        return 'REPLY ok=%s mid=0x%02x result=0x%02x %s' % (
            ev.get('ok'), ev.get('mid', 0), ev.get('result', 0), ev.get('text', ''))
    if t == 'error':
        return 'ERROR ' + ev.get('msg', '')
    if t == 'status':
        return 'STATUS ' + ev.get('msg', '')
    if t == 'image':
        return 'IMAGE size=%s' % ev.get('size', 0)
    if t == 'users':
        return 'USERS count=%d' % len(ev.get('list', []))
    return t

def proto_log(ev: dict):
    try:
        line = ev.get('ts', '') + ' ' + fmt_proto(ev)
        with proto_lock:
            with open(PROTO_LOG, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def send_packet(data: bytes, label=''):
    with ser_lock:
        s = ser
    if s is None:
        push({'type': 'error', 'msg': '串口未连接'})
        return
    try:
        s.write(data)
        push({'type': 'raw_tx', 'hex': data.hex(' '), 'label': label})
    except Exception as e:
        push({'type': 'error', 'msg': f'发送失败: {e}'})


# ===================== 解析 =====================
def parse_note(data: bytes):
    global last_box
    if len(data) < 1:
        return
    nid = data[0]
    if nid == 0x01:
        if len(data) < 17:
            push({'type': 'note', 'text': '人脸状态(数据过短)'})
            return
        # 实测 NOTE 载荷为小端：state 原始字节 01 00 → LE=0x0001(未检测到人脸)；
        # 按 face.py 的 '>H' 大端解析会读成 0x100（协议表里不存在的码）
        state, left, top, right, bottom, yaw, pitch, roll = struct.unpack('<HHHHHHHH', data[1:17])
        with box_lock:
            last_box = {'box': {'left': left, 'top': top, 'right': right, 'bottom': bottom},
                        'state': state, 'state_text': FACE_STATE.get(state, f'未知(0x{state:02x})')}
        note_stats[state] = note_stats.get(state, 0) + 1
        push({'type': 'note', 'nid': nid, 'state': state,
              'state_text': FACE_STATE.get(state, f'未知(0x{state:02x})'),
              'box': {'left': left, 'top': top, 'right': right, 'bottom': bottom},
              'yaw': yaw, 'pitch': pitch, 'roll': roll, 'raw': data.hex(' '),
              'stats': dict(sorted(note_stats.items()))})
    elif nid == 0x00:
        push({'type': 'note', 'text': '设备 ready'})
    elif nid == 0x02:
        push({'type': 'note', 'text': '设备 error'})
    else:
        push({'type': 'note', 'text': f'未知 NOTE nid=0x{nid:02x}'})


def parse_reply(data: bytes):
    if len(data) < 2:
        push({'type': 'reply', 'text': f'REPLY 过短 {data.hex(" ")}'})
        return
    mid, result = struct.unpack('BB', data[:2])
    rd = data[2:]
    name = MID_NAMES.get(mid, f'未知命令 0x{mid:02x}')
    if result != 0x00:
        txt = f'{name} 失败 result=0x{result:02x} {MR_REUSLTS.get(result, "未知错误")}'
        if result in RESULT_HINT:
            txt += ' ' + RESULT_HINT[result]
        push({'type': 'reply', 'ok': False, 'mid': mid, 'result': result, 'text': txt})
        return
    # ---- 各命令成功回包逐字段翻译（对齐手册 V1.7 回包格式表）----
    if mid == 0x12:  # UNLOCK: uid(2B) + name(32B) + admin(1B) + unlockstatus(1B)
        if len(rd) >= 36:
            uid, uname, isadmin, ust = struct.unpack('>H 32s B B', rd[:36])
            uname_s = uname.rstrip(b'\x00').decode('utf-8', 'ignore')
            push({'type': 'reply', 'ok': True, 'mid': mid,
                  'text': f'解锁成功 uid={uid} 用户={uname_s} 管理员={isadmin} 开锁状态={ust}'})
        else:
            push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '解锁成功(载荷过短)'})
    elif mid == 0x13:  # ENROLL_5: uid(2B) + face_dir(累计掩码)
        uid, fd = struct.unpack('>H B', rd[:3]) if len(rd) >= 3 else (0, 0)
        push({'type': 'reply', 'ok': True, 'mid': mid,
              'text': f'五向录入·单方向成功 uid={uid} 已录入方向掩码=0x{fd:02x}'})
        if fd == enroll_direction['middle']:
            push({'type': 'status', 'msg': '-> 录入左脸'})
            send_packet(make_enroll_5_data(ctx['username'], enroll_direction['left']), 'enroll left')
        elif fd == 0x05:
            push({'type': 'status', 'msg': '-> 录入右脸'})
            send_packet(make_enroll_5_data(ctx['username'], enroll_direction['right']), 'enroll right')
        elif fd == 0x07:
            push({'type': 'status', 'msg': '-> 录入上脸'})
            send_packet(make_enroll_5_data(ctx['username'], enroll_direction['up']), 'enroll up')
        elif fd == 0x17:
            push({'type': 'status', 'msg': '-> 录入下脸'})
            send_packet(make_enroll_5_data(ctx['username'], enroll_direction['down']), 'enroll down')
        elif fd == ALL_DIRS:
            push({'type': 'status', 'msg': '五向录入全部完成 ✅'})
            ctx['enroll5_active'] = False
    elif mid == 0x1d:  # ENROLL_SINGLE: uid(2B) + face_dir(1B, 01=正脸)
        uid, fd = struct.unpack('>H B', rd[:3]) if len(rd) >= 3 else (0, 0)
        push({'type': 'reply', 'ok': True, 'mid': mid,
              'text': f'单脸录入成功 uid={uid} face_dir=0x{fd:02x}'})
    elif mid == 0x24:  # GET_ALL_USERID: user_counts(1B) + users_id[50*2B 大端]
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '获取用户数据成功'})
        push({'type': 'users', 'list': decode_user_list(rd)})
    elif mid == 0x22:  # GETUSERINFO: uid(2B) + name(32B) + admin(1B)
        if len(rd) >= 35:
            uid, uname, isadmin = struct.unpack('>H 32s B', rd[:35])
            uname_s = uname.rstrip(b'\x00').decode('utf-8', 'ignore')
            push({'type': 'reply', 'ok': True, 'mid': mid,
                  'text': f'用户信息 uid={uid} 姓名={uname_s} 管理员={isadmin}'})
        else:
            push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '用户信息(载荷过短)'})
    elif mid == 0xb0:  # READ USB UVC PARAMETERS: usb_type(1) + 旋转/镜像(1) + JPEG质量(1)
        if len(rd) >= 3:
            ut, flag, q = struct.unpack('BBB', rd[:3])
            push({'type': 'reply', 'ok': True, 'mid': mid,
                  'text': f'UVC参数: USB类型=0x{ut:02x}({"USB2.0" if ut==0x20 else ("USB1.1" if ut==0x11 else "未知")}) '
                          f'旋转180={ "开" if flag & 0x01 else "关"} 镜像={"开" if flag & 0x02 else "关"} JPEG质量={q}%'
                          + (f' | 原始载荷={rd.hex(" ")}' if len(rd) > 3 else '')})
        else:
            push({'type': 'reply', 'ok': True, 'mid': mid,
                  'text': 'UVC参数(载荷过短) ' + rd.hex(' ')})
    elif mid == 0x30:  # GET VERSION: 版本信息字符串
        ver = rd.rstrip(b'\x00').decode('utf-8', 'ignore') or rd.hex(' ')
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': f'模组版本: {ver}'})
    elif mid == 0x93:  # GET SN: 32B，前 8 字节有效
        sn = rd[:8].rstrip(b'\x00').decode('utf-8', 'ignore') or rd[:8].hex(' ')
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': f'设备序列号: {sn} (raw {rd.hex(" ")})'})
    elif mid == 0x23:
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '清除录入状态成功（未开始录入，仅复位检测）'})
    elif mid == 0x01:
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '模组复位成功'})
    elif mid == 0x21:
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '已删除所有用户'})
    elif mid == 0x20:
        push({'type': 'reply', 'ok': True, 'mid': mid, 'text': '删除用户成功'})
    elif mid == 0x02:
        push({'type': 'reply', 'ok': True, 'mid': mid,
              'text': '状态查询成功' + (f' 载荷={rd.hex(" ")}' if rd else '')})
    else:
        push({'type': 'reply', 'ok': True, 'mid': mid,
              'text': f'{name} 成功' + (f' 载荷={rd.hex(" ")}' if rd else '')})


def decode_user_list(rd: bytes):
    """GET_ALL_USERID 成功回包: user_counts(1B) + 50 个 ID 各 2 字节(高字节在前)"""
    if not rd:
        return [{'uid': '-', 'note': '载荷为空'}]
    counts = rd[0]
    ids = []
    for i in range(counts):
        off = 1 + i * 2
        if off + 2 <= len(rd):
            ids.append(int.from_bytes(rd[off:off + 2], 'big'))
    return [{'uid': u} for u in ids] or [{'uid': '-', 'note': f'counts={counts} 但无可解析 ID'}]


# ===================== 真实串口读取线程 =====================
def reader_loop():
    global ser
    msg_types = {0: 'REPLY', 1: 'NOTE', 2: 'IMAGE'}
    header = bytearray(2)
    while running:
        with ser_lock:
            s = ser
        if s is None:
            break
        try:
            if s.in_waiting <= 0:
                time.sleep(0.01)
                continue
            header[0:1] = header[1:2]
            nb = s.read(1)
            if not nb:
                continue
            header[1:2] = nb
            if header.hex() != 'efaa':
                continue
            hdr = s.read(3)
            if len(hdr) < 3:
                continue
            msg_type, size = struct.unpack('>B H', hdr)
            if msg_type not in msg_types:
                push({'type': 'error', 'msg': f'未知 MsgType=0x{msg_type:02x}, 跳过 (请核对协议)'})
                if size > 0:
                    s.read(size + 1)
                continue
            data = s.read(size) if size > 0 else b''
            check = s.read(1)
            frame = bytes(header) + hdr + data + check
            push({'type': 'raw_rx', 'hex': frame.hex(' '), 'kind': msg_types[msg_type]})
            if msg_type == 0x01:
                parse_note(data)
            elif msg_type == 0x00:
                parse_reply(data)
            else:
                push({'type': 'image', 'size': size, 'check': check.hex()})
        except Exception as e:
            push({'type': 'error', 'msg': f'读取异常: {type(e).__name__} {e}'})
            break


# ===================== UVC 视频抓取 + 叠加 =====================
def make_placeholder_frame():
    if HAVE_CV2 and np is not None:
        f = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(f, 'NO CAMERA / synthetic', (30, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 1)
        cv2.putText(f, 'box overlay still works', (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 90, 90), 1)
        return f
    return None


def draw_overlay(frame, box, state, state_text, maxv):
    if box is None or frame is None:
        return frame
    h, w = frame.shape[:2]
    x = int(box['left'] / maxv * w); y = int(box['top'] / maxv * h)
    bw = int((box['right'] - box['left']) / maxv * w); bh = int((box['bottom'] - box['top']) / maxv * h)
    col = (63, 209, 122) if state == 0x00 else ((255, 107, 94) if state >= 0x08 else (255, 194, 75))
    cv2.rectangle(frame, (x, y), (x + bw, y + bh), col, 2)
    cv2.putText(frame, state_text, (max(2, x), max(16, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
    return frame


def scan_video_devices():
    """列出系统里所有 /dev/video* 节点"""
    import glob
    return sorted(glob.glob('/dev/video*'))


def diagnose_video_fail(device):
    """打开失败时，用 v4l2-ctl 拿到操作系统层面的真实原因"""
    import subprocess
    try:
        out = subprocess.run(['v4l2-ctl', '-d', device, '--info'],
                             capture_output=True, text=True, timeout=3)
        txt = (out.stdout + out.stderr).strip()
        low = txt.lower()
        if 'busy' in low or 'device or resource busy' in low:
            return (f'{device} 被占用(Device or resource busy)：设备可能正被其他程序占用，'
                    f'或 VMware 虚拟 USB 通道卡死。请到 VMware 菜单「可移动设备」中断开再重连该摄像头，或重启 VM。')
        if 'no such device' in low:
            return f'{device} 不存在'
        if txt:
            return f'{device} 无法打开：{txt[:200]}'
    except FileNotFoundError:
        return f'{device} 无法打开（v4l2-ctl 未安装，无法进一步诊断）'
    except Exception as e:
        return f'{device} 无法打开：{e}'
    return f'{device} 无法打开（未知原因）'


def try_open_capture(device):
    """优先用 V4L2 后端打开（避掉 ffmpeg/gstreamer 后端在部分 UVC 上打不开的坑）"""
    if not HAVE_CV2:
        return None, 'opencv 未安装'
    last_err = ''
    for use_backend in (True, False):
        try:
            cap = cv2.VideoCapture(device, cv2.CAP_V4L2) if use_backend else cv2.VideoCapture(device)
        except Exception as e:
            last_err = f'打开异常: {e}'
            continue
        if cap is not None and cap.isOpened():
            return cap, ''
        try:
            cap.release()
        except Exception:
            pass
        last_err = diagnose_video_fail(device)
    return None, last_err


def video_loop(cap):
    global video_cap, latest_frame
    while video_running:
        frame = None
        if cap is not None:
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
        if frame is None:
            frame = make_placeholder_frame()
        with box_lock:
            b = last_box
        if b is not None:
            frame = draw_overlay(frame, b['box'], b['state'], b['state_text'], coord_max)
        with frame_lock:
            latest_frame = frame
        time.sleep(1/20)
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass
    video_cap = None


def start_video(device, maxv, auto=True):
    global video_thread, video_running, video_device, coord_max, video_err, video_cap
    coord_max = max(1, int(maxv))
    video_running = False
    if video_thread is not None:
        try:
            video_thread.join(timeout=2)
        except Exception:
            pass
    cap, err = try_open_capture(device)
    chosen = device
    # 主节点打不开时，自动尝试其它 /dev/video* 节点
    if cap is None and auto:
        for d in scan_video_devices():
            if d == device:
                continue
            c2, e2 = try_open_capture(d)
            if c2 is not None:
                cap, err, chosen = c2, '', d
                break
    video_device = chosen
    video_err = err
    if cap is not None:
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        except Exception:
            pass
    video_cap = cap
    video_running = True
    video_thread = threading.Thread(target=video_loop, args=(cap,), daemon=True)
    video_thread.start()


# ===================== REST 路由 =====================
@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html'))


@app.route('/api/ports')
def api_ports():
    if list_ports is None:
        return jsonify({'ok': False, 'msg': 'pyserial 未安装', 'ports': []})
    out = []
    for p in list_ports.comports():
        out.append({'device': p.device, 'desc': p.description,
                    'ch340': (getattr(p, 'vid', None) == CH340_VID and getattr(p, 'pid', None) == CH340_PID)})
    return jsonify({'ok': True, 'ports': out})


@app.route('/api/connect', methods=['POST'])
def api_connect():
    global ser, reader_thread, running
    if serial is None:
        return jsonify({'ok': False, 'msg': f'pyserial 未安装: {_import_err}'})
    d = request.get_json(force=True, silent=True) or {}
    port = d.get('port', '')
    baud = int(d.get('baud', 115200))
    if not port:
        return jsonify({'ok': False, 'msg': '请选择串口'})
    with ser_lock:
        if ser is not None:
            try: ser.close()
            except Exception: pass
        ser = None
    running = True
    try:
        ser = serial.Serial(port, baud, timeout=1)
    except Exception as e:
        running = False
        return jsonify({'ok': False, 'msg': f'打开 {port} 失败: {e}'})
    push({'type': 'status', 'msg': f'已连接 {port} @ {baud}'})
    reader_thread = threading.Thread(target=reader_loop, daemon=True)
    reader_thread.start()
    return jsonify({'ok': True})


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    global ser, running
    running = False
    with ser_lock:
        if ser is not None:
            try: ser.close()
            except Exception: pass
            ser = None
    push({'type': 'status', 'msg': '已断开'})
    return jsonify({'ok': True})


@app.route('/api/command', methods=['POST'])
def api_command():
    d = request.get_json(force=True, silent=True) or {}
    cmd = d.get('cmd', '')
    # 每个新会话清空 NOTE 状态直方图，方便判断"这次到底有没有检测到过脸"
    if cmd in ('enroll', 'enroll5', 'unlock'):
        note_stats.clear()
    try:
        if cmd == 'enroll':
            ctx['username'] = d.get('username', 'admin')
            # timeout 单位秒；快速检测(5s)用短超时做姿态扫描，0=用模组默认
            send_packet(make_enroll_data(ctx['username'], timeout=int(d.get('timeout', 0))), 'enroll')
        elif cmd == 'enroll5':
            ctx['username'] = d.get('username', 'admin')
            ctx['enroll5_active'] = True
            send_packet(make_cmd_with_no_data(no_data_cmd['face_reset']), 'face_reset')
            time.sleep(0.05)
            push({'type': 'status', 'msg': '录入正脸'})
            send_packet(make_enroll_5_data(ctx['username'], enroll_direction['middle']), 'enroll middle')
        elif cmd == 'unlock':
            send_packet(make_unlock_data(), 'unlock')
        elif cmd == 'getuser':
            send_packet(make_get_user_data(), 'getuser')
        elif cmd == 'deluser':
            uid = int(d.get('uid', 0))
            send_packet(make_deluser_data(uid), 'deluser')
        elif cmd == 'delall':
            send_packet(make_cmd_with_no_data(no_data_cmd['delalluser']), 'delall')
        elif cmd == 'reset':
            send_packet(make_cmd_with_no_data(no_data_cmd['reset']), 'reset')
        elif cmd == 'version':
            send_packet(make_cmd_with_no_data(no_data_cmd['version']), 'version')
        elif cmd == 'sn':
            send_packet(make_cmd_with_no_data(no_data_cmd['sn']), 'sn')
        elif cmd == 'uvcread':
            send_packet(make_read_uvc_data(), 'uvc read')
        elif cmd == 'uvcset':
            send_packet(make_set_uvc_data(int(d.get('usbType', 0x20)),
                                          int(d.get('rotate180', 0)),
                                          int(d.get('mirror', 0)),
                                          int(d.get('jpegQ', 80))), 'uvc set')
        elif cmd == 'status':
            send_packet(make_cmd_with_no_data(no_data_cmd['getstatus']), 'status')
        else:
            return jsonify({'ok': False, 'msg': '未知命令'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)})
    return jsonify({'ok': True})


@app.route('/api/video/start', methods=['POST'])
def api_video_start():
    if not HAVE_CV2:
        return jsonify({'ok': False, 'msg': 'opencv 未安装: pip install opencv-python-headless',
                        'device': None, 'err': 'opencv 未安装', 'busy': False, 'running': False})
    d = request.get_json(force=True, silent=True) or {}
    device = d.get('device', '/dev/video0')
    start_video(device, int(d.get('coordMax', 100)), auto=bool(d.get('auto', True)))
    busy = ('busy' in video_err.lower()) or ('占用' in video_err) or ('卡死' in video_err)
    return jsonify({'ok': video_cap is not None, 'device': video_device,
                    'err': video_err, 'busy': busy, 'running': video_running})


@app.route('/api/video/devices')
def api_video_devices():
    return jsonify({'ok': True, 'devices': scan_video_devices() if HAVE_CV2 else []})


@app.route('/api/video/stop', methods=['POST'])
def api_video_stop():
    global video_running
    video_running = False
    return jsonify({'ok': True})


@app.route('/api/video')
def api_video():
    if not HAVE_CV2:
        return Response('opencv 未安装', status=500)
    def gen():
        while True:
            with frame_lock:
                f = latest_frame.copy() if latest_frame is not None else None
            if f is None:
                f = make_placeholder_frame()
                if f is None:
                    f = np.zeros((240, 320, 3), np.uint8)
            _, buf = cv2.imencode('.jpg', f)
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
            time.sleep(1/15)
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/api/stream')
def api_stream():
    def gen():
        while True:
            try:
                ev = event_q.get(timeout=1)
            except queue.Empty:
                yield ': keepalive\n\n'
                continue
            yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/status')
def api_status():
    return jsonify({'have_cv2': HAVE_CV2, 'video_running': video_running,
                    'video_device': video_device, 'video_err': video_err,
                    'coord_max': coord_max,
                    'ser_connected': ser is not None,
                    'ser_port': (ser.port if ser is not None else None)})


@app.route('/api/protolog')
def api_protolog():
    try:
        with open(PROTO_LOG, 'r', encoding='utf-8') as f:
            lines = f.readlines()[-500:]
        return Response(''.join(lines), mimetype='text/plain; charset=utf-8')
    except Exception as e:
        return Response('暂无协议日志: ' + str(e), mimetype='text/plain; charset=utf-8')


if __name__ == '__main__':
    print('FM225 可视化调试平台 → http://127.0.0.1:5000')
    if serial is None:
        print('[警告] pyserial 未安装, 串口功能不可用。')
    if not HAVE_CV2:
        print('[提示] opencv 未安装, 视频预览不可用。pip install opencv-python-headless')
    # 0.0.0.0: 允许同网段其他设备访问 (如 http://192.168.150.139:5000)
    app.run(host='0.0.0.0', port=5000, threaded=True)
