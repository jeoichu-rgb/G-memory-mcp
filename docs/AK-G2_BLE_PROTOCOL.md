# AK-G2 (AfterKiss) BLE 协议逆向文档

## 设备信息

- 品牌：思蜜科技 (Sistalk) / 怪兽派对
- 型号：AK-G2 (AfterKiss)
- BLE 名称：afterkiss
- BLE 地址：77:03:A2:10:46:05
- 功能：伸缩（抽插）、震动、吮吸、加热、压力感应

## 逆向方法

1. 从 APK 用 **jadx** 反编译 Java/.dex 层，找到 MethodChannel 桥接和 CRC 封包逻辑
2. 从 APK 的 `lib/arm64-v8a/libapp.so`（Flutter Dart AOT 编译产物）用 **blutter** 反编译，得到 ARM64 汇编 + Dart 符号
3. 手动分析 blutter 输出的 ARM64 汇编，还原 Dart 源码逻辑
4. 用 **bleak**（Python BLE 库）连接设备测试

## 协议版本判定

APK 中 `mix_device` 包有三个协议版本：V1、V2、V3。

### 排除 V1 的证据

V1 (`ble_device_v1.dart`, 10373 行) 的特征：
- 认证方式：读取 "8001" 特征值，XOR 解密后写回（`_decrypt` 方法，行 2251-2617）
- 电机控制：写入 "600a"（`_multiMotor`）、"6003"（`_dualMotor`）等特征值
- 旧产品判断：`_isOldProduct()` = `!containsKey("9001")`

**AK-G2 排除 V1 的原因：**
- AK-G2 **没有** "8001" 特征值 → `_decrypt` 直接跳过
- AK-G2 **没有** "600a"、"6003" 特征值 → `_multiMotor` 和 `_dualMotor` 会触发 `NullCastError` 崩溃
- AK-G2 **有** "9001" → 不是旧产品
- V1 的 `init()` 流程中根本没有调用 `_decrypt`

### 排除 V3 的证据

V3 期望 36 字节的 challenge 数据，AK-G2 在 "9001" 上发送的数据是 6 字节。格式不匹配。

### 确认 V2 的证据

V2 (`ble_device_v2.dart`, 13694 行) 的字符串常量完美匹配 AK-G2：
- `"init op character(9001)"` — "9001" 是 op 特征值
- `"init function character(9002)"` — "9002" 是 function 特征值
- `"failed to set notify value for op characteristic(9001)"`
- `"failed to set notify value for function characteristic(9002)"`
- `"Function characteristic is null"` — 在 `_handleFunction` 的匿名闭包中

AK-G2 的 GATT 服务恰好有 "9001" 和 "9002"，与 V2 完全吻合。

## AK-G2 的 GATT 特征值

已知的特征值（从 nRF Connect 截图和 bleak 扫描）：
- Service 9000 → **9001**（op 操作通道）、**9002**（function 功能通道）
- Service FFF0 → FFF1
- 没有 "8001"、"600a"、"6003" 等 V1 特征值

完整 UUID 格式：`0000XXXX-0000-1000-8000-00805f9b34fb`（标准 BLE 16-bit UUID 扩展）

## 初始化流程（无认证）

来源：`BLEDeviceV2::init()`（行 597-930，地址 0x1691074）

```
1. discoverServices()                    // 发现 GATT 服务
2. await Future.delayed(50ms)            // 等待 50ms
3. await _initMtu()                      // 协商 MTU
4. await Future.delayed(100ms)           // 等待 100ms
5. field_8b = services["9002"]           // 存储 function 特征值
6. field_87 = services["9001"]           // 存储 op 特征值
7. await initCharacteristics()           // 订阅通知等
8. callback()                            // 完成
```

**关键发现：整个 init 流程没有任何认证/握手/challenge-response 步骤。**

证据：init 函数从地址 0x1691074 到 0x1691428，完整反汇编中没有出现任何加密、MethodChannel 调用、challenge 处理逻辑。只有 discoverServices → delay → initMtu → delay → 存特征值引用 → initCharacteristics → 回调。

## 统一帧格式（两个通道共用）

### 帧结构

```
byte[0] = cmd                                    // 命令字节
byte[1] = (direction << 7) | (data_length << 3)  // 方向位 + 编码长度
byte[2..] = data                                 // 数据字节
```

**无 CRC，无加密，无认证。** 直接写入特征值。

### 方向位（bit 7）

