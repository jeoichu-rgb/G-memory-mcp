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

## 已确认的协议：9002 (Function) 通道

### 帧格式

来源：`BLEDeviceV2::_handleFunction()`（行 3576-4117，地址 0x169c3f4）

```
字节 [0]：cmd（命令字节）
字节 [1]：(data.length << 3) | 0x80（编码后的长度）
字节 [2..]：数据字节
```

**没有 CRC。** 直接写入 "9002" 特征值，使用 BLE `writeWithoutResponse`。

#### 帧格式的汇编证据

```arm64
// 0x169c4dc: strb w0, [x5]           // buffer[0] = cmd
// 0x169c50c: lsl x0, x1, #3          // data.length << 3
// 0x169c510: orr x2, x0, #0x80       // | 0x80
// 0x169c52c: strb w2, [x0, #1]       // buffer[1] = (len << 3) | 0x80
// 然后循环复制 data 字节到 buffer[2..]
```

#### 长度编码示例

| 数据字节数 | 编码后的第二字节 | 二进制 |
|-----------|-----------------|--------|
| 0 | 0x80 | 1000 0000 |
| 1 | 0x88 | 1000 1000 |
| 2 | 0x90 | 1001 0000 |
| 3 | 0x98 | 1001 1000 |
| 4 | 0xA0 | 1010 0000 |

#### 写入方式的证据

在 `_handleFunction` 的匿名闭包（地址 0x169c884）中：

```arm64
// 0x169c914: ldur w2, [x1, #0x8b]    // field_8b = "9002" 特征值
// 0x169c940: List(7) [..., "withoutResponse", ...]
// 0x169c948: bl BluetoothCharacteristic::write
```

写到 `field_8b`（init 中存储的 "9002" 特征值），参数 `withoutResponse: true`。

### 已确认的命令

#### 电机控制（伸缩/抽插）— cmd 0xA0

来源：`BLEDeviceV2::writeMotor()`（行 13433-13694，地址 0x16cfddc）

```arm64
// 0x16cfee0: movz x2, #0xa0          // cmd = 160 = 0xA0
// 0x16cfee4: bl BLEDeviceV2::_handleFunction
```

数据格式：`List<int>`，电机等级列表（推测 0-100）。

单电机示例（level=50）：
```
发送：[0xA0, 0x88, 0x32]
       cmd   len=1  50
```

双值示例（level=50, 70）：
```
发送：[0xA0, 0x90, 0x32, 0x46]
       cmd   len=2  50    70
```

停止电机：
```
发送：[0xA0, 0x88, 0x00]
       cmd   len=1  0
```

### 待查的命令

以下命令也走 9002 (`_handleFunction`)，但 cmd 字节未确认：

**writeExtend（震动/拓展）**
- 调用位置：行 12692-12693（地址 0x16cd7cc）
- 功能：控制按摩棒上的振动马达
- cmd 字节：查看行 12680-12695 附近的 `movz` 指令

**writePowerOff（关机）**
- 调用位置：行 3539-3540（地址 0x169c390）
- cmd 字节：查看行 3530-3542 附近的 `movz` 指令

查询方法：
```bash
sed -n '3530,3542p' /tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart
sed -n '12680,12695p' /tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart
```

## 待确认的协议：9001 (Op) 通道

### 走 9001 的功能

以下功能通过 `_dispatchOperateWithTimeout` → `_handleOperate` 写入 "9001"：
- `writeMotorLevel`（电机档位，可能是吮吸）
- `writeHeatingStatus`（加热开关）
- `writeHeatingSetting`（加热设置）
- `writeLightSetting`（灯光）
- `writePressureStatus`（压力配置）
- `writeTravelLock`（旅行锁）
- 所有 `read*` 函数（读设备信息）

### 帧格式猜测：可能使用 CRC

**未经证实的推测，需要看 `_handleOperate` 代码确认。**

APK 的 Java 层存在一个 MethodChannel `"com.sistalk.mp/bluetooth_plugin"`，提供两个方法：

#### genData（CRC 封包）

来源：`P9.a.a()`（jadx 反编译）

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

