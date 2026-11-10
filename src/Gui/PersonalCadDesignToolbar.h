// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

#include <string_view>
#include <QHash>
#include <QString>

namespace Gui
{
class Application;
class MainWindow;

bool usesPersonalCadDesignToolbar(std::string_view workbench);
QHash<QByteArray, QString> personalCadDesignCommandKeywords(MainWindow& window);
void installPersonalCadDesignToolbar(MainWindow& window, Application& app);
}
