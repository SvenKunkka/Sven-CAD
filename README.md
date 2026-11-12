# Sven CAD

**简体中文** · [English](#english)

Sven CAD（原 PersonalCAD）基于 FreeCAD 26.3rc2，面向 Apple Silicon Mac 上的参数化机械建模、静态装配和工程出图。默认使用简体中文和毫米，设置、缓存和恢复目录与 FreeCAD 分开。

- 设计工作区默认使用浅色界面。顶部按实体、曲面和装配分组，常用命令带图标和文字。部分建模操作采用与 Fusion 360 相同的默认快捷键。
- 3D 视口默认采用 Fusion 导航。中键拖动平移，Shift＋中键拖动旋转，滚轮缩放。Mac 触控板支持双指平移、Shift＋双指旋转和双指捏合缩放。双击中键或按 F6 适应窗口。
- 保留草图约束、零件及曲面建模、静态装配与 BOM、工程图、几何测量、FCStd 和有色 STEP。
- 支持在受支持的导入 STEP 实体上偏移平面和调整直孔孔径；提供 M2–M4 公制螺纹、常用紧固件、装配干涉检查、组件替换及带来源清单的一键出包。
- 本地候选版已用合成装配验证建模到出包流程；大型装配的完整图纸视图仍有性能限制，真实规模图纸目前仅验证标题栏与 BOM。特征历史与多规格配置仍在开发中。
- 移除仿真、CAM、BIM、参数表、材料管理和网格编辑与交换入口。工程图支持 PDF、SVG 和二维 DXF。
- 针对 macOS 模型树查询、布局恢复、视口错误和自动恢复写入做了加固。遇到依赖已移除模块的旧 FCStd 会报告受影响对象，装配缺失外链时会保护原文件。

浅色版在 Apple M5、macOS 26.6.2 上验证了旧界面布局迁移、全宽工具栏、工作台切换、草图和拉伸操作，并核对了 23 项默认快捷键绑定。此前提交 `05f3e7e` 的稳定性验收覆盖三轮冷启动、300 次模型树查询和两小时内 440 轮 CAD 操作。

- 构建、Qt 补丁和打包步骤见 [macOS 构建文档](personal-cad/toolchain/README.md)。
- 合成样件、辅助功能查询和回归步骤见 [测试文档](personal-cad/qa/cad/README.md)。
- 这是独立维护的 FreeCAD 分支，开发和测试使用了 OpenAI Codex。上游说明及署名保留在 [FreeCAD README](README.upstream.md)。

## English

Sven CAD (formerly PersonalCAD) is a FreeCAD 26.3rc2 fork for parametric mechanical modeling, static assemblies, and technical drawings on Apple Silicon Macs. It defaults to Simplified Chinese and millimeters, with a separate settings, cache, and recovery profile.

- The design workspace opens in a light theme. The ribbon groups labeled commands under Solid, Surface, and Assembly. Selected modeling commands share their default shortcuts with Fusion 360.
- The 3D viewport defaults to Fusion navigation. Drag with the middle button to pan, hold Shift to orbit, and scroll to zoom. On a Mac trackpad, swipe with two fingers to pan, add Shift to orbit, and pinch to zoom. Double-click the middle button or press F6 to fit the view.
- Includes constrained sketches, part and surface modeling, static joints and BOM, drawings, geometric measurement, FCStd, and colored STEP.
- Offsets planar faces and resizes supported straight bores on imported STEP solids; provides M2–M4 metric threads, common fasteners, assembly interference checks, component replacement, and a delivery package with a source manifest.
- A local candidate has been tested from modeling through delivery on a synthetic assembly. Full drawing views of large assemblies remain performance-limited; real-scale drawing checks currently cover the title block and BOM. Feature history and product configurations remain in development.
- Removes simulation, CAM, BIM, spreadsheets, material management, and mesh editing and exchange. Drawing outputs include PDF, SVG, and 2D DXF.
- Hardens macOS model-tree accessibility, layout restore, viewport error handling, and recovery writes. Legacy files that require removed modules report affected objects. A missing assembly link blocks saves to protect the source file.

Testing on an Apple M5 running macOS 26.6.2 covered layout migration, the full-width ribbon, workbench switching, sketching, and extrusion, along with checks of 23 default shortcut bindings. Earlier stability testing on commit `05f3e7e` covered three cold launches, 300 model-tree queries, and 440 CAD cycles in two hours.

- See the [macOS build guide](personal-cad/toolchain/README.md) for build, Qt patch, and packaging steps.
- See the [QA guide](personal-cad/qa/cad/README.md) for synthetic fixtures and regression tests.
- This is an independently maintained fork. OpenAI Codex assisted with development and testing. Upstream documentation and credit are retained in the [FreeCAD README](README.upstream.md).

## License · 许可证

FreeCAD's license and component notices are preserved. Qt patches apply to Qt
sources and retain the licensing of the affected upstream files. [FreeCAD license](LICENSE)
