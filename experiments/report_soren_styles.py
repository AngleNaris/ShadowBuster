"""Produce gain-matched auditions and a Markdown report from completed measurements."""
import json
import sys
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy import signal
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from experiments.compare_soren_styles import DEST, CORE, PY
from experiments.capture_soren_baseline import digest


def main():
    report=json.loads((DEST/'measurements.json').read_text(encoding='utf-8'))
    assert len(report['tracks'])==2 and all(len(t['versions'])==7 for t in report['tracks'])
    lines=['# Soren 风格链 / 无风格对照实验','',
      '同一母带前输入、每首七版本、完整曲长。Neutral **不是无风格**：仍有 Mid/Side RMS 匹配、Mid 饱和、参考频谱匹配、Side 低频收紧、低通、渐进校正、已有 Mid 存在感校正和立体声处理。',
      '', '## 方法与限制',
      '- 人声为独立 HTDemucs 估计；统一 shifts=0 和历史原曲活动掩码。人声/伴奏比是窗口中位数，不是分轨真值，也不是主观听感。',
      '- 宽带和 1–4/4–8 kHz 指标均使用 Mid；伴奏估计为混音减人声估计。亮度采用频段相对宽带电平，排除整体增益。',
      '- style-off 跳过全部风格链，使用 Pop 的目标 LUFS（不加载参考音频），只做整体增益、linked limiter、polyphase 重采样、全局真峰值下调和 PCM24 抖动。',
      '- 风格模式沿用原有 1.5 dB 限制器驱动预算；无风格模式迭代整体增益以接近目标。因此其差异同时包含风格链与限制器工作量，不能单独定位某个处理器。',
      '- 非线性限幅仍可能改变频谱和估计比例。“无风格”不等于逐样本恒定增益。无活动限制器的合成测试验证 1–12 kHz 相对频谱；不能声称活动限制器在所有音频上完全不改频谱。',
      '- 未进行人工试听；试听文件包含原输出与统一 LUFS 的副本，不用追加人声增益补偿。',
      '', '## 运行时',f'- Python: `{PY}`', f'- 核心: `{CORE}`',f'- SHA256: `{digest(CORE)}`',
      '- GPU Python 运行时 torch 2.7.1+cu128 / NumPy 2.5.2，CUDA 可用；Soren 核心实际为 NumPy/SciPy CPU DSP，GPU 用于 HTDemucs。',
      '- CPU/GPU 暂存核心一致；开发核心原来缺少暂存版 Mid 保护，保留其原默认链，仅添加同一无风格路径。未改写历史安装测试副本。',
      '', '## 结果']
    auditions=[]
    for track in report['tracks']:
        base=track['baseline']; lines += ['', '### '+track['track'],f"母带前：{base['lufs']:.2f} LUFS；宽带 Mid 人声/伴奏 {base['vocal_estimate']['wide_mid']['vocal_accompaniment_db']:.2f} dB。",'',
        '|版本|LUFS / 目标|4× TP|比例 Δ dB|1–4k 亮度 Δ|4–8k 亮度 Δ|M/S Δ|GR max / 活动%|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
        common=min([base['lufs']]+[v['lufs'] for v in track['versions']])-.2
        folder=DEST/track['track']/'level_matched';folder.mkdir(exist_ok=True)
        allversions=[dict(output=track['input'],label='00_premaster',lufs=base['lufs'])]+track['versions']
        for v in allversions:
            out=folder/(v['label']+'.wav')
            if not out.exists():
                x,sr=sf.read(v['output'],always_2d=True)
                sf.write(out,x*10**((common-v['lufs'])/20),sr,subtype='PCM_24')
            auditions.append({'path':str(out),'sha256':digest(out),'target_lufs':common})
        for v in track['versions']:
            est=v['vocal_estimate'];be=base['vocal_estimate']
            delta=est['wide_mid']['vocal_accompaniment_db']-be['wide_mid']['vocal_accompaniment_db']
            brightness=lambda band:(est[band]['level_db']-est['wide_mid']['level_db'])-(be[band]['level_db']-be['wide_mid']['level_db'])
            lim=v['limiter'];stats=lim['limiter']
            lines.append(f"|{v['label']}|{v['lufs']:.2f} / {lim['target_lufs']:.2f}|{v['true_peak_4x_dbtp']:.2f}|{delta:+.2f}|{brightness('1-4k_mid'):+.2f}|{brightness('4-8k_mid'):+.2f}|{v['mid_side_db']-base['mid_side_db']:+.2f}|{stats['max_gain_reduction_db']:.2f} / {100*stats['active_fraction']:.1f}|")
        off=track['versions'][-1]
        delta=off['vocal_estimate']['wide_mid']['vocal_accompaniment_db']-base['vocal_estimate']['wide_mid']['vocal_accompaniment_db']
        styled=[v['vocal_estimate']['wide_mid']['vocal_accompaniment_db']-base['vocal_estimate']['wide_mid']['vocal_accompaniment_db'] for v in track['versions'][:-1]]
        lines += ['',f"风格版本宽带比例变化范围 {min(styled):+.2f} 至 {max(styled):+.2f} dB；style-off 为 {delta:+.2f} dB。",
                  f"style-off 目标误差 {abs(off['lufs']-off['limiter']['target_lufs']):.3f} LU，真峰值 {off['true_peak_4x_dbtp']:.3f} dBTP。",
                  f"试听目录：`{folder}`。该组统一到 {common:.2f} LUFS。"]
        assert digest(Path(track['input']))==track['input_sha256']
    lines+=['','## 归因边界','Pop 的四种 EQ 在两首上都呈现约 2–2.5 dB 的宽带人声/伴奏估计比例下降，EQ 切换差异很小；Rock 和 EDM 则上升，不能宣称所有风格都造成同样退后。style-off 的比例偏移仅约 +0.2 dB，亮度变化也较小，支持 Pop 参考驱动的风格链是本次退后的主要贡献者，而非仅由最终响度处理决定。Rock/EDM 改变了比例但并非透明替代。style-off 为达到 −7.62 LUFS 付出 6.14/7.47 dB 最大衰减和较高限制器活动率，可能牺牲动态；剩余比例/亮度变化仍可能来自限幅与分离误差。无法由本实验单独分配频谱匹配、Mid 校正、立体声处理的因果份额。', '', '## 检查与复现', '`experiments/compare_soren_styles.py` 运行矩阵；`measurements.json` 保存全部 sample peak、4× true peak、crest、频段估计、参数、限制器活动、输入输出哈希。`baseline.json` 与 core_before 文件保存修改前核心。', '项目 tests/ 最终 254 passed、29 subtests passed；Node 语法与 git diff --check 通过（后者只有已有 UI 文件换行符警告）。CPU 无风格烟测通过；固定合成输入、禁用随机抖动后，修改前后 styled 输出逐样本一致。历史上游文件哈希核验通过；母带前文件来自上次实验，历史报告没有保存其字节哈希，本次在处理前固定哈希并在完成后复核，不声称已证明其历史字节身份。详情见 provenance_and_default.json。根目录无范围 pytest 误收集第三方包的 1746 项收集错误日志也保留。未修改 UI 控件或文案，未覆盖原曲和历史结果，未提交或上传。']
    (DEST/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (DEST/'auditions.json').write_text(json.dumps(auditions,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