| bit 7 | 含义 | 场景 |
|-------|------|------|
| 1 | app→device 写命令 | 发送控制指令 |
| 0 | device→app 读响应/读请求 | 设备回复或 app 请求读取 |

### 长度编码（bits 3-6）

| 数据字节数 | byte[1]（写） | byte[1]（读请求） |
|-----------|--------------|------------------|
| 0 | 0x80 | 0x00 |
| 1 | 0x88 | 0x08 |
| 2 | 0x90 | 0x10 |
| 3 | 0x98 | 0x18 |
| 4 | 0xA0 | 0x20 |

最大数据长度：15 字节（4 bits）。

### 发送端汇编证据（_handleOperate / _handleFunction）

```arm64
// 写入 cmd
0x169e1f0: strb w0, [x3]           // buffer[0] = cmd

// 编码 byte[1]
0x169e20c: lsl  x5, x3, #7         // action << 7
0x169e258: lsl  x0, x1, #3         // data.length << 3
0x169e260: orr  x2, x1, x0         // 合并
0x169e27c: strb w2, [x0, #1]       // buffer[1] = 结果
```

### 接收端汇编证据（onValueReceived）

```arm64
// 最少 2 字节
0x1691c94: cmp  x1, #2             // data.length >= 2?

// 解析 byte[1]
0x1691d10: asr  x0, x1, #7         // action = byte[1] >> 7
0x1691d48: and  w0, w1, #0x78      // mask bits 3-6
0x1691d50: asr  x1, x0, #3         // length = (byte[1] & 0x78) >> 3

// 长度校验
0x1691d84: add  x2, x0, #2         // 期望 = length + 2
0x1691d94: cmp  x3, x2             // 实际 >= 期望?
```

收发完美对称。日志字符串 `"on op received: {data}, cmd = 0x{cmd}, action = {action}, length = {length}"` 进一步印证。

## 9002 (Function) 通道 —— 已确认

### 写入方式

`writeWithoutResponse`（射后不管，无 GATT 级 ACK）。写入 `field_8b`（"9002" 特征值）。

### 命令表

| cmd | 函数名 | 功能 | 数据格式 |
|-----|--------|------|---------|
| 0xA0 | writeMotor | 伸缩/抽插 | `[level]` 或 `[level1, level2]`，0-100 |
| 0xA2 | writePowerOff | 关机 | 空（无数据） |
| 0xA3 | writeExtend | 震动 | `[level]`，0-100（推测） |

### 汇编证据

```arm64
// writeMotor
0x16cfee0: movz x2, #0xa0          // cmd = 0xA0
0x16cfee4: bl   _handleFunction

// writePowerOff
0x169c38c: movz x2, #0xa2          // cmd = 0xA2
0x169c390: bl   _handleFunction

// writeExtend
0x16cd7c8: movz x2, #0xa3          // cmd = 0xA3
0x16cd7cc: bl   _handleFunction
```

### 发送示例

```
伸缩 level=50:   [0xA0, 0x88, 0x32]     // cmd, len=1+方向, 50
伸缩 停止:       [0xA0, 0x88, 0x00]     // cmd, len=1+方向, 0
震动 level=50:   [0xA3, 0x88, 0x32]     // cmd, len=1+方向, 50
关机:            [0xA2, 0x80]           // cmd, len=0+方向
```

## 9001 (Op) 通道 —— 已确认

### 写入方式

`write`（with response，等 GATT 级 ACK）。写入 `field_87`（"9001" 特征值）。通过 `GlobalBLEOperationQueue` 排队执行，request-response 模式。

### 命令表

同一功能的读和写**使用相同的 cmd**，靠 byte[1] 的 bit 7 区分方向。

#### 设备信息（只读）

| cmd | 函数名 | 功能 |
|-----|--------|------|
| 0x02 | readDeviceID | 设备 ID |
| 0x04 | readFirmwareVersion | 固件版本 |
| 0x06 | readMAC | MAC 地址 |
| 0x08 | readSerialNumber | 序列号 |
| 0x0a | readVariantID | 变体 ID |
| 0x0e | readGroupProductID | 组产品 ID |
| 0x10 | readGroupVariantID | 组变体 ID |
| 0x12 | readHardwareID | 硬件 ID |

#### 功能控制（读/写）

| cmd | 功能 | 读函数 | 写函数 |
|-----|------|--------|--------|
| 0x20 | 旅行锁 | readTravelLock | writeTravelLock |
| 0x22 | 加热设置 | readHeatingSetting | writeHeatingSetting |
| 0x24 | 灯光 | readLightSetting | writeLightSetting |
| 0x26 | 加热开关 | readHeatingStatus | writeHeatingStatus |
| 0x2e | 吮吸电机 | readMotorLevel | writeMotorLevel |