帧格式：`[cmd, data_length, data..., crc_lo, crc_hi]`

#### CRC-16 算法

来源：`P9.a.b()`（jadx 反编译），已验证的 Python 实现：

```python
def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc = (((crc << 8) | (crc >> 8)) & 0xFFFF) ^ b
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def gen_data(cmd, data=b''):
    frame = bytes([cmd, len(data)]) + data
    return frame + crc16(frame)
```

#### decrypt（Native 解密）

`P9.b` 的 MethodChannel 还有 `decrypt` 方法，调用 `BLEDeviceDecrypt.nativeMpDecrypt(data)`。用途不明，可能用于 9001 上接收的加密数据。

### 推测 9001 使用 CRC 的理由

1. `_handleOperate` 是请求-响应模式（不是 fire-and-forget），需要完整性校验
2. Java 层的 genData/CRC 必然被某处调用，9002 不用 CRC，所以大概率是 9001
3. 之前所有往 9001 发送的裸数据都返回了 `01 08 01`（可能是 CRC 校验失败）

### 确认方法

```bash
# 查看 _handleOperate 完整代码（约 986 行）
sed -n '4342,5328p' /tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart

# 搜索是否引用了 MethodChannel 或 genData
grep -n "invokeMethod\|MethodChannel\|genData\|bluetooth_plugin" /tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart

# 查看 _dispatchOperateWithTimeout 的 cmd 参数
sed -n '4277,4341p' /tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart
```

## 设备发送的数据（9001 通知）

### 心跳

格式与发送帧一致：`[cmd, (len<<3)|0x80, data...]`
- `0x14, 0x88, XX` — 心跳类型 A（cmd=0x14，1字节数据）
- `0x15, 0x88, XX` — 心跳类型 B（cmd=0x15，1字节数据）

### 状态消息

- `0x01, 0x20, XX, XX, XX, XX` — cmd=0x01，4字节数据。之前误以为是 auth challenge，实际是设备状态信息。
  - 注意：0x20 = `(4 << 3) | 0x00`，**没有** 0x80 位。设备发送的帧可能不加 0x80 标志位，或者 0x80 表示方向（app→device 才加）。

### 压力感应

`onValueReceived` 中有 cmd 0x14a (330) 的处理，调用 `MixDeviceSensorModel::fromBytes` 解析传感器数据。

### 按键事件

`onReceivedKeyData` 处理 4 字节按键数据：
- byte[0]：按键类型（0-6，映射到 KeyType 枚举）
- byte[2]：按键值（0-100）
- 创建 `MixDeviceKeyEvent(type, eventType, value)` 分发

## 设备功能与 BLE 命令对照

| 功能 | 物理按键 | BLE 函数 | 通道 | 帧格式 |
|------|---------|----------|------|--------|
| 伸缩（抽插） | A加速 B减速 | writeMotor | 9002 | `[0xA0, (n<<3)\|0x80, levels...]` |
| 震动（棒子马达） | D | writeExtend | 9002 | `[cmd?, (n<<3)\|0x80, level?]` |
| 关机 | F长按 | writePowerOff | 9002 | `[cmd?, ...]` |
| 吮吸（机身马达） | D同键 | writeMotorLevel? | 9001 | 可能 CRC 格式 |
| 加热 | C | writeHeatingStatus | 9001 | 可能 CRC 格式 |
| 压力感应 | E | 设备推送 | 9001 通知 | cmd 0x14a |

## 之前的失败尝试和原因

在 `D:\Eric\afterkiss_iv.py` 中进行了 74+ 次尝试，全部返回 `01 08 01`。

失败原因（全部踩中）：
1. **写错特征值**：往 "9001"（op）写，电机命令应该走 "9002"（function）
2. **帧格式错误**：发送 AES 加密数据和裸字节，设备期望 `[cmd, encoded_len, data...]`
3. **误判为认证失败**：以为 `01 08 01` 是 "auth failed"，实际可能是 "帧格式无法解析"
4. **误判 challenge**：以为 `01 20 XX XX XX XX` 是 challenge 需要回应，实际是设备状态消息

