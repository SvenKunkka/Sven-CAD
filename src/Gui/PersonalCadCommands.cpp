// SPDX-License-Identifier: LGPL-2.1-or-later

#include "PersonalCadCommands.h"

#include <algorithm>
#include <memory>
#include <string>
#include <string_view>

#include <QCoreApplication>
#include <QDialog>
#include <QFile>
#include <QIcon>
#include <QLineEdit>
#include <QTimer>
#include <QVBoxLayout>

#include <App/Document.h>
#include <App/DocumentObject.h>
#include <App/DocumentObserver.h>
#include <App/GeoFeatureGroupExtension.h>

#include "Action.h"
#include "Application.h"
#include "Command.h"
#include "CommandCompleter.h"
#include "Control.h"
#include "Document.h"
#include "MDIView.h"
#include "MainWindow.h"
#include "PersonalCadDesignToolbar.h"
#include "Selection/Selection.h"
#include "ViewProviderDocumentObject.h"
#include "Workbench.h"
#include "WorkbenchManager.h"

namespace Gui
{
namespace
{

constexpr const char* extrudeId = "PersonalCAD_Extrude";
constexpr const char* pressPullId = "PersonalCAD_PressPull";
constexpr const char* toolboxId = "PersonalCAD_ModelToolbox";
constexpr const char* recentEditId = "Part_SvenAdjustLastFaceEdit";

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

bool selectsEdges(const std::vector<std::string>& elements)
{
    return !elements.empty() && std::ranges::all_of(elements, [](const std::string& element) {
        return element.starts_with("Edge");
    });
}

QString pressPullSearchTerms(Document* document)
{
    if (sketchInEdit(document)) {
        return QStringLiteral("推拉 拉伸 轮廓 Extrude Profile");
    }
    const auto selection = Selection().getSelectionEx();
    if (selection.size() == 1) {
        const auto& selected = selection.front();
        const auto& elements = selected.getSubNames();
        if (isObjectType(selected.getObject(), "Part::Part2DObject")) {
            return QStringLiteral("推拉 拉伸 轮廓 Extrude Profile");
        }
        if (selectsEdges(elements)) {
            return QStringLiteral("推拉 圆角 Fillet Edge");
        }
        if (elements.size() == 1 && elements.front().starts_with("Face")
            && selected.getObject()
            && selected.getObject()->getTypeId() == Base::Type::fromName("Part::Feature")) {
            return QStringLiteral("推拉 平面偏移 孔径 改径 Offset Face Resize Hole");
        }
        return {};
    }
    const auto references = Selection().getSelectionT(nullptr, ResolveMode::NoResolve);
    const bool reselect = references.size() == 2
        && std::ranges::any_of(references, [](const App::SubObjectT& reference) {
            return !reference.hasSubElement()
                && isObjectType(reference.getSubObject(), "Part::SvenPlanarOffset");
        })
        && std::ranges::any_of(references, [](const App::SubObjectT& reference) {
            return reference.getOldElementName() == "Face"
                && isObjectType(reference.getSubObject(), "Part::Feature");
        });
    return reselect ? QStringLiteral("推拉 重新选面 Reselect Face") : QString();
}

bool commandAvailable(Command* command)
{
    if (!command || !command->allowedByMaturity()) {
        return false;
    }
    command->initAction();
    command->testActive();
    const auto* action = command->getAction();
    return action && action->isEnabled();
}

CommandCompleter::CommandScope toolboxScope(MainWindow& window, Document* document)
{
    auto commands = personalCadDesignCommandKeywords(window);
    if (auto* workbench = WorkbenchManager::instance()->active()) {
        for (const auto& toolbar : workbench->getToolbarItems()) {
            for (const auto& name : toolbar.second) {
                const auto id = QByteArray::fromStdString(name);
                if (!commands.contains(id)) {
                    commands.insert(id, {});
                }
            }
        }
    }
    const bool editingSketch = sketchInEdit(document);
    if (editingSketch) {
        const auto& registered = Application::Instance->commandManager().getCommands();
        for (const auto& [name, command] : registered) {
            const QByteArray id(command->getName());
            if (id.startsWith("Sketcher_") && !commands.contains(id)) {
                commands.insert(id, {});
            }
        }
    }
    commands[extrudeId] += QStringLiteral(" 拉伸 挤出 Pad");
    const auto pressPullTerms = pressPullSearchTerms(document);
    if (!pressPullTerms.isEmpty()) {
        commands[pressPullId] = pressPullTerms;
    }
    commands[recentEditId] = QStringLiteral(" 最近参数 调整上次操作 Adjust Last Operation Edit Last Q");
    commands.remove(toolboxId);
    auto& manager = Application::Instance->commandManager();
    for (auto it = commands.begin(); it != commands.end();) {
        const auto& id = it.key();
        const bool allowedInSketch = id.startsWith("Sketcher_") || id.startsWith("PersonalCAD_Sketch")
            || id.startsWith("Std_View") || id == extrudeId || id == pressPullId;
        if ((editingSketch && !allowedInSketch)
            || (id == pressPullId && pressPullTerms.isEmpty())
            || !commandAvailable(manager.getCommandByName(id))) {
            it = commands.erase(it);
        }
        else {
            ++it;
        }
    }
    return commands;
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
              pressPullId,
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Press Pull"),
              QT_TRANSLATE_NOOP("PersonalCadCommands", "Extrudes profiles, fillets edges, offsets planar faces, or resizes cylindrical holes"),
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
            if (selectsEdges(elements)) {
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
            if (elements.size() == 1 && elements.front().starts_with("Face") && editFace(manager)) {
                return;
            }
        }
        const auto references = getSelection().getSelectionT(nullptr, ResolveMode::NoResolve);
        if (references.size() == 2
            && std::ranges::any_of(references, [](const App::SubObjectT& reference) {
                return isObjectType(reference.getSubObject(), "Part::SvenPlanarOffset");
            }) && editFace(manager)) {
            return;
        }
        getMainWindow()->showMessage(
            QCoreApplication::translate(
                "PersonalCadCommands",
                "Select a sketch profile, edges from one body, or one face on a STEP solid."
            ),
            5000
        );
    }

private:
    static bool editFace(CommandManager& manager)
    {
        constexpr auto command = "Part_SvenPlanarOffset";
        if (!manager.getCommandByName(command)) {
            Command::addModule(Gui, "PartGui");
        }
        if (auto* offset = manager.getCommandByName(command)) {
            offset->invoke(0);
            return true;
        }
        return false;
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
        const auto document = std::make_shared<App::DocumentWeakPtrT>(getActiveDocument());
        const auto workbench = WorkbenchManager::instance()->activeName();
        auto scope = toolboxScope(*mainWindow, getActiveGuiDocument());
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
        auto* completer = new CommandCompleter(search, search, std::move(scope));
        QObject::connect(completer, &CommandCompleter::commandActivated, dialog,
                         [dialog, mainWindow, document, workbench](const QByteArray& id) {
            if (id.isEmpty() || id == toolboxId) {
                return;
            }
            dialog->accept();
            QTimer::singleShot(0, mainWindow, [id, document, workbench] {
                auto& manager = Application::Instance->commandManager();
                auto* command = manager.getCommandByName(id.constData());
                auto* active = Application::Instance->activeDocument();
                if (**document == (active ? active->getDocument() : nullptr)
                    && workbench == WorkbenchManager::instance()->activeName()
                    && (id != pressPullId || !pressPullSearchTerms(active).isEmpty())
                    && commandAvailable(command)) {
                    command->invoke(0);
                    return;
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
