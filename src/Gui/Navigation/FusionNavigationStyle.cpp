// SPDX-License-Identifier: LGPL-2.1-or-later

#include <Inventor/events/SoKeyboardEvent.h>
#include <Inventor/events/SoLocation2Event.h>
#include <Inventor/events/SoMotion3Event.h>
#include <Inventor/events/SoMouseButtonEvent.h>
#include <QCoreApplication>

#include "FusionNavigationStyle.h"
#include "View3DInventorViewer.h"

/* TRANSLATOR Gui::FusionNavigationStyle */

using namespace Gui;

TYPESYSTEM_SOURCE(Gui::FusionNavigationStyle, Gui::UserNavigationStyle)

const char* FusionNavigationStyle::mouseButtons(ViewerMode mode)
{
    switch (mode) {
        case SELECTION:
            return QT_TR_NOOP("Press left mouse button");
        case PANNING:
            return QT_TR_NOOP("Drag with the middle mouse button or swipe with two fingers");
        case DRAGGING:
            return QT_TR_NOOP("Hold Shift and drag with the middle mouse button or two fingers");
        case ZOOMING:
            return QT_TR_NOOP("Scroll the mouse wheel or pinch with two fingers");
        default:
            return "";
    }
}

NavigationStyle::ViewerMode FusionNavigationStyle::middleDragMode(const SoEvent* event) const
{
    if (!event->wasShiftDown()) {
        return PANNING;
    }
    // Quarter maps Command on macOS to the Coin Control modifier.
    return event->wasCtrlDown() ? ZOOMING : DRAGGING;
}

void FusionNavigationStyle::setOrbitCenter(const SoEvent* event, bool atCursor)
{
    if (!getCamera()) {
        return;
    }

    const auto previousMode = getRotationCenterMode();
    setRotationCenter(viewer->getFocalPoint());
    if (atCursor) {
        setRotationCenterMode(
            RotationCenterMode::ScenePointAtCursor | RotationCenterMode::FocalPointAtCursor
        );
    }
    else {
        setRotationCenterMode(RotationCenterMode::BoundingBoxCenter);
    }
    saveCursorPosition(event);
    setRotationCenterMode(previousMode);
}

SbBool FusionNavigationStyle::processSoEvent(const SoEvent* event)
{
    if (isSeekMode()) {
        if (currentmode == SEEK_WAIT_MODE
            && SoMouseButtonEvent::isButtonPressEvent(event, SoMouseButtonEvent::BUTTON1)) {
            setViewingMode(SEEK_MODE);
            seekToPoint(event->getPosition());
            return true;
        }
        return UserNavigationStyle::processSoEvent(event);
    }
    if (!isAnimating() && isViewing()) {
        setViewing(false);
    }

    if (!button3down && !viewer->isEditing() && handleEventInForeground(event)) {
        return true;
    }

    const auto pos = event->getPosition();
    const auto normalized = normalizePixelPos(pos);
    const auto previous = lastmouseposition;
    lastmouseposition = normalized;

    if (event->isOfType(SoMouseButtonEvent::getClassTypeId())) {
        const auto* mouse = static_cast<const SoMouseButtonEvent*>(event);
        const bool press = mouse->getState() == SoButtonEvent::DOWN;
        switch (mouse->getButton()) {
            case SoMouseButtonEvent::BUTTON3:
                if (press && button1down
                    && (currentmode == INTERACT || viewer->isEditing())) {
                    break;
                }
                button3down = press;
                if (press) {
                    const auto mode = middleDragMode(event);
                    suppressLeftRelease = button1down;
                    if (mode == DRAGGING) {
                        setOrbitCenter(event, button1down);
                    }
                    clearSelectionStartPosition();
                    setViewingMode(mode);
                }
                else if (currentmode == PANNING || currentmode == DRAGGING
                         || currentmode == ZOOMING) {
                    setViewingMode(IDLE);
                }
                return true;
            case SoMouseButtonEvent::BUTTON1:
                button1down = press;
                if (button3down || (!press && suppressLeftRelease)) {
                    suppressLeftRelease = press;
                    if (press && currentmode == DRAGGING) {
                        setOrbitCenter(event, true);
                        setViewingMode(DRAGGING);
                    }
                    return true;
                }
                suppressLeftRelease = false;
                updateSelectionStartPosition(press, pos);
                setViewingMode(press ? SELECTION : IDLE);
                if (processClickEvent(mouse)) {
                    return true;
                }
                break;
            case SoMouseButtonEvent::BUTTON2:
                if (button3down) {
                    return true;
                }
                if (!press && !viewer->isEditing() && isPopupMenuEnabled()) {
                    openPopupMenu(pos);
                }
                break;
            default:
                break;
        }
    }
    else if (event->isOfType(SoLocation2Event::getClassTypeId())) {
        if (button3down) {
            const auto mode = middleDragMode(event);
            if (mode != currentmode) {
                if (mode == DRAGGING) {
                    setOrbitCenter(event, button1down);
                }
                setViewingMode(mode);
            }
            if (auto* camera = getCamera()) {
                if (mode == PANNING) {
                    panCamera(
                        camera,
                        viewer->getViewportRegion().getViewportAspectRatio(),
                        panningplane,
                        normalized,
                        previous
                    );
                }
                else if (mode == DRAGGING) {
                    // WindowCenter would ignore the pivot chosen for this Fusion mouse orbit.
                    const auto previousMode = getRotationCenterMode();
                    setRotationCenterMode(RotationCenterMode::BoundingBoxCenter);
                    spin_simplified(normalized, previous);
                    setRotationCenterMode(previousMode);
                }
                else {
                    zoomByCursor(normalized, previous);
                }
            }
            return true;
        }
        if (currentmode == SELECTION && button1down) {
            auto mode = currentmode;
            const auto* motion = static_cast<const SoLocation2Event*>(event);
            const bool handled = handleSelectionDragMotion(motion, mode, event->wasCtrlDown());
            if (mode != currentmode) {
                setViewingMode(mode);
            }
            return handled;
        }
    }
    else if (event->isOfType(SoKeyboardEvent::getClassTypeId())) {
        if (processKeyboardEvent(static_cast<const SoKeyboardEvent*>(event))) {
            return true;
        }
    }
    else if (event->isOfType(SoMotion3Event::getClassTypeId())) {
        return processMotionEvent(static_cast<const SoMotion3Event*>(event));
    }

    return UserNavigationStyle::processSoEvent(event);
}
