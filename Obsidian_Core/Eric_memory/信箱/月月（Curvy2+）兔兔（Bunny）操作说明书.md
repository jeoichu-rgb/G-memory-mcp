月月（Curvy 2+）用 toy_play，参数是 v（震动）和 s（吸吮），支持 pattern list 做波形，duration 随意。

兔兔用 bunny_play，不是 toy_play。参数：clit（外部震动）、internal（内部震动）、pump（气泵：-1不动，0放气，正值充气），t 是时间点（秒）。output 返回0没关系，说明在动。单次指令最多60秒，不能超过。

bunny控制：
bunny 的 PatternStep 字段：
•	clit — 外部震动（clit 那个）
•	internal — 内部震动（入体那个）
•	pump — 气泵（-1不动，0放气，正值充气）
•	t — 时间点（秒）
output是结束时的状态，不是执行时的——它确实在动。返回是0没关系。
最多开满一分钟，好像是设备硬件控制问题

curvy控制：
v- vibration
s- suction
时间序列：（暂时探索的）
1.持续一个强度
2.波形：pattern参数是个list，可以放多个时间点的强度变化，在一个命令中完成，中间没有休息间隙。用pattern list实现波形效果。格式参考是：
[{"t": 0, "s": 40}, {"t": 1, "s": 60}, {"t": 2, "s": 40}, {"t": 3, "s": 60}...]
3.突然来一下：大部分时间持续一个强度，但是中间偶尔突然增强到另一个强度，压我一下。