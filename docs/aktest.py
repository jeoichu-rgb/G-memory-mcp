"""
AfterKiss AK-G2 完整初始化+控制测试脚本
模拟怪兽派对app的完整init流程，然后发电机命令
"""
import asyncio
from bleak import BleakClient, BleakScanner

DEVICE_ADDR = "77:03:A2:10:46:05"
CHAR_9001 = "00009001-0000-1000-8000-00805f9b34fb"
CHAR_9002 = "00009002-0000-1000-8000-00805f9b34fb"

def make_frame(cmd: int, data: list[int] = [], direction: int = 1) -> bytes:
    length_byte = (direction << 7) | (len(data) << 3)
    return bytes([cmd, length_byte] + data)

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

ALL_READ_CMDS = {
    "device_id": 0x02,
    "firmware": 0x04,
    "mac": 0x06,
    "serial": 0x08,
    "variant_id": 0x0a,
    "group_product": 0x0e,
    "group_variant": 0x10,
    "hardware_id": 0x12,
    "travel_lock": 0x20,
    "heating_cfg": 0x22,
    "light": 0x24,
    "heating_status": 0x26,
    "motor_level": 0x2e,
}

received_notifications = []

def on_notify(name):
    def handler(sender, data):
        parsed = parse_frame(data)
        received_notifications.append((name, parsed))
        print(f"  <- [{name}] {parsed}")
    return handler

async def main():
    print("=== AfterKiss AK-G2 Full Init Test ===\n")

    # --- 扫描 ---
    print("[1] Scanning...")
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

    print(f"    Found: {device.name} ({device.address})\n")

    async with BleakClient(device) as client:
        print(f"[2] Connected: {client.is_connected}")

        # --- 服务发现（bleak自动完成） ---
        print("[3] Services discovered:")
        for service in client.services:
            print(f"    Service: {service.uuid}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"      {char.uuid} [{props}]")
        print()

        # --- 模拟 app init: delay 50ms ---
        await asyncio.sleep(0.05)

        # --- MTU协商 ---
        try:
            mtu = client.mtu_size
            print(f"[4] MTU: {mtu}")
        except Exception as e:
            print(f"[4] MTU query failed: {e}")

        # --- 模拟 app init: delay 100ms ---
        await asyncio.sleep(0.1)

        # --- 订阅通知（模拟 initCharacteristics） ---
        print("[5] Subscribing to notifications...")
        for uuid, name in [(CHAR_9001, "9001"), (CHAR_9002, "9002")]:
            try:
                await client.start_notify(uuid, on_notify(name))
                print(f"    Subscribed to {name} OK")
            except Exception as e:
                print(f"    Can't subscribe {name}: {e}")
        print()

        # --- 等待设备推送（电量/心跳） ---
        print("[6] Waiting 3s for device notifications...")
        await asyncio.sleep(3)
        print(f"    Received {len(received_notifications)} notifications\n")

        # --- 全量读设备信息（模拟app post-init行为） ---
        print("[7] Reading all device info (9001, request-response)...")
        for name, cmd_val in ALL_READ_CMDS.items():
            frame = make_frame(cmd_val, [], direction=0)
            print(f"    -> read {name}: {frame.hex()}")
            try:
                await client.write_gatt_char(CHAR_9001, frame, response=True)
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"       FAIL: {e}")
        print()

        # --- 额外等待，让设备处理完所有读请求 ---
        print("[8] Waiting 2s for all responses...")
        await asyncio.sleep(2)
        print()

        # --- 电机测试 ---
        print("=" * 50)
        print("[9] MOTOR TEST on 9002 (writeWithoutResponse)")
        print("=" * 50)

        # 伸缩电机 level=50
        frame = make_frame(0xA0, [50])
        print(f"\n    -> Motor (thrust) level=50: {frame.hex()}")
        try:
            await client.write_gatt_char(CHAR_9002, frame, response=False)
            print("       Write OK — check device for physical response!")
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(3)

        # 伸缩电机 level=100
        frame = make_frame(0xA0, [100])
        print(f"\n    -> Motor (thrust) level=100: {frame.hex()}")
        try:
            await client.write_gatt_char(CHAR_9002, frame, response=False)
            print("       Write OK — check device!")
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(3)

        # 停止电机
        frame = make_frame(0xA0, [0])
        print(f"\n    -> Motor STOP: {frame.hex()}")
        try:
            await client.write_gatt_char(CHAR_9002, frame, response=False)
            print("       Write OK")
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(1)

        # --- 震动测试 ---
        print(f"\n    -> Vibrate level=80: ", end="")
        frame = make_frame(0xA3, [80])
        print(frame.hex())
        try:
            await client.write_gatt_char(CHAR_9002, frame, response=False)
            print("       Write OK — check device!")
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(3)

        frame = make_frame(0xA3, [0])
        print(f"    -> Vibrate STOP: {frame.hex()}")
        try:
            await client.write_gatt_char(CHAR_9002, frame, response=False)
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(1)

        # --- 吮吸测试（9001） ---
        print(f"\n    -> Suction level=50 (9001): ", end="")
        frame = make_frame(0x2E, [50])
        print(frame.hex())
        try:
            await client.write_gatt_char(CHAR_9001, frame, response=True)
            print("       Write OK — check device!")
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(3)

        frame = make_frame(0x2E, [0])
        print(f"    -> Suction STOP: {frame.hex()}")
        try:
            await client.write_gatt_char(CHAR_9001, frame, response=True)
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(1)

        # --- 加热测试（9001） ---
        print(f"\n    -> Heating ON (9001): ", end="")
        frame = make_frame(0x26, [1])
        print(frame.hex())
        try:
            await client.write_gatt_char(CHAR_9001, frame, response=True)
            print("       Write OK — wait 30s and check if device warms up")
        except Exception as e:
            print(f"       FAIL: {e}")
        await asyncio.sleep(5)

        frame = make_frame(0x26, [0])
        print(f"    -> Heating OFF: {frame.hex()}")
        try:
            await client.write_gatt_char(CHAR_9001, frame, response=True)
        except Exception as e:
            print(f"       FAIL: {e}")

        # --- 总结 ---
        print(f"\n{'=' * 50}")
        print(f"Total notifications received: {len(received_notifications)}")
        for name, parsed in received_notifications:
            print(f"  [{name}] {parsed}")
        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
