// SPDX-License-Identifier: LGPL-2.1-or-later

#ifndef GUI_FUSIONNAVIGATIONSTYLE_H
#define GUI_FUSIONNAVIGATIONSTYLE_H

#include "NavigationStyle.h"

namespace Gui
{

class FusionNavigationStyle: public UserNavigationStyle
{
    TYPESYSTEM_HEADER_WITH_OVERRIDE();

public:
    const char* mouseButtons(ViewerMode mode) override;

protected:
    SbBool processSoEvent(const SoEvent* event) override;

private:
    void setOrbitCenter(const SoEvent* event, bool atCursor);
    ViewerMode middleDragMode(const SoEvent* event) const;
    bool suppressLeftRelease = false;
};

}  // namespace Gui

#endif  // GUI_FUSIONNAVIGATIONSTYLE_H