#### 特殊

| cmd | 函数名 | 备注 |
|-----|--------|------|
| 0x148→0x48? | writePressureStatus | cmd 超过 1 字节，实际帧中为低 8 位，待验证 |

### 发送格式

```
读请求:  [cmd, 0x00]                           // 方向=0, 长度=0
写命令:  [cmd, 0x80 | (len << 3), data...]     // 方向=1
```

### 发送示例

```
读设备ID:        [0x02, 0x00]              // 读请求
读固件版本:      [0x04, 0x00]              // 读请求
读加热状态:      [0x26, 0x00]              // 读请求
写加热开启:      [0x26, 0x88, 0x01]        // 写命令, 1字节数据
写加热关闭:      [0x26, 0x88, 0x00]        // 写命令, 1字节数据
写吮吸等级50:    [0x2e, 0x88, 0x32]        // 写命令, level=50
读旅行锁状态:    [0x20, 0x00]              // 读请求
```

### 汇编证据（cmd 字节定位规律）

读函数的 cmd 在函数起始地址 +0xFC 处，写函数在 +0x108 处：

```arm64
// readHeatingStatus (addr 0x16a79a0)
0x16a7a9c: movz x0, #0x26      // offset +0xFC → cmd = 0x26

// writeHeatingStatus (addr 0x16a7f14)
0x16a801c: movz x0, #0x26      // offset +0x108 → cmd = 0x26（同一个cmd）
```

## 设备发送的数据（9001 通知）

### 帧格式

与发送帧完全一致：`[cmd, (action<<7)|(length<<3), data...]`

### 设备推送通知（action=1，设备主动发送）

| cmd | 功能 | 数据 | byte[1] |
|-----|------|------|---------|
| 0x13 | 加热状态变更 | 1 字节（data[0]==2 → 加热中） | 0x88 |
| 0x14 | 电量 | 1 字节（电量百分比） | 0x88 |
| 0x15 | 心跳 | 1 字节 | 0x88（代码中跳过日志） |

日志字符串证据：`"v2 battery = {value}"`（cmd 0x14 的处理分支）。

### 读响应（action=0，响应 app 的读请求）

设备收到读请求后，在 9001 上发送通知：
```
[cmd, (0<<7)|(len<<3), response_data...]
```

`onValueReceived` 通过 cmd 匹配挂起的 `OPModel`，调用 `onResultCallback` 回传数据。

### 之前误判的消息

- `0x01, 0x20, XX, XX, XX, XX` — 之前误以为是 auth challenge，实际是设备状态信息（cmd=0x01, action=0, length=4）

### 压力感应

`onValueReceived` 中有 cmd 0x14a (330) 的处理，调用 `MixDeviceSensorModel::fromBytes` 解析传感器数据。

### 按键事件

`onReceivedKeyData` 处理 4 字节按键数据：
- byte[0]：按键类型（0-6，映射到 KeyType 枚举）
- byte[2]：按键值（0-100）
- 创建 `MixDeviceKeyEvent(type, eventType, value)` 分发

## 设备功能与 BLE 命令完整对照

| 功能 | 物理按键 | BLE 函数 | 通道 | 帧格式 | 状态 |
|------|---------|----------|------|--------|------|
| 伸缩（抽插） | A加速 B减速 | writeMotor | 9002 | `[0xA0, 0x88, level]` | ✅ 已确认 |
| 震动（棒子马达） | D | writeExtend | 9002 | `[0xA3, 0x88, level]` | ✅ cmd确认 |
| 关机 | F长按 | writePowerOff | 9002 | `[0xA2, 0x80]` | ✅ cmd确认 |
| 吮吸（机身马达） | D同键 | writeMotorLevel | 9001 | `[0x2e, 0x88, level]` | ✅ cmd确认 |
| 加热开关 | C | writeHeatingStatus | 9001 | `[0x26, 0x88, 0x01/0x00]` | ✅ cmd确认 |
| 加热设置 | — | writeHeatingSetting | 9001 | `[0x22, 0x80+, data...]` | ✅ cmd确认 |
| 灯光 | — | writeLightSetting | 9001 | `[0x24, 0x80+, data...]` | ✅ cmd确认 |
| 旅行锁 | — | writeTravelLock | 9001 | `[0x20, 0x80+, data...]` | ✅ cmd确认 |
| 压力感应 | E | 设备推送 | 9001 通知 | cmd 0x14a | 来源确认 |
| 电量 | — | 设备推送 | 9001 通知 | `[0x14, 0x88, level]` | ✅ 已确认 |
| 心跳 | — | 设备推送 | 9001 通知 | `[0x15, 0x88, XX]` | ✅ 已确认 |

