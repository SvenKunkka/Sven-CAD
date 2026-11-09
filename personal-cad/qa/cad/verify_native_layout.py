"""Build and run the exact personal layout helpers against Qt offscreen screens."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


INCLUDES = r"""
#include <QApplication>
#include <QCryptographicHash>
#include <QDockWidget>
#include <QMainWindow>
#include <QScreen>
#include <QStringList>
#include <algorithm>
#include <iostream>
#include <sstream>
using MainWindow = QMainWindow;
#define CHECK(x) if (!(x)) { std::cerr << "Failed: " << #x << '\n'; return 1; }
"""

TEST = r"""
int main(int argc, char** argv) {
    QApplication app(argc, argv);
    MainWindow window;
    window.setMinimumSize(300, 200);
    const QRect screen = app.primaryScreen()->availableGeometry();
    CHECK(readLayoutGeometry("-1200 30 800 600  ") == QRect(-1200, 30, 800, 600));
    for (auto value : {"", "0 0 0 700", "0 0 900 -1", "0 0 900 700 trailing",
                       "2147483647 0 900 700", "-2147483648 0 900 700", "0 0 18000000 900"}) {
        CHECK(!readLayoutGeometry(value).isValid());
    }
    CHECK(screen.contains(fitWindowToScreen(window, {})));
    CHECK(screen.contains(fitWindowToScreen(window, QRect(-7000, 4000, 10000, 10000))));
    CHECK(fitWindowToScreen(window, QRect(0, 0, 1, 1)).size() == QSize(300, 200));
    const QRect secondaryRequested(-850, 100, 600, 500);
    const QRect monitorResult = fitWindowToScreen(window, secondaryRequested);
    if (app.screens().size() == 2) {
        CHECK(monitorResult == secondaryRequested);
        CHECK(std::ranges::any_of(app.screens(), [=](auto* display) {
            return display->name() == "External" && display->availableGeometry().contains(monitorResult);
        }));
    } else {
        CHECK(screen.contains(monitorResult));
    }
    auto noDocks = layoutDocks(window);
    auto dock = new QDockWidget("Model", &window);
    dock->setObjectName("Model");
    window.addDockWidget(Qt::LeftDockWidgetArea, dock);
    CHECK(layoutDocks(window) != noDocks);
    const QByteArray valid = window.saveState(layoutVersion);
    const QByteArray digest = layoutDigest(valid);
    CHECK(readLayoutState(valid.toBase64().toStdString(), digest.toStdString()) == valid);
    CHECK(readLayoutState(valid.toBase64().toStdString(), "").isEmpty());
    CHECK(readLayoutState("bad%!", digest.toStdString()).isEmpty());
    CHECK(readLayoutState(valid.first(8).toBase64().toStdString(), digest.toStdString()).isEmpty());
    CHECK(window.restoreState(valid, layoutVersion));
    CHECK(!window.restoreState(valid, layoutVersion + 1));
    CHECK(floatingDocksAreReachable(window));
    dock->setFloating(true);
    dock->resize(340, 240);
    dock->move(-9000, 9000);
    CHECK(!floatingDocksAreReachable(window));
    CHECK(window.restoreState(valid, layoutVersion));
    CHECK(!dock->isFloating());
    window.removeDockWidget(dock);
    delete dock;
    CHECK(layoutDocks(window) == noDocks);
    QWidget detachedView;
    detachedView.showFullScreen();
    app.processEvents();
    CHECK(detachedView.isFullScreen());
    CHECK(window.restoreState(window.saveState(layoutVersion), layoutVersion));
    CHECK(detachedView.isFullScreen());
    std::cout << layoutScreens().constData() << std::endl;
}
"""


def main():
    repo = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    source_path = args.repo / "src/Gui/MainWindow.cpp"
    source = source_path.read_text(encoding="utf-8")
    print(f"{source_path} SHA256 {hashlib.sha256(source_path.read_bytes()).hexdigest()}", flush=True)
    helpers = source.split("constexpr int layoutVersion", 1)[1].split("}  // namespace\n#endif", 1)[0]
    env = dict(os.environ, PKG_CONFIG_PATH=str(prefix / "lib/pkgconfig"))
    flags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "Qt6Widgets"], env=env, text=True
    ))
    version = subprocess.check_output(["pkg-config", "--modversion", "Qt6Widgets"], env=env, text=True)
    print(f"QtWidgets {version.strip()}", flush=True)
    compiler = prefix / "bin/arm64-apple-darwin20.0.0-clang++"
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    primary = {"name": "Main", "x": 0, "y": 0, "width": 1400, "height": 900}
    secondary = {"name": "External", "x": -1100, "y": 0, "width": 1100, "height": 800}
    with tempfile.TemporaryDirectory(prefix="freecad-layout-qt-") as temporary:
        folder = Path(temporary)
        source = folder / "check.cpp"
        executable = folder / "check"
        source.write_text(INCLUDES + "constexpr int layoutVersion" + helpers + TEST, encoding="utf-8")
        command = [str(compiler), "-std=c++23", "-isysroot", sdk, str(source),
                   "-Wl,-rpath," + str(prefix / "lib"), *flags, "-o", str(executable)]
        print(shlex.join(command), flush=True)
        subprocess.run(command, check=True)
        fingerprints = []
        for screens in ([primary, secondary], [primary]):
            config = folder / f"{len(screens)}.json"
            config.write_text(json.dumps({"screens": screens}), encoding="utf-8")
            platform = f"offscreen:configfile={config}"
            fingerprint = subprocess.check_output([str(executable), "-platform", platform], text=True).strip()
            fingerprints.append(fingerprint)
            print(f"{len(screens)} monitor(s): {fingerprint}", flush=True)
        if fingerprints[0] == fingerprints[1]:
            raise RuntimeError("Screen inventory did not change after simulated removal")
    print("Passed: digest, strict geometry, dock inventory/version, floating fallback, detached fullscreen")


if __name__ == "__main__":
    main()
