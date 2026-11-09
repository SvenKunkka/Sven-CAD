// SPDX-License-Identifier: LGPL-2.1-or-later

#include "PersonalCadCommands.h"

#include <algorithm>
#include <string>

#include <QCoreApplication>
#include <QDialog>
#include <QFile>
#include <QIcon>
#include <QLineEdit>
#include <QTimer>
#include <QVBoxLayout>

#include <App/Document.h>
#include <App/DocumentObject.h>
#include <App/GeoFeatureGroupExtension.h>

#include "Action.h"
#include "Application.h"
#include "Command.h"
#include "CommandCompleter.h"
#include "Control.h"
#include "Document.h"
#include "MDIView.h"
#include "MainWindow.h"
#include "Selection/Selection.h"
#include "ViewProviderDocumentObject.h"
#include "WorkbenchManager.h"

namespace Gui
{
namespace
{

constexpr const char* extrudeId = "PersonalCAD_Extrude";
constexpr const char* toolboxId = "PersonalCAD_ModelToolbox";

bool isObjectType(const App::DocumentObject* object, const char* typeName)
{
    const auto type = Base::Type::fromName(typeName);
    return object && !type.isBad() && object->isDerivedFrom(type);
}

App::DocumentObject* sketchInEdit(const Document* document)
{
    if (!document) {
        return nullptr;
    }
    auto* provider = dynamic_cast<ViewProviderDocumentObject*>(document->getInEdit());
    auto* object = provider ? provider->getObject() : nullptr;
    return isObjectType(object, "Sketcher::SketchObject") ? object : nullptr;
}

App::DocumentObject* findBody(App::DocumentObject* object)
{
    if (isObjectType(object, "PartDesign::Body")) {
        return object;
    }
    auto* group = object ? App::GeoFeatureGroupExtension::getGroupOfObject(object) : nullptr;
    return isObjectType(group, "PartDesign::Body") ? group : nullptr;
}

bool canExtrude(Document* document)
{
    if (!document) {
        return false;
    }
    if (document->getInEdit()) {
        return sketchInEdit(document) != nullptr;
    }
    return !Control().activeDialog();
}

Command* nativeCommand(const char* id, const char* workbench)
{
    auto* app = Application::Instance;
    auto& manager = app->commandManager();
    auto* command = manager.getCommandByName(id);
    if (!command) {
        app->activateWorkbench(workbench);
        command = manager.getCommandByName(id);
    }
    return command;
}

class ModelingCommand: public Command
{
public:
    ModelingCommand(const char* id, const char* label, const char* tip, const char* icon)
        : Command(id)
    {
        sAppModule = "Gui";
        sGroup = "PersonalCAD";
        sMenuText = label;
        sToolTipText = tip;
        sStatusTip = tip;
        sPixmap = icon;
    }

    const char* className() const override
    {
        return "PersonalCadCommands";
    }
};

class ExtrudeCommand: public ModelingCommand
{
public:
    ExtrudeCommand()
        : ModelingCommand(
              extrudeId,
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Extrude"),
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Extrudes a sketch or profile"),
              "PartDesign_Pad"
          )
    {
        eType = ForEdit;
    }

    bool isActive() override
    {
        return canExtrude(getActiveGuiDocument());
    }

protected:
    void activated(int) override
    {
        auto* document = getActiveGuiDocument();
        auto* appDocument = document->getDocument();
        const std::string documentName = appDocument->getName();
        std::string finishedSketch;
        if (auto* sketch = sketchInEdit(document)) {
            if (sketch->getDocument() != appDocument || !sketch->getNameInDocument()) {
                return;
            }
            finishedSketch = sketch->getNameInDocument();
            Application::Instance->commandManager().runCommandByName("Sketcher_LeaveSketch");
            document = getActiveGuiDocument();
            if (!document || document->getDocument() != appDocument || document->getInEdit()
                || Control().activeDialog()) {
                return;
            }
        }

        App::DocumentObject* profile = nullptr;
        if (!finishedSketch.empty()) {
            profile = appDocument->getObject(finishedSketch.c_str());
            if (!profile) {
                return;
            }
        }
        else {
            auto selection = getSelection().getSelectionEx();
            if (selection.size() == 1) {
                profile = selection.front().getObject();
            }
        }

        auto* body = findBody(profile);
        const bool usePad = body || (!profile && WorkbenchManager::instance()->activeName()
                                                   == "PartDesignWorkbench");
        auto* command = usePad ? nativeCommand("PartDesign_Pad", "PartDesignWorkbench")
                               : nativeCommand("Part_Extrude", "PartWorkbench");
        if (!command || getActiveDocument() != appDocument) {
            return;
        }
        if (!finishedSketch.empty()) {
            getSelection().clearSelection(documentName.c_str());
            if (!getSelection().addSelection(documentName.c_str(), finishedSketch.c_str())) {
                return;
            }
        }
        if (body && body->getDocument() == appDocument) {
            if (auto* view = Application::Instance->activeView()) {
                view->setActiveObject(body, PDBODYKEY);
            }
        }
        command->invoke(0);
    }
};

class PressPullCommand: public ModelingCommand
{
public:
    PressPullCommand()
        : ModelingCommand(
              "PersonalCAD_PressPull",
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Press Pull"),
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Extrudes sketch profiles or fillets edges"),
              "Part_Extrude"
          )
    {
        eType = ForEdit;
    }

