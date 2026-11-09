// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <string_view>

namespace Gui
{
class Application;
class MainWindow;

bool usesPersonalCadDesignToolbar(std::string_view workbench);
void installPersonalCadDesignToolbar(MainWindow& window, Application& app);
}
