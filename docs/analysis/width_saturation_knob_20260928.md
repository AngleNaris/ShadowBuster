# 宽度/饱和度旋钮响应修订（2026-09-28）

## 问题

用户报告"调整声场和饱和度效果不显著"。定位到三个独立根因：

1. **试听宽度死区**（`ui/draft_audio.js`）：旧公式
   `min(0.4, space·(10^(width_db/20)−1))` 在默认档（space 0.6 / 6dB）请求值
   0.597 已越过 0.4 硬盖——**默认位向上调整输出零变化**（单调性断裂，非"弱"）。
2. **正式宽度预算封顶低于请求**：`soundstage_reshape.py` 的 `WIDTH_BANDS`
   固定增长阶梯（+1/+2/+3/+2dB）与 `mastering/soundstage.py widen()` 的
   增长/S-M 上限在默认档位附近就封顶，0–12dB 旋钮上约 2/3 行程无响应。
   `soundstage.py` 旧注释已记录过一次同类问题（8dB 请求仅兑现 ~14%）。
3. **试听饱和度过弱且与正式曲线不一致**：旧公式 `sat·(tanh(8x)/8 − x)`
   小信号增益恰为 1，增量纯为三次谐波项，默认 0.2 档在典型低频幅度下约
   −0.3dB（安静段完全无感）；正式链路（`bass_enhance.py` soft_clip
   drive=1.6）小信号增益 1.74，主要是低频提升+温和谐波——两者听感不同源。

## 修订

### 宽度语义：旋钮授权增长（用户意志优先）

- **UI 单位**：宽度计量表由 0–12dB 改为 **0–1 授权比例（显示 %）**，
  100% = +12dB Side 增益；`data-step` 0.05。默认 0.5（= 旧 6dB 位置）。
  持久化快照升版 v1→v2（`space_width` 除以 12 迁移；裸键路径同档处理）。
- **映射**：`main.width_knob_to_db()` 把比例线性映射到 DSP 的 dB 接口
  （钳制 [0,12]）；`processing_cli` 的 `--space-width-db` 保持 dB（专业接口）。
- **reshape（`apollo_scripts/soundstage_reshape.py`）**：
  `constrain_width_delta(mix, delta, sr, growth_db)` 新增授权增长参数
  （能量预算 = `es·10^(growth_db/10)`），growth_db 取本阶段实际请求的最大
  Side 增益（static=other 轨增益、dynamic=shelf 提升量）。旧固定增长阶梯
  废弃；`WIDTH_BANDS` 只保留**单声道兼容占比上限**并放宽：
  120–300Hz 0.10→0.40、300–2k 0.35→0.60、2k–8k 0.55→0.75、8k+ 0.45→0.70
  （Side/Mid 能量比上限）。
- **母带末端（`mastering/soundstage.py widen()`）**：同一语义。增长预算 =
  请求（`10^(width_db/10)`）；全局占比上限 0.70（旧为 S/M ≤ 0dB）、频段
  0.60/0.75/0.75。120Hz 以下不生成宽度、鼓攻击段收紧、`width_decreased`
  回退保护等结构性规则不变。
- 效果：默认档（50%）在典型素材上 Side 兑现 ~+4dB（旧 ~+2–3dB 且到顶）；
  100% 在窄素材上可兑现至占比上限（低中频段 S/M ≤ +1.76dB、全局
  ≤ +3.67dB 能量比）。

### 饱和度：试听与正式同形

- `ui/draft_audio.js` 饱和项改为 `sat·(tanh(low·1.6)/tanh(1.6) − low)`，
  与 `bass_enhance.soft_clip(drive=1.6)` 同形：小信号增益 1.74、低频提升
  与谐波特征和正式导出一致。正式链路不改。

### 明确不做

- 不加"已到预算上限"的 UI 提示（产品决策 2026-09-28）。
- `packaging/soren_core.py`（legacy styled 引擎）的声场定型不在本次范围。

## 兼容与测试

- localStorage：v1 快照/裸键 dB 值自动迁移为 0-1 比例；未存过宽度的用户
  取 HTML 默认 0.5。
- 缓存身份：reshape/mastering DSP 代码变更使既有渲染缓存整体失效（既有语义）。
- 测试更新：`test_adaptive_soundstage.py`（授权增长/验证）、
  `test_reference_mastering.py`（授权语义 + 占比上限）、
  `test_default_source.py`（宽度默认 0.5）、`test_ui_units.py`（新单位与
  v2 迁移）、`test_ui_param_mapping.py`（`width_knob_to_db`）、
  `tests/draft_audio.cjs`（默认以上宽度仍有响应、满档≈3×半档、默认饱和可闻）。