## Java 层 CRC 代码（V2 不使用）

APK 的 Java 层存在 MethodChannel `"com.sistalk.mp/bluetooth_plugin"`，提供 `genData` 和 `decrypt` 方法。

**经汇编分析确认：V2 的 `_handleOperate` 和 `_handleFunction` 中均无任何 MethodChannel 调用。** grep 搜索 `invokeMethod`、`MethodChannel`、`genData`、`bluetooth_plugin` 在整个 `ble_device_v2.dart`（13694 行）中零命中。

CRC 代码可能用于 V1 或 V3，与 AK-G2 无关。

### genData（参考，V2 不用）

```java
public static byte[] a(byte cmd, byte[] data) {
    byte[] frame = new byte[data.length + 4];
    frame[0] = cmd;
    frame[1] = (byte) data.length;
    System.arraycopy(data, 0, frame, 2, data.length);
    byte[] crc = b(frame);  // CRC-16 CCITT
    frame[frame.length - 2] = crc[0];
    frame[frame.length - 1] = crc[1];
    return frame;
}
```

## 之前的失败尝试和原因

在 `D:\Eric\afterkiss_iv.py` 中进行了 74+ 次尝试，全部返回 `01 08 01`。

失败原因（全部踩中）：
1. **写错特征值**：往 "9001"（op）写电机命令，应该走 "9002"（function）
2. **帧格式错误**：发送 AES 加密数据和裸字节，设备期望 `[cmd, encoded_len, data...]`
3. **误判为 CRC 失败**：以为 9001 需要 CRC 封包，实际 V2 两个通道都不用 CRC
4. **误判 challenge**：以为 `01 20 XX XX XX XX` 是 challenge 需要回应，实际是设备状态消息（cmd=0x01, action=0, length=4）

## 测试脚本

