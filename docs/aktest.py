"""
AfterKiss AK-G2 方向位对比测试
核心假设：direction=0 用于所有 host→device 帧（包括写），
direction=1 仅用于 device→host 推送通知
"""
import asyncio
from bleak import BleakClient, BleakScanner

DEVICE_ADDR = "77:03:A2:10:46:05"
CHAR_9001 = "00009001-0000-1000-8000-00805f9b34fb"
CHAR_9002 = "00009002-0000-1000-8000-00805f9b34fb"

def make_frame(cmd: int, data: list[int] = [], direction: int = 0) -> bytes:
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

received = []

def on_notify(name):
    def handler(sender, data):
        parsed = parse_frame(data)
        received.append((name, parsed))
        if parsed.get("action") != "push":
            print(f"  <- [{name}] {parsed}")
    return handler

async def main():
    print("=== AfterKiss 方向位对比测试 ===\n")

    device = await BleakScanner.find_device_by_address(DEVICE_ADDR, timeout=10)
    if not device:
        devices = await BleakScanner.discover(timeout=5)
        for d in devices:
            if d.name and "afterkiss" in d.name.lower():
                device = d
                break
    if not device:
        print("设备未找到!")
        return

    print(f"找到: {device.name} ({device.address})\n")

    async with BleakClient(device) as client:
        print(f"已连接: {client.is_connected}\n")

        await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        await client.start_notify(CHAR_9001, on_notify("9001"))
        await client.start_notify(CHAR_9002, on_notify("9002"))
        print("已订阅 9001 + 9002 通知\n")

        await asyncio.sleep(2)

        # ========================================
        # 测试 1: 9002 伸缩电机 direction=0 (新假设)
        # ========================================
        print("=" * 60)
        print("测试 1: 伸缩电机 direction=0 (cmd=0xA0, 9002)")
        print("=" * 60)

        frame = make_frame(0xA0, [50], direction=0)
        print(f"  -> 发送: {frame.hex()} (level=50, direction=0)")
        print(f"     注意：这和之前的 a08832 (direction=1) 不同！")
        print(f"     之前: a08832  现在: {frame.hex()}")
        await client.write_gatt_char(CHAR_9002, frame, response=False)
        print("     写入成功 — 检查设备是否有物理反应！")
        await asyncio.sleep(5)

        frame = make_frame(0xA0, [0], direction=0)
        print(f"  -> 停止: {frame.hex()}")
        await client.write_gatt_char(CHAR_9002, frame, response=False)
        await asyncio.sleep(2)

        # ========================================
        # 测试 2: 9002 震动 direction=0
        # ========================================
        print()
        print("=" * 60)
        print("测试 2: 震动 direction=0 (cmd=0xA3, 9002)")
        print("=" * 60)

        frame = make_frame(0xA3, [50], direction=0)
        print(f"  -> 发送: {frame.hex()} (level=50, direction=0)")
        await client.write_gatt_char(CHAR_9002, frame, response=False)
        print("     写入成功 — 检查设备是否震动！")
        await asyncio.sleep(5)

        frame = make_frame(0xA3, [0], direction=0)
        print(f"  -> 停止: {frame.hex()}")
        await client.write_gatt_char(CHAR_9002, frame, response=False)
        await asyncio.sleep(2)

        # ========================================
        # 测试 3: 9001 吮吸 direction=0
        # ========================================
        print()
        print("=" * 60)
        print("测试 3: 吮吸 direction=0 (cmd=0x2E, 9001)")
        print("=" * 60)

        frame = make_frame(0x2E, [50], direction=0)
        print(f"  -> 发送: {frame.hex()} (level=50, direction=0)")
        print(f"     注意：之前发的是 2e8832 (direction=1)")
        print(f"     现在发的是: {frame.hex()}")
        await client.write_gatt_char(CHAR_9001, frame, response=True)
        print("     写入成功 — 检查设备是否吮吸！")
        await asyncio.sleep(5)

        frame = make_frame(0x2E, [0], direction=0)
        print(f"  -> 停止: {frame.hex()}")
        await client.write_gatt_char(CHAR_9001, frame, response=True)
        await asyncio.sleep(2)

        # ========================================
        # 测试 4: 9001 加热 direction=0
        # ========================================
        print()
        print("=" * 60)
        print("测试 4: 加热 direction=0 (cmd=0x26, 9001)")
        print("=" * 60)

        frame = make_frame(0x26, [1], direction=0)
        print(f"  -> 发送: {frame.hex()} (加热开, direction=0)")
        print(f"     注意：之前发的是 268801 (direction=1)")
        print(f"     现在发的是: {frame.hex()}")
        await client.write_gatt_char(CHAR_9001, frame, response=True)
        print("     写入成功 — 等几秒看设备是否发热！")
        await asyncio.sleep(5)

        frame = make_frame(0x26, [0], direction=0)
        print(f"  -> 停止: {frame.hex()}")
        await client.write_gatt_char(CHAR_9001, frame, response=True)
        await asyncio.sleep(1)

        print(f"\n{'=' * 60}")
        print("对比总结:")
        print(f"  旧方式 (direction=1): a08832, a38832, 2e8832, 268801")
        print(f"  新方式 (direction=0): a00832, a30832, 2e0832, 260801")
        print(f"  差异只在 byte[1] 的 bit7: 0x88→0x08 (有数据时)")
        print(f"\n总通知数: {len(received)}")
        non_push = [(n, p) for n, p in received if p.get("action") != "push"]
        if non_push:
            print("非推送通知:")
            for name, parsed in non_push:
                print(f"  [{name}] {parsed}")
        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