## 测试脚本框架

```python
import asyncio
from bleak import BleakClient, BleakScanner

DEVICE_ADDR = "77:03:A2:10:46:05"
CHAR_9001 = "00009001-0000-1000-8000-00805f9b34fb"
CHAR_9002 = "00009002-0000-1000-8000-00805f9b34fb"

def make_func_cmd(cmd: int, data: list[int]) -> bytes:
    """V2 function 通道帧格式（9002，无 CRC）"""
    length_byte = (len(data) << 3) | 0x80
    return bytes([cmd, length_byte] + data)

def crc16(data: bytes) -> bytes:
    """CRC-16 CCITT 变种，用于 9001 op 通道（待确认）"""
    crc = 0xFFFF
    for b in data:
        crc = (((crc << 8) | (crc >> 8)) & 0xFFFF) ^ b
        crc ^= (crc & 0xFF) >> 4
        crc ^= (crc << 12) & 0xFFFF
        crc ^= ((crc & 0xFF) << 5) & 0xFFFF
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def make_op_cmd(cmd: int, data: bytes = b'') -> bytes:
    """9001 op 通道帧格式（CRC，待确认）"""
    frame = bytes([cmd, len(data)]) + data
    return frame + crc16(frame)

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

        # 列出所有服务和特征值
        for service in client.services:
            print(f"\nService: {service.uuid}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                print(f"  {char.uuid} [{props}]")

        # 订阅通知
        for uuid, name in [(CHAR_9001, "9001"), (CHAR_9002, "9002")]:
            try:
                await client.start_notify(uuid, lambda s, d, n=name: print(f"  <- [{n}] {d.hex()}"))
                print(f"Subscribed to {name}")
            except Exception as e:
                print(f"Can't subscribe {name}: {e}")

        print("\nWaiting 3s for device messages...")
        await asyncio.sleep(3)

        # ============ 测试 1：伸缩电机（9002, cmd 0xA0）============
        print("\n=== Test: Motor on 9002 (cmd 0xA0) ===")
        for level in [20, 50, 0]:
            cmd = make_func_cmd(0xA0, [level])
            print(f"  -> [9002] {cmd.hex()} (level={level})")
            try:
                await client.write_gatt_char(CHAR_9002, cmd, response=False)
                print("     OK")
            except Exception as e:
                print(f"     FAIL: {e}")
            await asyncio.sleep(3)

        # ============ 测试 2：双值电机（可能伸缩+吮吸）============
        print("\n=== Test: Dual motor values ===")
        cmd = make_func_cmd(0xA0, [30, 30])
        print(f"  -> [9002] {cmd.hex()} (dual 30,30)")
        try:
            await client.write_gatt_char(CHAR_9002, cmd, response=False)
            print("     OK")
        except Exception as e:
            print(f"     FAIL: {e}")
        await asyncio.sleep(3)

        # 停止
        cmd = make_func_cmd(0xA0, [0, 0])
        await client.write_gatt_char(CHAR_9002, cmd, response=False)

        print("\nDone.")

if __name__ == "__main__":
    asyncio.run(main())
```

## 后续工作

1. **最优先**：用测试脚本验证 9002 + cmd 0xA0 是否能控制伸缩电机
2. 查 writeExtend 和 writePowerOff 的 cmd 字节（VPS 上 sed 命令已列出）
3. 看 `_handleOperate` 代码确认 9001 帧格式是否用 CRC
4. 查 9001 上各功能的 cmd 字节
5. 完善脚本支持所有功能

## 文件位置（VPS）

- blutter 输出：`/tmp/blutter_out/`
- V2 主文件：`/tmp/blutter_out/asm/mix_device/src/bluetooth/v2/ble_device_v2.dart`（13694 行）
- V1 参考：`/tmp/blutter_out/asm/mix_device/src/bluetooth/v1/ble_device_v1.dart`（10373 行）
- 按键事件：`/tmp/blutter_out/asm/mix_device/src/mix_device_key.dart`（192 行）
- 字符串表：`/tmp/blutter_out/strings.txt`
