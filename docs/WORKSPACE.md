# 工作区统一说明（权威源 / 资源 / 派生产物 / 外部保留）

> 2026-09-09 统一化改造记录。原则：**单一权威源，派生物可随时重建**；
> git 不自动提交，不 reset/clean/stash 用户内容。

## 权威源码（git tracked，唯一修改入口）

| 内容 | 路径 | 说明 |
|---|---|---|
| Soren core | `packaging/soren_core.py` | 打包/运行时同名 `core_decrypted.py`；测试与构建统一从这里出发 |
| Soren 风格层 | `packaging/soren_original.py` | 权威 styled 实现（新于外部仓库未跟踪旧稿） |
| DSP 脚本 | `apollo_scripts/` | 开发态 DSP_DIR（DSP 语义不在本统一范围） |
| 应用入口 | `main.py`、`processing_cli.py`、`studio_backend.py` | GUI / CLI / 后端与路由 |
| 构建 | `ShadowBuster.spec`、`packaging/runtime_sync.ps1`、`packaging/build_*.ps1` | 打包与 stage 同步（含 Assert-SameFile 自检） |
| dev runtime | `tools/make_dev_runtime.py` | 生成开发态 canonical 运行时（见下） |

## 资源（只读引用，绝不修改外部）

- Soren 模型/配置：外部仓库 `D:/_3.AI/audio_upscale/Soren_src` 的
  `model/ profiles/ secured_genres/ test_model.py`，经 `SB_SOREN` 传入
  （runtime_sync 与 dev runtime 同一来源）。
- Apollo/Lew 权重：外部 `D:/_3.AI/audio_upscale/Apollo`。
- 外部 Soren_src 是独立 git 仓（含其自身未提交/未跟踪内容）：
  不 reset、不 clean、不 commit、不覆盖。

## 派生产物（可重建，git 已 ignore，不手工编辑）

| 产物 | 来源 |
|---|---|
| `packaging/stage/runtime{,_gpu}/` | `packaging/runtime_sync.ps1`（canonical 代码 + SB_SOREN 资源 + 权重） |
| `dev_runtime/Soren_src/` | `python tools/make_dev_runtime.py`（canonical 代码 + SB_SOREN 资源 junction；`--verify` 自校验） |
| `build/`、`dist/`、`packaging/out/` | PyInstaller / 安装器输出 |
| `*.backup-20260909-005908`（dist、packaging/out、packaging/stage） | 回滚备份，保留 |

## Soren 目录路由

`studio_backend._resolve_runtime()`：
1. 打包态（不变）：exe 同级 `runtime/` 或 `SB_ASSETS`；用户级 GPU 环境仅换解释器。
2. 开发态 `studio_backend._dev_soren_dir()`：只返回 `dev_runtime/Soren_src` 路径，导入模块时不生成、不依赖外部资源。
   实际执行母带或计算相关缓存身份之前校验；缺失/陈旧时自动安全生成，
   含未知内容或用户改动时拒绝并报错指向工具，**绝不回退执行外部旧 DSP**。
3. `SB_SOREN` 仅作为资源源（生成器与 `runtime_sync.ps1`），不作为执行目录。

## dev runtime 安全约定（tools/make_dev_runtime.py）

- 只覆盖 manifest 登记的 owned 文件，且当前哈希须等于生成期哈希；用户改动 → 拒绝
  （`--force` 才允许，且先备份到 `dev_runtime/.backups/<UTC>/`）。
- 未知内容一律不删除（含 `__pycache__`）；代码写入用临时文件 + 原子替换；
  verify 无 manifest / manifest 损坏即失败。
- 资源 junction **不是访问控制**：生成器从不写外部源，使用约定为只读，
  禁止经 `dev_runtime` 资源目录写外部。
- 缓存身份（pipeline_cache）已纳入 dev runtime manifest：资源源切换/重建后旧缓存自动失效。

## 测试与验证

- 唯一入口：`pytest.ini` 限定 `testpaths = tests`；命令 `python -m pytest tests -q`
  （基线 314 passed / 1 skipped / 29 subtests；统一后 328 passed / 1 skipped / 29 subtests，2026-09-09）。
- `python tools/make_dev_runtime.py --verify` 与开发实际解析入口已验证。未重打安装包，现有已安装程序未改变。
- Soren 测试一律加载 canonical（`packaging/soren_core.py`）；stage/dev 副本只做
  "存在才比对" 的一致性抽查（`tests/test_dev_soren_runtime.py` 等）。
- 历史直改 stage 的实验脚本（`experiments/patch_soren_style.py`）已标记停用并拒绝执行。

## Git 纪律与保留文件

- 无自动 commit；实施统一化后 tracked 文件的 M 标记属预期，人工确认后提交。
- 保留的未跟踪用户文件：`repo.bundle`（旧史备份 git bundle）。
- 保留的外部内容：`D:/_3.AI/audio_upscale/Soren_src` 整仓（含 M `core_decrypted.py`
  与未跟踪旧版 `soren_original.py`）、三处 `*.backup-20260909-005908`。
