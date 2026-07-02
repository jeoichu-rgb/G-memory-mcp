"""
ak_bridge.py — AfterKiss AK-G2 BLE Bridge
端口：8768（frpc → VPS:7004）

三通道控制（全部通过 9002 cmd 0xA0）：
  byte[3] = thrust   伸缩/抽插 0-100
  byte[4] = suction  吮吸      0-100
  byte[5] = vibrate  震动      0-100

帧格式：[0xA0, 0xA0, 0x03, thrust, suction, vibrate]
写入方式：writeWithoutResponse（ATT Write Command）

PatternStep per-channel mode/curve：
- mode_thrust / mode_suction / mode_vibrate：各自独立选 "ramp"（默认）或 "step"
- curve_*：各自独立选曲线，仅 ramp 时生效
  "linear"（默认）/ "ease_in" / "ease_out" / "ease_in_out"
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from bleak import BleakClient, BleakScanner
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ─── 配置 ───────────────────────────────────────────────
DEVICE_ADDR    = "77:03:A2:10:46:05"
DEVICE_NAME    = "afterkiss"
CHAR_9001      = "00009001-0000-1000-8000-00805f9b34fb"
CHAR_9002      = "00009002-0000-1000-8000-00805f9b34fb"
HOST           = "0.0.0.0"
PORT           = 8768

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ak_bridge")

# ─── 全局状态 ────────────────────────────────────────────
ble_client: Optional[BleakClient] = None
ble_lock   = asyncio.Lock()
is_playing = False
battery_level: Optional[int] = None


# ─── 协议帧构造 ─────────────────────────────────────────
def make_motor_frame(thrust: int, suction: int, vibrate: int) -> bytes:
    thrust  = max(0, min(100, thrust))
    suction = max(0, min(100, suction))
    vibrate = max(0, min(100, vibrate))
    return bytes([0xA0, 0xA0, 0x03, thrust, suction, vibrate])


def parse_notify(data: bytes) -> dict:
    if len(data) < 2:
        return {"raw": data.hex()}
    cmd    = data[0]
    action = data[1] >> 7
    length = (data[1] & 0x78) >> 3
    payload = data[2:2+length] if len(data) >= 2+length else data[2:]
    return {"cmd": cmd, "action": action, "length": length, "payload": payload}


# ─── BLE 连接管理 ────────────────────────────────────────
async def connect_device():
    global ble_client, battery_level
    log.info("扫描设备...")

    device = await BleakScanner.find_device_by_address(DEVICE_ADDR, timeout=10)
    if not device:
        devices = await BleakScanner.discover(timeout=5)
        for d in devices:
            if d.name and DEVICE_NAME in d.name.lower():
                device = d
                break
    if not device:
        raise RuntimeError(f"未找到设备 {DEVICE_ADDR}，请确认设备已开机")

    log.info(f"找到设备: {device.name or '(无名)'} [{device.address}]")
    client = BleakClient(device)
    await client.connect()
    if not client.is_connected:
        raise RuntimeError("连接失败")
    log.info("连接成功")

    def on_9001_notify(_sender, data: bytes):
        global battery_level
        parsed = parse_notify(data)
        if parsed.get("cmd") == 0x14 and parsed.get("payload"):
            battery_level = parsed["payload"][0]
            log.info(f"电量: {battery_level}%")
        elif parsed.get("cmd") == 0x15:
            pass  # 心跳，静默
        else:
            log.info(f"9001 通知: {data.hex()}")

    await client.start_notify(CHAR_9001, on_9001_notify)
    log.info("已订阅 9001 通知，设备就绪")

    ble_client = client


async def ensure_connected():
    global ble_client
    if ble_client is None or not ble_client.is_connected:
        log.info("重新连接设备...")
        await connect_device()


# ─── 写入函数 ────────────────────────────────────────────
async def write_motors(thrust: int, suction: int, vibrate: int):
    frame = make_motor_frame(thrust, suction, vibrate)
    await ble_client.write_gatt_char(CHAR_9002, frame, response=False)


async def zero_motors():
    try:
        await write_motors(0, 0, 0)
    except Exception as e:
        log.warning(f"归零时出错: {e}")


# ─── 曲线函数 ────────────────────────────────────────────
def _apply_curve(t: float, curve: str) -> float:
    if curve == "ease_in":
        return t * t
    elif curve == "ease_out":
        return 1.0 - (1.0 - t) * (1.0 - t)
    elif curve == "ease_in_out":
        return t * t * (3.0 - 2.0 * t)
    else:
        return t  # linear


def _interp(current: int, target: int, raw: float, mode: str, curve: str) -> int:
    if mode == "step":
        return current
    frac = _apply_curve(raw, curve)
    return int(current + (target - current) * frac)


# ─── Lifespan ────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_device()
    yield
    log.info("关闭中，归零设备...")
    await zero_motors()
    if ble_client:
        await ble_client.disconnect()


app = FastAPI(title="AKBridge", lifespan=lifespan)


# ─── 请求模型 ────────────────────────────────────────────
class PatternStep(BaseModel):
    t:              float
    thrust:         int = 0
    suction:        int = 0
    vibrate:        int = 0
    mode_thrust:    str = "ramp"
    curve_thrust:   str = "linear"
    mode_suction:   str = "ramp"
    curve_suction:  str = "linear"
    mode_vibrate:   str = "ramp"
    curve_vibrate:  str = "linear"

class PlayRequest(BaseModel):
    thrust:   int = 0
    suction:  int = 0
    vibrate:  int = 0
    duration: float = 5.0
    pattern:  Optional[list[PatternStep]] = None


# ─── 播放逻辑 ────────────────────────────────────────────
async def execute_play(req: PlayRequest):
    global is_playing

    await ensure_connected()

    try:
        if req.pattern:
            steps = sorted(req.pattern, key=lambda x: x.t)
            start = asyncio.get_event_loop().time()

            for i, step in enumerate(steps):
                now  = asyncio.get_event_loop().time() - start
                wait = step.t - now
                if wait > 0:
                    await asyncio.sleep(wait)

                if i + 1 < len(steps):
                    next_step    = steps[i + 1]
                    seg_duration = next_step.t - step.t

                    if seg_duration > 0:
                        needs_ramp = (
                            step.mode_thrust  == "ramp" or
                            step.mode_suction == "ramp" or
                            step.mode_vibrate == "ramp"
                        )

                        if not needs_ramp:
                            await write_motors(step.thrust, step.suction, step.vibrate)
                            await asyncio.sleep(seg_duration)
                        else:
                            ticks = max(1, int(seg_duration / 0.2))
                            for tick in range(ticks):
                                raw = tick / ticks
                                th = _interp(step.thrust,  next_step.thrust,  raw, step.mode_thrust,  step.curve_thrust)
                                su = _interp(step.suction, next_step.suction, raw, step.mode_suction, step.curve_suction)
                                vi = _interp(step.vibrate, next_step.vibrate, raw, step.mode_vibrate, step.curve_vibrate)
                                await write_motors(th, su, vi)
                                await asyncio.sleep(seg_duration / ticks)
                    else:
                        await write_motors(step.thrust, step.suction, step.vibrate)

                else:
                    elapsed = asyncio.get_event_loop().time() - start
                    remain  = req.duration - elapsed
                    while remain > 0:
                        await write_motors(step.thrust, step.suction, step.vibrate)
                        wait = min(2.0, remain)
                        await asyncio.sleep(wait)
                        remain -= wait

        else:
            elapsed  = 0.0
            interval = 2.0
            while elapsed < req.duration:
                await write_motors(req.thrust, req.suction, req.vibrate)
                wait     = min(interval, req.duration - elapsed)
                await asyncio.sleep(wait)
                elapsed += wait

    finally:
        await zero_motors()
        is_playing = False


# ─── HTTP 接口 ───────────────────────────────────────────
@app.get("/status")
async def status():
    connected = ble_client is not None and ble_client.is_connected
    return {
        "connected": connected,
        "playing": is_playing,
        "device": DEVICE_ADDR,
        "battery": battery_level,
    }


@app.post("/play")
async def play(req: PlayRequest):
    global is_playing

    if is_playing:
        raise HTTPException(status_code=409, detail="设备正在播放中，请等待结束")

    async with ble_lock:
        is_playing = True
        try:
            await execute_play(req)
        except Exception as e:
            is_playing = False
            log.error(f"播放出错: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return {
        "status": "done",
        "thrust": req.thrust, "suction": req.suction, "vibrate": req.vibrate,
        "duration": req.duration,
    }


@app.post("/stop")
async def stop():
    global is_playing
    async with ble_lock:
        await zero_motors()
        is_playing = False
    return {"status": "stopped"}


# ─── 入口 ────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
