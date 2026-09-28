/* ShadowBuster — 前端逻辑 + QWebChannel 桥接 + 处理特效 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    processing: false,
    files: [],
    selectedFile: null,
    view: "process",
    output: "",
    reference: "",
    eq: "Neutral",
    startTime: 0,
    fi: 0, ftotal: 0, si: 0, frac: 0,
    currentFileIndex: -1,
    fileStatuses: [],
  };

  /* ─── 桥接（QWebChannel 信号模式）─── */
  let api = null;
  const ready = new Promise((resolve) => {
    if (window.pyApi) { api = window.pyApi; resolve(); return; }
    new QWebChannel(qt.webChannelTransport, (channel) => {
      api = channel.objects.bridge;
      window.pyApi = api;
      api.fileProgress.connect((fi, ftotal, fname) => {
        const changedFile = state.currentFileIndex !== fi;
        state.fi = fi; state.ftotal = ftotal;
        if (changedFile) {
          state.currentFileIndex = fi;
          state.si = 0; state.frac = 0; progDisplay = 0;
          setFileStatus(fi, "processing", true);
          updateRemainingCount();
        }
        state.lastFracAt = Date.now();
      });
      api.stageChanged.connect((i, frac, label) => {
        state.si = i; state.frac = frac;
        state.lastFracAt = Date.now();
        setStage(i, frac);
      });
      api.fileFinished.connect((fi, ftotal, fname, succeeded, error) => {
        state.fi = fi; state.ftotal = ftotal;
        state.frac = 1;
        progDisplay = 100;
        processBtn.style.setProperty("--btn-progress", "100%");
        setFileStatus(fi, succeeded ? "done" : "fail");
      });
      // 质量摘要经磁盘报告进入报告页；前端不再维护面板载荷（守卫连接已随面板退役）
    if (api.previewOutputPeaks) api.previewOutputPeaks.connect((raw) => onPreviewOutputPeaks(raw));
    if (api.draftReady) api.draftReady.connect(onDraftReady);
    if (api.draftFailed) api.draftFailed.connect(onDraftFailed);
    if (api.draftChunkReady) api.draftChunkReady.connect(raw=>{
      const p=JSON.parse(raw); if(p.session!==draft.id || p.generation!==draft.player.generation)return;
      if(p.error) { draft.player.stop(); draft.ready=false; cacheState('error','试听缓存读取失败'); playerError(p.error); paintTransport(); return; }
      draft.player.chunk(p);
    });
    if (api.draftProgress) api.draftProgress.connect(onDraftProgress);
    if (api.audioInfoReady) api.audioInfoReady.connect(onAudioInfo);
    if (api.playerWaveformReady) api.playerWaveformReady.connect(onPlayerWaveform);
      // logLine 不再渲染（UI 禁止显示任何日志）
      api.done.connect((path, ok, fail, errText) => {
        fx.setActive(false);
        // 进度条先走满 100%，停顿一下，再复位按钮状态
        processBtn.style.setProperty("--btn-progress", "100%");
        stopProgressLoop();
        processBtn.disabled = true;   // 停顿期间防误触
        document.querySelectorAll(".stage").forEach((s) => {
          s.classList.remove("active", "error", "done");
          if (fail > 0) s.classList.add("error"); else s.classList.add("done");
        });
        // 仅失败时弹窗；无错误不显示任何日志/提示，静默完成
        if (fail > 0) {
          showErrorModal(
            `处理结束，但存在失败：成功 ${ok} / 失败 ${fail}。`,
            `${errText || "未捕获到详细错误。"}`,
            path
          );
        }
        setTimeout(finishProcessing, 1300);
      });
      api.failed.connect((msg) => {
        fx.setActive(false);
        failStages();
        showErrorModal("处理过程发生错误，未能完成。", msg || "", "");
        finishProcessing();
      });
      // OS 文件拖入：Qt 层取路径后推给前端队列
      api.filesDropped.connect((paths) => addPaths(paths));
      api.dragHover.connect((on) => {
        const q = $("player-file-picker");
        if (q) q.classList.toggle("drop-hover", !!on);
      });
      api.updateInfo.connect((raw) => onUpdateInfo(raw));
      api.gpuStatus.connect((raw) => onGpuStatus(raw));
      if (api.cacheStatus) api.cacheStatus.connect((raw) => onCacheStatus(raw));
      resolve();
    });
  });

  /* ─── 参数持久化：像真实效果器一样记住上次参数 ─── */
  const STORAGE_PREFIX = "sb_param_";
  const UNIT_DIVISORS = { vocal: 2, guidance: 10, space_width: 2, sat: 10, trans: 10, space: 10, denoise: 10 };
  let actualUnits = null;
  function loadValue(key, fallback) {
    if (actualUnits && Object.hasOwn(UNIT_DIVISORS, key)) return actualUnits[key] ?? fallback;
    try {
      const raw = localStorage.getItem(STORAGE_PREFIX + key);
      return raw === null ? fallback : raw;
    } catch (e) { return fallback; }
  }
  function saveValue(key, value) {
    if (actualUnits && Object.hasOwn(UNIT_DIVISORS, key)) {
      actualUnits[key] = String(value);
      // 数值与单位标记保存在同一个快照中；写入失败不留下半迁移状态。
      key = "actual_units";
      value = JSON.stringify({ version: 1, values: actualUnits });
    }
    try { localStorage.setItem(STORAGE_PREFIX + key, String(value)); } catch (e) {}
  }
  function loadNumber(key, fallback) {
    const raw = loadValue(key, null);
    if (raw === null) return fallback;
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : fallback;
  }

  // 一次性读取旧档位，不覆盖旧键；存储不可用时仍在内存中使用真实值。
  try {
    const snapshot = JSON.parse(loadValue("actual_units", "null"));
    if (snapshot && snapshot.values &&
        typeof snapshot.values === "object" && !Array.isArray(snapshot.values)) {
      if (snapshot.version === 1) {
        // v1 快照的 space_width 是 dB；宽度语义改为 0-1 授权比例（100% = +12dB）。
        const db = Number.parseFloat(snapshot.values.space_width);
        if (Number.isFinite(db)) snapshot.values.space_width = String(db / 12);
        saveValue("actual_units", JSON.stringify({ version: 2, values: snapshot.values }));
      }
      if (snapshot.version === 2) actualUnits = snapshot.values;
    }
  } catch (e) {}
  if (!actualUnits) {
    const values = {};
    const version = Number.parseInt(loadValue("param_units_version", ""), 10) || 0;
    Object.entries(UNIT_DIVISORS).forEach(([key, divisor]) => {
      const value = Number.parseFloat(loadValue(key, ""));
      if (!Number.isFinite(value)) return;
      let actual = version >= 1 ? value : value / divisor;
      if (key === "space_width") actual = actual / 12;   // dB → 0-1 授权比例
      values[key] = String(actual);
    });
    actualUnits = values;
    saveValue("actual_units", JSON.stringify({ version: 2, values }));
  }

  /* ─── 主题：深/浅色 + 强调色（localStorage 持久化，首帧由 index.html 引导脚本应用）─── */
  const ACCENTS = [
    { id: "violet", name: "暗紫", sw: "#4f378b" },
    { id: "indigo", name: "靛蓝", sw: "#2f4d9e" },
    { id: "ember",  name: "酒红", sw: "#7a2740" },
    { id: "moss",   name: "墨绿", sw: "#2f5e46" },
    { id: "amber",  name: "琥珀", sw: "#8a5a1e" },
  ];
  const CUSTOM_KEY = "ui_accent_custom";
  function hexToHsl(hex) {
    const n = hex.replace("#", "");
    const r = parseInt(n.slice(0, 2), 16) / 255;
    const g = parseInt(n.slice(2, 4), 16) / 255;
    const b = parseInt(n.slice(4, 6), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    const l = (max + min) / 2;
    let h = 0, s = 0;
    if (d > 0) {
      s = d / (1 - Math.abs(2 * l - 1));
      if (max === r) h = 60 * (((g - b) / d) % 6);
      else if (max === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
    }
    if (h < 0) h += 360;
    return { h, s: s * 100, l: l * 100 };
  }
  function hslToHex(h, s, l) {
    s /= 100; l /= 100;
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
    const to = (x) => Math.round(x * 255).toString(16).padStart(2, "0");
    return "#" + to(f(0)) + to(f(8)) + to(f(4));
  }
  const theme = (() => {
    const root = document.documentElement;
    const rootStyle = root.style;
    let mode = root.dataset.mode === "light" ? "light" : "dark";
    /* 主题默认色迁移：1.4.2 起默认强调色改为红色（ember）。
       仅首次执行一次，把旧的 violet 默认覆盖为红色，之后保留用户选择。 */
    if (loadValue("ui_accent_migrated", "") !== "1") {
      saveValue("ui_accent", "ember");
      saveValue("ui_accent_migrated", "1");
    }
    let accent = (root.dataset.accent === "custom" || ACCENTS.some((a) => a.id === root.dataset.accent))
      ? root.dataset.accent : "ember";
    const notify = () => document.dispatchEvent(new CustomEvent("sb-theme"));
    /* 自定义主题色：内联变量覆盖样式表。强调色直接取所选拾色值，中性色按
       色相派生（饱和度/亮度公式与内置主题的 CSS 生成规则完全一致），
       浅色模式下一组主色显式取浅色中性值。 */
    const INLINE_VARS = ["--c-accent", "--c-accent-hi", "--c-accent-rgb", "--logo-hue-rotate",
      "--fx-hue-1", "--fx-hue-2", "--c-bg", "--c-panel", "--c-panel-2", "--c-key", "--c-border",
      "--n-head", "--n-code", "--n-dot", "--n-radial", "--n-knob1", "--n-knob2", "--n-knob-b",
      "--n-scr1", "--n-scr2", "--n-scr3", "--n-scr-b"];
    const LIGHT_NEUTRALS = { "--c-bg": "#f1f0f1", "--c-panel": "#fafafa", "--c-panel-2": "#ffffff",
      "--c-key": "#e9e8ea", "--c-border": "#d6d5d8" };
    const DARK_NEUTRALS = [["--c-bg", 10, 8], ["--c-panel", 11, 14], ["--c-panel-2", 11, 18],
      ["--c-key", 12, 16], ["--c-border", 10, 22], ["--n-head", 12, 21], ["--n-code", 13, 9],
      ["--n-dot", 11, 11], ["--n-radial", 14, 9], ["--n-knob1", 11, 23], ["--n-knob2", 10, 11],
      ["--n-knob-b", 10, 29], ["--n-scr1", 6, 38], ["--n-scr2", 9, 19], ["--n-scr3", 12, 9],
      ["--n-scr-b", 14, 5]];
    function applyCustom() {
      const hex = loadValue(CUSTOM_KEY, "#4f378b");
      const { h, s, l } = hexToHsl(hex);
      const hi = hslToHex(h, Math.min(s, 60), Math.min(68, Math.max(30, l + 15)));
      const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
      const set = (k, v) => rootStyle.setProperty(k, v);
      set("--c-accent", hex);
      set("--c-accent-hi", hi);
      set("--c-accent-rgb", `${r}, ${g}, ${b}`);
      set("--logo-hue-rotate", Math.round(((h - 262 + 540) % 360) - 180) + "deg");
      set("--fx-hue-1", Math.round(h));
      set("--fx-hue-2", Math.round((h + 330) % 360));
      if (mode === "light") {
        Object.keys(LIGHT_NEUTRALS).forEach((k) => set(k, LIGHT_NEUTRALS[k]));
      } else {
        DARK_NEUTRALS.forEach(([k, s2, l2]) => set(k, hslToHex(h, s2, l2)));
      }
    }
    function clearCustom() {
      INLINE_VARS.forEach((k) => rootStyle.removeProperty(k));
    }
    function apply() {
      root.dataset.mode = mode;
      root.dataset.accent = accent;
      if (accent === "custom") applyCustom(); else clearCustom();
      notify();   // fx canvas 等读取 CSS 变量的模块按事件刷新
    }
    apply();
    function setAccentInternal(id) {
      if (id === accent) return;
      if (id !== "custom" && !ACCENTS.some((a) => a.id === id)) return;
      accent = id;
      saveValue("ui_accent", accent);
      apply();
    }
    return {
      apply,
      get mode() { return mode; },
      get accent() { return accent; },
      get customColor() { return loadValue(CUSTOM_KEY, "#4f378b"); },
      setMode(v) {
        if (v === mode) return;
        mode = v === "light" ? "light" : "dark";
        saveValue("ui_mode", mode);
        apply();
        if (api && api.setNativeTheme) {
          try { api.setNativeTheme(mode); } catch (e) {}
        }
      },
      setAccent(id) { setAccentInternal(id); },
      setCustomColor(hex) {
        if (!/^#[0-9a-fA-F]{6}$/.test(hex)) return;
        saveValue(CUSTOM_KEY, hex);
        if (accent === "custom") apply(); else setAccentInternal("custom");
      },
    };
  })();

  /* ─── 硬件面板：注入四角螺丝（共享契约，纯装饰）─── */
  document.querySelectorAll(".hdw").forEach((panel) => {
    ["tl", "tr", "bl", "br"].forEach((pos) => {
      const s = document.createElement("span");
      s.className = `screw ${pos}`;
      s.setAttribute("aria-hidden", "true");
      panel.appendChild(s);
    });
  });

  /* ─── 必填提示：控件级红框，平滑闪烁一次后熄灭 ───
     ⚠ 不触碰元素的 animation（避免顶掉面板入场动画）；
     此 QtWebEngine 的 CSS transition 不插值，改用 Web Animations API
     （JS 驱动、与 CSS 动画同源，实测可用）做一次平滑光晕。 */
  function announce(msg) {
    const el = $("live");
    if (el) {
      el.textContent = "";
      requestAnimationFrame(() => { el.textContent = msg; });
    }
  }
  const needTimers = new WeakMap();
  function markNeed(el) {
    announce(`缺少输入：${el.dataset.need || "请完成标红的设置"}`);
    clearNeed(el);
    el.classList.add("need");
    const anim = el.animate([
      { offset: 0.0, boxShadow: "0 0 0 0 rgba(255,77,79,0)",        borderColor: "#4a2a2c" },
      { offset: 0.32, boxShadow: "0 0 0 2px rgba(255,77,79,0.55), 0 0 30px rgba(255,77,79,0.75)", borderColor: "rgb(255,77,79)" },
      { offset: 0.62, boxShadow: "0 0 0 2px rgba(255,77,79,0.4), 0 0 16px rgba(255,77,79,0.5)",  borderColor: "rgb(255,77,79)" },
      { offset: 1.0, boxShadow: "0 0 0 0 rgba(255,77,79,0)",        borderColor: "#4a2a2c" },
    ], { duration: 1350, easing: "ease-in-out" });
    el._needAnim = anim;
    anim.onfinish = () => { el.classList.remove("need"); el._needAnim = null; };
    needTimers.set(el, setTimeout(() => {   // 兜底：异常中断也保证熄灭
      el.classList.remove("need");
      if (el._needAnim) { el._needAnim.cancel(); el._needAnim = null; }
    }, 2200));
  }
  function clearNeed(el) {
    if (needTimers.has(el)) { clearTimeout(needTimers.get(el)); needTimers.delete(el); }
    if (el._needAnim) { el._needAnim.cancel(); el._needAnim = null; }
    el.classList.remove("need");
  }

  /* ─── 阶段与当前文件进度 ─── */
  const STAGE_COUNT = Math.max(1, document.querySelectorAll(".stage").length);
  function setStage(i, frac) {
    document.querySelectorAll(".stage").forEach((s, idx) => {
      s.classList.toggle("active", idx === i);
      s.classList.toggle("done", idx < i);
    });
  }
  /* 精细进度：后端阶段内真实进度（高频/demucs 流式上报阶段内 0~1 的中间值）；
     无真实数据的阶段（低频/母带）由 rAF 缓慢向阶段上界趋近，进度条始终在动 */
  state.lastFracAt = 0;
  let progDisplay = 0, progRaf = 0;
  function realPct() {
    return ((state.si + state.frac) / STAGE_COUNT) * 100;
  }
  function progressLoop() {
    if (!state.processing) return;
    const real = realPct();
    const stageTop = ((state.si + 1) / STAGE_COUNT) * 100;
    if (Date.now() - state.lastFracAt > 700) {
      const margin = Math.min(1.5, 100 / STAGE_COUNT / 10);
      const target = Math.max(real, stageTop - margin);
      progDisplay += (target - progDisplay) * 0.004;
    } else {
      progDisplay = Math.max(progDisplay, real);
    }
    progDisplay = Math.max(0, Math.min(100, progDisplay));
    processBtn.style.setProperty("--btn-progress", `${progDisplay}%`);
    progRaf = requestAnimationFrame(progressLoop);
  }
  function startProgressLoop() { if (!progRaf) progRaf = requestAnimationFrame(progressLoop); }
  function stopProgressLoop() { cancelAnimationFrame(progRaf); progRaf = 0; }
  function failStages() {
    document.querySelectorAll(".stage").forEach((s) => {
      if (s.classList.contains("active")) s.classList.add("error");
    });
  }

  /* ─── 旋钮：慢速 + 指针/高亮同原点（-135° 起，270° 行程）─── */
  const KNOB_RANGE = 270;
  function snapToStep(value, min, max, step) {
    const places = Math.max(0, (String(step).split(".")[1] || "").length);
    const snapped = Number((Math.round((value - min) / step) * step + min).toFixed(places));
    return Math.max(min, Math.min(max, snapped));
  }

  function bindKnob(knobEl, valueEl, fmt, storeKey, onChange) {
    const min = +knobEl.dataset.min, max = +knobEl.dataset.max;
    const step = +knobEl.dataset.step, def = +knobEl.dataset.default;
    let value = def;
    if (storeKey) {
      value = snapToStep(loadNumber(storeKey, def), min, max, step);
    }
    function angleOf(v) { return -135 + (v - min) / (max - min) * KNOB_RANGE; }
    function render() {
      knobEl.style.setProperty("--knob-a", angleOf(value));
      knobEl.style.setProperty("--knob-p", (value - min) / (max - min));
      const text = fmt(value);
      knobEl.setAttribute("aria-valuenow", value);
      knobEl.setAttribute("aria-valuetext", text);
      valueEl.textContent = text;
      if (storeKey) saveValue(storeKey, value);
      if (onChange) onChange(value);
    }
    let dragging = false, lastY = 0, dragAccum = 0;
    knobEl.addEventListener("pointerdown", (e) => {
      if(knobEl.getAttribute('aria-disabled')==='true')return;
      dragging = true; lastY = e.clientY;
      knobEl.setPointerCapture(e.pointerId);
    });
    knobEl.addEventListener("pointermove", (e) => {
      if (!dragging || knobEl.getAttribute('aria-disabled')==='true') return;
      const dy = lastY - e.clientY; lastY = e.clientY;
      // 慢速：灵敏度 0.35，累加阈值 8px 才步进
      dragAccum += dy * 0.35;
      if (Math.abs(dragAccum) >= 8) {
        const delta = Math.sign(dragAccum) * Math.max(1, Math.round(Math.abs(dragAccum) / 8));
        dragAccum = 0;
        value = snapToStep(value + delta * step, min, max, step);
        render();
      }
    });
    const end = () => { dragging = false; dragAccum = 0; };
    knobEl.addEventListener("pointerup", end);
    knobEl.addEventListener("pointercancel", end);
    knobEl.addEventListener("dblclick", () => { if(knobEl.getAttribute('aria-disabled')==='true')return; value = def; render(); });
    knobEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      if(knobEl.getAttribute('aria-disabled')==='true')return;
      value = snapToStep(value + (e.deltaY < 0 ? step : -step), min, max, step);
      render();
    }, { passive: false });
    knobEl.addEventListener("keydown", (e) => {
      if(knobEl.getAttribute("aria-disabled")==="true")return;
      const d = e.key === "ArrowUp" || e.key === "ArrowRight" ? step
              : e.key === "ArrowDown" || e.key === "ArrowLeft" ? -step : 0;
      if (d) { e.preventDefault(); value = snapToStep(value + d, min, max, step); render(); }
    });
    render();
    const get = () => value;
    // 程序化设值（预设应用）：与手动调节同一条渲染/持久化路径。
    get.set = (v) => { value = snapToStep(Number(v), min, max, step); render(); };
    return get;
  }

  function bindFader(faderEl, valueEl, fmt, storeKey) {
    const min = +faderEl.dataset.min, max = +faderEl.dataset.max;
    const step = +faderEl.dataset.step, def = +faderEl.dataset.default;
    let value = storeKey ? snapToStep(loadNumber(storeKey, def), min, max, step) : def;

    function setFromClientX(clientX) {
      const rect = faderEl.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      value = snapToStep(min + ratio * (max - min), min, max, step);
      render();
    }
    function render() {
      const ratio = (value - min) / (max - min);
      const text = fmt(value);
      faderEl.style.setProperty("--fader-p", ratio);
      faderEl.setAttribute("aria-valuenow", value);
      faderEl.setAttribute("aria-valuetext", text);
      valueEl.textContent = text;
      if (storeKey) saveValue(storeKey, value);
    }

    let dragging = false;
    faderEl.addEventListener("pointerdown", (e) => {
      dragging = true;
      faderEl.setPointerCapture(e.pointerId);
      setFromClientX(e.clientX);
    });
    faderEl.addEventListener("pointermove", (e) => { if (dragging) setFromClientX(e.clientX); });
    const end = () => { dragging = false; };
    faderEl.addEventListener("pointerup", end);
    faderEl.addEventListener("pointercancel", end);
    faderEl.addEventListener("dblclick", () => { if(faderEl.getAttribute('aria-disabled')==='true')return; value = def; render(); });
    faderEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      if(faderEl.getAttribute('aria-disabled')==='true')return;
      value = snapToStep(value + (e.deltaY < 0 ? step : -step), min, max, step);
      render();
    }, { passive: false });
    faderEl.addEventListener("keydown", (e) => {
      const delta = e.key === "ArrowUp" || e.key === "ArrowRight" ? step
                  : e.key === "ArrowDown" || e.key === "ArrowLeft" ? -step : 0;
      if (delta) {
        e.preventDefault();
        value = Math.max(min, Math.min(max, value + delta));
        render();
      }
    });
    render();
    const get = () => value;
    // 程序化设值（预设应用）：与手动调节同一条渲染/持久化路径。
    get.set = (v) => { value = snapToStep(Number(v), min, max, step); render(); };
    return get;
  }

  /* ─── 声场宽度：扇形计量控件（canvas 绘制：发光边 + 内向渐变 + 粒子 + 发射线）─── */
  // 满扇形半角 50°（总张角 100°）；apex 固定在底部中点，上方留出铭牌条。
  const WIDTH_FAN = { inset: 3, apexBottom: 0, maxHalfDeg: 50, rays: 25, seed: 20260901 };
  let widthThemeBound = false;
  let widthMeterEl = null, widthSpread = 0, widthSpreadTarget = 0, widthSpreadRaf = 0, widthLastFrac = 0;
  let widthDisplayFrac = 0, widthTargetFrac = 0, widthAnimRaf = 0;

  function widthAccentRgb() {
    const v = getComputedStyle(document.documentElement).getPropertyValue("--c-accent-rgb").trim();
    return v || "138, 99, 255";
  }

  function rayLength(px, py, phi, w) {
    const inset = WIDTH_FAN.inset;
    const sin = Math.sin(phi), cos = Math.cos(phi);
    let t = (py - inset) / cos;                          // 上边界
    if (sin > 1e-6) t = Math.min(t, (w - inset - px) / sin);
    if (sin < -1e-6) t = Math.min(t, (inset - px) / sin);
    return t;
  }

  function sectorGeometry(px, py, half, w, h) {
    const inset = WIDTH_FAN.inset;
    const t = Math.tan(half);
    const dxTop = (py - inset) * t;
    if (dxTop >= px - inset) {
      const yWall = py - (px - inset) / t;
      return {
        pts: [[px, py], [inset, yWall], [inset, inset], [w - inset, inset], [w - inset, yWall]],
        edges: [[[px, py], [inset, yWall]], [[px, py], [w - inset, yWall]]],
      };
    }
    const xL = px - dxTop, xR = px + dxTop;
    return {
      pts: [[px, py], [xL, inset], [xR, inset]],
      edges: [[[px, py], [xL, inset]], [[px, py], [xR, inset]]],
    };
  }

  function widthSpreadFrame(now = performance.now()) {
    widthSpread += (widthSpreadTarget - widthSpread) * 0.14;
    if (Math.abs(widthSpreadTarget - widthSpread) < 0.004) widthSpread = widthSpreadTarget;
    const canvas = widthMeterEl && widthMeterEl.querySelector(".width-fan");
    if (canvas) drawWidthFan(canvas, widthDisplayFrac);
    widthSpreadRaf = (widthSpread !== widthSpreadTarget || widthSpreadTarget > 0)
      ? requestAnimationFrame(widthSpreadFrame) : 0;
  }
  function setWidthHover(on) {
    widthSpreadTarget = on ? 1 : 0;
    if (!widthSpreadRaf) widthSpreadRaf = requestAnimationFrame(widthSpreadFrame);
  }
  function widthAnimFrame() {
    widthDisplayFrac += (widthTargetFrac - widthDisplayFrac) * 0.18;
    if (Math.abs(widthTargetFrac - widthDisplayFrac) < 0.001) widthDisplayFrac = widthTargetFrac;
    if (widthMeterEl) drawWidthFan(widthMeterEl.querySelector(".width-fan"), widthDisplayFrac);
    widthAnimRaf = (widthDisplayFrac !== widthTargetFrac) ? requestAnimationFrame(widthAnimFrame) : 0;
  }
  function animateWidthTo(frac) {
    widthTargetFrac = frac;
    if (!widthAnimRaf) widthAnimRaf = requestAnimationFrame(widthAnimFrame);
  }
  // 主题切换重绘：不经 rAF 直接按当前分数重画一帧——rAF 在窗口被遮挡/节流
  // 时可能不触发，画布会停留在旧主题色的最后一帧（声场颜色"没切过去"）。
  function repaintWidthFan() {
    if (widthMeterEl) drawWidthFan(widthMeterEl.querySelector(".width-fan"), widthDisplayFrac);
  }

  function drawWidthFan(canvas, frac) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (w < 20 || h < 20) return;
    const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const inset = WIDTH_FAN.inset, apexBottom = WIDTH_FAN.apexBottom;
    const px = w / 2, py = h - apexBottom;
    const maxHalf = (Math.PI / 180) * WIDTH_FAN.maxHalfDeg;
    const half = maxHalf * frac;
    const rgb = widthAccentRgb();

    // 空余区域的发射细线：铺满整个 100° 范围，扇形填充会覆盖其中已激活部分
    ctx.strokeStyle = `rgba(${rgb}, 0.16)`;
    ctx.lineWidth = 1;
    for (let i = 0; i < WIDTH_FAN.rays; i++) {
      const phi = -maxHalf + (maxHalf * 2) * (i / (WIDTH_FAN.rays - 1));
      const len = rayLength(px, py, phi, w);
      if (len <= 2) continue;
      ctx.beginPath();
      ctx.moveTo(px + Math.sin(phi) * 2.5, py - Math.cos(phi) * 2.5);
      ctx.lineTo(px + Math.sin(phi) * len, py - Math.cos(phi) * len);
      ctx.stroke();
    }

    if (frac > 0.002) {
      const sector = sectorGeometry(px, py, half, w, h);
      // 内向渐变：边缘略亮，向 apex 渐隐
      ctx.beginPath();
      sector.pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.closePath();
      const grad = ctx.createRadialGradient(px, py, 0, px, py, Math.max(1, py - inset));
      grad.addColorStop(0, `rgba(${rgb}, 0.04)`);
      grad.addColorStop(0.7, `rgba(${rgb}, 0.16)`);
      grad.addColorStop(1, `rgba(${rgb}, 0.32)`);
      ctx.fillStyle = grad;
      ctx.fill();

      // hover 雷达扫描：窄线束从左向右单向扫描，后方带明显渐隐拖尾
      if (widthSpread > 0.01) {
        const phase = (performance.now() % 3600) / 3600;
        const sweep = -half + half * 2 * phase;
        const edgeFade = Math.min(1, phase / 0.14, (1 - phase) / 0.14);
        const tail = Math.min(half * 1.15, 0.92);
        ctx.save();
        ctx.beginPath();
        sector.pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
        ctx.closePath(); ctx.clip();
        for (let i = 34; i >= 1; i--) {
          const a = sweep - tail * (i / 34);
          if (a < -half || a > half) continue;
          const trailFade = 1 - i / 26;
          const len = rayLength(px, py, a, w);
          const ex = px + Math.sin(a) * len, ey = py - Math.cos(a) * len;
          // 单条尾巴：末端（外侧）最亮，向发射点（顶点）渐隐，
          // 叠起来后呈现“末端拖尾最长、发射点最短”的雷达拖尾
          const alpha = (0.05 + trailFade * 0.12) * widthSpread * edgeFade;
          const grad = ctx.createLinearGradient(px, py, ex, ey);
          grad.addColorStop(0, `rgba(${rgb}, ${(alpha * 0.04).toFixed(3)})`);
          grad.addColorStop(0.55, `rgba(${rgb}, ${(alpha * 0.4).toFixed(3)})`);
          grad.addColorStop(1, `rgba(${rgb}, ${alpha.toFixed(3)})`);
          ctx.strokeStyle = grad;
          ctx.lineWidth = 1.15 + trailFade * 1.6;
          ctx.shadowColor = `rgba(${rgb}, ${((0.22 + trailFade * 0.68) * widthSpread * edgeFade).toFixed(3)})`;
          ctx.shadowBlur = 3 + trailFade * 10;
          ctx.beginPath(); ctx.moveTo(px, py);
          ctx.lineTo(ex, ey);
          ctx.stroke();
        }
        const scanLen = rayLength(px, py, sweep, w);
        ctx.strokeStyle = `rgba(${rgb}, ${(0.95 * widthSpread * edgeFade).toFixed(3)})`;
        ctx.lineWidth = 1.55;
        ctx.shadowColor = `rgba(${rgb}, ${(0.95 * widthSpread * edgeFade).toFixed(3)})`;
        ctx.shadowBlur = 9;
        ctx.beginPath(); ctx.moveTo(px, py);
        ctx.lineTo(px + Math.sin(sweep) * scanLen, py - Math.cos(sweep) * scanLen);
        ctx.stroke(); ctx.restore();
      }
      ctx.save();
      ctx.strokeStyle = `rgba(${rgb}, 0.95)`;
      ctx.lineWidth = 1.6;
      ctx.shadowColor = `rgba(${rgb}, 0.8)`;
      ctx.shadowBlur = 9;
      for (const e of sector.edges) {
        ctx.beginPath();
        ctx.moveTo(e[0][0], e[0][1]);
        ctx.lineTo(e[1][0], e[1][1]);
        ctx.stroke();
      }
      ctx.restore();
    } else {
      // 0 宽度：仅一条中线细光柱
      ctx.save();
      ctx.strokeStyle = `rgba(${rgb}, 0.4)`;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(px, py - 2);
      ctx.lineTo(px, inset);
      ctx.stroke();
      ctx.restore();
    }
  }

  function bindWidthMeter(meterEl, fmt, storeKey) {
    widthMeterEl = meterEl;
    const min = +meterEl.dataset.min, max = +meterEl.dataset.max;
    const step = +meterEl.dataset.step, def = +meterEl.dataset.default;
    let value = storeKey ? snapToStep(loadNumber(storeKey, def), min, max, step) : def;
    const valueEl = meterEl.querySelector(".width-value");

    // 拖动映射：中线 = 0，右缘 = 最大；向右滑扩大、向左滑缩小，
    // 到 0 后继续向左被钳位（不再反向扩大）。
    function setFromClientX(clientX) {
      const rect = meterEl.getBoundingClientRect();
      const x0 = rect.left + rect.width / 2;
      const xMax = rect.left + rect.width - 3;
      const ratio = Math.max(0, Math.min(1, (clientX - x0) / Math.max(1, xMax - x0)));
      value = snapToStep(min + ratio * (max - min), min, max, step);
      render();
    }
    // 与右侧滑轨对齐：正方形顶边 = 第一条滑轨顶边，底边 = 第二条滑轨底边，
    // 高度正好覆盖两条滑轨的实际跨度（标签行不参与，避免视觉偏移）
    function syncMeterSize() {
      const stack = meterEl.closest(".fader-bank") &&
                    meterEl.closest(".fader-bank").querySelector(".fader-stack");
      if (!stack) return;
      const tracks = stack.querySelectorAll(":scope > .fader-wrap .fader");
      if (tracks.length < 2) return;
      const stackRect = stack.getBoundingClientRect();
      const top = tracks[0].getBoundingClientRect().top - stackRect.top;
      const bottom = tracks[tracks.length - 1].getBoundingClientRect().bottom - stackRect.top;
      // 宽度铭牌已位于正方形上方（与右侧标签行等高），
      // 正方形自身的偏移需减去标签占用的高度，避免被整体下推
      const label = meterEl.previousElementSibling;
      const labelH = label ? label.getBoundingClientRect().height : 0;
      let side = Math.round(bottom - top);
      if (side > 40) {
        meterEl.style.width = side + "px";
        meterEl.style.height = side + "px";
        meterEl.style.marginTop = Math.max(0, Math.round(top - labelH)) + "px";
      }
    }
    function render() {
      meterEl.style.setProperty("--wp", (value - min) / (max - min));
      syncMeterSize();
      widthLastFrac = (value - min) / (max - min);
      animateWidthTo(widthLastFrac);
      const text = fmt(value);
      meterEl.setAttribute("aria-valuenow", value);
      meterEl.setAttribute("aria-valuetext", text);
      valueEl.textContent = text;
      if (storeKey) saveValue(storeKey, value);
    }

    let dragging = false;
    meterEl.addEventListener("pointerdown", (e) => {
      dragging = true;
      meterEl.setPointerCapture(e.pointerId);
      setFromClientX(e.clientX);
    });
    meterEl.addEventListener("pointermove", (e) => { if (dragging) setFromClientX(e.clientX); });
    const end = () => { dragging = false; };
    meterEl.addEventListener("pointerup", end);
    meterEl.addEventListener("pointercancel", end);
    meterEl.addEventListener("dblclick", () => { if(meterEl.getAttribute('aria-disabled')==='true')return; value = def; render(); });
    meterEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      if(meterEl.getAttribute('aria-disabled')==='true')return;
      value = snapToStep(value + (e.deltaY < 0 ? step : -step), min, max, step);
      render();
    }, { passive: false });
    meterEl.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" || e.key === "ArrowUp" ? step
              : e.key === "ArrowLeft" || e.key === "ArrowDown" ? -step : 0;
      if (d) { e.preventDefault(); value = snapToStep(value + d, min, max, step); render(); }
    });
    meterEl.addEventListener("pointerenter", () => setWidthHover(true));
    meterEl.addEventListener("pointerleave", () => setWidthHover(false));
    if (!widthThemeBound) {
      // 深/浅色或主题色切换时重绘（颜色取自 CSS 变量）。sb-theme 由主题模块
      // 在 data-mode/data-accent 写入后同步派发，比属性观察器 + 动画帧可靠。
      widthThemeBound = true;
      document.addEventListener("sb-theme", repaintWidthFan);
      window.addEventListener("resize", () => render());
    }
    render();
    const get = () => value;
    // 程序化设值（预设应用）：与手动调节同一条渲染/持久化路径。
    get.set = (v) => { value = snapToStep(Number(v), min, max, step); render(); };
    return get;
  }

  const getQuality = bindKnob($("knob-quality"), $("val-quality"),
    (v) => (["快速", "标准", "精细"])[v] || "标准", "quality");
  const getGuidance = bindKnob($("knob-guidance"), $("val-guidance"), (v) => v.toFixed(1), "guidance");
  const getVocal = bindKnob($("knob-vocal"), $("val-vocal"),
    (v) => (v > 0 ? "+" : "") + v.toFixed(1) + " dB", "vocal");
  const getWidth = bindWidthMeter($("width-meter"), (v) => Math.round(v * 100) + " %", "space_width");
  const getSub = bindKnob($("knob-sub"), $("val-sub"), (v) => `+${v} dB`, "sub");
  const getSat = bindKnob($("knob-sat"), $("val-sat"), (v) => Math.round(v * 100) + "%", "sat");
  const getPunch = bindKnob($("knob-punch"), $("val-punch"), (v) => `+${v} dB`, "punch");
  const getTrans = bindKnob($("knob-trans"), $("val-trans"), (v) => Math.round(v * 100) + "%", "trans");
  const getSpace = bindFader($("fader-space"), $("val-space"), (v) => Math.round(v * 100) + "%", "space");
  const getDenoise = bindFader($("fader-denoise"), $("val-denoise"), (v) => Math.round(v * 100) + "%", "denoise");
  const getGuitar = bindKnob($("knob-guitar"), $("val-guitar"), (v) => Math.round(v * 100) + "%", "guitar");

  /* ─── 面板 bypass 开关：关闭 = 该面板对应阶段全部跳过，状态持久化 ─── */
  const BYPASS_CONTROLS = {
    "bp-lew": ["lew", "vocals"],
    "bp-bass-drums": ["bass", "drums"],
    "bp-reshape": ["reshape"],
    "bp-soren": ["soren"],
  };
  const bypassState = {};
  const bypassPainters = [];
  Object.entries(BYPASS_CONTROLS).forEach(([id, stages]) => {
    const btn = $(id);
    const rack = btn.closest(".rack");
    const enabled = stages.every((stage) => loadValue("bypass_" + stage, "1") === "1");
    stages.forEach((stage) => { bypassState[stage] = enabled; });

    function render() {
      btn.setAttribute("aria-checked", enabledForPanel() ? "true" : "false");
      rack.dataset.bypassed = enabledForPanel() ? "0" : "1";
    }

    function enabledForPanel() {
      return stages.every((stage) => bypassState[stage]);
    }

    render();
    bypassPainters.push(render);
    btn.addEventListener("click", () => {
      const next = !enabledForPanel();
      stages.forEach((stage) => {
        bypassState[stage] = next;
        saveValue("bypass_" + stage, next ? "1" : "0");
      });
      render();
    });
  });
  function activeBypassList() {
    return Object.entries(bypassState).filter(([, on]) => !on).map(([stage]) => stage);
  }
  // 程序化设值（预设应用）：更新状态后重绘全部面板开关。
  function setBypassList(list) {
    const off = new Set(list || []);
    Object.keys(bypassState).forEach((stage) => {
      bypassState[stage] = !off.has(stage);
      saveValue("bypass_" + stage, bypassState[stage] ? "1" : "0");
    });
    bypassPainters.forEach((paint) => paint());
  }


  /* ─── 自定义下拉组件（非原生，同一契约）─── */
  function buildDropdown(ddId, options, initial, onChange, storeKey) {
    const trigger = $(`${ddId}-trigger`);
    const label = $(`${ddId}-label`);
    const panel = $(`${ddId}-panel`);
    let value = initial;
    if (storeKey) {
      const saved = loadValue(storeKey, null);
      value = saved && options.some((o) => o.v === saved) ? saved : initial;
    }
    let open = false;

    function renderOptions() {
      panel.innerHTML = "";
      options.forEach((opt) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "dd-opt";
        b.dataset.v = opt.v;
        b.textContent = opt.label;
        b.setAttribute("role", "option");
        b.setAttribute("aria-selected", opt.v === value ? "true" : "false");
        b.addEventListener("click", () => {
          select(opt.v);
          close();
        });
        panel.appendChild(b);
      });
    }
    function setValue(v) {
      value = v;
      const opt = options.find((o) => o.v === v);
      label.textContent = opt ? opt.label : v;
      renderOptions();
    }
    function select(v) {
      setValue(v);
      if (onChange) onChange(v);
      if (storeKey) saveValue(storeKey, v);
    }
    function openPanel() {
      open = true;
      trigger.setAttribute("aria-expanded", "true");
      panel.classList.add("open");
      // fixed 定位浮层：按触发按钮的视口坐标放置，不参与页面滚动高度。
      // 若存在 transformed 祖先（如设置弹窗），该祖先成为 fixed 的包含块，
      // 需换算为相对包含块的坐标，否则面板会整体错位。
      const r = trigger.getBoundingClientRect();
      let host = null;
      for (let el = trigger.parentElement; el && el !== document.body; el = el.parentElement) {
        const cs = getComputedStyle(el);
        if (cs.transform !== "none" || cs.perspective !== "none" ||
            cs.filter !== "none" || (cs.willChange || "").includes("transform")) { host = el; break; }
      }
      if (host) {
        const hr = host.getBoundingClientRect();
        panel.style.left = `${Math.round(r.left - hr.left)}px`;
        panel.style.top = `${Math.round(r.bottom - hr.top + 2)}px`;
      } else {
        panel.style.left = `${Math.round(r.left)}px`;
        panel.style.top = `${Math.round(r.bottom + 2)}px`;
      }
      panel.style.width = `${Math.round(r.width)}px`;
      // 初始焦点到选中项
      const sel = panel.querySelector('[aria-selected="true"]');
      if (sel) sel.focus();
    }
    function close() {
      if (!open) return;
      open = false;
      trigger.setAttribute("aria-expanded", "false");
      panel.classList.remove("open");
    }
    function toggle() { open ? close() : openPanel(); }
    // 页面滚动 / 窗口尺寸变化时收起浮层（fixed 面板不会自己跟随视口）
    document.addEventListener("scroll", () => { if (open) close(); }, true);
    window.addEventListener("resize", () => { if (open) close(); });

    trigger.addEventListener("click", () => toggle());
    // 点击外部关闭
    document.addEventListener("pointerdown", (e) => {
      if (!e.target.closest(`#${ddId}`)) close();
    });
    // 键盘导航
    trigger.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
        e.preventDefault();
        if (!open) openPanel(); else { const f = panel.querySelector(".dd-opt"); if (f) f.focus(); }
      } else if (e.key === "Escape") {
        close(); trigger.focus();
      }
    });
    panel.addEventListener("keydown", (e) => {
      const opts = [...panel.querySelectorAll(".dd-opt")];
      const idx = opts.indexOf(document.activeElement);
      if (e.key === "ArrowDown") {
        e.preventDefault(); (opts[(idx + 1) % opts.length] || opts[0]).focus();
      } else if (e.key === "ArrowUp") {
        e.preventDefault(); (opts[(idx - 1 + opts.length) % opts.length] || opts[opts.length - 1]).focus();
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault(); if (document.activeElement) document.activeElement.click();
      } else if (e.key === "Escape") {
        close(); trigger.focus();
      }
    });

    setValue(value);
    const get = () => value;
    // 程序化同步（如设置弹窗中的缓存容量）：仅更新显示，不触发 onChange/持久化
    get.set = (v) => {
      const s = String(v);
      if (options.some((o) => o.v === s)) setValue(s);
    };
    return get;
  }

  // 2026-09-28 移除内置流派选择（参考曲目版权受限）；风格只来自用户参考音频。
  /* ─── 响度分段（与 EQ 风格同组件）─── */
  state.loudness = loadValue("loudness", "normal");
  const loudnessBtns = document.querySelectorAll(".seg-btn[data-loudness]");
  function paintLoudness() {
    loudnessBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.loudness === state.loudness)));
  }
  loudnessBtns.forEach((btn) => btn.addEventListener("click", () => {
    state.loudness = btn.dataset.loudness;
    saveValue("loudness", state.loudness);
    paintLoudness();
  }));
  paintLoudness();
  const getLoudness = () => state.loudness;
  function setLoudness(v) { state.loudness = String(v); saveValue("loudness", state.loudness); paintLoudness(); }

  // style_blend 保留存储/API 名称；数值控制 styled 处理力度，而非干湿波形比例。
  const getBlend = bindFader($("fader-blend"), $("val-blend"),
    (v) => Math.round(v * 100) + "%", "style_blend");

  /* ─── EQ 分段 ─── */
  state.eq = loadValue("eq", "Neutral");
  const eqBtns = document.querySelectorAll(".seg-btn[data-eq]");
  function paintEq() {
    eqBtns.forEach((btn) => btn.setAttribute("aria-pressed", btn.dataset.eq === state.eq ? "true" : "false"));
  }
  eqBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      state.eq = btn.dataset.eq;
      saveValue("eq", state.eq);
      paintEq();
    });
  });
  paintEq();
  function setEq(v) { state.eq = String(v); saveValue("eq", state.eq); paintEq(); }

  /* ─── 预设：采样器式固定槽位（8 格）───
     左键空槽 = 保存当前面板参数；左键已存槽 = 一键应用；右键已存槽两次 =
     用当前参数覆盖（第一次右键槽位上显示覆盖符号，再右键一次确认，4 秒
     未确认自动取消）。只快照面板控件（含面板 bypass 开关），不含文件队列/
     输出目录/参考路径——路径依赖具体机器，进预设会在别处失效。*/
  const PRESET_STORAGE_KEY = "sb_presets";
  const PRESET_ARM_MS = 4000;
  const PRESET_GETTERS = {
    quality: getQuality, guidance: getGuidance, sub: getSub, sat: getSat,
    punch: getPunch, trans: getTrans, vocal: getVocal, guitar: getGuitar,
    space: getSpace, denoise: getDenoise, space_width: getWidth,
    style_blend: getBlend, loudness: getLoudness, eq: () => state.eq,
    bypass: activeBypassList,
  };
  const PRESET_SETTERS = {
    quality: getQuality.set, guidance: getGuidance.set, sub: getSub.set,
    sat: getSat.set, punch: getPunch.set, trans: getTrans.set,
    vocal: getVocal.set, guitar: getGuitar.set, space: getSpace.set,
    denoise: getDenoise.set, space_width: getWidth.set,
    style_blend: getBlend.set, loudness: setLoudness, eq: setEq,
    bypass: setBypassList,
  };
  function readSlots() {
    try {
      const raw = JSON.parse(localStorage.getItem(PRESET_STORAGE_KEY) || "null");
      if (raw && raw.version === 2 && raw.slots &&
          typeof raw.slots === "object" && !Array.isArray(raw.slots)) return raw.slots;
    } catch (e) {}
    return {};
  }
  function writeSlots(slots) {
    try { localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify({ version: 2, slots })); } catch (e) {}
  }
  function capturePanel() {
    const p = {};
    for (const [k, get] of Object.entries(PRESET_GETTERS)) p[k] = get();
    return p;
  }
  function applyPanel(p) {
    for (const [k, set] of Object.entries(PRESET_SETTERS)) {
      if (p && p[k] !== undefined && p[k] !== null) set(p[k]);
    }
  }
  const padEls = [...document.querySelectorAll("#preset-pads .preset-pad")];
  let armedSlot = 0, armedTimer = 0;
  function paintPads() {
    const slots = readSlots();
    padEls.forEach((pad) => {
      const n = pad.dataset.slot;
      const data = slots[n];
      pad.classList.toggle("filled", !!data);
      pad.classList.toggle("armed", armedSlot === n);
      pad.querySelector(".pad-num").textContent = armedSlot === n ? "✕" : n;
      pad.title = armedSlot === n
        ? `槽位 ${n}：再次右键 = 移除该预设（恢复空槽）；左键 = 用当前参数覆盖；4 秒未确认自动取消`
        : data ? `槽位 ${n}：已存预设 — 点击应用；右键 = 移除/覆盖（待确认）`
               : `槽位 ${n}：空 — 点击保存当前面板参数`;
      pad.setAttribute("aria-label", pad.title);
    });
  }
  function disarmPad() {
    if (!armedSlot) return;
    armedSlot = 0;
    clearTimeout(armedTimer);
    paintPads();
  }
  padEls.forEach((pad) => {
    const n = pad.dataset.slot;
    pad.addEventListener("click", () => {
      const slots = readSlots();
      if (armedSlot === n) {           // 待确认态下左键：用当前参数覆盖该槽
        armedSlot = 0;
        clearTimeout(armedTimer);
        slots[n] = { savedAt: Date.now(), params: capturePanel() };
        writeSlots(slots);
        paintPads();
        return;
      }
      disarmPad();                     // 其他槽位动作前先取消待确认
      if (slots[n]) {
        applyPanel(slots[n].params);
      } else {
        slots[n] = { savedAt: Date.now(), params: capturePanel() };
        writeSlots(slots);
        paintPads();
      }
    });
    pad.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const slots = readSlots();
      if (!slots[n]) return;
      if (armedSlot === n) {           // 待确认态下再右键：移除，恢复空槽
        armedSlot = 0;
        clearTimeout(armedTimer);
        delete slots[n];
        writeSlots(slots);
        paintPads();
        return;
      }
      disarmPad();                     // 第一次右键：待确认，槽位显示 ✕
      armedSlot = n;
      paintPads();
      armedTimer = setTimeout(disarmPad, PRESET_ARM_MS);
    });
  });
  paintPads();

  /* ─── 队列管理 ─── */
  const fileListEl = $("file-list");
  const clearBtn = $("btn-clear");
  const fileMenu=$('player-file-menu'),fileTrigger=$('player-file-picker');
  function placeFileMenu(){
    const r=$('player-file-picker').getBoundingClientRect(),width=Math.min(420,innerWidth-24);
    fileMenu.style.width=`${width}px`;
    fileMenu.style.left=`${Math.max(12,Math.min(r.right-width,innerWidth-width-12))}px`;
    fileMenu.style.top=`${Math.max(12,Math.min(r.bottom+8,innerHeight-fileMenu.offsetHeight-12))}px`;
  }
  fileMenu.addEventListener('toggle',()=>{
    const open=fileMenu.matches(':popover-open');fileTrigger.setAttribute('aria-expanded',String(open));
    if(open)placeFileMenu();
  });
  fileMenu.addEventListener('beforetoggle',e=>{if(e.newState==='open')placeFileMenu();});
  window.addEventListener('resize',()=>{if(fileMenu.matches(':popover-open'))placeFileMenu();});
  $('content').addEventListener('scroll',()=>{if(fileMenu.matches(':popover-open'))placeFileMenu();});
  let lastFileWheel=0;
  $('player-file-picker').addEventListener('wheel',e=>{
    e.preventDefault();
    if(state.processing||draft.busy||!e.deltaY||state.files.length<2)return;
    const now=performance.now();if(now-lastFileWheel<140)return;lastFileWheel=now;
    const index=state.files.indexOf(state.selectedFile),next=Math.max(0,Math.min(state.files.length-1,index+(e.deltaY>0?1:-1)));
    if(next!==index){state.selectedFile=state.files[next];renderFileList(e.deltaY>0?1:-1);}
  },{passive:false});
  /* ─── 名称跑马灯：截断时 hover 快速往返；滚轮换曲时上下滚动交接 ─── */
  function mqInner(el) { return el ? el.querySelector(".mq") : null; }
  function mqStart(el) {
    const inner = mqInner(el);
    if (!inner) return;
    const span = inner.scrollWidth - inner.clientWidth;
    if (span <= 4) return;
    inner.getAnimations().forEach((a) => a.cancel());
    inner.classList.add("running");
    /* 两端各停一拍：只看位移的话，名字的头和尾都只是一闪而过 */
    inner.animate([
      { transform: "translateX(0)", offset: 0 },
      { transform: "translateX(0)", offset: 0.18 },
      { transform: `translateX(${-span}px)`, offset: 0.5 },
      { transform: `translateX(${-span}px)`, offset: 0.68 },
      { transform: "translateX(0)", offset: 1 },
    ], { duration: Math.max(1600, span * 12), iterations: Infinity, easing: "ease-in-out" });
  }
  function mqStop(el) {
    const inner = mqInner(el);
    if (!inner) return;
    inner.getAnimations().forEach((a) => a.cancel());
    inner.classList.remove("running");
  }
  function bindNameMarquee(el) {
    if (!el || el.dataset.marquee) return;
    el.dataset.marquee = "1";
    /* 悬停态自己记：滚轮换曲不产生 pointer 事件，:hover 在 WebEngine 里可能滞后 */
    el.addEventListener("pointerenter", () => { el.dataset.hovering = "1"; mqStart(el); });
    el.addEventListener("pointerleave", () => { delete el.dataset.hovering; mqStop(el); });
  }
  /* 刻度带跟着换曲方向滚过一个周期；周期与 style.css 的 10px 一致 */
  function rollTicks(rollDir) {
    if (!rollDir) return;
    document.querySelectorAll(".pf-ticks i").forEach((el) => {
      el.getAnimations().forEach((a) => a.cancel());
      el.animate([{ transform: "translateY(0)" }, { transform: `translateY(${-10 * rollDir}px)` }],
        { duration: 260, easing: "cubic-bezier(.25,.6,.35,1)" });
    });
  }
  let nameRollToken = 0, nameRollPending = null;
  function setPlayerFileName(text, rollDir) {
    const host = $("player-file-name"), inner = mqInner(host);
    if (!inner) { if (host) host.textContent = text; return; }
    if ((nameRollPending ?? inner.textContent) === text) return;
    if (!rollDir || matchMedia("(prefers-reduced-motion: reduce)").matches) {
      nameRollPending = null; mqStop(host); inner.textContent = text; return;
    }
    const token = ++nameRollToken;
    mqStop(host);
    rollTicks(rollDir);
    /* 真滚动：新旧两个名字贴成一条带子（.mq-roll），整条平移正好一个框高。
       不用透明度——带子是被框沿裁掉的，读起来就是名字从框外滚进来。
       滚轮比动画快，连着滚时拿上一段的目标名当“旧名”，免得跳过中间那首。 */
    const box = $("player-file-picker");
    const outgoing = nameRollPending ?? inner.textContent;
    nameRollPending = text;
    box.querySelectorAll(".mq-roll").forEach((el) => el.remove());
    const from = rollDir > 0 ? 0 : -100, to = rollDir > 0 ? -100 : 0;
    const roll = document.createElement("span");
    roll.className = "mq-roll";
    [outgoing, text].forEach((t) => {
      const row = document.createElement("span");
      row.textContent = t;
      roll.append(row);
    });
    if (rollDir < 0) roll.prepend(roll.lastElementChild);   // 上一首从上面下来
    roll.style.transform = `translateY(${from}%)`;
    inner.style.visibility = "hidden";
    box.append(roll);
    const anim = roll.animate(
      [{ transform: `translateY(${from}%)` }, { transform: `translateY(${to}%)` }],
      { duration: 260, easing: "cubic-bezier(.22,.61,.36,1)", fill: "forwards" });
    const land = () => {
      roll.remove();
      if (token !== nameRollToken) return;
      nameRollPending = null;
      inner.textContent = text;
      inner.style.visibility = "";
      if (host.dataset.hovering) mqStart(host);
    };
    anim.finished.then(land, land);
  }
  bindNameMarquee($('player-file-name'));
  const FILE_STATUS_LABELS = {
    pending: "待处理",
    processing: "处理中",
    done: "已完成",
    fail: "处理失败",
  };
  function setFileStatus(index, status, follow = false) {
    if (index < 0 || index >= state.files.length) return;
    state.fileStatuses[index] = status;
    const item = fileListEl.children[index];
    if (!item || !item.classList.contains("file-item")) return;
    item.classList.remove("processing", "done", "fail");
    if (status !== "pending") item.classList.add(status);
    if (status === "processing") item.setAttribute("aria-current", "true");
    else item.removeAttribute("aria-current");
    const name = item.querySelector(".fi-name")?.textContent || "";
    item.setAttribute("aria-label", `${name}，${FILE_STATUS_LABELS[status]}`);
    if (status !== "pending") announce(`${name}，${FILE_STATUS_LABELS[status]}`);
    if (follow) {
      item.scrollIntoView({
        block: "nearest",
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      });
    }
  }
  function renderFileList(rollDir) {
    fileListEl.innerHTML = "";
    if (!state.files.length) {
      fileListEl.innerHTML = '<span class="file-empty">尚未添加歌曲</span>';
      clearBtn.disabled = true;
    } else {
      clearBtn.disabled = false;
    }
    state.files.forEach((f, i) => {
      const name = f.replace(/\\/g, "/").split("/").pop();
      const item = document.createElement("div");
      const status = state.fileStatuses[i] || "pending";
      item.className = `file-item${status === "pending" ? "" : ` ${status}`}`;
      item.setAttribute("role", "listitem");
      item.setAttribute("aria-label", `${name}，${FILE_STATUS_LABELS[status]}`);
      if (status === "processing") item.setAttribute("aria-current", "true");
      const selected = state.selectedFile === f;
      if (selected) item.classList.add("selected");
      item.innerHTML = `<span class="fi-status" aria-hidden="true"></span>` +
        `<button type="button" class="fi-name" title="${escapeHtml(f)}" aria-pressed="${selected}"><span class="mq">${escapeHtml(name)}</span></button>` +
        `<button class="fi-x" aria-label="移除" title="移除">` +
        `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" fill="none" stroke-linecap="square"/></svg>` +
        `</button>`;
      bindNameMarquee(item.querySelector(".fi-name"));
      item.addEventListener("click", (ev) => {
        if (ev.target.closest(".fi-x")) return;    // 移除按钮不触发选中
        if(state.processing||draft.busy)return;
        if (state.selectedFile !== f) {
          state.selectedFile = f;
          renderFileList();
        }
        fileMenu.hidePopover();fileTrigger.focus();
      });
      item.querySelector(".fi-x").addEventListener("click", () => {
        if (state.processing||draft.busy) return;
        item.classList.add("leaving");
        setTimeout(() => {
          const index=state.files.indexOf(f);if(index<0)return;
          state.files.splice(index, 1);
          state.fileStatuses.splice(index, 1);
          if (state.selectedFile === f) state.selectedFile = state.files[0] || null;
          renderFileList();
        }, 150);
      });
      fileListEl.appendChild(item);
    });
    $("queue-count").textContent = `${state.files.length} 首`;
    setPlayerFileName(state.selectedFile?.replace(/\\/g,'/').split('/').pop()||'添加歌曲…',rollDir);
    if(fileMenu.matches(':popover-open'))placeFileMenu();
    refreshPreviewFiles();
    refreshReportVersions();
    if (state.files.length) {
      const queueCard = document.querySelector(".queue");
      if (queueCard) clearNeed(queueCard);
    }
  }
  function addPaths(paths) {
    if (state.processing || draft.busy || !paths || !paths.length) return;
    const additions = paths.filter((p) => !state.files.includes(p));
    if (!additions.length) return;
    state.files.push(...additions);
    state.selectedFile = additions[additions.length - 1];
    state.fileStatuses.push(...additions.map(() => "pending"));
    renderFileList();
  }
  async function addFiles() {
    if (state.processing||draft.busy) return;
    addPaths(await api.selectInputs());
  }
  $("btn-add").addEventListener("click", addFiles);
  $("btn-clear").addEventListener("click", () => {
    if (state.processing||draft.busy) return;
    state.files = [];
    state.selectedFile = null;
    state.fileStatuses = [];
    renderFileList();
  });

  /* ─── 输出 / 参考音频 ─── */
  state.output = loadValue("output", "");
  $("output-path").value = state.output;
  state.reference = loadValue("reference", "");
  $("ref-path").value = state.reference;

  async function chooseOutput() {
    if (state.processing) return;
    const path = await api.selectOutput();
    if (path) {
      state.output = path; $("output-path").value = path;
      saveValue("output", path); clearNeed($("output-path"));
      refreshPreviewOutputs();
      refreshReportVersions();
    }
  }
  async function chooseReference() {
    if (state.processing || $('ref-card').inert) return;
    const path = await api.selectReference();
    if (path) {
      state.reference = path; $("ref-path").value = path; saveValue("reference", path);
    }
  }
  $("output-card").addEventListener("click", chooseOutput);
  $("ref-card").addEventListener("click", chooseReference);
  $("output-card").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chooseOutput(); } });
  $("ref-card").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); chooseReference(); } });
  [$("output-card"), $("ref-card")].forEach((card) => {
    card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drop-hover"); });
    card.addEventListener("dragleave", () => card.classList.remove("drop-hover"));
    card.addEventListener("drop", (e) => { e.preventDefault(); card.classList.remove("drop-hover"); });
  });
  // Qt 层把拖入路径连同落点坐标推过来：按落点路由到输出目录 / 参考音频 / 队列
  window.__sbDropAt = (paths, x, y) => {
    if (!paths || !paths.length) return;
    const isAudio = (p) => /\.(wav|mp3|flac|ogg|m4a|aiff?|ape|wma)$/i.test(p);
    const hit = document.elementFromPoint(x, y);
    const card = hit && (hit.closest("#output-card") || hit.closest("#ref-card"));
    if (card === $("ref-card")) {
      if(card.inert)return;
      const audio = paths.find(isAudio);
      if (audio) { state.reference = audio; $("ref-path").value = audio; saveValue("reference", audio); }
      return;
    }
    if (card === $("output-card")) {
      const dir = paths.find((p) => !/\.[^.\\/]+$/.test(p.split(/[\\/]/).pop()));
      if (dir) { state.output = dir; $("output-path").value = dir; saveValue("output", dir); clearNeed($("output-path")); }
      return;
    }
    addPaths(paths.filter(isAudio));
  };


  /* ─── 主按钮：开始 / 停止（进度条）─── */
  const processBtn = $("btn-process");
  const processLabel = $("process-label");
  const processRemaining = $("process-remaining");
  const procIcon = $("proc-icon");
  function updateRemainingCount() {
    const remaining = Math.max(0, state.ftotal - state.fi - 1);
    processRemaining.textContent = `剩余 ${remaining} 个`;
  }
  const PROC_ICON_PLAY = '<path d="M8 5v14l11-7L8 5Z" fill="currentColor"/>';
  const PROC_ICON_STOP = '<path d="M7 7h10v10H7z" fill="currentColor"/>';

  /* ─── 按钮进度条星星特效：在进度填充区内闪烁的小星 + 发光 ─── */
  const btnFx = (() => {
    let canvas = null, ctx = null, raf = 0, stars = [];

    function drawStar(x, y, r, alpha, hue) {
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.shadowBlur = 8;
      ctx.shadowColor = `hsla(${hue}, 90%, 65%, ${alpha})`;
      ctx.strokeStyle = `hsla(${hue}, 90%, 75%, ${alpha})`;
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(x - r, y); ctx.lineTo(x + r, y);
      ctx.moveTo(x, y - r); ctx.lineTo(x, y + r);
      ctx.stroke();
      ctx.fillStyle = `hsla(${hue}, 90%, 80%, ${alpha * 0.85})`;
      ctx.beginPath();
      ctx.moveTo(x, y - r * 0.45);
      ctx.lineTo(x + r * 0.45, y);
      ctx.lineTo(x, y + r * 0.45);
      ctx.lineTo(x - r * 0.45, y);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }

    function tick() {
      const r = processBtn.getBoundingClientRect();
      const w = r.width, h = r.height;
      ctx.clearRect(0, 0, w, h);
      const prog = parseFloat(processBtn.style.getPropertyValue("--btn-progress")) || 0;
      const progX = (w * prog) / 100;
      const t = Date.now() / 1000;
      // 星星有生命周期：在已填充区域内随机落点、单次淡入淡出后消失，
      // 持续补新 → 星星会随进度扩散到整条进度条，而不是永远挤在最左端
      if (stars.length < 14 && Math.random() < 0.5) {
        stars.push({
          born: t,
          life: 1.1 + Math.random() * 1.4,
          x: Math.random() * Math.max(progX, 12),
          y: h * (0.2 + Math.random() * 0.6),
          r: 1.2 + Math.random() * 2.2,
          hue: Math.random() < 0.7 ? 150 : (Math.random() < 0.5 ? 190 : 45),
        });
      }
      stars = stars.filter((s) => t - s.born < s.life && s.x <= progX + 8);
      for (const s of stars) {
        const age = (t - s.born) / s.life;
        const a = Math.sin(Math.PI * Math.min(1, age));   // 一次平滑闪烁
        if (a > 0.03) drawStar(s.x, s.y, s.r, a, s.hue);
      }
      raf = requestAnimationFrame(tick);
    }

    return {
      start() {
        if (raf) return;
        if (!canvas) {
          canvas = document.createElement("canvas");
          canvas.className = "btn-fx";
          canvas.setAttribute("aria-hidden", "true");
          processBtn.appendChild(canvas);
          ctx = canvas.getContext("2d");
        }
        const r = processBtn.getBoundingClientRect();
        canvas.width = Math.max(1, Math.round(r.width * devicePixelRatio));
        canvas.height = Math.max(1, Math.round(r.height * devicePixelRatio));
        ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
        stars = [];
        raf = requestAnimationFrame(tick);
      },
      stop() {
        cancelAnimationFrame(raf); raf = 0;
        stars = [];
        if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      },
    };
  })();

  /* ─── 错误弹窗：任何错误以风格一致的窗口呈现；成功不显示任何日志 ─── */
  let errorCopyText = "";
  let errorCopyGeneration = 0;
  function showErrorModal(summary, detail, path) {
    errorCopyGeneration += 1;
    errorCopyText = [summary, path ? `产物目录：${path}` : "", detail].filter(Boolean).join("\n\n");
    $("err-copy").textContent = "复制错误信息";
    $("err-copy").disabled = false;
    const modal = $("err-modal");
    const body = $("err-body");
    modal.hidden = false;
    let html = `<div>${escapeHtml(summary)}</div>`;
    if (path) html += `<div>产物目录：${escapeHtml(path)}</div>`;
    if (detail) html += `<div class="err-code">${escapeHtml(detail)}</div>`;
    body.innerHTML = html;
    // 下一帧加 open 触发过渡动画
    requestAnimationFrame(() => modal.classList.add("open"));
  }
  function hideErrorModal() {
    const modal = $("err-modal");
    modal.classList.remove("open");
    setTimeout(() => { modal.hidden = true; }, 160);
  }
  $("err-copy").addEventListener("click", async () => {
    const button = $("err-copy");
    const generation = errorCopyGeneration;
    button.disabled = true;
    try {
      if (api && api.copyText) {
        const copied = await new Promise((resolve) => api.copyText(errorCopyText, resolve));
        if (!copied) throw new Error("Clipboard unavailable");
      } else {
        await navigator.clipboard.writeText(errorCopyText);
      }
      if (generation === errorCopyGeneration) button.textContent = "已复制";
    } catch (_) {
      if (generation === errorCopyGeneration) button.textContent = "复制失败，重试";
    } finally {
      if (generation === errorCopyGeneration) button.disabled = false;
    }
  });
  $("err-ok").addEventListener("click", hideErrorModal);
  $("err-close").addEventListener("click", hideErrorModal);
  $("err-backdrop").addEventListener("click", hideErrorModal);
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function finishProcessing() {
    state.processing = false;
    stopProgressLoop();
    progDisplay = 0;
    processBtn.disabled = false;
    processBtn.classList.remove("processing", "stop");
    procIcon.innerHTML = PROC_ICON_PLAY;
    processLabel.textContent = "BUSTER!";
    processRemaining.hidden = true;
    processBtn.style.setProperty("--btn-progress", "0%");
    btnFx.stop();
    renderCache();   // 恢复缓存行（清空 / 改容量）可用
    refreshPreviewOutputs();   // 新成品落盘：预览版本选择与报告随之更新
    refreshReportVersions();
  }
  function setProcessing() {
    state.processing = true;
    progDisplay = 0;
    processBtn.classList.add("processing", "stop");
    procIcon.innerHTML = PROC_ICON_STOP;
    processLabel.textContent = "停止";
    updateRemainingCount();
    processRemaining.hidden = false;
    btnFx.start();
    startProgressLoop();
    renderCache();   // 处理期间禁用缓存清空与容量修改
  }

  processBtn.addEventListener("click", () => {
    if (state.processing) {
      // 停止
      api.cancel();
      processLabel.textContent = "停止中…";
      processBtn.disabled = true;

    } else {
      startProcess();
    }
  });

  function collectParams() {
    // Shared controls; live draft processing intentionally approximates export DSP.
    return {
      reference: state.reference,
      quality: getQuality(), guidance: getGuidance(),
      sub: getSub(), sat: getSat(), punch: getPunch(), trans: getTrans(),
      space: getSpace(), denoise: getDenoise(), guitar: getGuitar(),
      space_width: getWidth(), vocal: getVocal(),
      bypass: activeBypassList(),
      // 2026-09-28 起无内置流派：有参考 → 参考母带；无参考 → 无风格/EQ-only。
      genre: "",
      style_mode: state.reference ? "styled" : (state.eq === "Neutral" ? "off" : "eq_only"),
      style_blend: getBlend(),
      loudness: getLoudness(), eq: state.eq,
    };
  }

  async function startProcess() {
    if (draft.busy) { announce("请先完成或取消草稿准备"); return; }
    stopTransport();
    // 缺失提示改为控件红框：无歌曲 → BUSTER 红框并打开文件弹窗；无输出目录 → 输出框红框
    if (!state.files.length) { fileMenu.showPopover();markNeed(fileMenu);return; }
    if (!state.output) { markNeed($("output-path")); return; }
    clearNeed(document.querySelector(".queue"));
    clearNeed($("output-path"));

    state.startTime = Date.now();
    state.fi = 0; state.ftotal = state.files.length; state.si = 0; state.frac = 0;
    state.currentFileIndex = -1;
    state.fileStatuses = state.files.map(() => "pending");
    renderFileList();
    fx.setActive(true);
    setProcessing();
    document.querySelectorAll(".stage").forEach((s) => s.classList.remove("active", "done", "error"));

    try {
      await api.process({
        files: state.files,
        output: state.output,
        reference: state.reference,
        ...collectParams(),
      });
    } catch (e) {
      fx.setActive(false);
      finishProcessing();
    }
  }

  /* ─── 参数说明浮层：打开 / 拖动 / 关闭 ─── */
  const HELP_CONTENT = {
    player: {title:'试听播放器',html:'<p>在版本选择右侧用滚轮切换歌曲，点击文件名框展开文件列表，可添加、移除或清空。切歌后暂停。原声可直接播放；点击草稿自动准备，准备中再点可取消。就绪后原声与草稿共用播放时钟，切换保持位置。</p><p>时间轴：暗色表示未准备，主题色逐步填充表示准备中，亮起表示缓存就绪，警示色表示需要重新准备。悬停时间轴可查看状态。</p><p>可实时调整重建引导、吉他、Sub、饱和、鼓身、瞬态、人声、声场、宽度、EQ和参考强度。降噪为简化模拟；响度采用正式档位目标和实时测量，实时限幅与正式导出仍有差异。</p><p>在时间尺上方拖选循环区间，拖两端调整、拖中间移动。双击循环带或聚焦后按 Enter 精确编辑；方向键移动区间，Delete 清除。下方时间尺用于播放定位。监听旋钮支持拖动、滚轮与方向键，双击恢复 100%，不影响导出。Δ 按钮仅在草稿就绪并选中时可用，监听草稿减去同步原声的差值（含增益、削减与相位变化），切换音源自动关闭。准备或草稿模式下质量档、参考选择与预设暂时锁定，切回原声可修改；其他旋钮实时生效。</p>'},
    lew: {
      title: "高频 · 怎么调",
      html:
        "<h3>质量档</h3><p><b>先用标准</b>。快速处理更快；<b>精细</b>会改用更细的六轨分层（吉他和合成器/键盘分开处理），时长接近；如果设备里没有六轨组件，会自动用回四轨。</p>" +
        "<h3>重建引导</h3><p>调高会加入更多修复后的细节。声音变尖或不自然时，<b>往回调一点</b>。</p>" +
        "<h3>人声</h3><p><b>听不清歌词就调高</b>，歌声太突出就调低。保持 0，会尽量保留原曲中歌声与伴奏的关系。</p>" +
        "<h3>操作</h3><p>上下拖动旋钮，或用滚轮调节。<b>双击恢复默认值</b>。</p>",
    },
    bass: {
      title: "低频 · 怎么调",
      html:
        "<h3>Sub 提升</h3><p>让低音更深、更有分量。<b>轰头、发闷就调低</b>。</p>" +
        "<h3>鼓身</h3><p>让鼓声更厚、更有力；<b>同时让低音在鼓点瞬间轻轻让位</b>（有硬上限，不会挖空低音）。鼓声盖过歌声时，调低一点。</p>" +
        "<h3>瞬态</h3><p>让每一下鼓点更清楚。敲击声太硬、太刺耳时，调低。</p>" +
        "<h3>谐波饱和</h3><p>让低音更饱满、更容易听见。<b>太多可能变粗糙</b>，先保持默认。</p>" +
        "<h3>自动清晰</h3><p><b>默认开启</b>：检测到低音发闷时，在增强前自动做小幅收敛（幅度由低频旋钮整体授权，上限 1.5dB）。<b>四个低频旋钮全部归零时，所有自动整理一并关闭</b>，低频保持原样。</p>" +
        "<h3>吉他</h3><p>让吉他更清楚、更亮、少一点浑浊（自动使用<b>六轨分层</b>）。<b>0% 完全不处理</b>；往右增强，太亮或发刺就回调。</p>" +
        "<h3>操作</h3><p><b>一次只调一项</b>，处理完成后用播放器比较，再调下一项。双击旋钮恢复默认值。</p>",
    },
    space: {
      title: "声场 · 怎么调",
      html:
        "<h3>宽度</h3><p>设置现有立体声背景可增加的最大宽度。低频和明显的鼓点起音会受到保护，所以<b>不是整曲均匀变宽</b>；声音散了就调低。</p>" +
        "<h3>声场</h3><p>控制空间处理的融入程度：既缩放宽度变化，也包含对持续拥挤的伴奏背景做<b>小幅自动整理</b>（只在实际发生堆积时动作，上限很小）。0% 时两者都关闭；<b>高频降噪独立生效</b>。</p>" +
        "<h3>高频降噪</h3><p>减轻背景里的沙沙声：只对<b>确认是稳定嘶声</b>的部分做小幅度处理，所以<b>没有嘶声的歌几乎不会有变化</b>；细节变少就调低，不是越高越好。</p>" +
        "<h3>面板开关</h3><p>关闭后，<b>展开效果、自动整理和降噪都停用</b>，原来的设置会保留。</p>" +
        "<h3>操作</h3><p>左右拖动调节，双击恢复默认值。<b>比较成品时建议戴耳机</b>。</p>",
    },
    soren: {
      title: "母带 · 怎么调",
      html:
        "<h3>参考音频</h3><p>拖入音频或点击选择一首<b>你想接近的歌</b>，音色方向会以参考为准；<b>响度仍由你选的响度档决定</b>，不会被参考的音量带走。不选参考就是无风格母带。</p>" +
        "<h3>响度</h3><p>轻柔更舒缓，标准适合日常，响亮更满、更响。<b>更响不等于更好听</b>。</p>" +
        "<h3>EQ 风格</h3><p><b>平直</b>：少改音色；<b>温暖</b>：更厚；<b>明亮</b>：更亮；<b>融合</b>：尝试更融合的整体音色。</p>" +
        "<h3>风格强度</h3><p>控制参考母带的处理力度，主要改变压缩、瞬态与密度，<b>需要先选参考音频</b>；它不是干湿混合，调低不会把两条不同相位的波形相加，音色和声场只做有限修正。拿不准就保持默认。</p>" +
        "<h3>怎么选</h3><p><b>先保持默认，再按喜好微调</b>。关闭面板可跳过这一步。</p>",
    },
  };
  /* 试听说明：所有帮助面板共用一段固定文案。正式批处理不做实时试听；
     片段级快速试听走顶部「预览」视图。文案保持一句、加粗、口语化。 */
  const AUDITION_NOTE =
    "<h3>试听</h3><p><b>在文件列表上方点击草稿准备缓存，即可全曲播放、拖动进度并实时调参。草稿为近似效果，正式成品以导出为准。</b></p>";
  const helpDialog = $("help-dialog");
  const helpTitle = $("help-title");
  const helpBody = $("help-body");
  let helpOpen = false;
  let helpTrigger = null;

  function placeHelpCentered() {
    const w = helpDialog.offsetWidth, h = helpDialog.offsetHeight;
    helpDialog.style.left = Math.max(8, Math.round((innerWidth - w) / 2)) + "px";
    helpDialog.style.top = Math.max(8, Math.round((innerHeight - h) / 2)) + "px";
  }
  function openHelp(key, trigger) {
    const c = HELP_CONTENT[key];
    if (!c) return;
    helpTitle.textContent = c.title;
    helpBody.innerHTML = c.html + AUDITION_NOTE;
    helpTrigger = trigger || helpTrigger;
    if (!helpOpen) {
      helpOpen = true;
      placeHelpCentered();
      helpDialog.classList.add("open");
    }
  }
  function closeHelp() {
    if (!helpOpen) return;
    helpOpen = false;
    helpDialog.classList.remove("open");
    if (helpTrigger) { helpTrigger.focus({ preventScroll: true }); helpTrigger = null; }
  }
  document.querySelectorAll(".rack-help").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (helpOpen && helpTitle.textContent === HELP_CONTENT[btn.dataset.help].title) {
        closeHelp();
      } else {
        openHelp(btn.dataset.help, btn);
      }
    });
  });
  $("help-close").addEventListener("click", closeHelp);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && helpOpen) closeHelp();
  });
  // 标题栏拖动
  const dragBar = $("help-drag");
  dragBar.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".help-close")) return;
    const r = helpDialog.getBoundingClientRect();
    const ox = e.clientX - r.left, oy = e.clientY - r.top;
    dragBar.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const w = helpDialog.offsetWidth;
      const x = Math.min(Math.max(8, ev.clientX - ox), innerWidth - w - 8);
      const y = Math.min(Math.max(8, ev.clientY - oy), innerHeight - 40);
      helpDialog.style.left = x + "px";
      helpDialog.style.top = y + "px";
    };
    const up = () => {
      dragBar.removeEventListener("pointermove", move);
      dragBar.removeEventListener("pointerup", up);
    };
    dragBar.addEventListener("pointermove", move);
    dragBar.addEventListener("pointerup", up);
  });

  /* ─── 设置弹窗：版本 / 更新占位 / 主题色 / 外观模式 ─── */
  const settingsModal = $("settings-modal");
  let settingsTrigger = null;
  function fillVersion() {
    const vEl = $("set-version");
    if (api && api.appVersion) {
      try { api.appVersion((v) => { vEl.textContent = v || "—"; }); } catch (e) {}
    }
  }
  function openSettings(trigger) {
    settingsTrigger = trigger || null;
    fillVersion();
    // 缓存行每次打开都向后端要最新状态（容量 + 已用量），不用本地缓存值
    cacheMessage = "";
    if (api && api.refreshCacheInfo) api.refreshCacheInfo();
    renderCache();
    settingsModal.classList.add("open");
    if (api && api.checkGpuEnv && !GpuState.checking && !GpuState.downloading) {
      GpuState.checking = true;
      gpuDlBtn.disabled = true;
      gpuDlBtn.hidden = true;
      gpuCancelBtn.hidden = true;
      gpuRestartBtn.hidden = true;
      gpuStatusEl.textContent = "正在检查 GPU 环境…";
      api.checkGpuEnv();
      api.probeDevice();
    }
    if (settingsTrigger) settingsTrigger.focus({ preventScroll: true });
  }
  function closeSettings() {
    if (!settingsModal.classList.contains("open")) return;
    settingsModal.classList.remove("open");
    if (settingsTrigger) { settingsTrigger.focus({ preventScroll: true }); settingsTrigger = null; }
  }
  $("btn-settings").addEventListener("click", (e) => {
    if (settingsModal.classList.contains("open")) closeSettings();
    else openSettings(e.currentTarget);
  });
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-ok").addEventListener("click", closeSettings);
  $("settings-backdrop").addEventListener("click", closeSettings);
  // 检查更新：GitHub Releases（后端线程查询，结果经 updateInfo 回传）
  const updateStatus = $("update-status");
  const updateBtn = $("btn-check-update");
  const openUpdateBtn = $("btn-open-update");
  function onUpdateInfo(raw) {
    let r = null;
    try { r = JSON.parse(raw); } catch (e) {}
    if (!r) { updateStatus.textContent = "检查失败"; return; }
    if (!r.ok) { updateStatus.textContent = "检查失败（网络或仓库不可访问）"; return; }
    const cur = String(r.current || "0").split(".").map(Number);
    const lat = String(r.latest || "0").split(".").map(Number);
    const newer = lat[0] > cur[0] || (lat[0] === cur[0] && (lat[1] > cur[1] || (lat[1] === cur[1] && lat[2] > cur[2])));
    if (newer && r.url) {
      updateStatus.textContent = `发现新版本 V${r.latest}，当前 V${r.current}`;
      openUpdateBtn.hidden = false;
      openUpdateBtn.onclick = () => { if (api && api.openExternal) api.openExternal(r.url); };
    } else {
      updateStatus.textContent = `已是最新版本 V${r.current}`;
      openUpdateBtn.hidden = true;
    }
  }
  updateBtn.addEventListener("click", () => {
    updateStatus.textContent = "正在检查…";
    openUpdateBtn.hidden = true;
    if (api && api.checkUpdate) api.checkUpdate();
    else updateStatus.textContent = "后端未连接，无法检查";
  });

  /* ─── GPU 环境：下载安装 CUDA 运行时（v1.5）─── */
  const gpuStatusEl = $("gpu-status");
  const gpuProg = $("gpu-progress"), gpuProgFill = $("gpu-progress-fill");
  const gpuDlBtn = $("btn-gpu-download"), gpuCancelBtn = $("btn-gpu-cancel"), gpuRestartBtn = $("btn-gpu-restart");
  const GpuState = { installed: null, source: null, dev: null, downloading: false, checking: false };
  const fmtSize = (b) => (b >= 1073741824 ? (b / 1073741824).toFixed(1) + " GB" : Math.ceil(b / 1048576) + " MB");
  function setGpuBar(pct) {
    gpuProg.hidden = false;
    gpuProgFill.style.width = Math.max(0, Math.min(100, pct)) + "%";
  }
  function gpuRow() {
    // 按当前状态刷新按钮可见性与状态文案（不含进度事件）
    const inst = GpuState.installed;
    gpuDlBtn.hidden = GpuState.downloading || !!inst;
    gpuCancelBtn.hidden = !GpuState.downloading;
    gpuRestartBtn.hidden = true;
    if (inst) {
      const loc = GpuState.source === "system" ? "，来自系统环境" : "";
      if (GpuState.dev === "cuda") gpuStatusEl.textContent = `GPU 加速已启用（CUDA${loc}）`;
      else if (GpuState.dev === "cpu") gpuStatusEl.textContent = `GPU 环境已就绪（V${inst.version || "?"}${loc}），但未检测到可用 NVIDIA GPU`;
      else gpuStatusEl.textContent = `GPU 环境已就绪（V${inst.version || "?"}${loc}）`;
    }
  }
  function onGpuStatus(raw) {
    let r = null;
    try { r = JSON.parse(raw); } catch (e) {}
    if (!r) return;
    if (["state", "done", "cancelled", "error"].includes(r.type)) {
      GpuState.checking = false;
      gpuDlBtn.disabled = false;
      gpuRestartBtn.hidden = true;
    }
    if (r.type === "state") {
      if (r.dev) {
        gpuStatusEl.textContent = r.platform === "darwin"
          ? "使用系统内置的 Apple Silicon（MPS/CPU）推理，无需下载 GPU 包"
          : "开发模式使用本地环境，无需下载 GPU 包";
        gpuDlBtn.hidden = true; gpuCancelBtn.hidden = true; gpuRestartBtn.hidden = true;
        GpuState.installed = null; GpuState.source = null; GpuState.downloading = false;
        gpuProg.hidden = true;
        return;
      }
      GpuState.installed = r.installed || null;
      GpuState.source = r.source || (r.installed ? "app" : null);
      GpuState.downloading = false;
      gpuProg.hidden = true;
      if (GpuState.installed) {
        gpuRow();
      } else if (r.writable === false) {
        gpuStatusEl.textContent = "未检测到可用的 GPU 环境；应用安装目录不可写，无法下载安装。请以管理员身份运行应用，或将应用安装到当前用户可写的目录";
        gpuDlBtn.hidden = true; gpuCancelBtn.hidden = true;
      } else if (r.manifest && r.nvidiaDriver === false) {
        gpuStatusEl.textContent = `未检测到可用的 GPU 环境；未检测到 NVIDIA 显卡驱动，下载 CUDA 运行时（约 ${fmtSize(r.manifest.totalSize)}）也无法启用 GPU 加速`;
        gpuDlBtn.hidden = false; gpuCancelBtn.hidden = true;
      } else if (r.manifest) {
        gpuStatusEl.textContent = `未检测到可用的 GPU 环境，可下载 CUDA 运行时（约 ${fmtSize(r.manifest.totalSize)}）安装到应用目录`;
        gpuDlBtn.hidden = false; gpuCancelBtn.hidden = true;
      } else {
        gpuStatusEl.textContent = r.error ? `GPU 环境信息获取失败：${r.error}` : "未检测到 GPU 环境发布信息";
        gpuDlBtn.hidden = true; gpuCancelBtn.hidden = true;
      }
    } else if (r.type === "scanning") {
      gpuProg.hidden = true;
      gpuCancelBtn.hidden = true; gpuRestartBtn.hidden = true;
      gpuDlBtn.hidden = true;
      gpuStatusEl.textContent = r.phase === "app" ? "正在检查应用目录中的 GPU 环境…"
        : r.phase === "system" ? "正在检查系统环境中的 GPU 环境…"
        : "正在检查 GPU 环境…";
    } else if (r.type === "device") {
      GpuState.dev = r.device;
      if (GpuState.installed) gpuRow();
      else if (r.device === "mps") gpuStatusEl.textContent = "GPU 加速已启用（Apple Silicon MPS）";
    } else if (r.type === "busy") {
      if (GpuState.downloading) return;
      gpuProg.hidden = true;
      gpuCancelBtn.hidden = true;
      gpuRestartBtn.hidden = true;
      gpuDlBtn.hidden = GpuState.checking;
      gpuDlBtn.disabled = GpuState.checking;
      gpuStatusEl.textContent = "GPU 环境正在检查或安装，请稍后重试";
    } else if (r.type === "progress") {
      GpuState.downloading = true;
      const pct = r.total ? Math.round((r.cur / r.total) * 100) : 0;
      const phaseTxt = { info: "获取清单", download: `下载分卷 ${r.part || ""}`.trim(),
        assemble: "组装安装包", verify: "校验完整性", extract: "解压安装" }[r.phase] || r.phase;
      gpuStatusEl.textContent = `${phaseTxt} ${pct}%（约 ${fmtSize(r.total)}）`;
      setGpuBar(pct);
      gpuDlBtn.hidden = true; gpuRestartBtn.hidden = true;
      gpuCancelBtn.hidden = false; gpuCancelBtn.disabled = false;
    } else if (r.type === "done") {
      GpuState.installed = { version: r.version || "" };
      GpuState.source = "app";
      GpuState.downloading = false;
      gpuStatusEl.textContent = `GPU 环境安装完成（V${r.version}），重启应用后生效`;
      gpuProg.hidden = true;
      gpuDlBtn.hidden = true; gpuCancelBtn.hidden = true;
      gpuRestartBtn.hidden = false; gpuRestartBtn.disabled = false;
    } else if (r.type === "cancelled") {
      GpuState.downloading = false;
      gpuStatusEl.textContent = "下载已取消，进度已保留，可再次下载续传";
      gpuCancelBtn.hidden = true;
      if (!GpuState.installed) gpuDlBtn.hidden = false;
    } else if (r.type === "error") {
      GpuState.downloading = false;
      gpuStatusEl.textContent = r.msg ? `失败：${r.msg}` : "失败：未知错误";
      gpuProg.hidden = true; gpuCancelBtn.hidden = true;
      if (!GpuState.installed) { gpuDlBtn.hidden = false; gpuDlBtn.disabled = false; }
    }
  }
  gpuDlBtn.addEventListener("click", () => {
    if (!api || !api.startGpuInstall || GpuState.checking || GpuState.downloading) return;
    gpuDlBtn.disabled = true;
    gpuStatusEl.textContent = "正在准备下载…";
    const started = api.startGpuInstall();
    if (started === false) {
      gpuDlBtn.disabled = false;
      gpuStatusEl.textContent = "GPU 环境正在检查或安装，请稍后重试";
    }
  });
  gpuCancelBtn.addEventListener("click", () => {
    if (api && api.cancelGpuInstall) {
      api.cancelGpuInstall();
      gpuCancelBtn.disabled = true;
      gpuStatusEl.textContent = "正在取消…";
    }
  });
  gpuRestartBtn.addEventListener("click", () => {
    if (api && api.restartApp) {
      gpuRestartBtn.disabled = true;
      gpuStatusEl.textContent = "正在重启应用…";
      api.restartApp();
    }
  });

  /* ─── 处理缓存：容量选择 / 已用显示 / 手动清空 ───
     容量与已用量的唯一来源是后端 pipeline_cache（跨会话一致），
     前端不写 localStorage；处理进行中由前端禁用、后端拒绝双保险。 */
  const CACHE_CAPS = [
    { v: "0", label: "关闭" },
    { v: "2", label: "2 GiB" }, { v: "5", label: "5 GiB" },
    { v: "10", label: "10 GiB" }, { v: "20", label: "20 GiB" },
    { v: "50", label: "50 GiB" }, { v: "100", label: "100 GiB" },
  ];
  const cacheStatusEl = $("cache-status");
  const cacheTrigger = $("dd-cache-cap-trigger");
  const cacheClearBtn = $("btn-cache-clear");
  let cacheInfo = null;       // {capacity_gb, used_bytes, entries}（后端回传）
  let cacheBusy = false;      // 容量设置 / 清空进行中
  let cacheMessage = "";      // 一次性状态提示（已清空 / 失败 / 拒绝）
  let cacheClearArmed = false, cacheClearTimer = 0;

  function fmtCacheBytes(b) {
    b = Math.max(0, Number(b) || 0);
    if (b >= 1073741824) return (b / 1073741824).toFixed(2) + " GiB";
    return Math.max(1, Math.round(b / 1048576)) + " MB";
  }
  function cacheStatusText() {
    if (cacheMessage) return cacheMessage;
    if (cacheBusy) return "正在更新…";
    if (!cacheInfo) return api && api.refreshCacheInfo ? "正在读取…" : "—";
    const cap = Number(cacheInfo.capacity_gb);
    if (cap === 0) return "已关闭，处理不写入缓存";
    return `已用 ${fmtCacheBytes(cacheInfo.used_bytes)} · ${cacheInfo.entries} 条`;
  }
  function renderCache() {
    const cap = cacheInfo ? Number(cacheInfo.capacity_gb) : null;
    const used = cacheInfo ? Number(cacheInfo.used_bytes || 0) : null;
    cacheTrigger.disabled = state.processing || cacheBusy;
    const clearable = cap !== null && cap > 0 && used !== null && used > 0;
    if (cacheClearArmed && (state.processing || cacheBusy || !clearable)) disarmCacheClear();
    cacheClearBtn.disabled = state.processing || cacheBusy || !clearable;
    cacheStatusEl.textContent = cacheStatusText();
  }
  function disarmCacheClear() {
    cacheClearArmed = false;
    if (cacheClearTimer) { clearTimeout(cacheClearTimer); cacheClearTimer = 0; }
    cacheClearBtn.textContent = "清空缓存";
    cacheClearBtn.classList.remove("set-btn--danger");
  }
  const cacheCap = buildDropdown("dd-cache-cap", CACHE_CAPS, "5", (v) => {
    if (!api || !api.setCacheCapacity) {
      cacheMessage = "后端未连接，无法设置"; renderCache(); return;
    }
    cacheBusy = true;
    renderCache();
    // QWebChannel 带返回值的槽经回调回传：false = 后端拒绝（如处理进行中）
    api.setCacheCapacity(Number(v), (started) => {
      if (started === false) { cacheBusy = false; renderCache(); }
    });
  }, null);   // 容量以后端为唯一持久化来源，不写 localStorage
  function syncCacheCapacity(gb) {
    const s = String(gb);
    // 后端处于非档位值（0..100 之间任意整数）：补一个选项再显示，不静默改值
    if (!CACHE_CAPS.some((o) => o.v === s) && /^\d+$/.test(s)) {
      CACHE_CAPS.push({ v: s, label: `${s} GiB` });
    }
    cacheCap.set(s);
  }
  function onCacheStatus(raw) {
    let r = null;
    try { r = JSON.parse(raw); } catch (e) {}
    if (!r || !r.type) return;
    if (r.type === "info" || r.type === "cleared") {
      cacheInfo = { capacity_gb: Number(r.capacity_gb), used_bytes: Number(r.used_bytes || 0),
        entries: Number(r.entries || 0) };
      cacheBusy = false;
      syncCacheCapacity(cacheInfo.capacity_gb);
      cacheMessage = r.type === "cleared" ? "已清空" : "";
    } else if (r.type === "busy") {
      cacheBusy = false;
      cacheMessage = r.op === "capacity" ? "处理进行中，请稍后再改容量" : "处理进行中，请稍后再清空";
    } else if (r.type === "error") {
      cacheBusy = false;
      cacheMessage = `失败：${r.msg || "未知错误"}`;
    }
    renderCache();
  }
  // 两步确认：第一次点击进入待确认（变红、4 秒未确认自动复位），再次点击才执行
  cacheClearBtn.addEventListener("click", () => {
    if (cacheClearBtn.disabled) return;
    if (!cacheClearArmed) {
      cacheClearArmed = true;
      cacheClearBtn.textContent = "确认清空？";
      cacheClearBtn.classList.add("set-btn--danger");
      cacheClearTimer = setTimeout(disarmCacheClear, 4000);
      return;
    }
    disarmCacheClear();
    if (!api || !api.clearCache) { cacheMessage = "后端未连接，无法清空"; renderCache(); return; }
    cacheBusy = true;
    renderCache();
    // false = 后端拒绝（处理进行中 / 缓存操作排队中）；其余结果经 cacheStatus 回传
    api.clearCache((started) => {
      if (started === false) { cacheBusy = false; renderCache(); }
    });
  });

  // 主题色 swatches（预设插入到自定义选择器之前）
  const swWrap = $("accent-swatches");
  const customLabel = $("swatch-custom");
  const customPicker = $("accent-custom-picker");
  function syncSwatches() {
    swWrap.querySelectorAll(".swatch").forEach((s) => s.setAttribute("aria-pressed", "false"));
    if (theme.accent === "custom") {
      customLabel.setAttribute("aria-pressed", "true");
      customLabel.style.background = theme.customColor;   // 选中后显示所选颜色本体
    } else {
      const btn = swWrap.querySelector(`.swatch[data-accent="${theme.accent}"]`);
      if (btn) btn.setAttribute("aria-pressed", "true");
    }
  }
  ACCENTS.forEach((a) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch";
    b.dataset.accent = a.id;
    b.style.setProperty("--sw", a.sw);
    b.title = a.name;
    b.setAttribute("aria-label", `主题色 ${a.name}`);
    b.addEventListener("click", () => { theme.setAccent(a.id); syncSwatches(); });
    swWrap.insertBefore(b, customLabel);
  });
  customPicker.addEventListener("input", () => {
    theme.setCustomColor(customPicker.value);
    syncSwatches();
  });
  syncSwatches();
  // 外观模式分段按钮
  const modeBtns = document.querySelectorAll("#settings-modal .seg-btn[data-mode]");
  const syncModeBtns = () => modeBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === theme.mode)));
  modeBtns.forEach((b) => b.addEventListener("click", () => { theme.setMode(b.dataset.mode); syncModeBtns(); }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && settingsModal.classList.contains("open")) closeSettings();
  });

  /* ─── 处理特效：向上粒子 + 底部主题色渐变动态（无波形条）─── */
  const fx = (() => {
    const canvas = $("fx");
    const ctx = canvas.getContext("2d");
    let active = false, raf = 0;
    let particles = [];
    // 主题色跟随：从 CSS 变量取值，主题切换（sb-theme 事件）时刷新
    let hue1 = 260, hue2 = 215, accentRgb = "109,85,184";
    function readThemeColors() {
      const cs = getComputedStyle(document.documentElement);
      const h1 = parseInt(cs.getPropertyValue("--fx-hue-1"), 10);
      const h2 = parseInt(cs.getPropertyValue("--fx-hue-2"), 10);
      if (Number.isFinite(h1)) hue1 = h1;
      if (Number.isFinite(h2)) hue2 = h2;
      const rgb = (cs.getPropertyValue("--c-accent-rgb") || "").trim();
      if (/^\d{1,3},\s*\d{1,3},\s*\d{1,3}$/.test(rgb)) accentRgb = rgb;
    }
    readThemeColors();
    document.addEventListener("sb-theme", readThemeColors);

    function resize() {
      canvas.width = window.innerWidth * devicePixelRatio;
      canvas.height = window.innerHeight * devicePixelRatio;
      ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    }
    function spawnParticles() {
      if (particles.length < 42 && Math.random() < 0.5) {
        particles.push({
          x: Math.random() * window.innerWidth,
          y: window.innerHeight + 10,
          vx: (Math.random() - 0.5) * 0.4,
          vy: -(0.6 + Math.random() * 1.6),
          r: 0.6 + Math.random() * 1.8,
          hue: Math.random() < 0.7 ? hue1 : hue2,
          a: 0.3 + Math.random() * 0.5,
        });
      }
    }
    function tick() {
      if (!active) return;
      const w = window.innerWidth, h = window.innerHeight;
      ctx.clearRect(0, 0, w, h);
      // 底部主题色渐变动态：缓呼吸 + 渐停点缓慢漂移
      const t = Date.now() / 1000;
      const breath = 0.09 + 0.06 * Math.sin(t * 1.4);
      const drift = 0.22 + 0.10 * Math.sin(t * 0.35);
      const g = ctx.createLinearGradient(0, h * 0.60, 0, h);
      g.addColorStop(0, `rgba(${accentRgb},0)`);
      g.addColorStop(drift, `rgba(${accentRgb},${(0.10 + breath * 0.3).toFixed(3)})`);
      g.addColorStop(1, `rgba(${accentRgb},${breath.toFixed(3)})`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
      spawnParticles();
      for (const p of particles) {
        p.x += p.vx; p.y += p.vy;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${p.hue}, 60%, 60%, ${p.a})`;
        ctx.fill();
      }
      particles = particles.filter((p) => p.y > -10 && p.x > -10 && p.x < w + 10);
      raf = requestAnimationFrame(tick);
    }
    return {
      setActive(v) {
        active = v;
        if (v && !raf) { resize(); raf = requestAnimationFrame(tick); }
        else if (!v) { cancelAnimationFrame(raf); raf = 0; ctx.clearRect(0, 0, window.innerWidth, window.innerHeight); }
      },
      resizeCanvas() { resize(); if (active) ctx.clearRect(0, 0, window.innerWidth, window.innerHeight); },
    };
  })();

  window.addEventListener("resize", () => fx.resizeCanvas());
  ready.then(() => {
    // 桥接就绪：同步原生窗口底色（浅色模式启动时）并回填版本号
    if (api && api.setNativeTheme) {
      try { api.setNativeTheme(theme.mode); } catch (e) {}
    }
    fillVersion();
  });

  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => e.preventDefault());


/* ═══════════════ 视图切换（连体段控滑块）/ 预览 / 报告 ═══════════════ */

/* ─── 顶栏段控：滑动指示块（此环境 CSS transition 不插值，动画走 WAAPI）─── */
const tabGlider = $("tab-glider");
function moveTabGlider(animate) {
  if (!tabGlider) return;
  const btn = document.querySelector(".topbar-nav .tab-btn[aria-pressed='true']");
  if (!btn || !btn.isConnected) return;
  // 取整到整数像素：此环境下小数定位会让 1px 边框渲染残缺
  const x = Math.round(btn.offsetLeft), w = Math.round(btn.offsetWidth);
  if (!w) return;
  if (animate && tabGlider.offsetWidth &&
      !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const fromLeft = Math.round(tabGlider.offsetLeft), fromWidth = Math.round(tabGlider.offsetWidth);
    tabGlider.style.left = x + "px";
    tabGlider.style.width = w + "px";
    tabGlider.animate(
      [{ left: fromLeft + "px", width: fromWidth + "px" },
       { left: x + "px", width: w + "px" }],
      { duration: 200, easing: "cubic-bezier(0.25, 0.6, 0.35, 1)" });
  } else {
    tabGlider.style.left = x + "px";
    tabGlider.style.width = w + "px";
  }
}
window.addEventListener("resize", () => moveTabGlider(false));
moveTabGlider(false);

function openOverlay(which) {
  // 三页切换：效果器（默认）/ 预览 / 报告。文件 UI（待处理队列）始终显示在上方。
  state.view = which;
  const tabs = [["process", "tab-process", "pane-process"],
                ["report", "tab-report", "pane-report"]];
  const pane = $(tabs.find(([name]) => name === which)[2]);
  tabs.forEach(([name, tabId, paneId]) => {
    $(paneId).hidden = name !== which;
    $(tabId).setAttribute("aria-pressed", name === which ? "true" : "false");
  });
  moveTabGlider(true);
  // 面板入场：轻量上浮淡入（WAAPI 驱动）
  if (pane && !pane.hidden && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    pane.animate([{ opacity: 0, transform: "translateY(8px)" },
                  { opacity: 1, transform: "none" }],
                 { duration: 220, easing: "cubic-bezier(0.25, 0.6, 0.35, 1)" });
  }
  // 底部主按钮随视图切换：效果器=BUSTER!，预览=PREVIEW（渲染入口），
  // 报告页无动作（隐藏）。批处理进行中一律回到 停止 展示。
  const dock = document.querySelector(".dock");
  dock.hidden = which === "report";
  if (!state.processing) {
    processLabel.textContent = "BUSTER!";
    processBtn.disabled = draft.busy;
  }

  if (which === "report") {
    refreshReportVersions("reveal");   // 覆盖启动后直进报告的场景（版本列表兜底刷新）
    renderReport("reveal");
  }
  $("content").scrollTo(0, 0);
}

$("tab-process").addEventListener("click", () => openOverlay("process"));
$("tab-report").addEventListener("click", () => openOverlay("report"));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("pane-report").hidden) openOverlay("process");
});

function normPath(p) { return String(p ?? "").replace(/\\/g, "/"); }
/* 后端频谱数据以 base64 传输（JSON 整数列表在此体积下解析过慢）；
   旧版预览缓存里仍是 data 数组，原样透传。 */
function decodeSpec(spec) {
  if (spec && typeof spec.b64 === "string") {
    const bin = atob(spec.b64);
    const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    spec.data = u;
    delete spec.b64;
  }
  return spec;
}
const specCache = new Map();
const specRequested = new Set();
function ensureSpec(path) {
  const key = normPath(path);
  if (!key || specCache.has(key) || specRequested.has(key)) return;
  specRequested.add(key);
  api.previewLoadOutput(path);
}
function onPreviewOutputPeaks(raw) {
  let p = null;
  try { p = JSON.parse(raw); } catch (e) { return; }
  if (!p || !p.path || !p.spec) return;
  const key = normPath(p.path);
  const spec = decodeSpec(p.spec);
  const entry = { spec, duration: Number(spec.duration) || 0 };
  specCache.set(key, entry);
  if (state.view === "report") rpSpecFade(() => renderSpectral(), 260);
}

function fileUrl(path) {
  return "file:///" + encodeURI(String(path).replace(/\\/g, "/"));
}
const SPEC_STOPS = [
  [0.0, 0, 0, 4], [0.12, 24, 12, 66], [0.25, 74, 12, 107], [0.38, 120, 28, 109],
  [0.5, 165, 44, 96], [0.63, 207, 68, 70], [0.75, 237, 105, 37],
  [0.88, 251, 155, 6], [1.0, 252, 255, 164],
];
function specColor(v) {
  const t = Math.max(0, Math.min(1, v / 255));
  for (let i = 1; i < SPEC_STOPS.length; i++) {
    if (t <= SPEC_STOPS[i][0]) {
      const a = SPEC_STOPS[i - 1], b = SPEC_STOPS[i];
      const k = (t - a[0]) / (b[0] - a[0]);
      return [a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k, a[3] + (b[3] - a[3]) * k];
    }
  }
  return [252, 255, 164];
}
/* 频谱位图一次成像、按 spec 缓存：drawSpecs 高频重绘只做贴图。
   后端下发的行已按对数频率分布（载荷带 fmin），逐行 1:1 取用即可；
   单行取其覆盖的源行区间最大值，瞬态与谐波峰不被平均抹平。
   旧版报告下发线性 bin，才需要在显示行上做对数重排。 */
const SPEC_FMIN = 30;   // 对数轴下限（Hz），上限 = sr/2
/* Δ 图把相邻源行并成一半显示行取均值：逐 bin 差值里的单点抖动
   （频谱泄漏、限制器）会被放大成主信号，均值才代表该频段整体移动。 */
function specDisplayH(spec, half) {
  const cap = half ? Math.ceil(spec.h / 2) : spec.h;
  return Math.min(cap, spec.fmin ? 512 : 384);
}
/* 显示行 r（0=顶部=最高频）覆盖的源行闭区间，自高频往下 */
function specSpans(spec, H) {
  const last = spec.h - 1;
  const i0 = new Int32Array(H), i1 = new Int32Array(H);
  const fHi = (spec.sr || 44100) / 2;
  const fLo = Math.min(SPEC_FMIN, fHi / 2);
  const edge = (q) => spec.fmin ? (last + 1) * (1 - q / H)
                                : Math.pow(fLo / fHi, q / H) * last;
  for (let r = 0; r < H; r++) {
    let hi = Math.min(last, Math.ceil(edge(r)) - 1);
    let lo = Math.max(0, Math.min(last, Math.floor(edge(r + 1))));
    if (hi < lo) hi = lo;
    i0[r] = lo; i1[r] = hi;
  }
  return { i0, i1 };
}
const SPEC_LUT = (() => {
  const l = new Uint8Array(256 * 3);
  for (let v = 0; v < 256; v++) {
    const [r, g, b] = specColor(v);
    l[v * 3] = r; l[v * 3 + 1] = g; l[v * 3 + 2] = b;
  }
  return l;
})();
const specOffscreen = new WeakMap();
function ensureOffscreen(spec) {
  let off = specOffscreen.get(spec);
  if (off) return off;
  const H = specDisplayH(spec, false);
  off = document.createElement("canvas");
  off.width = spec.w; off.height = H;
  const octx = off.getContext("2d");
  const img = octx.createImageData(spec.w, H);
  const d = spec.data;
  const { i0, i1 } = specSpans(spec, H);
  for (let x = 0; x < spec.w; x++) {
    const col = x * spec.h;
    for (let r = 0; r < H; r++) {
      let v = 0;
      for (let b = i0[r]; b <= i1[r]; b++) {
        const val = d[col + b];                       // 时间主序：d[t * h + f]
        if (val > v) v = val;
      }
      const o = (r * spec.w + x) * 4;                 // 显示行 r=0 在图顶部
      const c = v * 3;
      img.data[o] = SPEC_LUT[c]; img.data[o + 1] = SPEC_LUT[c + 1];
      img.data[o + 2] = SPEC_LUT[c + 2]; img.data[o + 3] = 255;
    }
  }
  octx.putImageData(img, 0, 0);
  specOffscreen.set(spec, off);
  return off;
}
/* 选段拖饼几何（CSS px）：位于选段顶部中央，抓取后整体移动范围 */
const FREQ_TICKS = [20000, 18000, 16000, 14000, 12000, 11000, 10000, 9000, 8000,
  7000, 6000, 5000, 4000, 3000, 2000, 1000, 500, 400, 300, 200, 100];
function drawFreqScale(ctx, w, h, dpr, fHi) {
  if (!fHi || w < 280 * dpr) return;
  const fLo = Math.min(SPEC_FMIN, fHi / 2);
  const yOf = (f) => (Math.log(f / fHi) / Math.log(fLo / fHi)) * h;   // 0=顶部(高频)
  ctx.save();
  ctx.font = `${9 * dpr}px sans-serif`;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.shadowColor = "rgba(0,0,0,0.55)";
  ctx.shadowBlur = 2 * dpr;
  ctx.fillStyle = cssVar("--c-text", "#e6e0e9");
  let lastY = -Infinity;
  let firstY = null;
  for (const f of FREQ_TICKS) {
    if (f >= fHi) continue;
    const y = yOf(f);
    if (y < 10 * dpr || y > h - 5 * dpr) continue;
    if (lastY !== -Infinity && y - lastY < 12.5 * dpr) continue;   // 自高频往下、间距不足则跳过
    if (firstY === null) firstY = y;
    lastY = y;
    ctx.fillText(f >= 1000 ? `${f / 1000}k` : String(f), w - 5 * dpr, y);
  }
  if (firstY !== null && firstY > 16 * dpr) {
    ctx.fillText("Hz", w - 5 * dpr, firstY - 13 * dpr);
  }
  ctx.restore();
}
const draft = {id:0,busy:false,ready:false,key:null,player:new DraftPlayer()};
const transport = {file:null,duration:0,source:'original',versions:[],lastVersion:null,loop:false,range:[0,0],dragging:false,token:0,delta:false};
const waveCache=new Map();let wavePath=null, waveRms=[], waveAnimation=null, waveRevision=0;
async function transitionPlayerWaveform(values) {
  const canvas=$('player-waveform'),revision=++waveRevision;
  const opacity=getComputedStyle(canvas).opacity;
  waveAnimation?.cancel();
  canvas.style.opacity=opacity;
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(!reduced&&waveRms.length&&+opacity>0){
    waveAnimation=canvas.animate([{opacity},{opacity:0}],{duration:120,fill:'forwards'});
    await waveAnimation.finished.catch(()=>{});
    if(revision!==waveRevision)return;
  }
  canvas.style.opacity='0';waveAnimation?.cancel();
  waveRms=values;drawPlayerWaveform();
  if(!reduced&&values.length){
    waveAnimation=canvas.animate([{opacity:0},{opacity:1}],{duration:160,fill:'forwards'});
    await waveAnimation.finished.catch(()=>{});
    if(revision!==waveRevision)return;
  }
  canvas.style.opacity='1';waveAnimation?.cancel();waveAnimation=null;
}
function requestPlayerWaveform() {
  const path=['original','draft'].includes(transport.source)?transport.file:transport.source;
  if(path===wavePath)return;
  wavePath=path;transitionPlayerWaveform(waveCache.get(path)||[]);
  if(path&&!waveCache.has(path))api?.playerWaveform?.(path);
}
function onPlayerWaveform(raw) {
  const data=JSON.parse(raw);
  if(data.path!==wavePath)return;
  const values=data.rms||[];
  if(!data.error){waveCache.set(data.path,values);if(waveCache.size>4)waveCache.delete(waveCache.keys().next().value);}
  transitionPlayerWaveform(values);
}
function drawPlayerWaveform() {
  const canvas=$('player-waveform'),r=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1;
  canvas.width=Math.round(r.width*dpr);canvas.height=Math.round(r.height*dpr);
  const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
  const color=getComputedStyle(canvas).getPropertyValue('--c-accent-hi');
  // A single RMS envelope exposes passage dynamics without a second outline.
  const scale=Math.max(.01,...waveRms);
  function envelope(values,alpha){
    if(!values.length)return;
    ctx.fillStyle=color;ctx.globalAlpha=alpha;ctx.beginPath();
    for(let side=0;side<2;side++)for(let step=0;step<=Math.ceil(r.width);step++){
      const x=side?r.width-step:step,index=Math.min(values.length-1,Math.max(0,Math.floor(x/r.width*values.length)));
      const h=Math.min(1,Math.sqrt((values[index]||0)/scale))*(r.height-2)/2;
      const y=r.height/2+(side?h:-h);if(!side&&!step)ctx.moveTo(x,y);else ctx.lineTo(x,y);
    }
    ctx.closePath();ctx.fill();
  }
  envelope(waveRms,.65);
  // Cut fine transparent gaps on device-pixel boundaries to keep stripes crisp.
  ctx.setTransform(1,0,0,1,0,0);
  for(let x=2;x<r.width;x+=3){
    const left=Math.round(x*dpr),right=Math.round((x+1)*dpr);
    ctx.clearRect(left,0,right-left,canvas.height);
  }
}
document.addEventListener('sb-theme',drawPlayerWaveform);
const nativeAudio = $('player-audio');
const getMonitorVolume=bindKnob($('player-volume'),$('player-volume-value'),v=>`${v}%`,null,value=>{
  nativeAudio.volume=value/100;draft.player.volume(value/100);
});
function draftTime(t) { t=Math.max(0,Math.floor(t||0));return `${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`; }
function preciseTime(t) { const ms=Math.round(Math.max(0,t||0)*1000);return `${String(Math.floor(ms/60000)).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`; }
function paintPlayerTime(t) {
  $('draft-time').textContent=preciseTime(t);$('player-total').textContent=` / ${draftTime(transport.duration).padStart(5,'0')}`;
  $('player-playhead').style.left=`${transport.duration?t/transport.duration*100:0}%`;
}
function renderPlayerRuler() {
  const ruler=$('player-ruler'), duration=transport.duration; ruler.innerHTML='';
  const wanted=duration/Math.max(2,Math.floor(ruler.clientWidth/85));
  const step=[1,2,5,10,15,30,60,120,300,600,1800,3600].find(v=>v>=wanted)||Math.ceil(wanted/3600)*3600;
  if(!duration)return;
  for(let t=0;t<=duration;t+=step/4) {
    const tick=document.createElement('i');tick.className='player-tick';tick.style.left=`${t/duration*100}%`;
    if(Math.abs(t/step-Math.round(t/step))<.001){tick.classList.add('major');const label=document.createElement('span');label.textContent=draftTime(t);if(t/duration>.94)label.style.transform='translateX(-100%)';tick.appendChild(label);}
    ruler.appendChild(tick);
  }
}
function usesDraftEngine(){return draft.ready&&['original','draft'].includes(transport.source);}
function playerPosition() { return usesDraftEngine() ? draft.player.currentPosition() : nativeAudio.currentTime||0; }
function playerPlaying() { return usesDraftEngine() ? draft.player.playing : !nativeAudio.paused; }
function playerError(message) { $('draft-status').textContent=message; $('draft-status').hidden=!message; }
let cacheStateToken=0, cacheFraction=0;
function cacheState(status, text, progress=0) {
  cacheStateToken++;   // 任何一次状态更新都作废尚未到期的「错误闪回」
  cacheFraction=Math.max(0,Math.min(1,progress));
  $('player').dataset.cache=status;
  /* 进度写在 #player 上：整条时间轴的进度底、底部的状态线都从这一处读 */
  $('player').style.setProperty('--cache-progress',`${cacheFraction*100}%`);
  /* 还没有任何进度时（fraction 仍为 0）用扫光替进度条表态 */
  $('player').dataset.cacheSweep=String(status==='preparing' && cacheFraction<=0);
  $('player-track').title=text; $('player-cache-description').textContent=text;
  const button=$('player-sources').querySelector('[data-source=draft]');
  if(button){button.textContent=draftButtonLabel();button.title=text;}
}
function draftButtonLabel(){return draft.busy?'取消创建':draft.ready?'草稿':'创建缓存';}
/* 取消或失败都不弹文字：红线在时间轴上闪 1 秒就收回，说明「这次没成」即可，
   不留一条要用户自己消化的常驻警示；闪回期间任何新状态都会作废这次恢复。 */
function flashCacheError(){
  cacheState('error','缓存准备失败');
  const token=cacheStateToken;
  setTimeout(()=>{if(token===cacheStateToken)cacheState('empty','缓存尚未准备');},1000);
}
function paintPlaybackPosition(){
  if(transport.dragging)return;
  const t=playerPosition();$('draft-seek').value=t;paintPlayerTime(t);
  $('draft-seek').setAttribute('aria-valuetext',`${draftTime(t)}，总长 ${draftTime(transport.duration)}`);
}
function paintTransport() {
  const t=playerPosition();
  const filesLocked=state.processing||draft.busy;
  $('btn-add').disabled=filesLocked;$('btn-clear').disabled=filesLocked||!state.files.length;
  fileListEl.querySelectorAll('button').forEach(button=>{button.disabled=filesLocked;});
  const deltaAvailable=transport.source==='draft'&&draft.ready;
  if(!deltaAvailable&&transport.delta){transport.delta=false;draft.player.delta(false);}
  $('player-delta').disabled=!deltaAvailable;
  $('player-delta').setAttribute('aria-pressed',String(transport.delta));
  $('draft-play').dataset.playing=String(playerPlaying());
  $('draft-play').setAttribute('aria-label',playerPlaying()?'暂停':'播放');
  $('draft-play').disabled=!transport.file || (transport.source==='draft'&&!draft.ready);
  $('draft-loop').disabled=!transport.duration;
  if(!state.processing)processBtn.disabled=draft.busy;
  const locked=draft.busy||transport.source==='draft';
  $('knob-quality').setAttribute('aria-disabled',String(locked));
  $('ref-card').inert=locked;
  $('ref-card').setAttribute('aria-disabled',String(locked));
  $('preset-card').inert=locked;
  $('player-start').disabled=!transport.duration;
  $('draft-seek').disabled=!transport.duration || (transport.source==='draft'&&!draft.ready);
  $('draft-seek').max=transport.duration||1;
  if(!transport.dragging) {
    $('draft-seek').value=t;
    paintPlayerTime(t);
  }
  $('draft-seek').setAttribute('aria-valuetext',`${draftTime(t)}，总长 ${draftTime(transport.duration)}`);
}
function stopTransport() { nativeAudio.pause();draft.player.stop();paintTransport(); }
function setPlayerLoop() {
  const duration=transport.duration;
  let [a,b]=transport.range;
  a=Math.max(0,Math.min(a,duration));b=Math.max(a,Math.min(b,duration));
  if(b<=a)transport.loop=false;
  transport.range=[a,b];
  $('draft-loop').setAttribute('aria-pressed',String(transport.loop));
  paintLoopRange();
  if(draft.ready)draft.player.loop(transport.loop?[a,b]:null);
  if(transport.loop && !usesDraftEngine() && (playerPosition()<a || playerPosition()>=b))nativeAudio.currentTime=a;
}
function paintLoopRange() {
  const [a,b]=transport.range,d=transport.duration,band=$('player-loop-region');
  band.hidden=!d||b<=a;band.dataset.active=String(transport.loop);
  band.style.left=`${d?a/d*100:0}%`;band.style.width=`${d?(b-a)/d*100:0}%`;
  $('player-loop-label').textContent=`${draftTime(a)} — ${draftTime(b)}`;
  $('player-loop-label').style.visibility=d && (b-a)/d*$('player-loop-lane').clientWidth>=100?'visible':'hidden';
  band.title=`${preciseTime(a)} — ${preciseTime(b)}；双击精确编辑`;
  $('player-loop-lane').setAttribute('aria-label',`循环区间 ${preciseTime(a)} 至 ${preciseTime(b)}；Enter 精确编辑，方向键移动，Delete 清除`);
}
function seekPlayer(t) {
  t=Math.max(0,Math.min(transport.duration,t));
  if(transport.loop && (t<transport.range[0] || t>=transport.range[1])) {transport.loop=false;setPlayerLoop();}
  if(usesDraftEngine())draft.player.seek(t);
  else if(nativeAudio.readyState)nativeAudio.currentTime=t;
  paintTransport();
}
async function chooseSource(source,keepPlaying=true) {
  if(source==='draft'&&!draft.ready)return;
  const time=playerPosition(), playing=keepPlaying&&playerPlaying(), token=++transport.token;
  const shared=usesDraftEngine()&&['original','draft'].includes(source);
  if(!shared)stopTransport();
  transport.source=source;if(!['original','draft'].includes(source))transport.lastVersion=source;
  renderPlayerSources();syncReportSelection();requestPlayerWaveform();playerError('');paintTransport();
  if(shared){draft.player.original(source==='original');return;}
  if(usesDraftEngine()) {
    draft.player.original(source==='original');draft.player.seek(time);draft.player.params(collectParams());draft.player.volume(getMonitorVolume()/100);
    setPlayerLoop();if(playing)await draft.player.play(true);
  } else {
    const path=source==='original'?transport.file:source;
    if(!path)return;
    nativeAudio.src=fileUrl(path);nativeAudio.volume=getMonitorVolume()/100;
    try {
      await new Promise((resolve,reject)=>{
        const done=()=>{nativeAudio.removeEventListener('loadedmetadata',ok);nativeAudio.removeEventListener('error',fail);};
        const ok=()=>{done();resolve();};const fail=()=>{done();reject(new Error('音频无法播放，请检查文件'));};
        nativeAudio.addEventListener('loadedmetadata',ok);nativeAudio.addEventListener('error',fail);
      });
      if(token!==transport.token)return;
      nativeAudio.currentTime=Math.min(time,Math.max(0,nativeAudio.duration-.001));
      if(playing)await nativeAudio.play();
    } catch(e) {if(token===transport.token)playerError(String(e));}
  }
  paintTransport();
}
const versionMenu=$('player-version-menu');
function placeVersionMenu(){
  const button=$('player-version-arrow');if(!button)return;
  const r=button.getBoundingClientRect(),width=Math.max(160,versionMenu.offsetWidth);
  versionMenu.style.left=`${Math.max(12,Math.min(r.right-width,innerWidth-width-12))}px`;
  versionMenu.style.top=`${Math.max(12,Math.min(r.bottom+8,innerHeight-versionMenu.offsetHeight-12))}px`;
}
versionMenu.addEventListener('toggle',()=>{
  const open=versionMenu.matches(':popover-open');$('player-version-arrow')?.setAttribute('aria-expanded',String(open));
  if(open){placeVersionMenu();(versionMenu.querySelector('[aria-selected="true"]')||versionMenu.querySelector('button'))?.focus();}
});
versionMenu.addEventListener('beforetoggle',e=>{if(e.newState==='open')placeVersionMenu();});
versionMenu.addEventListener('keydown',e=>{
  const items=[...versionMenu.querySelectorAll('button')],index=items.indexOf(document.activeElement);
  if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){
    e.preventDefault();const next=e.key==='Home'?0:e.key==='End'?items.length-1:(index+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next]?.focus();
  }else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();versionMenu.hidePopover();$('player-version-arrow')?.focus();}
});
window.addEventListener('resize',()=>{if(versionMenu.matches(':popover-open'))placeVersionMenu();});
$('content').addEventListener('scroll',()=>{if(versionMenu.matches(':popover-open'))placeVersionMenu();});
function renderPlayerSources() {
  const wrap=$('player-sources');
  function button(key){
    let b=wrap.querySelector(`[data-source="${key}"]`);
    if(!b){b=document.createElement('button');b.type='button';b.className='player-source';b.dataset.source=key;wrap.appendChild(b);}
    return b;
  }
  for(const [key,label] of [['original','原声'],['draft',draftButtonLabel()]]){
    const b=button(key);b.textContent=label;b.setAttribute('aria-pressed',String(transport.source===key));
    b.disabled=!transport.file||(key==='draft'&&state.processing);
    b.onclick=()=>key==='draft'&&(draft.busy||!draft.ready)?startDraft():chooseSource(key);
  }
  const selected=transport.versions.find(v=>v.path===transport.lastVersion)||transport.versions.reduce((a,b)=>!a||+b.version>+a.version?b:a,null);
  const main=button('versions');main.textContent='成品';main.disabled=!selected;
  main.setAttribute('aria-pressed',String(!['original','draft'].includes(transport.source)));
  main.title=selected?`切换到成品 V${selected.version}`:'尚无成品';main.onclick=()=>{if(selected)chooseSource(selected.path);};
  const arrow=button('version-menu');arrow.id='player-version-arrow';arrow.classList.add('player-version-arrow');
  arrow.setAttribute('aria-label','选择成品版本');arrow.setAttribute('aria-haspopup','listbox');arrow.setAttribute('aria-controls','player-version-menu');arrow.setAttribute('aria-expanded',String(versionMenu.matches(':popover-open')));
  arrow.disabled=!selected;
  if(!arrow.firstChild)arrow.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
  /* 声明式 invoker：浏览器自己处理「再点一次关掉」。JS 里 togglePopover 不行——
     第二次按下时 light-dismiss 先把菜单关了，click 处理器再 toggle 又把它开回来。 */
  arrow.setAttribute('popovertarget','player-version-menu');
  arrow.onkeydown=e=>{if(e.key==='ArrowDown'){e.preventDefault();versionMenu.showPopover();}};
  const signature=JSON.stringify(transport.versions.map(v=>[v.path,v.version]));
  if(versionMenu.dataset.versions!==signature){
    versionMenu.dataset.versions=signature;versionMenu.replaceChildren();
    for(const v of transport.versions){
      const option=document.createElement('button');option.type='button';option.role='option';option.textContent=`成品 V${v.version}`;option.dataset.path=v.path;
      option.onclick=()=>{versionMenu.hidePopover();chooseSource(v.path);arrow.focus();};versionMenu.appendChild(option);
    }
  }
  for(const option of versionMenu.children)option.setAttribute('aria-selected',String(option.dataset.path===selected?.path));
  $('player-mode').textContent=transport.source==='original'?'原声':transport.source==='draft'?'草稿':`成品 V${selected?.version||''}`;
}
function refreshPreviewFiles() {
  const file=state.selectedFile;if(file===transport.file)return;
  stopTransport();draft.id++;draft.busy=false;transport.dragging=false;seekPointer=null;transport.token++;transport.file=file;transport.source='original';transport.duration=0;
  transport.range=[0,0];transport.loop=false;transport.versions=[];transport.lastVersion=null;requestPlayerWaveform();$('player-loop-editor').close();renderPlayerRuler();
  draft.ready=false;nativeAudio.removeAttribute('src');nativeAudio.load();
  setPlayerLoop();cacheState('empty','缓存尚未准备');playerError('');paintTransport();renderPlayerSources();
  if(file){api.audioInfo(file);chooseSource('original',false);refreshPreviewOutputs();restoreDraftForFile(file);}
}
async function restoreDraftForFile(file){
  const id=draft.id,key=draftKey();draft.key=key;
  try{
    const raw=await api.draftLookup?.(file,collectParams(),id);
    if(id!==draft.id||file!==transport.file||key!==draftKey())return;
    const payload=raw?JSON.parse(raw):null;
    if(payload)await onDraftReady(JSON.stringify(payload));
  }catch(e){if(id===draft.id)playerError('读取已准备缓存失败：'+String(e));}
}
function onAudioInfo(raw) {
  const p=JSON.parse(raw);if(normPath(p.path)!==normPath(transport.file))return;
  if(p.error){playerError('无法读取歌曲：'+p.error);return;}
  transport.duration=p.duration;transport.range=[0,0];setPlayerLoop();renderPlayerRuler();paintTransport();
}
async function refreshPreviewOutputs() {
  const file=state.selectedFile;let outs=[];
  try {if(file&&state.output)outs=JSON.parse(await api.listOutputs(state.output,stemOf(file))).outputs||[];}catch(e){}
  if(file!==transport.file)return;
  transport.versions=outs;
  if(!['original','draft'].includes(transport.source)&&!outs.some(x=>x.path===transport.source))chooseSource('original',false);
  renderPlayerSources();
}
function draftKey() {
  const p=collectParams();return JSON.stringify([state.selectedFile,p.quality,p.reference,p.style_mode==='styled'&&!p.reference,(p.bypass||[]).includes('lew')]);
}
async function startDraft() {
  if(draft.busy){api.cancel();cacheState('preparing','正在取消准备',cacheFraction);return;}
  if(state.processing||!transport.file){playerError('请先选择歌曲，并等待当前处理完成');return;}
  if(!transport.duration){playerError('正在读取歌曲，请稍后重试');return;}
  stopTransport();draft.ready=false;draft.busy=true;draft.key=draftKey();draft.id++;
  cacheState('preparing','正在准备试听缓存');playerError('');renderPlayerSources();paintTransport();
  api.draftPrepare(state.selectedFile,0,transport.duration,collectParams(),draft.id);
}
async function onDraftReady(raw) {
  const p=JSON.parse(raw);if(p.id!==draft.id)return;
  if(draft.key!==draftKey()){draft.busy=false;cacheState('stale','输入或精度已变化，请准备缓存');paintTransport();renderPlayerSources();return;}
  try {
    await draft.player.load(p);
    if(p.id!==draft.id||draft.key!==draftKey()){draft.player.stop();draft.busy=false;cacheState('stale','输入或精度已变化，请准备缓存');paintTransport();renderPlayerSources();return;}
    const resumeAt=playerPosition(),resumePlaying=playerPlaying();if(!p.restored||['original','draft'].includes(transport.source))nativeAudio.pause();
    draft.busy=false;draft.ready=true;if(!p.restored)transport.source='draft';draft.player.original(transport.source==='original');
    draft.player.params(collectParams());draft.player.volume(getMonitorVolume()/100);draft.player.seek(resumeAt);
    transport.duration=draft.player.duration;renderPlayerRuler();
    cacheState('ready','试听缓存就绪',1);playerError('');
    renderPlayerSources();setPlayerLoop();if(resumePlaying&&usesDraftEngine())await draft.player.play(true);paintTransport();
  }catch(e){onDraftFailed(JSON.stringify({id:p.id,error:String(e)}));}
}
function onDraftFailed(raw) {
  const p=JSON.parse(raw);if(p.id!==draft.id)return;
  draft.busy=false;draft.ready=false;playerError('');flashCacheError();renderPlayerSources();paintTransport();
}
function onDraftProgress(raw) {const p=JSON.parse(raw);if(p.id===draft.id&&draft.key===draftKey())cacheState('preparing',`${p.label} · ${Math.round(p.fraction*100)}%`,p.fraction);}
async function togglePlayer() {
  if(!transport.file)return;
  playerError('');
  try {
    if(usesDraftEngine()) {
      if(draft.player.position>=draft.player.duration)draft.player.seek(transport.loop?transport.range[0]:0);
      const pending=draft.player.play(!draft.player.playing);paintTransport();await pending;
    }else if(nativeAudio.paused)await nativeAudio.play();else nativeAudio.pause();
  }catch(e){playerError('播放失败：'+String(e));}
  paintTransport();
}
draft.player.onneed=p=>api.draftReadChunk(draft.id,p.index,p.generation);
draft.player.onposition=p=>{if(usesDraftEngine()){if($('draft-play').dataset.playing!==String(p.playing))paintTransport();else paintPlaybackPosition();}};
$('draft-play').addEventListener('click',togglePlayer);
$('player-start').addEventListener('click',()=>seekPlayer(0));
const seekInput=$('draft-seek');let seekPointer=null;
function previewSeekPointer(e){
  const r=seekInput.getBoundingClientRect();
  seekInput.value=Math.max(0,Math.min(transport.duration,(e.clientX-r.left)/r.width*transport.duration));
  paintPlayerTime(+seekInput.value);
}
seekInput.addEventListener('pointerdown',e=>{
  if(e.button!==0||seekInput.disabled)return;
  e.preventDefault();seekInput.focus();seekPointer=e.pointerId;transport.dragging=true;
  seekInput.setPointerCapture(e.pointerId);previewSeekPointer(e);
});
seekInput.addEventListener('pointermove',e=>{if(e.pointerId===seekPointer)previewSeekPointer(e);});
seekInput.addEventListener('pointerup',e=>{
  if(e.pointerId!==seekPointer)return;
  previewSeekPointer(e);const target=+seekInput.value;seekPointer=null;transport.dragging=false;
  seekInput.releasePointerCapture(e.pointerId);seekPlayer(target);
});
function cancelSeek(){if(seekPointer!==null){seekPointer=null;transport.dragging=false;paintPlaybackPosition();}}
seekInput.addEventListener('pointercancel',cancelSeek);
seekInput.addEventListener('lostpointercapture',cancelSeek);
seekInput.addEventListener('input',()=>{transport.dragging=true;paintPlayerTime(+seekInput.value);});
seekInput.addEventListener('change',()=>{transport.dragging=false;seekPlayer(+seekInput.value);});
$('draft-loop').addEventListener('click',()=>{
  if(!transport.duration)return;
  if(transport.range[1]<=transport.range[0])transport.range=[0,transport.duration];
  transport.loop=!transport.loop;setPlayerLoop();
});
const loopLane=$('player-loop-lane');let loopGesture=null;
loopLane.addEventListener('pointerdown',e=>{
  if(e.button!==0||!transport.duration)return;
  const rect=loopLane.getBoundingClientRect(),time=Math.max(0,Math.min(transport.duration,(e.clientX-rect.left)/rect.width*transport.duration));
  loopGesture={x:e.clientX,time,range:[...transport.range],active:transport.loop,mode:e.target.dataset.edge||(e.target.closest('#player-loop-region')?'move':'create'),rect,moved:false};
  loopLane.setPointerCapture(e.pointerId);loopLane.focus();e.preventDefault();
});
loopLane.addEventListener('pointermove',e=>{
  const g=loopGesture;if(!g)return;
  if(Math.abs(e.clientX-g.x)<3&&!g.moved)return;g.moved=true;
  const d=transport.duration,t=Math.max(0,Math.min(d,(e.clientX-g.rect.left)/g.rect.width*d));
  let [a,b]=g.range;const minimum=Math.min(.05,d);
  if(g.mode==='create'){a=Math.min(t,g.time);b=Math.max(t,g.time);}
  else if(g.mode==='a')a=Math.min(t,b-minimum);
  else if(g.mode==='b')b=Math.max(t,a+minimum);
  else {const delta=Math.max(-a,Math.min(d-b,t-g.time));a+=delta;b+=delta;}
  transport.range=[Math.max(0,a),Math.min(d,b)];transport.loop=true;paintLoopRange();
});
function finishLoopGesture(cancel=false) {
  const g=loopGesture;if(!g)return;loopGesture=null;
  if(cancel||!g.moved){transport.range=g.range;transport.loop=g.active;}
  setPlayerLoop();
}
loopLane.addEventListener('pointerup',()=>finishLoopGesture());
loopLane.addEventListener('pointercancel',()=>finishLoopGesture(true));
loopLane.addEventListener('lostpointercapture',()=>finishLoopGesture(true));
let loopEditorTrigger=null;
function openLoopEditor() {
  if(!transport.duration)return;
  loopEditorTrigger=document.activeElement;
  const [a,b]=transport.range;$('player-a').value=preciseTime(a);$('player-b').value=preciseTime(b>a?b:transport.duration);
  $('player-loop-error').textContent='';$('player-loop-editor').showModal();$('player-a').focus();
}
$('player-loop-editor').addEventListener('close',()=>{
  if(loopEditorTrigger?.isConnected)loopEditorTrigger.focus();
});
loopLane.addEventListener('dblclick',openLoopEditor);
loopLane.addEventListener('keydown',e=>{
  if(e.key==='Enter'){e.preventDefault();openLoopEditor();}
  else if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();transport.range=[0,0];transport.loop=false;setPlayerLoop();}
  else if(['ArrowLeft','ArrowRight'].includes(e.key)){
    e.preventDefault();const [a,b]=transport.range,delta=Math.max(-a,Math.min(transport.duration-b,(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?10:1)));
    transport.range=[a+delta,b+delta];setPlayerLoop();
  }
});
$('player-loop-cancel').addEventListener('click',()=>$('player-loop-editor').close());
$('player-loop-form').addEventListener('submit',e=>{
  e.preventDefault();
  const parse=value=>{const v=value.trim();if(!/^(?:\d+:)?\d+(?:\.\d+)?$/.test(v))return NaN;const p=v.split(':').map(Number);return p.length===2?(p[1]<60?p[0]*60+p[1]:NaN):p[0];};
  const a=parse($('player-a').value),b=parse($('player-b').value);
  if(!Number.isFinite(a)||!Number.isFinite(b)||a<0||b<=a||b>transport.duration){$('player-loop-error').textContent='请输入有效时间，终点须晚于起点且不超过歌曲时长。';return;}
  transport.range=[a,b];transport.loop=true;setPlayerLoop();$('player-loop-editor').close();
});
new ResizeObserver(()=>{renderPlayerRuler();drawPlayerWaveform();paintLoopRange();paintPlaybackPosition();}).observe($('player-track'));
$('player-delta').addEventListener('click',()=>{
  if(transport.source!=='draft'||!draft.ready)return;
  transport.delta=!transport.delta;draft.player.delta(transport.delta);paintTransport();
});
for(const event of ['play','pause','ended','loadedmetadata'])nativeAudio.addEventListener(event,paintTransport);
function nativeTick(){
  if(usesDraftEngine()){if(draft.player.playing)paintPlaybackPosition();}
  else if(!nativeAudio.paused){
    const active=loopGesture?loopGesture.active:transport.loop;
    const range=loopGesture?loopGesture.range:transport.range;
    if(active&&nativeAudio.currentTime>=range[1])nativeAudio.currentTime=range[0];
    paintPlaybackPosition();
  }
  requestAnimationFrame(nativeTick);
}
requestAnimationFrame(nativeTick);
document.addEventListener('keydown',e=>{
  if(e.code!=='Space')return;
  e.preventDefault();e.stopImmediatePropagation();if(!e.repeat)togglePlayer();
},true);
document.addEventListener('keyup',e=>{if(e.code==='Space'){e.preventDefault();e.stopImmediatePropagation();}},true);
let lastDraftParams='';
setInterval(()=>{
  if(!draft.ready)return;
  if(draft.key!==draftKey()){
    draft.player.stop();draft.ready=false;cacheState('stale','输入、精度或参考已变化，请重新准备');
    if(transport.source==='draft')chooseSource('original',false);
    playerError('输入、精度或参考已变化，请重新准备。');
    renderPlayerSources();paintTransport();return;
  }
  const p=collectParams(),key=JSON.stringify(p);if(key!==lastDraftParams){lastDraftParams=key;draft.player.params(p);}
},50);
renderPlayerSources();

const rp = { versions: [], sel: null, reports: new Map() };
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function stemOf(path) {
  return String(path || "").replace(/\\/g, "/").split("/").pop().replace(/\.[^.]+$/, "");
}
function syncReportSelection(mode){
  const previous=rp.sel;
  rp.versions=transport.versions;
  const selected=rp.versions.find(v=>v.path===transport.lastVersion)||rp.versions.reduce((a,b)=>!a||+b.version>+a.version?b:a,null);
  rp.sel=selected?.path||null;
  if(previous!==rp.sel&&!mode)renderReport("morph");else renderReport(mode);
}
async function refreshReportVersions(mode){
  await refreshPreviewOutputs();syncReportSelection(mode);
}
async function loadReportData() {
  if (!rp.sel || rp.reports.has(rp.sel)) return;
  const path=rp.sel;
  let report = null;
  try {
    const raw = await api.reportLoad(path + ".quality.json");
    const parsed = JSON.parse(raw || "null");
    if (parsed && typeof parsed === "object" && !parsed.error) report = parsed;
  } catch (e) { /* 无报告：图表回退为频谱对比 */ }
  rp.reports.set(path, report);
}
/* 曲线数据：原始 + 选中版本（原始取自 compare.input_db，与成品同一条输入） */
function reportCurves() {
  const cmp = rp.sel && rp.reports.get(rp.sel) && rp.reports.get(rp.sel).compare;
  if (!cmp || !Array.isArray(cmp.centers) || !cmp.centers.length ||
      !Array.isArray(cmp.input_db) || !Array.isArray(cmp.output_db)) return { centers: null, curves: [] };
  const ver = (rp.versions.find((v) => v.path === rp.sel) || {}).version;
  return {
    centers: cmp.centers,
    curves: [{ label: "原始", base: true, db: cmp.input_db },
             { label: `V${ver}`, db: cmp.output_db }],
  };
}
/* ── 报告画布 hover 提示：指标含义 + 数值 ── */
const rpHitZones = new WeakMap();   // canvas → [{y0, y1, title, tip, rows: [{label, text}]}]
function rpTipFor(canvas) {
  const fig = canvas.closest(".rp-fig");
  if (!fig) return null;
  let tip = fig.querySelector(".rp-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.className = "rp-tip";
    fig.appendChild(tip);
  }
  return tip;
}
function attachRpTip(canvas) {
  canvas.addEventListener("pointermove", (e) => {
    const tip = rpTipFor(canvas);
    if (!tip) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const dpr = window.devicePixelRatio || 1;
    const zones = rpHitZones.get(canvas) || [];
    const zone = zones.find((z) => y * dpr >= z.y0 && y * dpr < z.y1);
    if (!zone) { tip.classList.remove("show"); return; }
    tip.innerHTML = "";
    const title = document.createElement("b");
    title.textContent = zone.title;
    tip.appendChild(title);
    if (zone.tip) {
      const desc = document.createElement("span");
      desc.textContent = zone.tip;
      tip.appendChild(desc);
    }
    zone.rows.forEach((r) => {
      const row = document.createElement("i");
      row.textContent = `${r.label}：${r.text}`;
      tip.appendChild(row);
    });
    tip.classList.add("show");
    const fig = canvas.closest(".rp-fig");
    const fr = fig.getBoundingClientRect();
    const tx = Math.min(Math.max(0, x + 14), fr.width - tip.offsetWidth - 4);
    const ty = Math.min(Math.max(0, y + 14), fr.height - tip.offsetHeight - 4);
    tip.style.left = tx + "px";
    tip.style.top = ty + "px";
  });
  canvas.addEventListener("pointerleave", () => {
    const tip = rpTipFor(canvas);
    if (tip) tip.classList.remove("show");
  });
}

/* ── 频谱曲线：对数频率轴 + 非线性纵轴 + 滚轮缩放 / 拖拽平移 / 双击复位 ──
   左侧低频天然陡峭，固定全频段视野会吃掉中高频差异；缩放后纵轴只按可见频段
   重新划段，远离核心区的两端按 1/8 倍率压缩，局部差异被放大到整个坐标系。 */
const rpZoom = { f0: 20, f1: 20000 };
const FLOOR_DB = -96;   // 压缩段下限，与后端 -90 dB 动态范围留余量
/* 内边距与横轴刻度由骨架和实图共用：两套数值一旦不同，数据到达的瞬间整幅会跳位 */
const rpPlotPad = (dpr) => ({ padL: 36 * dpr, padR: 12 * dpr, padT: 8 * dpr, padB: 18 * dpr });
const RP_FREQ_TICKS = [10, 15, 20, 30, 40, 50, 70, 100, 150, 200, 300, 400, 500, 700, 1000,
  1500, 2000, 3000, 4000, 5000, 7000, 10000, 15000, 20000];
function rpFreqTicks(ctx, X, dpr, h, faint) {
  ctx.textAlign = "center";
  let lastX = -Infinity;
  for (const f of RP_FREQ_TICKS) {
    if (f < rpZoom.f0 || f > rpZoom.f1) continue;
    const x = X(f);
    if (x - lastX < 34 * dpr) continue;
    lastX = x;
    ctx.fillStyle = faint;
    ctx.fillText(f >= 1000 ? `${f / 1000}k` : String(f), x, h - 6 * dpr);
  }
}
/* 占位骨架：先把坐标框、dB 网格与频率刻度画出来，再落一条中性参考线。
   空态不该是一句漂浮的文案——看着框就知道这里会放什么（与声场钻石同理）。 */
function drawFreqSkeleton(ctx, w, h, dpr) {
  const { padL, padR, padT, padB } = rpPlotPad(dpr);
  const plotW = w - padL - padR, plotH = h - padT - padB;
  if (plotW <= 0 || plotH <= 0) return;
  const fMin = Math.log10(rpZoom.f0), fMax = Math.log10(rpZoom.f1);
  const X = (f) => padL + ((Math.log10(Math.max(rpZoom.f0, Math.min(rpZoom.f1, f))) - fMin) / (fMax - fMin)) * plotW;
  const light = document.documentElement.dataset.mode === "light";
  const faint = cssVar("--c-text-faint", "#6b6378");
  ctx.font = `${10.5 * dpr}px sans-serif`;
  ctx.textAlign = "right";
  for (let v = -10; v >= -60; v -= 10) {
    const y = padT + ((-5 - v) / 60) * plotH;
    ctx.strokeStyle = light ? "rgba(0,0,0,0.08)" : "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = faint;
    ctx.fillText(String(v), padL - 6 * dpr, y + 3 * dpr);
  }
  const y0 = padT + plotH / 2;
  ctx.strokeStyle = light ? "rgba(0,0,0,0.16)" : "rgba(255,255,255,0.14)";
  ctx.lineWidth = dpr;
  ctx.beginPath(); ctx.moveTo(padL, y0); ctx.lineTo(w - padR, y0); ctx.stroke();
  rpFreqTicks(ctx, X, dpr, h, faint);
  ctx.textAlign = "center";
  ctx.fillText("处理完成后显示「原始 / 处理后」两条曲线", padL + plotW / 2, y0 - 9 * dpr);
}
function drawSpectrumChart(canvas, anim) {
  if (canvas) rpClipAt(canvas, anim && anim.reveal != null ? anim.reveal : 1);
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, w, h);
  ctx.font = `${10.5 * dpr}px sans-serif`;
  const { centers, curves } = reportCurves();
  if (!centers) {
    drawFreqSkeleton(ctx, w, h, dpr);
    return;
  }
  /* 换版本时传进来的处理后曲线是前后两版的插值：纵轴量程由插值后的数据反推，
     于是曲线和刻度一起平滑走到新状态，而不是硬切一张新图。 */
  const shown = anim && anim.outDb;
  const plot = shown
    ? curves.map((c) => (c.base || c.db.length !== shown.length ? c : { ...c, db: shown }))
    : curves;
  const fMin = Math.log10(rpZoom.f0), fMax = Math.log10(rpZoom.f1);
  const { padL, padR, padT, padB } = rpPlotPad(dpr);
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;
  const X = (f) => padL + ((Math.log10(Math.max(rpZoom.f0, Math.min(rpZoom.f1, f))) - fMin) / (fMax - fMin)) * plotW;
  /* 纵轴非线性：全曲低频滚降动辄 −90 dB，与可听段等比例会把曲线压成一条平线。
     按可见采样的百分位划出「核心区」，两端离群段以 1/8 倍率折进窄带，
     于是每格 dB 的高度不相等——差异读得出来，代价是轴必须标出分段边界。 */
  const vis = [];
  plot.forEach((c) => centers.forEach((f, k) => {
    const v = c.db[k];
    if (Number.isFinite(v) && f >= rpZoom.f0 && f <= rpZoom.f1) vis.push(v);
  }));
  const TAIL = 8;
  let coreHi = -20, coreLo = -40, hi = 0, lo = -90;
  if (vis.length > 3) {
    const sorted = vis.slice().sort((a, b) => a - b);
    const at = (q) => sorted[Math.max(0, Math.min(sorted.length - 1, Math.round(q * (sorted.length - 1))))];
    coreHi = at(0.96); coreLo = at(0.04);
    const pad = Math.min(6, Math.max(2, (coreHi - coreLo) * 0.08));
    coreHi += pad; coreLo -= pad;
    hi = Math.max(coreHi, at(1) + 1);
    lo = Math.min(coreLo, Math.max(FLOOR_DB, at(0) - 2));
    if (coreHi - coreLo < 6) { const m = (coreHi + coreLo) / 2; coreHi = m + 3; coreLo = m - 3; }
  }
  const coreSpan = Math.max(1e-6, coreHi - coreLo);
  const upTail = Math.max(0, hi - coreHi), dnTail = Math.max(0, coreLo - lo);
  const coreH = plotH / (1 + (upTail + dnTail) / (coreSpan * TAIL));
  const tailPx = plotH - coreH;
  const upPx = tailPx > 0 && (upTail + dnTail) > 0 ? tailPx * upTail / (upTail + dnTail) : 0;
  const dnPx = tailPx - upPx;
  const yCoreHi = padT + upPx, yCoreLo = yCoreHi + coreH;
  const Y = (v) => {
    if (v >= coreHi) return padT + (1 - (v - coreHi) / Math.max(1e-6, hi - coreHi)) * upPx;
    if (v <= coreLo) return yCoreLo + Math.min(1, (coreLo - v) / Math.max(1e-6, coreLo - lo)) * dnPx;
    return yCoreHi + (coreHi - v) / coreSpan * coreH;
  };
  const light = document.documentElement.dataset.mode === "light";
  const faint = cssVar("--c-text-faint", "#6b6378");
  const pal = rpThemeColors();
  const step = [1, 2, 3, 5, 6, 10, 15, 20, 30].find((s) => coreSpan / s <= 8) || 40;
  const gridVals = [];
  for (let v = Math.ceil(coreLo / step) * step; v <= coreHi; v += step) gridVals.push(v);
  /* 压缩段按各自窄带可用高度反推档距，且不贴着边界（会与分段边界线叠在一起） */
  [[lo, coreLo, dnPx], [coreHi, hi, upPx]].forEach(([a, b, px]) => {
    const span = b - a;
    if (!(span > 0 && px > 14 * dpr)) return;
    const room = Math.max(1, Math.floor(px / (16 * dpr)));
    const s = [1, 2, 5, 10, 20, 30].find((x) => span / x <= room) || 40;
    for (let v = Math.ceil((a + span * 0.15) / s) * s; v < b - span * 0.1; v += s) gridVals.push(v);
  });
  ctx.textAlign = "right";
  gridVals.forEach((v) => {
    const y = Y(v);
    ctx.strokeStyle = light ? "rgba(0,0,0,0.08)" : "rgba(255,255,255,0.06)";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = faint;
    ctx.fillText(String(Math.round(v)), padL - 6 * dpr, y + 3 * dpr);
  });
  /* 分段边界：让「这段被压过」在图上可读，而不是悄悄改了比例 */
  const edge = (v) => {
    const y = Y(v);
    ctx.strokeStyle = light ? "rgba(0,0,0,0.2)" : "rgba(255,255,255,0.16)";
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    if (y - padT > 12 * dpr && padT + plotH - y > 12 * dpr) {
      ctx.fillStyle = faint; ctx.font = `${9 * dpr}px sans-serif`;
      ctx.fillText("1:8", w - padR - 2 * dpr, y - 5 * dpr);
      ctx.font = `${10.5 * dpr}px sans-serif`;
    }
  };
  if (dnPx > 0) edge(coreLo);
  if (upPx > 0) edge(coreHi);
  /* 差异直接落在两条曲线之间：处理后高于原始=提升色，低于=衰减色 */
  const base = plot.find((c) => c.base), after = plot.find((c) => !c.base);
  ctx.save();
  ctx.beginPath(); ctx.rect(padL, padT, plotW, plotH); ctx.clip();
  if (base && after) {
    const pts = [];
    centers.forEach((f, k) => {
      const a = base.db[k], b = after.db[k];
      if (Number.isFinite(a) && Number.isFinite(b) && f >= rpZoom.f0 && f <= rpZoom.f1)
        pts.push([X(f), Y(a), Y(b), b - a]);
    });
    for (let i = 1; i < pts.length; i++) {
      const [x0, a0, b0, d0] = pts[i - 1], [x1, a1, b1, d1] = pts[i];
      ctx.beginPath();
      ctx.moveTo(x0, a0); ctx.lineTo(x1, a1); ctx.lineTo(x1, b1); ctx.lineTo(x0, b0);
      ctx.closePath();
      ctx.globalAlpha = 0.34;
      ctx.fillStyle = (d0 + d1) / 2 >= 0 ? pal.boost : pal.cut;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }
  /* 一律实线：原始=中性色，处理后=主题色，处理后高出原始的段落再叠一遍对比色。
     提升位置因此直接落在曲线上，不需要再对一张独立的 Δ 子图。 */
  const EPS_DB = 0.2;
  const strokeSegments = (pts, keep) => {
    ctx.beginPath();
    for (let i = 1; i < pts.length; i++) {
      const p = pts[i - 1], q = pts[i];
      if (!p || !q || (keep && !keep(i))) continue;
      ctx.moveTo(p[0], p[1]); ctx.lineTo(q[0], q[1]);
    }
    ctx.stroke();
  };
  ctx.lineCap = "round";
  plot.forEach((c) => {
    const pts = [];
    centers.forEach((f, k) => {
      const v = c.db[k];
      // 视野外的频点记 null 而非钳到画布边缘，否则会画出两端假的垂直线
      if (!Number.isFinite(v) || f < rpZoom.f0 || f > rpZoom.f1) { pts.push(null); return; }
      const bv = c.base || !base ? null : base.db[k];
      pts.push([X(f), Y(v), Number.isFinite(bv) ? v - bv : 0]);
    });
    ctx.lineWidth = 1.6 * dpr;
    ctx.strokeStyle = c.base ? pal.before : pal.after;
    strokeSegments(pts);
    if (!c.base && base) {
      ctx.strokeStyle = pal.boost;
      strokeSegments(pts, (i) => (pts[i - 1][2] + pts[i][2]) / 2 > EPS_DB);
    }
  });
  ctx.restore();
  rpFreqTicks(ctx, X, dpr, h, faint);   // 共享 X 轴刻度（画布最底部）
  /* 重放钩子：整幅图只是一次数据快照，逐帧改的仍是「画哪条曲线、画到哪儿」 */
  const target = (curves.find((c) => !c.base) || {}).db || null;
  const prev = rpShown && rpShown.outDb;
  const canMorph = target && prev && prev.length === target.length;
  rpHook((u, reveal) => {
    drawSpectrumChart(canvas, {
      outDb: reveal || !canMorph ? null : target.map((v, i) => rpLerp(prev[i], v, u)),
      reveal: reveal ? u : null,
    });
  });
  if (rpPending) rpPending.outDb = target ? target.slice() : null;
}
/* 缩放/平移交互：只绑定一次；hover 提示也在此时挂载 */
(() => {
  const canvas = $("rp-spectrum");
  if (!canvas) return;
  const L0 = Math.log(20), L1 = Math.log(20000);
  /* 最小跨度 2.5 倍 ≈ 4 个 1/3 倍频程中心；再窄就只剩一两个点，
     曲线退化成一截竖线，缩放反而让人误判成渲染故障。 */
  const MIN_SPAN = Math.log(2.5);
  let zoomAnim = null;
  /* 对数域内先定跨度、再平移窗口回域内：钳位只移动中心、不改变跨度。
     旧写法逐端钳位会让锚点偏向的一侧反复吃掉跨度，放大后缩不回全域。 */
  const setView = (c0, c1, instant) => {
    const span = Math.min(L1 - L0, Math.max(MIN_SPAN, c1 - c0));
    const mid = Math.min(L1 - span / 2, Math.max(L0 + span / 2, (c0 + c1) / 2));
    const to = { f0: mid - span / 2, f1: mid + span / 2 };
    if (instant) {
      if (zoomAnim) { cancelAnimationFrame(zoomAnim); zoomAnim = null; }
      rpZoom.f0 = Math.exp(to.f0); rpZoom.f1 = Math.exp(to.f1);
      drawSpectrumChart(canvas);
      return;
    }
    /* 视野突变会让曲线整片跳动；用一小段缓动把频率与纵轴量程一起推过去，
       读得出「是视野变了」而不是「图坏了」。再次操作时从当前动画位置取向。 */
    const from = { f0: Math.log(rpZoom.f0), f1: Math.log(rpZoom.f1) };
    if (Math.abs(from.f0 - to.f0) < 1e-4 && Math.abs(from.f1 - to.f1) < 1e-4) return;
    const t0 = performance.now(), dur = 190;
    if (zoomAnim) cancelAnimationFrame(zoomAnim);
    const step = (now) => {
      const u = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - u, 3);
      rpZoom.f0 = Math.exp(from.f0 + (to.f0 - from.f0) * e);
      rpZoom.f1 = Math.exp(from.f1 + (to.f1 - from.f1) * e);
      drawSpectrumChart(canvas);
      zoomAnim = u < 1 ? requestAnimationFrame(step) : null;
    };
    zoomAnim = requestAnimationFrame(step);
  };
  const fAt = (e) => {
    const rect = canvas.getBoundingClientRect();
    // 绘图内边距以 CSS 像素为基准（绘制时才乘 dpr），这里与 rect 同单位
    const frac = Math.max(0, Math.min(1, (e.clientX - rect.left - 36) / Math.max(1, rect.width - 48)));
    const a = Math.log(rpZoom.f0), b = Math.log(rpZoom.f1);
    return Math.exp(a + (b - a) * frac);   // 按当前视野反解，不能用固定全域
  };
  canvas.addEventListener("wheel", (e) => {
    if ($("pane-report").hidden) return;
    e.preventDefault();
    const anchor = Math.log(Math.min(20000, Math.max(20, fAt(e))));
    const k = e.deltaY > 0 ? 1.25 : 1 / 1.25;
    setView(anchor + (Math.log(rpZoom.f0) - anchor) * k,
            anchor + (Math.log(rpZoom.f1) - anchor) * k);
  }, { passive: false });
  let panning = false, panX = 0;
  canvas.addEventListener("pointerdown", (e) => {
    panning = true; panX = e.clientX;
    canvas.setPointerCapture(e.pointerId);
    canvas.style.cursor = "grabbing";
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!panning) {
      canvas.style.cursor = "grab";
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const dLog = ((e.clientX - panX) / Math.max(1, rect.width - 48))
      * (Math.log(rpZoom.f1) - Math.log(rpZoom.f0));
    panX = e.clientX;
    setView(Math.log(rpZoom.f0) - dLog, Math.log(rpZoom.f1) - dLog, true);
  });
  const end = () => { panning = false; canvas.style.cursor = "grab"; };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("dblclick", () => setView(L0, L1));
  attachRpTip(canvas);
})();

/* ── 报告数据配对：原始（首个有报告的选中版本的 input.metrics）+ 其 output ── */
function rpNum(v) { const n = Number(v); return Number.isFinite(n) ? n : null; }
function rpBandWidth(m, key) {
  const b = m && m.band_widths && m.band_widths[key];
  return rpNum(b && b.width);
}
/* 分频段 S/M 电平比（dB）＝听感宽度的直接读数：宽度比 sideE/(midE+sideE) 是它
   的非线性压缩，同一件事 0.03→0.02 只有 −1.4 dB，写成 −23% 会把轻微收窄读成
   大幅收窄。报告没有该字段（旧成品）时由宽度比反推，读数口径不变。 */
function rpBandSmDb(m, key) {
  const b = m && m.band_widths && m.band_widths[key];
  const db = rpNum(b && b.side_mid_db);
  if (db != null) return db;
  const w = rpNum(b && b.width);
  return (w == null || !(w > 0) || w >= 1) ? null : 10 * Math.log10(w / (1 - w));
}
function rpPair() {
  const rep = rp.sel && rp.reports.get(rp.sel);
  const outM = rep && ((rep.output && rep.output.metrics) || null);
  if (!outM) return null;
  const ver = (rp.versions.find((v) => v.path === rp.sel) || {}).version;
  return { inM: (rep.input && rep.input.metrics) || null, outM, ver, path: rp.sel };
}
/* 1/3 倍频程相对能量（响度无关）→ 指定频段能量变化 dB */
function rpBandDelta(lo, hi) {
  const { centers, curves } = reportCurves();
  const base = curves.find((c) => c.base), after = curves.find((c) => !c.base);
  if (!centers || !base || !after) return null;
  let si = 0, so = 0;
  for (let k = 0; k < centers.length; k++) {
    const f = centers[k];
    if (f < lo || f >= hi) continue;
    const a = base.db[k], b = after.db[k];
    if (Number.isFinite(a) && Number.isFinite(b)) { si += Math.pow(10, a / 10); so += Math.pow(10, b / 10); }
  }
  return si > 0 && so > 0 ? 10 * Math.log10(so / si) : null;
}

/* ── 处理摘要 KPI ── */
const RP_KPI_ICONS = {
  bass: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 12h2l2-6 3 15 3-11 2 6h6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  air: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 14c4 0 4-6 8-6s4 6 8 6" stroke-linecap="round"/><circle cx="12" cy="17.5" r="1.4" fill="currentColor" stroke="none"/></svg>',
  dyn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 18V6M9 18V9M14 18v-6M19 18V4" stroke-linecap="round"/></svg>',
  wide: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 5v14M6 8l-3 4 3 4M18 8l3 4-3 4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};
function rpDuration(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function renderHead() {
  const name = $("rp-name"), chips = $("rp-mchips"), time = $("rp-time");
  if (!name || !chips) return;
  const pair = rpPair();
  const rep = pair && rp.reports.get(pair.path);
  const fmt = (rep && rep.meta && rep.meta.output_format) || null;
  name.textContent = pair ? stemOf(pair.path) : "未选择成品";
  const meta = (rep && rep.meta) || {};
  const items = [];
  if (fmt) {
    items.push(fmt.format || "WAV");
    items.push(`${(fmt.sample_rate / 1000).toFixed(1)} kHz`);
    if (fmt.bit_depth) items.push(`${fmt.bit_depth} bit`);
    items.push(fmt.channels === 1 ? "单声" : "立体声");
    items.push(rpDuration(fmt.duration_seconds));
  }
  chips.innerHTML = "";
  items.forEach((text) => {
    const c = document.createElement("span");
    c.className = "rp-mchip";
    c.textContent = text;
    chips.appendChild(c);
  });
  if (!items.length) {
    const c = document.createElement("span");
    c.className = "rp-mchip";
    c.textContent = pair ? "该成品无格式信息（报告版本较早）" : "处理完成后显示";
    chips.appendChild(c);
  }
  const gen = String(meta.generated_at || "");
  time.textContent = gen.length >= 16 ? `${gen.slice(0, 10)} ${gen.slice(11, 16)}` : "—";
}
/* ── 报告动效：数字与图形都只是数据的一次快照 ──
   reveal（从别的页切进来）：数字从 0 长到显示值，画布按左右顺序画出来；
   morph（换成品版本）：所有数值、曲线、钻石从上一次真正画出来的状态插值到新状态。
   给整块卡片套淡入是没用的——那只说明"换了张图"，读不出数值怎么变的。 */
let rpShown = null;      // 上一次画到屏幕上的数值（morph 的起点）
let rpPending = null;    // 本次渲染算出的数值，渲染结束后提交为 rpShown
let rpHooks = [];        // 本次渲染登记的重放钩子 (u, reveal) => void
let rpRaf = 0;           // 当前重放的帧句柄
let rpGuard = 0;         // 收尾兜底定时器（rAF 不推进时也要落到终态）
const rpEase = (t) => 1 - Math.pow(1 - t, 3);
const rpLerp = (a, b, u) => (a == null || b == null ? b : a + (b - a) * u);
function rpHook(fn) { if (rpPending) rpHooks.push(fn); }

function rpRunAnim(mode) {
  const hooks = rpHooks;
  rpHooks = [];
  clearTimeout(rpGuard);
  cancelAnimationFrame(rpRaf);
  if (!hooks.length || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const reveal = mode === "reveal";
  const t0 = performance.now(), ms = reveal ? 620 : 420;
  const finish = () => {
    cancelAnimationFrame(rpRaf);
    hooks.forEach((h) => h(1, reveal));
  };
  const frame = (now) => {
    const t = Math.min(1, (now - t0) / ms), u = rpEase(t);
    hooks.forEach((h) => h(u, reveal));
    if (t < 1) rpRaf = requestAnimationFrame(frame);
  };
  hooks.forEach((h) => h(0, reveal));      // 先落到起点，否则首帧会闪一下终态
  rpRaf = requestAnimationFrame(frame);
  /* 窗口被遮挡时 rAF 可以整段不推进，重放会停在起点（读数全是 0）。
     定时器仍会触发，兜底把最后一帧补上。 */
  rpGuard = setTimeout(finish, ms + 250);
}
/* 「画出来」用的是裁剪而不是重绘整幅：位图和曲线都只写一次，逐帧推进的是遮挡边界 */
function rpClipAt(el, u) {
  if (el) el.style.clipPath = u >= 1 ? "" : `inset(0 ${((1 - u) * 100).toFixed(2)}% 0 0)`;
}
/* 换频谱视图整幅渐变，不做左右擦除：把当前画面拍成快照盖在新画面上淡出（dissolve），
   新画面本身一次性画好，读起来是「这一幅换成那一幅」而不是「又画了一遍」。 */
let rpSpecSnap = null;
function rpSpecFade(apply, ms = 240) {
  const canvas = $("rp-spec");
  rpSpecSnap?.remove();
  rpSpecSnap = null;
  if (!canvas || !canvas.width || !canvas.height ||
      matchMedia("(prefers-reduced-motion: reduce)").matches) { apply(); return; }
  const snap = document.createElement("canvas");
  snap.width = canvas.width;
  snap.height = canvas.height;
  snap.getContext("2d").drawImage(canvas, 0, 0);
  snap.style.cssText =
    "position:absolute;pointer-events:none;z-index:1;" +
    `left:${canvas.offsetLeft + canvas.clientLeft}px;top:${canvas.offsetTop + canvas.clientTop}px;` +
    `width:${canvas.clientWidth}px;height:${canvas.clientHeight}px;`;
  canvas.parentElement.appendChild(snap);
  rpSpecSnap = snap;
  apply();
  const anim = snap.animate([{ opacity: 1 }, { opacity: 0 }], { duration: ms, easing: "ease" });
  const done = () => { if (rpSpecSnap === snap) rpSpecSnap = null; snap.remove(); };
  anim.finished.then(done, done);
}
function renderSummary() {
  const wrap = $("rp-summary");
  if (!wrap) return;
  wrap.innerHTML = "";
  const pair = rpPair();
  const lra = pair && pair.inM && rpNum(pair.outM.lra_lu) != null && rpNum(pair.inM.lra_lu) != null
    ? pair.outM.lra_lu - pair.inM.lra_lu : null;
  let wide = null;
  const rpSigned = (v, dp) => (v == null ? "—"
    : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(dp));
  /* 宽度按中/高频各自 S/M 比（dB）等权平均后再取前后差：
     能量加权的总宽几乎只跟中频走（中频能量远高于高频），高频的实际
     拓宽被中频吃掉；低频则被刻意 mono 化，计入只会永远读出变窄。 */
  const smDb = (m, key) => rpBandSmDb(m, key);
  if (pair) {
    const parts = ["mid", "high"].map((k) => {
      const a = smDb(pair.inM, k), b = smDb(pair.outM, k);
      return a == null || b == null ? null : b - a;
    }).filter((v) => v != null);
    wide = parts.length ? parts.reduce((s, v) => s + v, 0) / parts.length : null;
  }
  const cards = [
    { ic: "bass", label: "低频能量", sub: "20–250 Hz 变化", v: rpBandDelta(20, 250), unit: "dB", dp: 1 },
    { ic: "air", label: "空气感能量", sub: "3–8 kHz 变化", v: rpBandDelta(3000, 8000), unit: "dB", dp: 1 },
    { ic: "dyn", label: "动态范围", sub: "LRA 变化", v: lra, unit: "LU", dp: 1, eps: 0.05 },
    { ic: "wide", label: "立体声宽度", sub: "中/高频 S/M 变化", v: wide, unit: "dB", dp: 2, eps: 0.05 },
  ];
  cards.forEach((c) => {
    const el = document.createElement("div");
    const eps = c.eps == null ? 0.05 : c.eps;
    const dir = c.v == null ? "flat" : c.v > eps ? "up" : c.v < -eps ? "down" : "flat";
    el.className = "rp-kpi " + dir;
    /* 方向不另印箭头：数字自带正负号，颜色已经表态，右侧再挂一只三角只是噪声 */
    const val = rpSigned(c.v, c.dp);
    el.innerHTML =
      `<span class="rp-kpi-ic">${RP_KPI_ICONS[c.ic]}</span>` +
      `<span class="rp-kpi-txt"><span class="rp-kpi-label">${c.label}</span>` +
      `<span class="rp-kpi-sub">${c.sub}</span></span>` +
      `<span class="rp-kpi-val"><span class="rp-kpi-num">${val}</span>` +
      `<span class="rp-kpi-unit">${c.v == null ? "" : c.unit}</span></span>`;
    wrap.appendChild(el);
  });
  const nums = [...wrap.querySelectorAll(".rp-kpi-num")];
  const prevK = rpShown && rpShown.kpi;
  rpHook((u, reveal) => {
    cards.forEach((c, i) => {
      const from = reveal ? 0 : (prevK ? prevK[i] : null);
      nums[i].textContent = rpSigned(rpLerp(from, c.v, u), c.dp);
    });
  });
  if (rpPending) rpPending.kpi = cards.map((c) => c.v);
}

/* ── 响度与动态：哑铃行（每行用该属性的常用总量程） ── */
const RP_DYN = [
  { label: "响度", sub: "LUFS", key: "integrated_lufs", dp: 1, ref: [-20, -6],
    tip: "整合响度（LUFS）：整曲感知响度，流媒体目标常约 -14。" },
  { label: "真实峰值", sub: "dBTP", key: "true_peak_4x_dbtp", dp: 1, ref: [-6, 0],
    tip: "4x 过采样真峰值（dBTP）：超过 0 会削波失真。" },
  { label: "动态范围", sub: "LU", key: "lra_lu", dp: 1, ref: [0, 12],
    tip: "响度范围（LU）：最响与最静段落差距，越大越有起伏。" },
  { label: "峰值响度比", sub: "dB", key: "crest_factor_db", dp: 1, ref: [6, 18],
    tip: "峰值与平均能量之比：越高越有冲击力，越低越压。" },
  { label: "立体声相关性", sub: "", key: "stereo_correlation", dp: 2, ref: [-1, 1],
    tip: "左右相关：近 1 声像稳，近 0 更宽散，负值提示反相风险。" },
];
const rpNiceStep = (range, want) => {
  const raw = range / want;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw) || p * 10;
};
const rpNumLabel = (v, dp) => (v < 0 ? `−${Math.abs(v).toFixed(dp)}` : v.toFixed(dp));
function renderDynamics() {
  const wrap = $("rp-dyn");
  if (!wrap) return;
  wrap.innerHTML = "";
  const pair = rpPair();
  const rows = [];
  RP_DYN.forEach((d) => {
    const b = pair && pair.inM ? rpNum(pair.inM[d.key]) : null;
    const a = pair ? rpNum(pair.outM[d.key]) : null;
    const fmt = (v) => (v == null ? "—" : rpNumLabel(v, d.dp));
    /* 量程取该属性的常用总范围：换一首歌轴不会变，圆点位置读得出绝对高低，
       不同文件之间也能横向比较。只有取值超出默认范围时才带余量外扩。 */
    let lo = d.ref[0], hi = d.ref[1];
    const vals = [b, a].filter((v) => v != null);
    if (vals.length) {
      const margin = (hi - lo) * 0.04;
      lo = Math.min(lo, Math.min(...vals) - margin);
      hi = Math.max(hi, Math.max(...vals) + margin);
    }
    const pct = (v) => Math.max(0, Math.min(1, (v - lo) / (hi - lo))) * 100;
    const step = rpNiceStep(hi - lo, 4);
    const tdp = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
    const xb = b == null ? null : pct(b), xa = a == null ? null : pct(a);
    /* 圆点读数先算好：刻度与某个圆点同值且几乎同位时不再重复印一遍 */
    const marks0 = [[b, xb], [a, xa]].filter(([, x]) => x != null);
    const halfLsb = Math.pow(10, -d.dp) / 2;
    let ticks = "";
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 0.01; v += step) {
      const x = pct(v);
      const align = x <= 2 ? "start" : x >= 98 ? "end" : "mid";
      const dup = marks0.some(([mv, mx]) => Math.abs(mx - x) < 6 && Math.abs(v - mv) < halfLsb);
      ticks += `<span class="rp-dyn-tick" style="left:${x}%"></span>` +
        (dup ? "" : `<span class="rp-dyn-sl rp-dyn-sl-${align}" style="left:${x}%">${rpNumLabel(v, tdp)}</span>`);
    }
    let conn = "";
    if (b != null && a != null) {
      const x0 = Math.min(pct(b), pct(a)), x1 = Math.max(pct(b), pct(a));
      conn = `<span class="rp-dyn-conn" style="left:${x0}%;width:${x1 - x0}%"></span>`;
    }
    /* 圆点 + 上方数值：越界读数钳位在轨道两端，数字仍显示真实值。
       前后距离小于一个读数宽度时两个上标会叠成一团，合并成居中「前 → 后」。 */
    const dot = (cls, x) => `<span class="rp-dyn-dot ${cls}" style="left:${x}%"></span>`;
    const num = (cls, x, txt) => `<span class="rp-dyn-num ${cls}` +
      `${x < 9 ? " rn-start" : x > 91 ? " rn-end" : ""}" style="left:${x}%">${txt}</span>`;
    /* 行方向决定「处理后」一侧的颜色：升=对比色、降=主题色。差值小于半个末位
       读数时两个数字印出来一样，按持平处理，避免读数不变而颜色却在表态。 */
    const dv = (b == null || a == null) ? null : a - b;
    const dir = dv == null || Math.abs(dv) < halfLsb ? "flat" : dv > 0 ? "up" : "down";
    let marks = (xb != null ? dot("before", xb) : "") + (xa != null ? dot("after", xa) : "");
    if (xb == null && xa == null) {
      /* 空态：读数槽留在原位（与声场卡「— → —」同一读法）。不复用 .both，
         它那侧的颜色是方向色，会把「还没数据」读成「衰减」。 */
      marks = `<span class="rp-dyn-num empty">— → —</span>`;
    } else if (xb != null && xa != null && Math.abs(xb - xa) < 11) {
      const mid = (xb + xa) / 2;
      const align = mid < 12 ? " rn-start" : mid > 88 ? " rn-end" : "";
      marks += `<span class="rp-dyn-num both${align}" style="left:${mid}%">` +
        `<i>${fmt(b)}</i> → <b>${fmt(a)}</b></span>`;
    } else {
      if (xb != null) marks += num("before", xb, fmt(b));
      if (xa != null) marks += num("after", xa, fmt(a));
    }
    const row = document.createElement("div");
    row.className = "rp-dyn-row " + dir;
    row.title = d.tip;
    row.innerHTML =
      `<div class="rp-dyn-label"><b>${d.label}</b>${d.sub ? `<i>${d.sub}</i>` : ""}</div>` +
      `<div class="rp-dyn-track">${ticks}${conn}${marks}</div>`;
    wrap.appendChild(row);
    rows.push({ d, b, a, lo, hi,
      dotB: row.querySelector(".rp-dyn-dot.before"), dotA: row.querySelector(".rp-dyn-dot.after"),
      conn: row.querySelector(".rp-dyn-conn"),
      numB: row.querySelector(".rp-dyn-num.before"), numA: row.querySelector(".rp-dyn-num.after"),
      both: row.querySelector(".rp-dyn-num.both") });
  });
  const prevD = rpShown && rpShown.dyn;
  if (pair) rpHook((u, reveal) => {
    const pctOf = (r, v) => Math.max(0, Math.min(1, (v - r.lo) / (r.hi - r.lo))) * 100;
    rows.forEach((r, i) => {
      const f = prevD && prevD[i];
      const b = reveal ? rpLerp(0, r.b, u) : rpLerp(f && f[0], r.b, u);
      const a = reveal ? rpLerp(0, r.a, u) : rpLerp(f && f[1], r.a, u);
      const xb = b == null ? null : pctOf(r, b), xa = a == null ? null : pctOf(r, a);
      const fmt = (v) => (v == null ? "—" : rpNumLabel(v, r.d.dp));
      if (r.dotB) r.dotB.style.left = xb + "%";
      if (r.dotA) r.dotA.style.left = xa + "%";
      if (r.conn && xb != null && xa != null) {
        r.conn.style.left = Math.min(xb, xa) + "%";
        r.conn.style.width = Math.abs(xa - xb) + "%";
      }
      if (r.numB) { r.numB.textContent = fmt(b); r.numB.style.left = xb + "%"; }
      if (r.numA) { r.numA.textContent = fmt(a); r.numA.style.left = xa + "%"; }
      if (r.both) {
        r.both.style.left = (xb + xa) / 2 + "%";
        r.both.innerHTML = `<i>${fmt(b)}</i> → <b>${fmt(a)}</b>`;
      }
    });
  });
  if (rpPending) rpPending.dyn = rows.map((r) => [r.b, r.a]);
}

/* ── 立体声场：3 频段钻石图（底角=听者，两侧角=本域角度上限，顶角=中置） ── */
const RP_FANS = [
  { key: "low", label: "低频", range: "20–250 Hz" },
  { key: "mid", label: "中频", range: "250 Hz–4 kHz" },
  { key: "high", label: "高频", range: "4–20 kHz" },
];
const rpFanDelta = (wi, wo) =>
  (wi == null || wo == null || !(wi > 0)) ? null : (wo - wi) / wi * 100;
/* 底部读数取 S/M 电平比的前后差（dB）：与听感宽度近似线性。宽度比百分比只在
   没有 dB 字段可读时兜底，绝不作为主读数列出（−23% 与 −1.4 dB 是同一件事，
   前者会让人以为声场被大幅收窄）。±0.5 dB 内读作持平。 */
const RP_FAN_FLAT_DB = 0.5;
const rpFanDb = (di, dok) => (di == null || dok == null ? null : dok - di);
const rpFanDbText = (d) => (d == null ? "—"
  : `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d).toFixed(1)} dB`);
const rpFanPctText = (d) => (d == null ? "—"
  : `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d).toFixed(0)}%`);
function renderStereoFans() {
  const wrap = $("rp-fans");
  if (!wrap) return;
  wrap.innerHTML = "";
  const pair = rpPair();
  const pal = rpThemeColors();
  const before = pal.before;
  const fans = [];
  RP_FANS.forEach((f) => {
    const wi = pair ? rpBandWidth(pair.inM, f.key) : null;
    const wo = pair ? rpBandWidth(pair.outM, f.key) : null;
    const di = pair ? rpBandSmDb(pair.inM, f.key) : null;
    const dok = pair ? rpBandSmDb(pair.outM, f.key) : null;
    const db = rpFanDb(di, dok);
    const delta = rpFanDelta(wi, wo);
    const dir = db == null ? "flat"
      : db > RP_FAN_FLAT_DB ? "up" : db < -RP_FAN_FLAT_DB ? "down" : "flat";
    /* 变宽=对比色、变窄=主题色，与频响、哑铃行、KPI 同一套读法 */
    const afterCol = dir === "up" ? pal.boost : pal.after;
    const block = document.createElement("div");
    block.className = "rp-fan " + dir;
    block.title = "钻石张角 = 该频段 Side/Mid 等效声像角；标题行为宽度比 "
      + "sideE/(midE+sideE)，底部为该频段 S/M 电平变化 dB（±0.5 dB 内读作持平），"
      + "角度域按本频段前后取值自动取档。";
    block.innerHTML =
      `<div class="rp-fan-hd"><b>${f.label}</b><i>${f.range}</i>` +
      `<span class="rp-fan-num"><span class="before">${wi == null ? "—" : wi.toFixed(2)}</span>` +
      `<span class="arrow">→</span><span class="after">${wo == null ? "—" : wo.toFixed(2)}</span></span></div>` +
      `<canvas></canvas>` +
      `<div class="rp-fan-ft">${db == null ? rpFanPctText(delta) : rpFanDbText(db)}</div>`;
    wrap.appendChild(block);
    const canvas = block.querySelector("canvas");
    /* 角度档位按最终取值定死并带进动画：否则钻石从 0 长出来时会边长边换档，
       框线一跳一跳，读起来像图坏了而不是声场在展开。 */
    const peak = Math.max(rpHalfAngle(wi) || 0, rpHalfAngle(wo) || 0) * 180 / Math.PI;
    const dom = RP_FAN_DOMAINS.find((d) => d >= peak * 1.15) || 45;
    drawFieldDiamond(canvas, wi, wo, before, afterCol, dom);
    fans.push({ canvas, wi, wo, di, dok, col: afterCol, dom, db,
      bEl: block.querySelector(".rp-fan-num .before"),
      aEl: block.querySelector(".rp-fan-num .after"),
      ftEl: block.querySelector(".rp-fan-ft") });
  });
  const prev = rpShown && rpShown.bands;
  const num = (v) => (v == null ? "—" : v.toFixed(2));
  rpHook((u, reveal) => {
    fans.forEach((r, i) => {
      const p = prev && prev[i];
      const wi = reveal ? rpLerp(0, r.wi, u) : rpLerp(p && p[0], r.wi, u);
      const wo = reveal ? rpLerp(0, r.wo, u) : rpLerp(p && p[1], r.wo, u);
      drawFieldDiamond(r.canvas, wi, wo, before, r.col, r.dom);
      if (r.bEl) r.bEl.textContent = num(wi);
      if (r.aEl) r.aEl.textContent = num(wo);
      /* 底部 dB：换版本时随当帧的前后取值走（差值本身在动），
         进页面时钻石是整体展开，比值恒定，按最终值从 0 长上来更诚实。 */
      if (r.ftEl) r.ftEl.textContent = r.db == null
        ? rpFanPctText(rpFanDelta(wi, wo))
        : rpFanDbText(reveal ? r.db * u : rpFanDb(r.di, r.dok));
    });
  });
  if (rpPending) rpPending.bands = fans.map((r) => [r.wi, r.wo]);
}
/* width∈[0,1] → S/M 幅度比 sqrt(w/(1-w)) → 等效半张角 atan(...)；width≥1 记满 45° */
function rpHalfAngle(wd) {
  if (wd == null || !(wd >= 0) || wd >= 1) return wd == null ? null : Math.PI / 4;
  return Math.atan(Math.sqrt(wd / (1 - wd)));
}
const RP_FAN_DOMAINS = [15, 20, 25, 30, 40, 45];   // 度：外框两侧角对应的半张角档位
function drawFieldDiamond(canvas, wBefore, wAfter, before, after, fixedDom) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
  if (!w || !h) return;
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, w, h);
  const light = document.documentElement.dataset.mode === "light";
  const hb = rpHalfAngle(wBefore), ha = rpHalfAngle(wAfter);
  const peakDeg = Math.max(hb || 0, ha || 0) * 180 / Math.PI;
  const domDeg = fixedDom || (RP_FAN_DOMAINS.find((d) => d >= peakDeg * 1.15) || 45);
  const dom = domDeg * Math.PI / 180;
  /* 顶角（中）— 底角（听者）为 boxH，L/R 恒落在半高处。宽度原先按
     tan(域角)×高度 定，15° 一档只有二十来 px，图基本看不清。 */
  const padX = 15 * dpr, padY = 10 * dpr;
  const cx = w / 2;
  const availH = h - padY * 2;
  let halfW = w / 2 - padX;
  if (!(availH > 20 * dpr && halfW > 8 * dpr)) return;
  /* 钻石框保持正方形：域角张满整幅宽度在宽窗口下会把图拉成扁宽一只（宽近高的两倍），
     高度不够时让宽度收回来，两侧留白好过声场形状失真。 */
  if (availH < halfW * 2) halfW = availH / 2;
  const boxH = Math.min(availH, halfW * 2);
  const top = padY + (availH - boxH) / 2;
  const ay = top + boxH, midY = top + boxH / 2;
  const A = [cx, ay], T = [cx, top];
  const tanDom = Math.tan(dom);
  /* q = tanθ / tan(域角)：θ=域角落在 L/R 角点，θ=0 落在顶角，中间按正切比例 */
  const rayPoint = (theta, side) => {
    const q = Math.max(0, Math.min(1, Math.tan(Math.min(theta, dom)) / tanDom));
    return [cx + side * halfW * q, midY - (1 - q) * boxH / 2];
  };
  const L = rayPoint(dom, -1), R = rayPoint(dom, 1);
  const frame = light ? "rgba(0,0,0,0.24)" : "rgba(255,255,255,0.22)";
  const faint = cssVar("--c-text-faint", "#6b6378");
  const label = (txt, x, y, color, align) => {
    ctx.font = `${8.5 * dpr}px sans-serif`; ctx.textAlign = align || "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round"; ctx.lineWidth = 3 * dpr;
    ctx.strokeStyle = cssVar("--c-bg", "#121014");
    ctx.strokeText(txt, x, y);
    ctx.fillStyle = color; ctx.fillText(txt, x, y);
  };
  /* 导引视线：域中值一档，读得出角度尺度 */
  ctx.save();
  ctx.setLineDash([2 * dpr, 3 * dpr]);
  ctx.lineWidth = dpr;
  ctx.strokeStyle = light ? "rgba(0,0,0,0.12)" : "rgba(255,255,255,0.1)";
  const midA = dom / 2;
  [rayPoint(midA, -1), rayPoint(midA, 1)].forEach((p) => {
    ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(p[0], p[1]); ctx.stroke();
  });
  ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(T[0], T[1]); ctx.stroke();
  ctx.restore();
  /* 外框钻石 */
  ctx.lineWidth = dpr;
  ctx.strokeStyle = frame;
  ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(L[0], L[1]);
  ctx.lineTo(T[0], T[1]); ctx.lineTo(R[0], R[1]); ctx.closePath(); ctx.stroke();
  const kite = (theta, color, fillA) => {
    if (theta == null) return;
    const pl = rayPoint(theta, -1), pr = rayPoint(theta, 1);
    ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(pl[0], pl[1]);
    ctx.lineTo(T[0], T[1]); ctx.lineTo(pr[0], pr[1]); ctx.closePath();
    ctx.globalAlpha = fillA; ctx.fillStyle = color; ctx.fill();
    ctx.globalAlpha = 0.95; ctx.strokeStyle = color; ctx.lineWidth = 1.6 * dpr;
    ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(pl[0], pl[1]); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(A[0], A[1]); ctx.lineTo(pr[0], pr[1]); ctx.stroke();
    ctx.globalAlpha = 1;
  };
  kite(hb, before, 0.13);
  kite(ha, after, 0.22);
  label("L", L[0] - 6 * dpr, L[1], faint, "right");
  label("R", R[0] + 6 * dpr, R[1], faint, "left");
  label("中", T[0], T[1] - 5 * dpr, faint);
  ctx.fillStyle = cssVar("--c-text-dim", "#9a91a8");
  ctx.beginPath(); ctx.arc(A[0], A[1], 3 * dpr, 0, Math.PI * 2); ctx.fill();   // 听者
}
/* ── 频谱变化：原始 / 处理后 / 变化(Δ) 三视图 + 发散色标 ──
   Δ 图逐格取前后 dB 差，只有当两图时间-频率网格一致（同时长、同 bin 数）
   时才成立；否则退回提示，不做插值对齐以免画出假的能量变化。 */
const SPEC_DELTA_RANGE = 12;   // Δ 色标满量程（dB）
const DELTA_DEADBAND = 0.4;    // 该量级以内的逐格差值不落色（前后处理的噪声底）
let rpSpecMode = "delta";

function hexToRgb(hex) {
  const s = String(hex || "").trim();
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(s);
  if (!m) return [128, 128, 128];
  let h = m[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const n = parseInt(h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function rgbToHex(c) {
  const h = (v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return `#${h(c[0])}${h(c[1])}${h(c[2])}`;
}
function mixRgb(a, b, t) { return [0, 1, 2].map((i) => a[i] + (b[i] - a[i]) * t); }
/* 色相旋转：对比色取 ±180°，饱和度与明度沿用主题色，
   因此换 accent 或切浅色模式时整套配色自动跟着走，无需逐主题配平。 */
function hueRotate(rgb, deg) {
  const r = rgb[0] / 255, g = rgb[1] / 255, b = rgb[2] / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  if (d === 0) return rgb.slice();
  const l = (mx + mn) / 2;
  const s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
  const h0 = mx === r ? (g - b) / d + (g < b ? 6 : 0)
    : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  const h = ((h0 * 60 + deg) / 360) % 1;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const f = (t) => {
    t = (t + 1) % 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  return [f(h + 1 / 3), f(h), f(h - 1 / 3)].map((v) => Math.round(v * 255));
}
let RP_PUB_KEY = "";
/* 相对亮度与对比度：色相旋转沿用主题色的明度，浅色主题下对比色可能几乎浮在
   面板上（indigo 的对比色 gold 在 #fafafa 上只有 2.1:1）。这里不逐主题凑色，
   只按对比度把推导色向文字色回推到刚好读得出，色相不变。 */
function relLum(c) {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
}
function contrastRatio(a, b) {
  const x = relLum(a), y = relLum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
function ensureSep(c, face, ink, minCr) {
  if (contrastRatio(c, face) >= minCr) return c;
  for (let i = 1; i <= 8; i++) {
    const m = mixRgb(c, ink, i / 8);
    if (contrastRatio(m, face) >= minCr) return m;
  }
  return mixRgb(c, ink, 1);
}
function rpThemeColors() {
  const base = cssVar("--c-bg", "#121014");
  const themeHex = cssVar("--c-accent-hi", "#6d55b8");
  const neutralHex = cssVar("--c-text-dim", "#9a91a8");
  const panel = hexToRgb(cssVar("--c-panel", "#211f26")), ink = hexToRgb(cssVar("--c-text", "#e6e0e9"));
  const th = hexToRgb(themeHex);
  const co = hueRotate(th, 180);
  const sep = (c) => rgbToHex(ensureSep(c, panel, ink, 2.6));
  const cutHex = sep(th);
  /* 全站只有三个语义色，图元与数字共用同一套：
       原始 / 处理前 = 中性色；处理后 / 衰减 = 主题色；提升 = 主题色的对比色。
     处理后与衰减本来就是同一个东西（成品相对原始偏低就是衰减），故同值。
     提升与衰减必须分属两个色相——同色相只差饱和时 Δ 频谱会糊成一片读不出正负。 */
  const out = {
    before: neutralHex, after: cutHex,
    boost: sep(co), cut: cutHex,
    base,
  };
  /* DOM 侧（KPI 箭头）与画布必须同源取色，否则主题一换画布跟着变、箭头仍是写死的绿/蓝。 */
  const key = out.boost + "|" + out.cut;
  if (key !== RP_PUB_KEY) {
    RP_PUB_KEY = key;
    const rs = document.documentElement.style;
    rs.setProperty("--rp-boost", out.boost);
    rs.setProperty("--rp-cut", out.cut);
  }
  return out;
}
/* 发散色标：中心为面板底色，正向取对比色（提升）、负向取主题色（衰减）；
   低端留暗部，使 ±1dB 的弱变化不至于把整张图染成一片色。 */
function buildDeltaLut(t) {
  const cool = hexToRgb(t.cut), warm = hexToRgb(t.boost), mid = hexToRgb(t.base);
  const lut = new Uint8Array(256 * 3);
  for (let u = 0; u < 256; u++) {
    const db = (u - 128) * (SPEC_DELTA_RANGE / 127);
    const c = db < 0 ? cool : warm;
    const k = Math.max(0, Math.abs(db) - DELTA_DEADBAND) / (SPEC_DELTA_RANGE - DELTA_DEADBAND);
    /* 实测逐格差值集中在 ±1~3 dB，线性映射到 ±12 满量程会把整张图压成黑色，
       所以先扣死区再用小指数伽马抬升低幅值：单调性不变，只是把可见范围让给真实变化区间。
       死区不可省——衰减色现在是饱和主题色，0.5dB 以内的噪声底不染掉，
       浅色模式（近白面板）会把整张图糊成一片淡红，「没变」和「微降」看着一样。 */
    const a = Math.pow(k, 0.55) * 0.92;
    lut[u * 3] = mid[0] + (c[0] - mid[0]) * a;
    lut[u * 3 + 1] = mid[1] + (c[1] - mid[1]) * a;
    lut[u * 3 + 2] = mid[2] + (c[2] - mid[2]) * a;
  }
  return lut;
}
let DELTA_LUT = buildDeltaLut(rpThemeColors());
const deltaOffCache = new WeakMap();
function ensureDeltaOff(inSpec, outSpec) {
  if (!inSpec || !outSpec) return null;
  if (inSpec.w !== outSpec.w || inSpec.h !== outSpec.h) return null;
  const lutKey = DELTA_LUT[3] + ":" + DELTA_LUT[255 * 3] + ":" + DELTA_LUT[128 * 3];
  const hit = deltaOffCache.get(outSpec);
  if (hit && hit.key === lutKey) return hit.off;
  const H = specDisplayH(outSpec, true);
  const off = document.createElement("canvas");
  off.width = outSpec.w; off.height = H;
  const octx = off.getContext("2d");
  const img = octx.createImageData(outSpec.w, H);
  const a = inSpec.data, b = outSpec.data, h = outSpec.h;
  const toDb = (v) => v * (90 / 255) - 90;
  const scale = 127 / SPEC_DELTA_RANGE;
  const { i0, i1 } = specSpans(outSpec, H);
  for (let x = 0; x < outSpec.w; x++) {
    const col = x * h;
    for (let r = 0; r < H; r++) {
      // 差值图取均值：绝对频谱取最大值是为了保住瞬态，但逐 bin 差值里的
      // 单点抖动（泄漏、限制器）会被极值放大成主信号，均值才代表该时频域整体移动。
      let sum = 0, cnt = 0;
      for (let k = i0[r]; k <= i1[r]; k++) {
        sum += toDb(b[col + k]) - toDb(a[col + k]);
        cnt++;
      }
      const mean = cnt ? sum / cnt : 0;
      let u = Math.round(128 + Math.max(-SPEC_DELTA_RANGE,
                                        Math.min(SPEC_DELTA_RANGE, mean)) * scale);
      if (u < 0) u = 0; else if (u > 255) u = 255;
      const o = (r * outSpec.w + x) * 4, c = u * 3;
      img.data[o] = DELTA_LUT[c]; img.data[o + 1] = DELTA_LUT[c + 1];
      img.data[o + 2] = DELTA_LUT[c + 2]; img.data[o + 3] = 255;
    }
  }
  octx.putImageData(img, 0, 0);
  deltaOffCache.set(outSpec, { off, key: lutKey });
  return off;
}
function rpSpecPair() {
  const pair = rpPair();
  const rep = pair && rp.reports.get(pair.path);
  const inPath = (rep && rep.input && rep.input.path) || state.selectedFile;
  const get = (path) => {
    if (!path) return null;
    ensureSpec(path);
    const e = specCache.get(normPath(path));
    return e && e.spec && e.spec.w ? e.spec : null;
  };
  return { inSpec: get(inPath), outSpec: get(pair && pair.path) };
}
function drawSpecBitmap(canvas, off, fHi, duration, note, anim) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
  if (!w || !h) return;
  rpClipAt(canvas, anim && anim.reveal != null ? anim.reveal : 1);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = cssVar("--c-bg", "#121014");
  ctx.fillRect(0, 0, w, h);
  if (off) {
    ctx.imageSmoothingEnabled = true;
    /* 换成品版本：先把上一版铺满，再按进度叠新版，读得出整幅频谱在换手 */
    const prev = anim && anim.fadeFrom && anim.fadeFrom !== off ? anim.fadeFrom : null;
    if (prev) {
      ctx.drawImage(prev, 0, 0, prev.width, prev.height, 0, 0, w, h);
      ctx.globalAlpha = anim.u;
    }
    ctx.drawImage(off, 0, 0, off.width, off.height, 0, 0, w, h);
    ctx.globalAlpha = 1;
    drawFreqScale(ctx, w, h, dpr, fHi);
    if (duration > 0 && w > 200 * dpr) {
      ctx.save();
      ctx.font = `${9 * dpr}px sans-serif`;
      ctx.textBaseline = "bottom";
      ctx.shadowColor = "rgba(0,0,0,0.55)";
      ctx.shadowBlur = 2 * dpr;
      ctx.fillStyle = cssVar("--c-text", "#e6e0e9");
      for (let i = 0; i <= 5; i++) {
        const x = (i / 5) * w;
        ctx.textAlign = i === 0 ? "left" : i === 5 ? "right" : "center";
        ctx.fillText(rpDuration((i / 5) * duration), Math.min(Math.max(x, 3 * dpr), w - 3 * dpr),
                     h - 2 * dpr);
      }
      ctx.restore();
    }
  } else {
    /* 没有位图也先把频率标尺画出来：这一格是频谱图，读标尺就看得出来 */
    drawFreqScale(ctx, w, h, dpr, fHi);
    ctx.fillStyle = cssVar("--c-text-faint", "#6b6378");
    ctx.font = `${10.5 * dpr}px sans-serif`;
    ctx.textAlign = "center";
    ctx.fillText(note || "频谱读取中…", w / 2 - 14 * dpr, h / 2);
  }
}
function renderSpectral() {
  const canvas = $("rp-spec");
  if (!canvas) return;
  const { inSpec, outSpec } = rpSpecPair();
  let off = null, fHi = 22050, dur = 0, note = "";
  if (rpSpecMode === "in" && inSpec) {
    off = ensureOffscreen(inSpec); fHi = (inSpec.sr || 44100) / 2; dur = inSpec.duration || 0;
  } else if (rpSpecMode === "out" && outSpec) {
    off = ensureOffscreen(outSpec); fHi = (outSpec.sr || 44100) / 2; dur = outSpec.duration || 0;
  } else {
    off = ensureDeltaOff(inSpec, outSpec);
    dur = (outSpec && outSpec.duration) || (inSpec && inSpec.duration) || 0;
    if (!off) {
      note = !(inSpec && outSpec) ? "处理完成后可见逐格变化"
        : "前后频谱网格不一致，无法逐格求差";
    }
  }
  drawSpecBitmap(canvas, off, fHi, dur, note);
  /* 重放钩子只在真正要动画时改画法，静态重绘（缩放、换主题）仍走上面的终态 */
  const prevOff = rpShown && rpShown.specOff;
  rpHook((u, reveal) => {
    drawSpecBitmap(canvas, off, fHi, dur, note, reveal
      ? { reveal: u }
      : (prevOff && prevOff !== off ? { fadeFrom: prevOff, u } : null));
  });
  if (rpPending) rpPending.specOff = off;
  else if (rpShown) rpShown.specOff = off;   // 静态重绘也要记下「屏幕上是什么」
}
/* 图例：色块 + 说明 */
function fillLegend(id, items) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = "";
  items.forEach(([text, color]) => {
    const item = document.createElement("span");
    item.className = "rp-legend-item";
    if (color) {
      const sw = document.createElement("span");
      sw.className = "rp-swatch";
      sw.style.background = color;
      item.appendChild(sw);
    }
    item.appendChild(document.createTextNode(text));
    el.appendChild(item);
  });
}
function renderReportLegend() {
  const t = rpThemeColors();
  fillLegend("rp-legend", [["原始", t.before], ["处理后 / 衰减", t.after], ["提升", t.boost]]);
}
document.querySelectorAll("#rp-spec-tabs .rp-spec-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.mode === rpSpecMode) return;
    rpSpecMode = btn.dataset.mode;
    document.querySelectorAll("#rp-spec-tabs .rp-spec-tab").forEach((b) => {
      b.setAttribute("aria-pressed", b === btn ? "true" : "false");
    });
    rpSpecFade(() => renderSpectral(), 240);
  });
});
async function renderReport(mode) {
  await loadReportData();
  if (state.view !== "report") return;   // 异步加载期间用户已切走
  DELTA_LUT = buildDeltaLut(rpThemeColors());
  rpHooks = [];
  rpPending = {};                        // 渲染器据此登记重放钩子，并写下本帧数据
  renderHead();
  renderSummary();
  drawSpectrumChart($("rp-spectrum"));
  renderDynamics();
  renderStereoFans();
  renderSpectral();
  renderReportLegend();
  rpShown = rpPending;
  rpPending = null;
  /* 各图先按终态画好（ resize / 换主题等静态重绘就走这条路），
     要动画时再由钩子逐帧改写回起点。 */
  if (mode) rpRunAnim(mode);
}

window.addEventListener("resize", () => {
  if (!$("pane-report").hidden) renderReport();
});
/* 报告配色全部由 CSS 变量推导，主题一换必须重绘，否则画布仍是旧主题色 */
document.addEventListener("sb-theme", () => {
  if (!$("pane-report").hidden) renderReport();
});

})();
