// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

namespace Gui
{

constexpr const char* defaultNavigationStyleName()
{
#ifdef FREECAD_PERSONAL_CAD
    return "Gui::FusionNavigationStyle";
#else
    return "Gui::CADNavigationStyle";
#endif
}

}  // namespace Gui
