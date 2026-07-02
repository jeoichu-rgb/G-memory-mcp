"""
AfterKiss AK-G2 控制脚本（HCI 抓包验证版）
2026-07-02: 通过 Android HCI snoop log 确认官方 APP 的实际数据格式
"""
import asyncio
from bleak import BleakClient, BleakScanner

DEVICE_ADDR = "77:03:A2:10:46:05"
CHAR_9001 = "00009001-0000-1000-8000-00805f9b34fb"
CHAR_9002 = "00009002-0000-1000-8000-00805f9b34fb"

def make_frame(cmd: int, data: list[int] = [], direction: int = 1) -> bytes:
    length_byte = (direction << 7) | (len(data) << 3)
    return bytes([cmd, length_byte] + data)

def cmd_motors(thrust: int = 0, motor2: int = 0, vibrate: int = 0) -> bytes:
    """9002 多电机统一控制（HCI 确认格式），等级 0-100"""
    return make_frame(0xA0, [0x03, thrust, motor2, vibrate])

def cmd_thrust(level: int) -> bytes:
    return cmd_motors(thrust=level)

def cmd_vibrate(level: int) -> bytes:
    return cmd_motors(vibrate=level)

def cmd_power_off() -> bytes:
    return make_frame(0xA2, [])

def cmd_read(cmd: int) -> bytes:
    return make_frame(cmd, [], direction=0)

def cmd_suction(level: int) -> bytes:
    return make_frame(0x2e, [level], direction=1)

def cmd_heating(on: bool) -> bytes:
    return make_frame(0x26, [0x01 if on else 0x00], direction=1)

def parse_frame(data: bytes) -> dict:
    if len(data) < 2:
        return {"raw": data.hex()}
    cmd = data[0]
    action = data[1] >> 7
    length = (data[1] & 0x78) >> 3
    payload = data[2:2+length] if len(data) >= 2+length else data[2:]
    return {
        "cmd": f"0x{cmd:02x}",
        "action": "push" if action == 1 else "response",
        "length": length,
        "payload": payload.hex() if payload else "",
        "raw": data.hex(),
    }

async def main():
    print("Scanning...")
    device = await BleakScanner.find_device_by_address(DEVICE_ADDR, timeout=10)
    if not device:
        devices = await BleakScanner.discover(timeout=5)
        for d in devices:
            if d.name and "afterkiss" in d.name.lower():
                device = d
                break
    if not device:
        print("Device not found!")
        return

    print(f"Found: {device.name} ({device.address})")

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")

        def on_notify(name):
            def handler(sender, data):
                parsed = parse_frame(data)
                print(f"  <- [{name}] {parsed}")
            return handler

        await client.start_notify(CHAR_9001, on_notify("9001"))
        await client.start_notify(CHAR_9002, on_notify("9002"))
        print("Subscribed to notifications")

        await asyncio.sleep(3)

        # ============ 伸缩（HCI 确认格式） ============
        print("\n=== Thrust (9002, cmd 0xA0, 4-byte data) ===")
        for level in [30, 70, 0]:
            frame = cmd_thrust(level)
            print(f"  -> {frame.hex()} (thrust={level})")
            await client.write_gatt_char(CHAR_9002, frame, response=False)
            await asyncio.sleep(3)

        # ============ 震动 ============
        print("\n=== Vibrate (9002, cmd 0xA0, byte[5]) ===")
        for level in [30, 60, 0]:
            frame = cmd_vibrate(level)
            print(f"  -> {frame.hex()} (vibrate={level})")
            await client.write_gatt_char(CHAR_9002, frame, response=False)
            await asyncio.sleep(3)

        # ============ 吮吸（9001，待 HCI 验证） ============
        print("\n=== Suction (9001, cmd 0x2E, 待验证) ===")
        for level in [5, 0]:
            frame = cmd_suction(level)
            print(f"  -> {frame.hex()} (suction={level})")
            try:
                await client.write_gatt_char(CHAR_9001, frame, response=True)
            except Exception as e:
                print(f"     FAIL: {e}")
            await asyncio.sleep(3)

        print("\nDone.")

if __name__ == "__main__":
    asyncio.run(main())
