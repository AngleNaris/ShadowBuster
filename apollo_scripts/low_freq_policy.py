"""低频协调授权策略（开发规格 v2 §6.3/§6.4/§5.3，P2-03/P2-04）。

单一来源的纯函数模块：studio_backend（管线编排）与 bass_enhance（DSP 脚本）
共用同一份授权公式，避免两处实现漂移。无第三方依赖（subprocess 边界两侧
都可安全导入）。

授权语义（不是审美评分）：低频面板旋钮表达的用户意图越强，允许低频协调
辅助（kick 让位、泥浊整理）动得越多；**全零旋钮 → 授权 0 → 辅助完全不动**
（规格 §5.3"所有低频旋钮归零即低频恒等"，不保留不可关闭的暗中去掩蔽）。

规格 §6.3 工程原型（可复现的初版映射，非最佳心理声学模型；RC0 授权上限
约 1.5 dB 来自 RC0 较低的旋钮值，正式发布前可经 profile 校准替换曲线，
但必须版本化）：

    u_low = clip(max(sub_db/4, punch_db/3,
                     transient_amount/0.5,
                     saturation_amount/0.4), 0, 1)
    最大 kick 让位衰减 D = 3.0 × u_low dB
    泥浊整理上限 = 1.5 × u_low dB
"""

CLARITY_MUD_CAP_DB_AT_AUTH_ONE = 1.5
SIDECHAIN_MAX_DUCK_DB = 6.0   # apply_bass_sidechain 既有上限；D=3×u_low 由
                              # amount=u_low/2 × max_duck=6 组合实现


def low_frequency_authorization(sub_db, punch_db, transient_amount,
                                saturation_amount):
    """低频协调授权 u_low ∈ [0, 1]：四个低频旋钮的归一化最大值。

    任一旋钮达到其"满授权"刻度（Sub 4dB / 鼓身 3dB / 瞬态 0.5 / 饱和 0.4）
    即授权满；全零授权 0。输入须为有限数值（调用方负责校验）。
    """
    values = (float(sub_db) / 4.0,
              float(punch_db) / 3.0,
              float(transient_amount) / 0.5,
              float(saturation_amount) / 0.4)
    return float(min(max(max(values), 0.0), 1.0))


def sidechain_amount_from_authorization(authorization):
    """u_low → apply_bass_sidechain 的 amount（0–0.5）。

    组合 max_duck_db=6 时，理论最大 duck = 6 × u_low/2 = 3.0 × u_low dB，
    精确对应规格 §6.3 的 D = 3.0 × u_low；实际 duck 再乘置信与 kick 包络。
    """
    return float(authorization) / 2.0


def clarity_mud_cap_db(authorization):
    """泥浊整理衰减上限（规格 §6.4：初版 1.5 × u_low dB）。"""
    return CLARITY_MUD_CAP_DB_AT_AUTH_ONE * float(authorization)


def scale_clarity_gains(gains, authorization):
    """按授权缩放 auto-clarity 增益 (mud_db, clarity_db)。

    mud 为负值：先按授权等比缩小，再夹到 −clarity_mud_cap_db(auth)（带内
    削减不越过 §6.4 预算）；clarity 为正值：按授权等比缩小。授权 0 →
    (0, 0)（精确恒等）。授权 ≥ 1 时 mud 上限 −1.5dB（较旧版固定 −2dB 收紧
    到规格预算内）。
    """
    auth = float(authorization)
    mud, clarity = float(gains[0]), float(gains[1])
    mud = max(mud * auth, -clarity_mud_cap_db(auth))
    clarity = clarity * auth
    return (mud, clarity)
