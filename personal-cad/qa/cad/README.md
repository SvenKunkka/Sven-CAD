# Personal CAD QA fixtures

From the repository root, run the fixture generator against a local macOS GUI application bundle. Use a new evidence directory for each run.

```sh
python3 personal-cad/qa/cad/run_fixtures.py --app /absolute/path/to/FreeCAD.app \
  --output /absolute/path/to/evidence/run-01 --mode baseline
```

The runner starts the bundle executable with isolated FreeCAD settings, cache and temporary paths. It records the Cocoa plugin actually mapped into the process and exits nonzero if FreeCAD crashes, times out, or fails a fixture assertion. The macro writes `report.json` with geometric metrics, constraints, placements, external links and SHA-256 for the generated files. A file hash identifies evidence from one run. Model contents and geometry are the comparison baseline across runs.

| Fixture | Recorded checks |
|---|---|
| `keyboard-housing.FCStd` | Fully constrained outlines, expression-driven pocket, width/length/depth geometry and volume before and after parameter edit, object types and constraints after reopen |
| `keyboard-colored.step` | One solid, unchanged bounds and volume, and identical per-face color distribution after importing into a new document |
| `keyboard-assembly.FCStd` | Cross-document housing link and fixed lid joint, solver displacement and solved position, matching state after closing and reopening |
| `keyboard-drawing.{pdf,svg,dxf}` | Projection and section visible edges plus actual file output |
| `legacy-mixed.FCStd`, `legacy-link-source.FCStd` | Spreadsheet-driven feature, Mesh feature and external link, with matching saved and reopened object sets |

Run Personal CAD against its own bundle and the earlier baseline's synthetic legacy artifacts.

```sh
python3 personal-cad/qa/cad/run_fixtures.py --app /absolute/path/to/PersonalCAD.app \
  --output /absolute/path/to/evidence/personal-01 --mode personal \
  --legacy-source /absolute/path/to/evidence/baseline-01
```

Personal mode verifies the same positive CAD workflows, its separate identity and settings, seven CAD workbenches, and the absence of the checked excluded modules. It verifies static BOM counting, reopening, preview, and CSV export. It checks that mesh import and STL export are refused and that CAD tessellation still works.

The compatibility checks use synthetic copies. They verify the mixed legacy diagnostic and unchanged hashes, migrate a stock material UUID while preserving the solid and face colors, and verify that a missing assembly link leads to safe refusal or blocks Save, Save As and Save Copy.

## External AX mutation driver

Run one GUI fixture at a time. Use the paired external probe in `personal-cad/qa/ax` to query the target PID from `ready.json`.

To automate one cold launch and 100 strict queries, first build the native probe with `personal-cad/qa/ax/build.sh`.

```sh
python3 personal-cad/qa/cad/run_ax_round.py --app /absolute/path/to/FreeCAD.app \
  --probe personal-cad/qa/ax/.build/ax-probe --output /absolute/path/to/evidence/ax-01
```

The round runner exits nonzero on either probe failure or application failure. For manual investigation, start the fixture separately.

```sh
python3 personal-cad/qa/cad/run_fixtures.py --app /absolute/path/to/FreeCAD.app \
  --output /absolute/path/to/evidence/ax-01 --scenario ax-table --duration 120
```

For a test Cocoa plugin, add `--cocoa-plugin-path /absolute/path/to/plugins/platforms`. For a separate Qt Widgets build, add `--widgets-library-path /absolute/path/to/widgets/lib`. The runner records each loaded path and SHA-256 and rejects any requested library that dyld did not map. Keep other libraries out of the Widgets directory so that the comparison replaces only that image.

The isolated `ax-table` window is named `AX Probe Table`, its accessible identifier is `PersonalCadAxProbeTable`, and its selected row is `AX Probe Sentinel`. Start the external reader, then create the empty `start-mutations` file in the evidence directory. The driver alternates between two and three rows while retaining the selection. `mutations.jsonl` records the time and internal selection after each edit. Create `stop` to stop cleanly. Preserve both external probe logs and driver output. A successful driver report only proves the internal operations completed.

Use `--scenario ax-model-tree` for a real FreeCAD document with the same sentinel. It adds/deletes adjacent parts, recomputes, undoes and closes/reopens a saved fixture. The document is synthetic and kept inside the evidence directory. `--scenario ax-model-tree-root` selects the top-level document row as a control using Qt's selection model, because FreeCAD's object selection represents only the document's contents.

Use `--scenario shutdown` to create a synthetic FreeCAD notification and quit while it is visible. The final `run-result.json` requires a clean process exit, even if the macro's `report.json` confirms the notification was visible before quitting. This scenario isolates the separate notification shutdown crash.

## Qt Widgets selection experiments

Apply the three tree patches in this order to a separate copy of the locked Qt 6.11.2 source with the Cocoa lifetime patch already applied.

1. `QT-AX-nested-tree-selection.patch` resolves selected model indexes in visible tree order.
2. `QT-AX-tree-selection-after-mutations.patch` reindexes cached tree cells and checks their model index before reuse.
3. `QT-AX-retain-tree-selection-on-layout.patch` preserves still-valid accessible items when Qt sends a layout reset.

Build the actual arm64 Widgets dylib with the project under `personal-cad/toolchain/qt-cocoa/widgets-C-project`. Use the same locked `reference-prefix` for Qt Core, Gui, private headers and build tools. Keep the Cocoa A/B builds separate.

```sh
cmake -S /absolute/path/to/widgets-C-project -B /absolute/path/to/widgets-build -G Ninja \
  -DCMAKE_PREFIX_PATH=/absolute/path/to/reference-prefix \
  -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0 \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_UNITY_BUILD=ON -DCMAKE_UNITY_BUILD_BATCH_SIZE=8 \
  -DQTBASE_SOURCE=/absolute/path/to/qtbase-source
cmake --build /absolute/path/to/widgets-build --target Widgets --parallel 3
```

The test project at `personal-cad/toolchain/qt-cocoa/test-project` compiles the native Qt accessibility tests against the locked reference libraries. With the Cocoa B plugin, run `treeViewTest` and `tableViewSelectionAfterModelChange` once with the reference Widgets and once with the candidate `DYLD_LIBRARY_PATH` set to the candidate's `lib` directory. `DYLD_PRINT_LIBRARIES=1` records the actual test mappings. Then run the external FreeCAD model-tree scenario with both exact candidate paths. Keep the complete output from each variant.


## Full acceptance and standalone diagnostics

Run the full signed-bundle matrix on an Apple Silicon Mac after generating the
synthetic baseline with the first command above. The two-hour soak is the default.

```sh
sh personal-cad/qa/ax/build.sh
python3 personal-cad/qa/cad/accept_personal.py \
  --app /path/to/PersonalCAD.app --output /path/to/evidence/acceptance \
  --legacy-source /path/to/evidence/baseline-01 \
  --probe personal-cad/qa/ax/.build/ax-probe
```

`verify_legacy_preflight.py --help` describes the native compiler and baseline
library inputs. `verify_native_layout.py --prefix /path/to/locked/pixi/environment`
checks synthetic monitor changes. The two optional FreeCADCmd probes require an
isolated profile. Set `ASSEMBLY_QA_REPORT` to a new result path for
`assembly_cli_probe.py`. For `native_cli_probe.py`, set `PERSONALCAD_QA_FIXTURES`
to the synthetic baseline output and `PERSONALCAD_QA_OUTPUT` to a new output
directory. The latter probe applies and restores a process-local file-size limit.