```python
import asyncio
from bleak import BleakClient, BleakScanner

DEVICE_ADDR = "77:03:A2:10:46:05"
CHAR_9001 = "00009001-0000-1000-8000-00805f9b34fb"
CHAR_9002 = "00009002-0000-1000-8000-00805f9b34fb"

def make_frame(cmd: int, data: list[int] = [], direction: int = 1) -> bytes:
    """统一帧格式，两个通道共用"""
    length_byte = (direction << 7) | (len(data) << 3)
    return bytes([cmd, length_byte] + data)

# ===== 9002 快捷函数（fire-and-forget） =====

def cmd_motor(level: int) -> bytes:
    """伸缩/抽插电机 (0-100)"""
    return make_frame(0xA0, [level])

def cmd_vibrate(level: int) -> bytes:
    """震动 (0-100)"""
    return make_frame(0xA3, [level])

def cmd_power_off() -> bytes:
    """关机"""
    return make_frame(0xA2, [])

# ===== 9001 快捷函数（request-response） =====

def cmd_read(cmd: int) -> bytes:
    """9001 读请求"""
    return make_frame(cmd, [], direction=0)

def cmd_write(cmd: int, data: list[int]) -> bytes:
    """9001 写命令"""
    return make_frame(cmd, data, direction=1)

def cmd_suction(level: int) -> bytes:
    """吮吸电机 (0-100)"""
    return cmd_write(0x2e, [level])

def cmd_heating(on: bool) -> bytes:
    """加热开关"""
    return cmd_write(0x26, [0x01 if on else 0x00])

# ===== 9001 读命令 =====
READ_CMDS = {
    "device_id":     0x02,
    "firmware":      0x04,
    "mac":           0x06,
    "serial":        0x08,
    "variant_id":    0x0a,
    "group_product": 0x0e,
    "group_variant": 0x10,
    "hardware_id":   0x12,
    "travel_lock":   0x20,
    "heating_cfg":   0x22,
    "light":         0x24,
    "heating":       0x26,
    "motor_level":   0x2e,
}

def parse_frame(data: bytes) -> dict:
    """解析设备发回的帧"""
    if len(data) < 2:
        return {"error": "too short", "raw": data.hex()}
    cmd = data[0]
    action = data[1] >> 7
    length = (data[1] & 0x78) >> 3
    payload = data[2:2+length] if len(data) >= 2+length else data[2:]
    return {
        "cmd": f"0x{cmd:02x}",
        "action": action,
        "direction": "push" if action == 1 else "response",
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

        for service in client.services:
            print(f"\nService: {service.uuid}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  {char.uuid} [{props}]")

        # 订阅通知
        def on_notify(name):
            def handler(sender, data):
                parsed = parse_frame(data)
                print(f"  <- [{name}] {parsed}")
            return handler

        for uuid, name in [(CHAR_9001, "9001"), (CHAR_9002, "9002")]:
            try:
                await client.start_notify(uuid, on_notify(name))
                print(f"Subscribed to {name}")
            except Exception as e:
                print(f"Can't subscribe {name}: {e}")

        print("\nWaiting 3s for device messages...")
        await asyncio.sleep(3)

        # ============ 测试 1：读取设备信息（9001） ============
        print("\n=== Test: Read device info (9001) ===")
        for name, cmd_val in [("device_id", 0x02), ("firmware", 0x04), ("heating", 0x26)]:
            frame = cmd_read(cmd_val)
            print(f"  -> [9001] {frame.hex()} (read {name})")
            try:
                await client.write_gatt_char(CHAR_9001, frame, response=True)
                print("     OK")
            except Exception as e:
                print(f"     FAIL: {e}")
            await asyncio.sleep(1)

        # ============ 测试 2：伸缩电机（9002, cmd 0xA0） ============
        print("\n=== Test: Motor on 9002 (cmd 0xA0) ===")
        for level in [20, 50, 0]:
            frame = cmd_motor(level)
            print(f"  -> [9002] {frame.hex()} (level={level})")
            try:
                await client.write_gatt_char(CHAR_9002, frame, response=False)
                print("     OK")
            except Exception as e:
                print(f"     FAIL: {e}")
            await asyncio.sleep(3)

        # ============ 测试 3：震动（9002, cmd 0xA3） ============
        print("\n=== Test: Vibrate on 9002 (cmd 0xA3) ===")
        for level in [30, 0]:
            frame = cmd_vibrate(level)
            print(f"  -> [9002] {frame.hex()} (level={level})")
            try:
                await client.write_gatt_char(CHAR_9002, frame, response=False)
                print("     OK")
            except Exception as e:
                print(f"     FAIL: {e}")
            await asyncio.sleep(3)

        # ============ 测试 4：吮吸电机（9001, cmd 0x2e） ============
        print("\n=== Test: Suction on 9001 (cmd 0x2e) ===")
        for level in [30, 0]:
            frame = cmd_suction(level)
            print(f"  -> [9001] {frame.hex()} (level={level})")
            try:
                await client.write_gatt_char(CHAR_9001, frame, response=True)
                print("     OK")
            except Exception as e:
                print(f"     FAIL: {e}")
            await asyncio.sleep(3)

        print("\nDone.")

if __name__ == "__main__":
    asyncio.run(main())
```

## 后续工作

1. ~~最优先：用测试脚本验证 9002 + cmd 0xA0 是否能控制伸缩电机~~ → 协议已完整还原
2. ✅ 确认 writeExtend (cmd 0xA3) 和 writePowerOff (cmd 0xA2)
3. ✅ 确认 9001 帧格式 — 与 9002 相同，无 CRC
4. ✅ 确认 9001 上所有功能的 cmd 字节
5. **待做：开机实测**，验证所有命令是否生效
6. 确认 writeHeatingSetting / writeLightSetting / writeTravelLock 的数据格式
7. 确认 writePressureStatus 的实际 cmd（反编译中为 0x148，超出字节范围）
8. 解析压力传感器数据格式（MixDeviceSensorModel::fromBytes）

## 文件位置（VPS）

- blutter 输出：`/tmp/blutter_out/`
- V2 主文件：`/tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart`（13694 行）
- V1 参考：`/tmp/blutter_out/asm/mix_device/src/bluetooth/v1/ble_device_v1.dart`（10373 行）
- 按键事件：`/tmp/blutter_out/asm/mix_device/src/mix_device_key.dart`（192 行）
- 字符串表：`/tmp/blutter_out/strings.txt`

## 关键中间文件（本地 D:\Eric\）

- `handleOperate.txt` — `_handleOperate` 函数反汇编（988 行）
- `grep_results.txt` — 全文件 movz 指令搜索结果
- `func_names.txt` — 函数名与行号映射
