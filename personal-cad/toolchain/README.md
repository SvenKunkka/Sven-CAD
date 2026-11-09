# macOS 源码构建 / macOS source build

在 Apple Silicon、macOS 15 或更新版本上使用原生 arm64 终端。需要 Xcode 命令行工具、Git、Pixi 和系统 Python 3.9 或更新版本。原始验证环境使用 Pixi 0.81.0。首次构建会下载锁定依赖和验证用参考应用，需要足够的磁盘空间和时间。

Use a native arm64 terminal on Apple Silicon with macOS 15 or later. Install Xcode Command Line Tools, Git, Pixi, and system Python 3.9 or later. The original verification used Pixi 0.81.0. The first build downloads locked dependencies and a reference app. Allow ample disk space and time.

```sh
bash personal-cad/toolchain/build-macos.sh
```

应用包生成在 `.personal-cad-build/artifacts/personal/PersonalCAD.app`，使用本地临时签名。构建脚本会核验包内动态库依赖和签名，并以隔离配置运行命令行冒烟测试。图形界面与 macOS 辅助功能需要单独测试。本目录用于复建源码，先前验证的应用包哈希仍对应当时的本机产物。

The locally ad-hoc signed app is staged at `.personal-cad-build/artifacts/personal/PersonalCAD.app`. Staging checks library dependencies and signatures and runs a CLI smoke test with an isolated profile. GUI and macOS Accessibility require separate testing. These scripts rebuild from source. The earlier verification digests identify the binaries tested at that time.

## 构建选项 / Build options

| Command | 用途 / Purpose |
| --- | --- |
| `bash personal-cad/toolchain/build-macos.sh configure` | 安装锁定依赖并生成 FreeCAD 构建配置。Install the locked dependencies and configure FreeCAD. |
| `bash personal-cad/toolchain/build-macos.sh baseline` | 构建 RC2 对照组 A/B，分别使用原版和已修补的 Cocoa 插件。Build RC2 controls A/B using the stock and patched Cocoa plugins, respectively. |
| `bash personal-cad/toolchain/build-qt.sh configure` | 校验 Qt 源码和 Vulkan 头文件，应用四个补丁并配置 Qt 目标。Verify Qt sources and Vulkan headers, apply the four patches, and configure the Qt targets. |
| `bash personal-cad/toolchain/build-qt.sh test` | 编译 Qt 补丁与原生辅助功能回归测试。Build the patched Qt targets and the native Accessibility regression binary. |
| `/usr/bin/python3 personal-cad/toolchain/stage-macos.py personal --plan` | 预览参考应用和 Pixi 清单共同确定的运行时文件范围。Preview runtime files selected from the reference app and Pixi package receipts. |

`PERSONAL_CAD_WORK` controls the build and staging directory, `PERSONAL_CAD_PIXI` can select an installed Pixi executable, and `CMAKE_BUILD_PARALLEL_LEVEL` defaults to 6. Use a fresh work directory to stage another app. Staging refuses to replace an existing app. The Qt and staging scripts also accept `PERSONAL_CAD_PREFIX` for an existing locked Pixi environment.

`PERSONAL_CAD_WORK` 指定构建与打包目录，`PERSONAL_CAD_PIXI` 可指定已安装的 Pixi 程序，`CMAKE_BUILD_PARALLEL_LEVEL` 默认 6。再次打包时可选择新的工作目录，打包器会拒绝覆盖已有应用。Qt 和打包脚本也支持用 `PERSONAL_CAD_PREFIX` 指向已锁定的 Pixi 环境。

## 来源与校验 / Inputs and verification

- FreeCAD 对照组固定在 `9fb16e7f981adb09f7d7f130ce085d68e9825358`，并校验递归子模块版本。The baseline is pinned to this commit and its recursive submodule revisions. [FreeCAD 26.3rc2](https://github.com/FreeCAD/FreeCAD/releases/tag/26.3rc2)
- 官方 FreeCAD DMG、Qt 6.11.2 源码及 Vulkan 头文件下载均核验 SHA-256，FreeCAD 参考应用也核验签名。The official FreeCAD DMG, Qt 6.11.2 sources, and Vulkan headers are checked against pinned SHA-256 hashes. The reference app's signature is also checked. [Qt sources](https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/) · [Vulkan header package](https://anaconda.org/conda-forge/libvulkan-headers)
- Qt 构建记录保存源码、补丁和两份产物哈希，打包前重新校验。重编产物的哈希可能随编译器和 SDK 改变。未提供 `--qt-build-receipt` 时，独立打包器仍要求原始验证的固定产物哈希。The Qt build receipt records source, patch, and output hashes, which staging rechecks. Compiler and SDK changes can alter rebuilt binary hashes. Without `--qt-build-receipt`, the standalone stager still requires the original verified binary hashes.
