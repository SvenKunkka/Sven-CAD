# FreeCAD 外部 AX 验证工具

探针对指定的真实进程调用 Apple Accessibility API。它只观察状态，不发送按键，不改变选择，不修改权限。只把它用于测试进程和合成样件。AX 标签匹配按完整字符串进行。日志会记录匹配的预期标签、属性和选中数量。

## 编译与验证

```sh
sh build.sh
python3 verify.py
```

验证脚本会短暂显示一个合成 AppKit 表格，在退出时关闭。覆盖参数错误、目标不存在、正确选中、标签不匹配、无目标视图、目标中途退出和空选择。若当前执行环境没有辅助功能权限，脚本记录该状态并以 77 退出，实际 GUI 查询记为跳过。

## 对单个 FreeCAD 进程采样

```sh
.build/ax-probe --pid "$freecad_pid" --expect "AX Probe Sentinel" \
  --samples 100 --interval-ms 100 --role AXTable --window-title "AX Probe Table" > round-A1.jsonl
```

请先在独立的 FreeCAD 测试进程准备一个确实选中该标签的表格或模型树，再由样件驱动在该标签保持选中时更换邻近对象。可省略窗口标题，按角色查找进程的窗口。模型树可尝试 `--role AXOutline`，或省略角色让探针查找这两种视图。存在稳定的 AXIdentifier 时可用 `--identifier` 将查询限制在该视图。

若未找到合成测试视图，用 `.build/ax-probe --pid "$freecad_pid" --window-title "AX Probe Table" --discover` 查询这个窗口下的有限角色和标识。发现模式只输出视图诊断，验收需要另跑选中项查询。

每轮探针先确认 PID 与进程启动标识，并通过 `AXIsProcessTrusted()` 检查权限。各样本通过 `AXRows` 和首行的 `AXChildren` 尝试填充 Qt 的原生辅助功能元素，再发出真正的 `AXSelectedChildren` 查询。要求结果中恰好有一个元素，并在该元素或少量子元素的 Title、Value 或 Description 读到完全匹配的标签。暖缓存结果另有字段，可用来复核专项 Qt 用例是否走到预期路径。

- 退出码 0 表示达到正向匹配次数。默认要求每个样本都匹配。
- 退出码 1 表示空选中、错误选中、无窗口、无支持的视图、扫描预算用尽或 AX 调用失败，详细状态见样本与汇总。
- 退出码 64 表示参数错误，69 表示目标不存在、退出、PID 复用或无法验证进程身份，77 表示辅助功能权限不足，74 表示证据输出失败。
- `--min-valid` 可降低试跑时的匹配门槛。对比时保留每条未匹配样本，并在有预期选择的验收批次使用默认严格门槛。进程或权限错误始终中断本轮。

探针证据须与样件动作日志、应用实际载入的 Cocoa 库标识、崩溃报告和文档重开结果联合验收。原生合成表格用于检验探针自身。FreeCAD A/B 用例还需要实际发生 Qt 对象变更和外部查询交错。

参考 [Apple AXUIElementCopyAttributeValue](https://developer.apple.com/documentation/applicationservices/1462085-axuielementcopyattributevalue) 与 [Qt 上游回归测试](https://github.com/qt/qtbase/commit/b74d6cb3c5e8418602494ae549b60839d88ef858)。
