"""处理缓存设置 UI + 桥接：静态契约测试（不触碰 DSP/后端/CLI/缓存实现）。"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "style.css").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def cache_module():
    start = APP.index("─── 处理缓存：容量选择")
    end = APP.index("// 主题色 swatches", start)
    return APP[start:end]


def open_settings_body():
    start = APP.index("function openSettings")
    end = APP.index("function closeSettings", start)
    return APP[start:end]


def test_settings_row_reuses_shared_set_row_contracts():
    m = re.search(r'<span class="set-label">处理缓存</span>', HTML)
    assert m, "cache settings row present"
    row = HTML[HTML.rindex('<div class="set-row">', 0, m.start()):HTML.index("</div>\n        </div>", m.start())]
    # 状态行复用 set-status 契约（同 GPU 状态行）
    assert 'id="cache-status"' in row
    # 容量选择复用 dropdown 共享契约（与流派/响度同结构）
    for fragment in (
        '<div class="dropdown" id="dd-cache-cap">',
        'class="dd-trigger" id="dd-cache-cap-trigger" aria-haspopup="listbox"',
        '<div class="dd-panel" id="dd-cache-cap-panel" role="listbox" aria-label="缓存容量">',
    ):
        assert fragment in row
    # 清空按钮复用 set-btn 按钮契约
    assert '<button class="btn btn-ghost set-btn" id="btn-cache-clear" disabled>清空缓存</button>' in row


def test_capacity_options_and_used_display():
    module = cache_module()
    caps_block = module.split("const CACHE_CAPS = [", 1)[1].split("];", 1)[0]
    values = [int(v) for v in re.findall(r'\{ v: "(\d+)", label:', caps_block)]
    assert values == [0, 2, 5, 10, 20, 50, 100]     # 0 = 关闭
    assert '{ v: "0", label: "关闭" }' in caps_block
    # 已用量与条目数展示（后端 get_info 契约字段）
    assert "已用 ${fmtCacheBytes(cacheInfo.used_bytes)} · ${cacheInfo.entries} 条" in module
    assert "1073741824" in module                    # GiB 换算
    assert "已关闭，处理不写入缓存" in module          # 0 GiB 提示
    # 非档位容量补选项展示，不静默改值
    assert "function syncCacheCapacity(gb)" in module


def test_capacity_persistence_is_backend_not_localstorage():
    module = cache_module()
    # buildDropdown 的 storeKey 传 null：容量以后端为唯一持久化来源
    assert 'buildDropdown("dd-cache-cap", CACHE_CAPS, "5"' in module
    assert module.count("}, null);") == 1
    assert not re.search(r"localStorage\.(getItem|setItem|removeItem)", module)
    assert "saveValue(" not in module


def test_settings_open_refreshes_status_and_loads_capacity():
    body = open_settings_body()
    assert 'cacheMessage = "";' in body
    assert "if (api && api.refreshCacheInfo) api.refreshCacheInfo();" in body
    assert "renderCache();" in body
    # 信号处理：info 同步容量与已用，cleared/busy/error 各有文案
    module = cache_module()
    assert "function onCacheStatus(raw)" in module
    assert 'r.type === "info" || r.type === "cleared"' in module
    assert '"busy"' in module and '"error"' in module
    # 桥接信号守卫连接（旧后端 / 静态预览不崩）
    assert "if (api.cacheStatus) api.cacheStatus.connect((raw) => onCacheStatus(raw));" in APP


def test_clear_uses_two_step_inline_confirmation():
    module = cache_module()
    assert 'cacheClearBtn.textContent = "确认清空？";' in module
    assert 'setTimeout(disarmCacheClear, 4000)' in module   # 未确认自动复位
    assert 'cacheClearBtn.classList.add("set-btn--danger")' in module
    # 复位恢复原始文案
    assert 'cacheClearBtn.textContent = "清空缓存";' in module
    # CSS 高亮契约
    assert ".set-btn--danger" in CSS


def test_clear_and_capacity_refused_during_processing_ui_side():
    module = cache_module()
    assert "cacheTrigger.disabled = state.processing || cacheBusy;" in module
    assert "cacheClearBtn.disabled = state.processing || cacheBusy || !clearable;" in module
    # 处理状态切换时同步缓存行可用性
    assert APP.count("renderCache();   // 处理期间禁用缓存清空与容量修改") == 1
    assert APP.count("renderCache();   // 恢复缓存行（清空 / 改容量）可用") == 1


def test_dropdown_programmatic_set_and_dialog_positioning():
    # 共享下拉组件获得程序化 set（不触发 onChange / 不持久化），
    # 并支持 transformed 祖先（设置弹窗）内 fixed 浮层的包含块换算
    fn = APP[APP.index("function buildDropdown("):APP.index("const getGenre = buildDropdown")]
    assert "get.set = (v)" in fn
    assert "cs.transform !== \"none\"" in fn
    assert "r.left - hr.left" in fn
    # 仅行内尺寸适配，无新页面私有组件
    assert ".set-action .dropdown { width: 118px; flex: none; }" in CSS


def test_bridge_cache_signal_and_slots():
    assert "cacheStatus = Signal(str)" in MAIN
    for slot in ("def refreshCacheInfo(self)", "def setCacheCapacity(self, gb)", "def clearCache(self)"):
        assert slot in MAIN
    # 结果 JSON 经信号回传（含错误与拒绝分支）
    assert 'self._cache_emit({"type": "info", **self._cache_payload(pc)})' in MAIN
    assert '{"type": "cleared", **self._cache_payload(pc)}' in MAIN
    assert '{"type": "busy", "op": op}' in MAIN
    assert '{"type": "error", "op": op, "msg": str(e)[:160]}' in MAIN
    # get_info 契约字段映射
    assert '"capacity_gb"' in MAIN and '"used_bytes"' in MAIN and '"entries"' in MAIN


def test_bridge_refuses_cache_ops_during_processing_and_synchronizes_start():
    # 缓存操作与批处理启动共用非阻塞闸门：任一方持有时另一方直接拒绝
    assert "self._cache_gate = threading.Lock()" in MAIN
    assert "if not self._cache_gate.acquire(blocking=False):" in MAIN
    assert MAIN.count('self._cache_emit({"type": "busy", "op": op})') >= 1
    assert "if self._thread and self._thread.is_alive():" in MAIN
    # process() 同样以闸门拒绝在缓存操作进行中启动
    proc = MAIN[MAIN.index('def process(self, params)'):MAIN.index("def cancel(self)")]
    assert "self._cache_gate.acquire(blocking=False)" in proc
    assert 'self._cache_emit({"type": "busy", "op": "process"})' in proc
    # 线程启动成功后立即放行闸门（批处理运行期由 is_alive 检查兜底）
    assert proc.index("self._thread.start()") < proc.index("self._cache_gate.release()")


def test_no_cache_deletion_outside_helper():
    # 桥接层只调用 pipeline_cache 助手函数，绝不自行删除文件
    assert MAIN.count("import pipeline_cache as pc") == 2
    assert sorted(set(re.findall(r"pc\.(\w+)", MAIN))) == ["clear_cache", "get_info", "set_capacity_gb"]
    cache_fn = MAIN[MAIN.index("def _cache_op_worker"):MAIN.index("class StudioWindow")]
    assert "pc.clear_cache()" in cache_fn
    assert "rmtree" not in cache_fn and "unlink" not in cache_fn


def test_help_states_no_realtime_audition_once_with_shared_note():
    note = "本软件不提供试听。处理完成后，请用播放器打开成品比较。"
    assert APP.count(note) == 1                       # 单一共享文案，随面板注入
    assert "const AUDITION_NOTE" in APP
    assert "helpBody.innerHTML = c.html + AUDITION_NOTE;" in APP
    # 不再有暗示实时试听的旧表述
    for phrase in ("听过再调", "打开试试", "试效果", "建议戴耳机比较"):
        assert phrase not in APP
    # 面板文案改为：处理后生成成品，再用外部播放器比较
    assert "处理完成后用播放器比较，再调下一项" in APP
    assert "比较成品时建议戴耳机" in APP
    # 文案保持加粗高亮
    assert "<b>" in APP[APP.index("const AUDITION_NOTE"):APP.index("const AUDITION_NOTE") + 200]


def test_asset_versions_bumped_for_cache_busting():
    assert 'style.css?v=48' in HTML
    assert 'app.js?v=46' in HTML
