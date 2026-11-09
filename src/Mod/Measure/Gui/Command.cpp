// SPDX-License-Identifier: LGPL-2.1-or-later

/***************************************************************************
 *   Copyright (c) 2023 David Friedli <david[at]friedli-be.ch>             *
 *                                                                         *
 *   This file is part of FreeCAD.                                         *
 *                                                                         *
 *   FreeCAD is free software: you can redistribute it and/or modify it    *
 *   under the terms of the GNU Lesser General Public License as           *
 *   published by the Free Software Foundation, either version 2.1 of the  *
 *   License, or (at your option) any later version.                       *
 *                                                                         *
 *   FreeCAD is distributed in the hope that it will be useful, but        *
 *   WITHOUT ANY WARRANTY; without even the implied warranty of            *
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU      *
 *   Lesser General Public License for more details.                       *
 *                                                                         *
 *   You should have received a copy of the GNU Lesser General Public      *
 *   License along with FreeCAD. If not, see                               *
 *   <https://www.gnu.org/licenses/>.                                      *
 *                                                                         *
 **************************************************************************/

#include <QApplication>

#include <App/Application.h>
#include <App/Document.h>
#include <Gui/Application.h>
#include <Gui/Command.h>
#include <Gui/Control.h>
#include <Gui/MainWindow.h>
#include <Gui/MDIView.h>
#include <Gui/View3DInventor.h>
#include <Gui/View3DInventorViewer.h>

#include "TaskMeasure.h"
#ifdef FREECAD_PERSONAL_CAD
#include <cmath>
#include <QMessageBox>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopExp_Explorer.hxx>
#include <Base/UnitsApi.h>
#include <Gui/Selection/Selection.h>
#include <Mod/Part/App/PartFeature.h>
#else
#include "TaskMassProperties.h"
#endif

//===========================================================================
// Std_Measure
// this is the Unified Measurement Facility Measure command
//===========================================================================


DEF_STD_CMD_A(StdCmdMeasure)

StdCmdMeasure::StdCmdMeasure()
    : Command("Std_Measure")
{
    sGroup = "Measure";
    sMenuText = QT_TR_NOOP("&Measure");
    sToolTipText = QT_TR_NOOP("Measures a feature");
    sWhatsThis = "Std_Measure";
    sStatusTip = QT_TR_NOOP("Measures a feature");
    sPixmap = "umf-measurement";
}

void StdCmdMeasure::activated(int iMsg)
{
    Q_UNUSED(iMsg);

    MeasureGui::TaskMeasure* task = new MeasureGui::TaskMeasure();
    task->setDocumentName(this->getDocument()->getName());
    Gui::Control().showDialog(task);
}

bool StdCmdMeasure::isActive()
{
    App::Document* doc = App::GetApplication().getActiveDocument();
    if (!doc || doc->countObjectsOfType<App::GeoFeature>() == 0) {
        return false;
    }

    Gui::MDIView* view = Gui::getMainWindow()->activeWindow();
    if (view && view->isDerivedFrom<Gui::View3DInventor>()) {
        Gui::View3DInventorViewer* viewer = static_cast<Gui::View3DInventor*>(view)->getViewer();
        return !viewer->isEditing();
    }
    return false;
}

#ifdef FREECAD_PERSONAL_CAD
DEF_STD_CMD_A(StdCmdMeasureVolume)

StdCmdMeasureVolume::StdCmdMeasureVolume()
    : Command("Std_MeasureVolume")
{
    sGroup = "Measure";
    sMenuText = QT_TR_NOOP("Measure Volume");
    sToolTipText = QT_TR_NOOP("Measure the geometric volume of the selected solid");
    sStatusTip = sToolTipText;
    sPixmap = "Measurement-Volume";
}

void StdCmdMeasureVolume::activated(int iMsg)
{
    Q_UNUSED(iMsg);
    const QString title = QCoreApplication::translate(className(), "Measure Volume");
    const auto selection = Gui::Selection().getSelectionEx(
        nullptr, App::DocumentObject::getClassTypeId(), Gui::ResolveMode::NoResolve, true
    );
    if (selection.empty() || selection.front().getSubNames().size() > 1) {
        return;
    }

    const auto& selected = selection.front();
    const auto& subnames = selected.getSubNames();
    const auto* subname = subnames.empty() ? nullptr : subnames.front().c_str();
    using enum Part::ShapeOption;
    const auto shape = Part::Feature::getShape(selected.getObject(), ResolveLink | Transform, subname);
    if (shape.IsNull() || !TopExp_Explorer(shape, TopAbs_SOLID).More()) {
        QMessageBox::information(
            Gui::getMainWindow(), title, QCoreApplication::translate(className(), "Select one solid.")
        );
        return;
    }

    GProp_GProps properties;
    BRepGProp::VolumeProperties(shape, properties, true);
    const Base::Quantity volume(std::abs(properties.Mass()), Base::Unit::Volume);
    const auto value = QString::fromStdString(Base::UnitsApi::toUnicodeSuperscript(volume.getUserString()));
    QMessageBox::information(
        Gui::getMainWindow(), title, QCoreApplication::translate(className(), "Volume: %1").arg(value)
    );
}

bool StdCmdMeasureVolume::isActive()
{
    return getDocument() && Gui::Selection().hasSelection();
}
#endif

void CreateMeasureCommands()
{
    Gui::CommandManager& rcCmdMgr = Gui::Application::Instance->commandManager();

    auto cmd = new StdCmdMeasure();
    cmd->initAction();
    rcCmdMgr.addCommand(cmd);
#ifdef FREECAD_PERSONAL_CAD
    auto volume = new StdCmdMeasureVolume();
    volume->initAction();
    rcCmdMgr.addCommand(volume);
#endif
}

#ifndef FREECAD_PERSONAL_CAD
DEF_STD_CMD_A(StdCmdMassProperties)

StdCmdMassProperties::StdCmdMassProperties()
    : Command("Std_MassProperties")
{
    sGroup = "MassProperties";
    sMenuText = QT_TR_NOOP("Mass Properties");
    sToolTipText = QT_TR_NOOP("Calculates mass properties of selected objects");
    sWhatsThis = "Std_MassProperties";
    sStatusTip = sToolTipText;
    sPixmap = "MassPropertiesIcon";
}

void StdCmdMassProperties::activated(int iMsg)
{
    Q_UNUSED(iMsg);

    MassPropertiesGui::TaskMassProperties* task = new MassPropertiesGui::TaskMassProperties();
    task->setDocumentName(this->getDocument()->getName());
    Gui::Control().showDialog(task);
}

bool StdCmdMassProperties::isActive()
{
    App::Document* doc = App::GetApplication().getActiveDocument();
    if (!doc || doc->countObjectsOfType<App::GeoFeature>() == 0) {
        return false;
    }

    Gui::MDIView* view = Gui::getMainWindow()->activeWindow();
    if (view && view->isDerivedFrom<Gui::View3DInventor>()) {
        Gui::View3DInventorViewer* viewer = static_cast<Gui::View3DInventor*>(view)->getViewer();
        return !viewer->isEditing();
    }
    return false;
}

void CreateMassPropertiesCommands()
{
    Gui::CommandManager& rcCmdMgr = Gui::Application::Instance->commandManager();

    auto cmd = new StdCmdMassProperties();
    cmd->initAction();
    rcCmdMgr.addCommand(cmd);
}
#endif
