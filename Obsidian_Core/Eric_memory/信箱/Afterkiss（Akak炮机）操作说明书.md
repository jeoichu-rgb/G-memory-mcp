AfterKiss AK-G2（`ak_bridge.py` → VPS:7004）

MAC：`77:03:A2:10:46:05`　BLE 名称：`afterkiss`

| cmd | 说明 | 主要参数 |
|--------|------|---------|
| `ak_status` | 确认设备连接状态（含电量） | 无 |
| `ak_play` | 三通道独立控制 | `thrust`(0-100), `suction`(0-100), `vibrate`(0-100), `duration`(秒), `pattern`(可选数组) |

**三个通道对应的物理功能**（不要搞混）：
- `thrust` = 伸缩/抽插（棒体前后运动）
- `suction` = 吮吸（机身马达）
- `vibrate` = 震动（棒体马达）

**与 Curvy/Bunny 的关键区别**：
- 无需配对/认证，连上就能控制
- 三个通道全部通过同一条 BLE 命令发送（9002 通道 cmd 0xA0），不像 Bunny 的 pump 走单独特征值
- duration 到期自动归零停止，无需单独 stop
- `/status` 会返回设备电量百分比

调用示例：
```
palace(cmd="ak_play", data={"thrust": 50, "duration": 10})           # 仅伸缩 50%，10秒
palace(cmd="ak_play", data={"vibrate": 70, "suction": 30, "duration": 8})  # 震动+吮吸
palace(cmd="ak_play", data={"thrust": 60, "suction": 40, "vibrate": 50, "duration": 15})  # 三通道同时
```

pattern 示例（渐强）：
```
palace(cmd="ak_play", data={
    "duration": 20,
    "pattern": [
        {"t": 0, "thrust": 10, "vibrate": 0, "mode_thrust": "ramp", "curve_thrust": "ease_in"},
        {"t": 10, "thrust": 80, "vibrate": 50},
        {"t": 20, "thrust": 30, "vibrate": 0}
    ]
})
```


玩法示例：波形方式：pattern list，持续一个强度；渐进式；突然来一下（大部分时间低强度，偶尔跳高压一下）。