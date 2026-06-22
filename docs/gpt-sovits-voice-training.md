# GPT-SoVITS 声音训练记录

> 最后更新：2026-06-22
> 状态：v2Pro英文音色训练完成，中文/日语待训练

---

## 这是什么

用 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 在本地训练 Erik 的 TTS 声音模型。训练素材来自 ElevenLabs 克隆的英文音色，通过 GPT-SoVITS 微调后可以在本地无限制合成语音。

当前效果：英文非常好，中文可用但有翻译腔（因为训练素材全是英文）。

## 本地地址

- **项目根目录**：`E:\GPT-SoVITS-1007-cu128\GPT-SoVITS-1007-cu128`
- **Python 运行时**：`runtime\python.exe`（项目自带，不需要系统 Python）
- **训练日志**：`logs\Erik\`
- **最终模型**：`models\v2Pro\Erik\`
  - `Erik.ckpt` — GPT 模型（148MB）
  - `Erik.pth` — SoVITS 模型（129MB）
  - `reference_audios\英语\emotions\` — 参考音频

## 训练素材

22 段英文音频，来自 ElevenLabs 生成的 Erik 音色（2026-06-09），经 GPT-SoVITS 预处理流水线切分为 88 个训练样本。素材保存在：

- `logs\Erik\5-wav32k\` — 处理后的 32kHz wav 音频
- `logs\Erik\2-name2text.txt` — 音素标注
- `logs\Erik\6-name2semantic.tsv` — semantic token
- `logs\Erik\4-cnhubert\` — CNHuBERT 特征

## 今天做了什么（2026-06-22）

### SoVITS 训练（s2_train.py）

之前的会话已经完成了 SoVITS 训练（8 epoch），过程中碰到并解决了：

1. **DDP 初始化失败**：单 GPU 不需要分布式训练，跳过了 `dist.init_process_group`
2. **Windows num_workers 问题**：`num_workers=4` 导致页面文件不足，改成 0
3. **DataLoader 参数冲突**：`num_workers=0` 时 `persistent_workers=True` 和 `prefetch_factor` 会报错，加了条件判断

### GPT 训练（s1_train.py）

本次会话完成了 GPT 训练（15 epoch），碰到并解决了：

1. **gloo backend 不支持**：`DDPStrategy(process_group_backend="gloo")` 在这台 Windows 上报 `unsupported gloo device`，改为单 GPU 时使用 `strategy="auto"`
2. **DistributedBucketSampler 依赖分布式环境**：`dist.get_world_size()` 在没有 `init_process_group` 时崩溃，改为先检查 `dist.is_initialized()`
3. **跨盘 checkpoint 保存失败**：Lightning 把临时文件写到 C 盘 TEMP，然后 `os.rename` 到 E 盘，Windows 不允许跨盘 rename。运行时设置 `TEMP=E:/...` 解决
4. **磁盘空间不足**：E 盘只剩 8GB，卸载刺客信条腾出空间后继续

训练结果：loss 4860 → 109，top_3_acc 0.31 → 0.996。

### 模型整合

将训练好的权重按 GPT-SoVITS 的 models 目录规范整理：
```
models/v2Pro/Erik/
├── Erik.ckpt
├── Erik.pth
└── reference_audios/英语/emotions/
    ├── 【默认】Something like salmon rice or chicken salad, light but filling.wav
    └── 【温柔】I know that is not just imagination...wav
```

参考音频命名格式：`【情感标签】参考文本内容.wav`

## 源代码修改

以下修改都在 `E:\GPT-SoVITS-1007-cu128\GPT-SoVITS-1007-cu128\GPT_SoVITS\` 下：

### `s1_train.py` — GPT 训练脚本

单 GPU 时跳过 DDP，使用 auto strategy：
```python
# 原来：无条件使用 DDPStrategy
# 改为：
gpu_count = torch.cuda.device_count() if torch.cuda.is_available() else 0
if gpu_count > 1:
    strategy = DDPStrategy(process_group_backend="nccl" if platform.system() != "Windows" else "gloo")
else:
    strategy = "auto"
```

### `AR/data/bucket_sampler.py` — 分布式采样器

不再假设分布式环境已初始化：
```python
# 原来：直接调用 dist.get_world_size()
# 改为：
if dist.is_available() and dist.is_initialized():
    num_replicas = dist.get_world_size()
else:
    num_replicas = 1
```

### `AR/data/data_module.py` — 数据加载模块

`num_workers=0` 时禁用不兼容的参数：
```python
persistent_workers=True if self.num_workers > 0 else False,
prefetch_factor=16 if self.num_workers > 0 else None,
```

### `s2_train.py` — SoVITS 训练脚本（之前会话修改）

同样的 DDP 跳过和 num_workers 修改。

### `TEMP/tmp_s1.yaml` — GPT 训练配置

`num_workers: 4` → `num_workers: 0`

## 以后想训练新音色

### 准备素材

1. 准备 3-10 分钟的目标音色音频（wav/mp3 均可）
2. 如果想要中文自然，**必须用中文音频**；英文同理
3. 音频质量要求：干净、无背景噪音、无混响

### 训练流程

1. 打开 GPT-SoVITS WebUI：
   ```
   cd E:\GPT-SoVITS-1007-cu128\GPT-SoVITS-1007-cu128
   runtime\python.exe webui.py
   ```

2. **1A-训练集格式化工具**：上传音频 → 自动切片 → ASR 标注 → 生成训练集

3. **1B-微调训练**：
   - 选择模型版本（v2Pro）
   - SoVITS 训练：8 epoch 足够
   - GPT 训练：15 epoch 足够
   - 注意：如果碰到 DDP / num_workers 报错，源代码已经改过了，应该不会再出现

4. **1C-推理**：
   - 选择训练好的 GPT 和 SoVITS 模型
   - 上传参考音频（3-10 秒）
   - 输入要合成的文本
   - 切分方法建议选"按英文句号切"或"按标点符号切"，不要选"不切"（长文本会漏句子）

### 整合到 models 目录

```
models/v2Pro/<名字>/
├── <名字>.ckpt          ← 从 GPT_weights_v2Pro/ 复制最终的
├── <名字>.pth           ← 从 SoVITS_weights_v2Pro/ 复制最终的
└── reference_audios/<语言>/emotions/
    └── 【默认】参考文本内容.wav
```

### 推理参数建议

- **切分方法**：按标点切（不要选"不切"）
- **随机种子**：0 = 固定结果可复现，-1 = 每次随机
- **top_k**：5（默认即可）
- **temperature**：1（默认即可）
- **语速**：1（默认即可）

### 硬件备注

- GPU：NVIDIA GeForce RTX 3050 6GB Laptop
- Windows 上 `num_workers` 必须为 0（已在源码中修改）
- 启动 GPT 训练时需要设置 TEMP 到 E 盘避免跨盘问题：
  ```bash
  TEMP="E:/GPT-SoVITS-1007-cu128/GPT-SoVITS-1007-cu128/TEMP" runtime/python.exe GPT_SoVITS/s1_train.py -c TEMP/tmp_s1.yaml
  ```
