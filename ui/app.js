/* ShadowBuster — 前端逻辑 + QWebChannel 桥接 + 处理特效 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    processing: false,
    files: [],
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
        const q = document.querySelector(".queue");
        if (q) q.classList.toggle("drop-hover", !!on);
      });
      resolve();
    });
  });

  /* ─── 参数持久化：像真实效果器一样记住上次参数 ─── */
  const STORAGE_PREFIX = "sb_param_";
  function loadValue(key, fallback) {
    try {
      const raw = localStorage.getItem(STORAGE_PREFIX + key);
      return raw === null ? fallback : raw;
    } catch (e) { return fallback; }
  }
  function saveValue(key, value) {
    try { localStorage.setItem(STORAGE_PREFIX + key, String(value)); } catch (e) {}
  }
  function loadNumber(key, fallback) {
    const raw = loadValue(key, null);
    if (raw === null) return fallback;
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : fallback;
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
    let accent = (root.dataset.accent === "custom" || ACCENTS.some((a) => a.id === root.dataset.accent))
      ? root.dataset.accent : "violet";
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
  function bindKnob(knobEl, valueEl, fmt, storeKey) {
    const min = +knobEl.dataset.min, max = +knobEl.dataset.max;
    const step = +knobEl.dataset.step, def = +knobEl.dataset.default;
    let value = def;
    if (storeKey) {
      value = loadNumber(storeKey, def);
      value = Math.max(min, Math.min(max, Math.round(value / step) * step));
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
    }
    let dragging = false, lastY = 0, dragAccum = 0;
    knobEl.addEventListener("pointerdown", (e) => {
      dragging = true; lastY = e.clientY;
      knobEl.setPointerCapture(e.pointerId);
    });
    knobEl.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dy = lastY - e.clientY; lastY = e.clientY;
      // 慢速：灵敏度 0.35，累加阈值 8px 才步进
      dragAccum += dy * 0.35;
      if (Math.abs(dragAccum) >= 8) {
        const delta = Math.sign(dragAccum) * Math.max(1, Math.round(Math.abs(dragAccum) / 8));
        dragAccum = 0;
        value = Math.max(min, Math.min(max, value + delta * step));
        render();
      }
    });
    const end = () => { dragging = false; dragAccum = 0; };
    knobEl.addEventListener("pointerup", end);
    knobEl.addEventListener("pointercancel", end);
    knobEl.addEventListener("dblclick", () => { value = def; render(); });
    knobEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      value = Math.max(min, Math.min(max, value + (e.deltaY < 0 ? step : -step)));
      render();
    }, { passive: false });
    knobEl.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowUp" || e.key === "ArrowRight" ? step
              : e.key === "ArrowDown" || e.key === "ArrowLeft" ? -step : 0;
      if (d) { e.preventDefault(); value = Math.max(min, Math.min(max, value + d)); render(); }
    });
    render();
    return () => value;
  }

  function bindFader(faderEl, valueEl, fmt, storeKey) {
    const min = +faderEl.dataset.min, max = +faderEl.dataset.max;
    const step = +faderEl.dataset.step, def = +faderEl.dataset.default;
    let value = storeKey ? loadNumber(storeKey, def) : def;
    value = Math.max(min, Math.min(max, Math.round(value / step) * step));

    function setFromClientX(clientX) {
      const rect = faderEl.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      value = Math.round((min + ratio * (max - min)) / step) * step;
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
    faderEl.addEventListener("dblclick", () => { value = def; render(); });
    faderEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      value = Math.max(min, Math.min(max, value + (e.deltaY < 0 ? step : -step)));
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
    return () => value;
  }

  /* ─── 声场宽度：倒三角控件（左右拖动张合顶边，双击复位）─── */
  function bindWidthMeter(meterEl, fmt, storeKey) {
    const min = +meterEl.dataset.min, max = +meterEl.dataset.max;
    const step = +meterEl.dataset.step, def = +meterEl.dataset.default;
    let value = storeKey ? loadNumber(storeKey, def) : def;
    value = Math.max(min, Math.min(max, Math.round(value / step) * step));
    const valueEl = meterEl.querySelector(".width-value");

    // 左右拖动：宽度 = 指针到方块中线的距离 / 半程，顶边随之张合
    function setFromClientX(clientX) {
      const rect = meterEl.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, Math.abs(clientX - rect.left - rect.width / 2) / (rect.width / 2)));
      value = Math.round((min + ratio * (max - min)) / step) * step;
      render();
    }
    function render() {
      meterEl.style.setProperty("--wp", (value - min) / (max - min));
      const text = fmt(value);
      meterEl.setAttribute("aria-valuenow", value);
      meterEl.setAttribute("aria-valuetext", text + " dB");
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
    meterEl.addEventListener("dblclick", () => { value = def; render(); });
    meterEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      value = Math.max(min, Math.min(max, value + (e.deltaY < 0 ? step : -step)));
      render();
    }, { passive: false });
    meterEl.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" || e.key === "ArrowUp" ? step
              : e.key === "ArrowLeft" || e.key === "ArrowDown" ? -step : 0;
      if (d) { e.preventDefault(); value = Math.max(min, Math.min(max, value + d)); render(); }
    });
    render();
    return () => value;
  }

  const getQuality = bindKnob($("knob-quality"), $("val-quality"),
    (v) => (["快速", "标准", "精细"])[v] || "标准", "quality");
  const getGuidance = bindKnob($("knob-guidance"), $("val-guidance"), (v) => (v / 10).toFixed(1), "guidance");
  const getVocal = bindKnob($("knob-vocal"), $("val-vocal"),
    (v) => (v > 0 ? "+" : "") + (v / 2).toFixed(1) + " dB", "vocal");
  const getWidth = bindWidthMeter($("width-meter"), (v) => "+" + (v / 2).toFixed(1), "space_width");
  const getSub = bindKnob($("knob-sub"), $("val-sub"), (v) => `+${v} dB`, "sub");
  const getSat = bindKnob($("knob-sat"), $("val-sat"), (v) => (v / 10).toFixed(2), "sat");
  const getPunch = bindKnob($("knob-punch"), $("val-punch"), (v) => `+${v} dB`, "punch");
  const getTrans = bindKnob($("knob-trans"), $("val-trans"), (v) => (v / 10).toFixed(2), "trans");
  const getSpace = bindFader($("fader-space"), $("val-space"), (v) => (v / 10).toFixed(2), "space");
  const getDenoise = bindFader($("fader-denoise"), $("val-denoise"), (v) => (v / 10).toFixed(2), "denoise");

  /* ─── 面板 bypass 开关：关闭 = 该面板对应阶段全部跳过，状态持久化 ─── */
  const BYPASS_CONTROLS = {
    "bp-lew": ["lew", "vocals"],
    "bp-bass-drums": ["bass", "drums"],
    "bp-reshape": ["reshape"],
    "bp-soren": ["soren"],
  };
  const bypassState = {};
  Object.entries(BYPASS_CONTROLS).forEach(([id, stages]) => {
    const btn = $(id);
    const rack = btn.closest(".rack");
    const enabled = stages.every((stage) => loadValue("bypass_" + stage, "1") === "1");
    stages.forEach((stage) => { bypassState[stage] = enabled; });

    function render() {
      btn.setAttribute("aria-checked", enabledForPanel() ? "true" : "false");
      btn.querySelector(".bp-label").textContent = enabledForPanel() ? "开" : "关";
      rack.dataset.bypassed = enabledForPanel() ? "0" : "1";
    }

    function enabledForPanel() {
      return stages.every((stage) => bypassState[stage]);
    }

    render();
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
    function select(v) {
      value = v;
      const opt = options.find((o) => o.v === v);
      label.textContent = opt ? opt.label : v;
      renderOptions();
      if (onChange) onChange(v);
      if (storeKey) saveValue(storeKey, v);
    }
    function openPanel() {
      open = true;
      trigger.setAttribute("aria-expanded", "true");
      panel.classList.add("open");
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

    renderOptions();
    return () => value;
  }

  const getGenre = buildDropdown("dd-genre", [
    { v: "Pop", label: "流行 Pop" }, { v: "EDM", label: "电子 EDM" },
    { v: "Rock", label: "摇滚 Rock" }, { v: "Dance", label: "舞曲 Dance" },
    { v: "Hiphop", label: "嘻哈 Hiphop" }, { v: "Ambient", label: "氛围 Ambient" },
    { v: "Chillout", label: "弛放 Chillout" }, { v: "Orchestral", label: "管弦 Orchestral" },
    { v: "Speech", label: "人声 Speech" }, { v: "Piano", label: "钢琴 Piano" },
  ], "Pop", (v) => { if (state.reference) { state.reference = ""; $("ref-path").value = ""; saveValue("reference", ""); } }, "genre");

  const getLoudness = buildDropdown("dd-loudness", [
    { v: "soft", label: "轻柔" }, { v: "dynamic", label: "动态" },
    { v: "normal", label: "标准" }, { v: "loud", label: "响亮" },
  ], "normal", () => {}, "loudness");

  /* ─── EQ 分段 ─── */
  state.eq = loadValue("eq", "Neutral");
  const eqBtns = document.querySelectorAll(".seg-btn[data-eq]");
  eqBtns.forEach((btn) => {
    btn.setAttribute("aria-pressed", btn.dataset.eq === state.eq ? "true" : "false");
    btn.addEventListener("click", () => {
      eqBtns.forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
      state.eq = btn.dataset.eq;
      saveValue("eq", state.eq);
    });
  });

  /* ─── 队列管理 ─── */
  const fileListEl = $("file-list");
  const clearBtn = $("btn-clear");
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
  function renderFileList() {
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
      item.innerHTML = `<span class="fi-status" aria-hidden="true"></span>` +
        `<span class="fi-name" title="${escapeHtml(f)}">${escapeHtml(name)}</span>` +
        `<button class="fi-x" aria-label="移除" title="移除">` +
        `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" fill="none" stroke-linecap="square"/></svg>` +
        `</button>`;
      item.querySelector(".fi-x").addEventListener("click", () => {
        if (state.processing) return;
        item.classList.add("leaving");
        setTimeout(() => {
          state.files.splice(i, 1);
          state.fileStatuses.splice(i, 1);
          renderFileList();
        }, 150);
      });
      fileListEl.appendChild(item);
    });
    $("queue-count").textContent = `${state.files.length} 首`;
    if (state.files.length) {
      clearNeed(document.querySelector(".queue"));
    }
  }
  function addPaths(paths) {
    if (state.processing || !paths || !paths.length) return;
    const additions = paths.filter((p) => !state.files.includes(p));
    if (!additions.length) return;
    state.files.push(...additions);
    state.fileStatuses.push(...additions.map(() => "pending"));
    renderFileList();
  }
  async function addFiles() {
    if (state.processing) return;
    addPaths(await api.selectInputs());
  }
  $("btn-add").addEventListener("click", addFiles);
  $("btn-clear").addEventListener("click", () => {
    if (state.processing) return;
    state.files = [];
    state.fileStatuses = [];
    renderFileList();
  });

  /* ─── 输出 / 参考音频 ─── */
  state.output = loadValue("output", "");
  $("output-path").value = state.output;
  state.reference = loadValue("reference", "");
  $("ref-path").value = state.reference;

  $("btn-output").addEventListener("click", async () => {
    if (state.processing) return;
    const path = await api.selectOutput();
    if (path) {
      state.output = path; $("output-path").value = path;
      saveValue("output", path);
      clearNeed($("output-path"));
    }
  });
  $("btn-ref").addEventListener("click", async () => {
    if (state.processing) return;
    const path = await api.selectReference();
    if (path) {
      state.reference = path;
      $("ref-path").value = path;
      saveValue("reference", path);
    }
  });

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
  function showErrorModal(summary, detail, path) {
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

  async function startProcess() {
    // 缺失提示改为控件红框：无歌曲 → 队列红框；无输出目录 → 输出输入框红框
    if (!state.files.length) { markNeed(document.querySelector(".queue")); return; }
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
        quality: getQuality(), guidance: getGuidance(),
        sub: getSub(), sat: getSat(), punch: getPunch(), trans: getTrans(),
        space: getSpace(), denoise: getDenoise(),
        space_width: getWidth(), vocal: getVocal(),
        bypass: activeBypassList(),
        genre: state.reference ? "" : getGenre(),
        loudness: getLoudness(), eq: state.eq,
      });
    } catch (e) {
      fx.setActive(false);
      finishProcessing();
    }
  }

  /* ─── 参数说明浮层：打开 / 拖动 / 关闭 ─── */
  const HELP_CONTENT = {
    lew: {
      title: "高频 · 参数说明",
      html:
        "<h3>质量档</h3><p>高频重建的精细程度：<b>快速</b>＝适合试听；<b>标准</b>＝日常使用；<b>精细</b>＝细节最多、但最慢。</p>" +
        "<h3>重建引导（0 – 2）</h3><p>重建产物的保留比例：<b>0</b>＝完全保留原始信号，<b>2</b>＝完全采用重建结果，默认 1.5（约 75%）。建议 1.0 – 1.75。</p>" +
        "<h3>人声（-6.0 – +6.0 dB）</h3><p>人声轨整体增益：声场拓宽与母带处理后若人声变靠后，+1.5 ~ +3 dB 可把人声拉回原位；<b>0.0</b>＝完全旁路。只改动人声轨差值，其余内容逐样本不变。</p>" +
        "<h3>操作</h3><ul><li>旋钮上下拖动或滚轮调节</li><li>双击旋钮恢复默认值</li></ul>",
    },
    bass: {
      title: "低频 · 参数说明",
      html:
        "<h3>Sub 提升（0 – 12 dB）</h3><p>次低音（30–60 Hz）low-shelf 增益，补低频包裹感；低发散虚就调高，发浑就调低。</p>" +
        "<h3>鼓身（0 – 10 dB）</h3><p>60–120 Hz 轻度 bell 提升，强化鼓的 body/punch（鼓点厚度与攻击感）。</p>" +
        "<h3>瞬态（0.00 – 1.00）</h3><p>瞬态强调，单独放大鼓点起音，让节奏更清晰、不糊。</p>" +
        "<h3>谐波饱和（0.00 – 1.00）</h3><p>为低频加入模拟暖感：0.30 左右合适，太高会明显失真。</p>" +
        "<h3>操作</h3><ul><li>旋钮上下拖动或滚轮调节</li><li>双击旋钮恢复默认值</li></ul>",
    },
    space: {
      title: "声场 · 参数说明",
      html:
        "<h3>声场（0.00 – 1.00）</h3><p>声场重塑强度（干湿比）：向「宽度」目标混合的比例，把堆在中间的铺底/鼓元素向两侧摊开。" +
        "<b>0</b>＝完全保留原样，<b>1.00</b>＝全量重塑；默认 0.60。只动 side（左右差），单声道合并不受影响。</p>" +
        "<h3>宽度（0 – +6.0 dB）</h3><p>声场宽度上限：铺底乐器轨最大 side 增益（鼓自动取一半）。<b>左右拖动</b>三角形即可展开/收拢，顶边宽度就是当前宽度范围；默认 <b>+3.0</b>。与「声场」推子配合决定最终有多宽。</p>" +
        "<h3>高频降噪（0.00 – 1.00）</h3><p>对铺底乐器轨 ≥10 kHz 的稳态沙沙噪声做门控衰减（自动噪声地板估计，类降噪采样）：贴着噪声电平的成分按比例衰减，" +
        "突出的音乐瞬态（镲片敲击）原样保留。默认 0.20；AI 音乐的擦片毛刺感明显时可在 0.2–0.4 之间调。</p>" +
        "<h3>面板开关</h3><p>关闭后，声场重塑与高频降噪会一起旁路；推子值保留，重新开启即可继续使用。</p>" +
        "<h3>操作</h3><ul><li>沿推子点击或横向拖动</li><li>滚轮或方向键精细调节</li><li>双击推子恢复默认值</li></ul>",
    },
    soren: {
      title: "母带 · 参数说明",
      html:
        "<h3>流派</h3><p>按曲风选择母带预设；选了参考音频时以参考为准，流派自动失效。</p>" +
        "<ul><li>流行 Pop · 电子 EDM · 摇滚 Rock</li>" +
        "<li>舞曲 Dance · 嘻哈 Hiphop · 钢琴 Piano</li>" +
        "<li>氛围 Ambient（环境铺底）· 弛放 Chillout（舒缓电子）</li>" +
        "<li>管弦 Orchestral · 人声 Speech</li></ul>" +
        "<h3>响度</h3><p><b>轻柔</b>＝保留动态；<b>标准</b>＝均衡；<b>响亮</b>＝适合流媒体响度竞争。</p>" +
        "<h3>EQ 风格</h3><p><b>平直</b>＝不染色；<b>温暖</b>＝加厚中低频；<b>明亮</b>＝提升高频光泽；<b>融合</b>＝整体更贴耳。</p>",
    },
  };
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
    helpBody.innerHTML = c.html;
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
    settingsModal.classList.add("open");
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
  // 占位：GitHub 更新入口，待接入 Releases 检查后启用按钮
  $("btn-check-update").addEventListener("click", () => {
    $("update-status").textContent = "占位：即将接入 GitHub Releases";
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
})();