    bool isActive() override
    {
        return canExtrude(getActiveGuiDocument());
    }

protected:
    void activated(int) override
    {
        auto& manager = Application::Instance->commandManager();
        if (sketchInEdit(getActiveGuiDocument())) {
            manager.runCommandByName(extrudeId);
            return;
        }

        auto selection = getSelection().getSelectionEx();
        if (selection.size() == 1) {
            auto& selected = selection.front();
            auto* object = selected.getObject();
            if (isObjectType(object, "Part::Part2DObject")) {
                manager.runCommandByName(extrudeId);
                return;
            }

            const auto& elements = selected.getSubNames();
            if (!elements.empty() && std::ranges::all_of(elements, [](const std::string& element) {
                    return element.starts_with("Edge");
                })) {
                auto* body = findBody(object);
                auto* command = body ? nativeCommand("PartDesign_Fillet", "PartDesignWorkbench")
                                     : nativeCommand("Part_Fillet", "PartWorkbench");
                if (body && body->getDocument() == getActiveDocument()) {
                    if (auto* view = Application::Instance->activeView()) {
                        view->setActiveObject(body, PDBODYKEY);
                    }
                }
                if (command) {
                    command->invoke(0);
                }
                return;
            }
        }
        getMainWindow()->showMessage(
            QCoreApplication::translate(
                "PersonalCadCommands",
                "Select a sketch profile or edges from one body. Individual face offset is unavailable."
            ),
            5000
        );
    }
};

class ModelToolboxCommand: public ModelingCommand
{
public:
    ModelToolboxCommand()
        : ModelingCommand(
              toolboxId,
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Model Toolbox"),
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Find and run a command by name"),
              "edit-find"
          )
    {
        eType = ForEdit;
    }

protected:
    void activated(int) override
    {
        auto* mainWindow = getMainWindow();
        auto* dialog = new QDialog(mainWindow);
        dialog->setObjectName(QStringLiteral("PersonalCadModelToolbox"));
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowTitle(QCoreApplication::translate("PersonalCadCommands", "Model Toolbox"));
        auto* layout = new QVBoxLayout(dialog);
        auto* search = new QLineEdit(dialog);
        const auto prompt = QCoreApplication::translate("PersonalCadCommands", "Search commands");
        search->setPlaceholderText(prompt);
        search->setAccessibleName(prompt);
        search->setMinimumWidth(360);
        layout->addWidget(search);
        auto* completer = new CommandCompleter(search, search);
        QObject::connect(completer, &CommandCompleter::commandActivated, dialog,
                         [dialog, mainWindow](const QByteArray& id) {
            if (id.isEmpty() || id == toolboxId) {
                return;
            }
            dialog->accept();
            QTimer::singleShot(0, mainWindow, [id] {
                auto& manager = Application::Instance->commandManager();
                auto* command = manager.getCommandByName(id.constData());
                if (command && command->allowedByMaturity()) {
                    command->initAction();
                    command->testActive();
                    if (command->getAction()->isEnabled()) {
                        command->invoke(0);
                        return;
                    }
                }
                getMainWindow()->showMessage(
                    QCoreApplication::translate("PersonalCadCommands", "Command unavailable in the current context"),
                    4000
                );
            });
        });
        dialog->open();
        search->setFocus();
    }
};

class JointCommand: public ModelingCommand
{
public:
    JointCommand()
        : ModelingCommand(
              "PersonalCAD_Joint",
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Joint"),
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Connect components using a joint type"),
              "Link"
          )
    {}

    bool isActive() override
    {
        if (WorkbenchManager::instance()->activeName() != "AssemblyWorkbench"
            || Control().activeDialog()) {
            return false;
        }
        // The adapter action is created before Assembly's resource is registered.
        if (!assemblyIconLoaded && getAction()) {
            const auto path = QStringLiteral(":/icons/Assembly_CreateJointFixed.svg");
            if (QFile::exists(path)) {
                assemblyIconLoaded = true;
                getAction()->setIcon(QIcon(path));
            }
        }
        auto* view = Application::Instance->activeView();
        auto* assembly = view ? view->getActiveObject<App::DocumentObject*>(ASSEMBLYKEY) : nullptr;
        auto* command = Application::Instance->commandManager().getCommandByName("Assembly_CreateJointFixed");
        return isObjectType(assembly, "Assembly::AssemblyObject") && command && command->isActive();
    }

protected:
    void activated(int) override
    {
        // The native joint task lets the user change joint type before accepting.
        Application::Instance->commandManager().runCommandByName("Assembly_CreateJointFixed");
    }

private:
    bool assemblyIconLoaded = false;
};

}  // namespace

void registerPersonalCadCommands(CommandManager& manager)
{
    manager.addCommand(new ExtrudeCommand());
    manager.addCommand(new PressPullCommand());
    manager.addCommand(new ModelToolboxCommand());
    manager.addCommand(new JointCommand());
}

}  // namespace Gui
