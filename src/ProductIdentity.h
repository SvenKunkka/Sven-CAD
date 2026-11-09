// SPDX-License-Identifier: LGPL-2.1-or-later

#pragma once

namespace ProductIdentity
{
#ifdef FREECAD_PERSONAL_CAD
inline constexpr char executableName[] = "PersonalCAD";
inline constexpr char displayName[] = "个人 CAD";
inline constexpr char desktopId[] = "local.personalcad.PersonalCAD";
#else
inline constexpr char executableName[] = "FreeCAD";
inline constexpr char desktopId[] = "org.freecad.FreeCAD";
#endif
}  // namespace ProductIdentity
