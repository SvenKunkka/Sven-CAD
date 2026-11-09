# PersonalCAD · 个人 CAD

**简体中文** · [English](#english)

PersonalCAD 是基于 FreeCAD 26.3rc2 的 Apple Silicon macOS 分支，面向参数化机械建模、静态装配和工程出图。默认使用简体中文和毫米，设置、缓存和恢复目录与 FreeCAD 分开。

- 保留草图约束、零件及曲面建模、静态装配与 BOM、工程图、几何测量、FCStd 和有色 STEP。
- 移除仿真、CAM、BIM、参数表、材料管理和网格编辑与交换入口。工程图支持 PDF、SVG 和二维 DXF。
- 针对 macOS 模型树查询、布局恢复、视口错误和自动恢复写入做了加固。遇到依赖已移除模块的旧 FCStd 会报告受影响对象，装配缺失外链时会保护原文件。

在 Apple M5、macOS 26.6.2 的本地签名包上，三轮冷启动的 300 次模型树查询全部匹配，两小时连续 CAD 测试完成 440 轮。该次验收针对源码提交 `05f3e7e` 和对应的 Qt 修补构建。

- 构建、Qt 补丁和打包步骤见 [macOS 构建文档](personal-cad/toolchain/README.md)。
- 合成样件、辅助功能查询和回归步骤见 [测试文档](personal-cad/qa/cad/README.md)。
- 这是独立维护的 FreeCAD 分支，开发和测试使用了 OpenAI Codex。上游说明及署名保留在 [FreeCAD README](README.upstream.md)。

## English

PersonalCAD is a FreeCAD 26.3rc2 fork for parametric mechanical modeling, static assemblies, and technical drawings on Apple Silicon macOS. It defaults to Simplified Chinese and millimeters, with a separate settings, cache, and recovery profile.

- Includes constrained sketches, part and surface modeling, static joints and BOM, drawings, geometric measurement, FCStd, and colored STEP.
- Removes simulation, CAM, BIM, spreadsheets, material management, and mesh editing and exchange. Drawing outputs include PDF, SVG, and 2D DXF.
- Hardens macOS model-tree accessibility, layout restore, viewport error handling, and recovery writes. Legacy files that require removed modules report affected objects. A missing assembly link blocks saves to protect the source file.

The local signed build for Apple M5 on macOS 26.6.2 matched all 300 model-tree queries over three cold launches and completed 440 CAD cycles in a two-hour soak. These results apply to source commit `05f3e7e` and its patched Qt build.

- See the [macOS build guide](personal-cad/toolchain/README.md) for build, Qt patch, and packaging steps.
- See the [QA guide](personal-cad/qa/cad/README.md) for synthetic fixtures and regression tests.
- This is an independently maintained fork. OpenAI Codex assisted with development and testing. Upstream documentation and credit are retained in the [FreeCAD README](README.upstream.md).

## License · 许可证

FreeCAD's license and component notices are preserved. Qt patches apply to Qt
sources and retain the licensing of the affected upstream files. [FreeCAD license](LICENSE)
