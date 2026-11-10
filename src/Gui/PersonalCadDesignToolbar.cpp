// SPDX-License-Identifier: LGPL-2.1-or-later

#include "PersonalCadDesignToolbar.h"

#include <array>
#include <initializer_list>

#include <QFile>
#include <QFrame>
#include <QHBoxLayout>
#include <QMenu>
#include <QScrollArea>
#include <QSignalBlocker>
#include <QStyle>
#include <QTabBar>
#include <QTimer>
#include <QToolBar>
#include <QToolButton>
#include <QVBoxLayout>

#include <App/DocumentObject.h>
#include "Action.h"
#include "Application.h"
#include "BitmapFactory.h"
#include "Command.h"
#include "Document.h"
#include "Language/Translator.h"
#include "MainWindow.h"
#include "ViewProviderDocumentObject.h"
#include "Workbench.h"
#include "WorkbenchManager.h"

namespace Gui
{
namespace
{
enum Tab
{
    Solid,
    Surface,
    Assembly,
    Sketch,
    TabCount
};

struct Label
{
    const char* en;
    const char* zh;
};

struct Button
{
    const char* command;
    Label label;
    const char* fallbackIcon = nullptr;
};

const std::array<Label, TabCount> tabLabels {{
    {"Solid", "实体"}, {"Surface", "曲面"}, {"Assembly", "装配"}, {"Sketch", "草图"}
}};
const std::array<const char*, TabCount> workbenches {{
    "PartDesignWorkbench", "SurfaceWorkbench", "AssemblyWorkbench", "SketcherWorkbench"
}};

int designTab(std::string_view name)
{
    if (name == "PartWorkbench") {
        return Solid;
    }
    for (int tab = 0; tab < TabCount; ++tab) {
        if (name == workbenches[tab]) {
            return tab;
        }
    }
    return -1;
}

class DesignToolbar : public QToolBar
{
public:
    DesignToolbar(MainWindow& window, Application& app)
        : QToolBar(&window)
        , mainWindow(window)
        , app(app)
        , host(new QWidget(this))
        , workspace(new QToolButton(host))
        , tabs(new QTabBar(host))
        , scroll(new QScrollArea(host))
    {
        setObjectName(QStringLiteral("PersonalCadDesignToolbar"));
        setMovable(false);
        setFloatable(false);
        setAllowedAreas(Qt::TopToolBarArea);
        toggleViewAction()->setVisible(false);
        host->setObjectName(QStringLiteral("PersonalCadDesignHost"));
        host->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        auto* layout = new QHBoxLayout(host);
        layout->setContentsMargins(8, 3, 8, 4);
        layout->setSpacing(10);
        layout->addWidget(workspace, 0, Qt::AlignVCenter);
        auto* column = new QVBoxLayout;
        column->setContentsMargins(0, 0, 0, 0);
        column->setSpacing(2);
        layout->addLayout(column, 1);

        workspace->setObjectName(QStringLiteral("PersonalCadWorkspaceSelector"));
        workspace->setPopupMode(QToolButton::InstantPopup);
        auto* workspaceMenu = new QMenu(workspace);
        workspace->setMenu(workspaceMenu);
        if (auto* command = app.commandManager().getCommandByName("Std_Workbench")) {
            command->initAction();
            if (auto* group = qobject_cast<WorkbenchGroup*>(command->getAction())) {
                workspaceMenu->addActions(group->getEnabledWbActions());
                connect(group, &WorkbenchGroup::workbenchListRefreshed, workspaceMenu,
                        [workspaceMenu](const QList<QAction*>& actions) {
                            workspaceMenu->clear();
                            workspaceMenu->addActions(actions);
                        });
            }
        }
        tabs->setObjectName(QStringLiteral("PersonalCadDesignTabs"));
        tabs->setExpanding(false);
        tabs->setDrawBase(false);
        for (const auto& label : tabLabels) {
            tabs->addTab(text(label));
        }
        tabs->setTabVisible(Sketch, false);
        column->addWidget(tabs);
        scroll->setObjectName(QStringLiteral("PersonalCadCommandGroups"));
        scroll->viewport()->setObjectName(QStringLiteral("PersonalCadCommandViewport"));
        scroll->setFrameShape(QFrame::NoFrame);
        scroll->setWidgetResizable(true);
        scroll->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        column->addWidget(scroll);
        addWidget(host);

        QFile stylesheet(QStringLiteral("qss:PersonalCADDesign.qss"));
        if (stylesheet.open(QIODevice::ReadOnly)) {
            setStyleSheet(QString::fromUtf8(stylesheet.readAll()));
        }

        update.setSingleShot(true);
        connect(&update, &QTimer::timeout, this, [this] { refresh(); });
        connect(tabs, &QTabBar::currentChanged, this, [this](int tab) { selectTab(tab); });
        connect(&window, &MainWindow::workbenchActivated, this, [this] { update.start(0); });
        inEdit = app.signalInEdit.connect([this](const ViewProviderDocumentObject&) {
            update.start(0);
        });
        resetEdit = app.signalResetEdit.connect([this](const ViewProviderDocumentObject&) {
            update.start(0);
        });
        activeView = app.signalActivateView.connect([this](const MDIView*) { update.start(0); });
        commandsChanged = app.commandManager().signalChanged.connect([this] { update.start(0); });

        window.addToolBar(Qt::TopToolBarArea, this);
        hide();
        update.start(0);
    }

    QHash<QByteArray, QString> commandKeywords()
    {
        refresh();
        return isHidden() ? QHash<QByteArray, QString>() : keywords;
    }

protected:
    void changeEvent(QEvent* event) override
    {
        QToolBar::changeEvent(event);
        if (event->type() == QEvent::LanguageChange) {
            update.start(0);
        }
    }

private:
    static QString text(Label label)
    {
        const bool chinese = Translator::instance()->activeLanguage() == "Chinese (Simplified)";
        return QString::fromUtf8(chinese ? label.zh : label.en);
    }

    bool editingSketch() const
    {
        auto* doc = app.activeDocument();
        auto* provider = doc ? dynamic_cast<ViewProviderDocumentObject*>(doc->getInEdit()) : nullptr;
        auto* object = provider ? provider->getObject() : nullptr;
        const auto type = Base::Type::fromName("Sketcher::SketchObject");
        return object && !type.isBad() && object->getTypeId().isDerivedFrom(type);
    }

    void selectTab(int tab)
    {
        if (tab < 0 || tab >= TabCount) {
            return;
        }
        if (tab == Sketch && !editingSketch()) {
            return;
        }

        // Keep an active sketch intact when looking at the other design pages.
        // The extrusion command can finish the sketch explicitly.
        if (editingSketch()) {
            sketchPage = tab;
        }
        else {
            app.activateWorkbench(tab == Solid && solidPart ? "PartWorkbench" : workbenches[tab]);
        }
        update.start(0);
    }

    void attachShortcutActions()
    {
        // Keep context-sensitive commands available even when their buttons are
        // in a different panel. Some module commands are registered lazily.
        for (const char* name : {"PersonalCAD_Extrude", "PersonalCAD_PressPull",
                                 "PersonalCAD_ModelToolbox", "PersonalCAD_Joint",
                                 "PersonalCAD_SketchLine", "PersonalCAD_SketchRectangle",
                                 "PersonalCAD_SketchCircle", "PersonalCAD_FreeformSelection",
                                 "PersonalCAD_PaintSelection", "PartDesign_Hole",
                                 "PartDesign_Fillet", "Part_ColorPerFace", "Std_Transform",
                                 "Std_BoxSelection", "Std_Measure", "Std_ViewFitAll",
                                 "Std_ToggleVisibility", "Std_Refresh",
                                 "Sketcher_CreateLine", "Sketcher_CreateRectangle",
                                 "Sketcher_CreateCircle", "Sketcher_Trimming", "Sketcher_Offset",
                                 "Sketcher_Projection", "Sketcher_ToggleConstruction",
                                 "Sketcher_Dimension"}) {
            if (auto* command = app.commandManager().getCommandByName(name)) {
                command->initAction();
                if (auto* action = command->getAction()->action(); !host->actions().contains(action)) {
                    host->addAction(action);
                }
            }
        }
    }

    void refresh()
    {
        auto* workbench = WorkbenchManager::instance()->active();
        const std::string name = workbench ? workbench->name() : std::string();
        const int active = designTab(name);
        if (active < 0) {
            hide();
            return;
        }
        if (active == Solid) {
            solidPart = name == "PartWorkbench";
        }
        if (active != Sketch) {
            lastDesignPage = active;
        }
        const bool isEditing = editingSketch();
        if (isEditing && !sketchWasEditing) {
            sketchPage = Sketch;
        }
        const int tab = isEditing ? sketchPage : (active == Sketch ? lastDesignPage : active);
        const auto language = Translator::instance()->activeLanguage();
        sketchWasEditing = isEditing;
        {
            const QSignalBlocker blocker(tabs);
            setWindowTitle(text({"Design", "设计"}));
            workspace->setText(windowTitle());
            workspace->setAccessibleName(text({"Workspace", "工作台"}));
            workspace->setToolTip(text({"Switch workspace", "切换工作台"}));
            tabs->setAccessibleName(windowTitle());
            for (int index = 0; index < TabCount; ++index) {
                tabs->setTabText(index, text(tabLabels[index]));
            }
            tabs->setTabVisible(Sketch, isEditing);
            tabs->setTabEnabled(Assembly, !isEditing);
            tabs->setCurrentIndex(tab);
        }
        const int revision = app.commandManager().getRevision();
        if (shownTab != tab || shownWorkbench != name || shownLanguage != language
            || shownCommandRevision != revision) {
            attachShortcutActions();
            rebuildGroups(tab);
            shownTab = tab;
            shownWorkbench = name;
            shownLanguage = language;
            shownCommandRevision = revision;
        }
        // A restored Qt layout may have saved the ribbon beside a legacy toolbar.
        if (mainWindow.toolBarArea(this) != Qt::TopToolBarArea) {
            mainWindow.addToolBar(Qt::TopToolBarArea, this);
        }
        if (!mainWindow.toolBarBreak(this)) {
            mainWindow.insertToolBarBreak(this);
        }
        show();
    }

    void addButton(QHBoxLayout& row, const Button& spec)
    {
        auto* command = app.commandManager().getCommandByName(spec.command);
        if (!command || !command->allowedByMaturity()) {
            return;
        }
        keywords.insert(spec.command, QStringLiteral("%1 %2").arg(
            QString::fromUtf8(spec.label.en), QString::fromUtf8(spec.label.zh)
        ));
        command->initAction();
        auto* action = command->getAction()->action();
        auto* button = new QToolButton(row.parentWidget());
        button->setObjectName(QString::fromLatin1(spec.command));
        button->setProperty("personalCadRole", "command");
        button->setAutoRaise(true);
        button->setToolButtonStyle(Qt::ToolButtonTextUnderIcon);
        button->setIconSize(QSize(24, 24));
        button->setDefaultAction(action);
        const auto applyLabel = [button, label = text(spec.label), fallback = spec.fallbackIcon] {
            button->setText(label);
            button->setAccessibleName(label);
            if (fallback && button->icon().isNull()) {
                button->setIcon(BitmapFactory().iconFromTheme(fallback));
            }
        };
        applyLabel();
        connect(action, &QAction::changed, button, applyLabel);
        row.addWidget(button);
    }

    void group(QHBoxLayout& ribbon, Label title, std::initializer_list<Button> pins,
               std::initializer_list<const char*> more = {})
    {
        auto* panel = new QWidget(ribbon.parentWidget());
        auto* column = new QVBoxLayout(panel);
        column->setContentsMargins(5, 0, 5, 0);
        column->setSpacing(0);
        auto* row = new QHBoxLayout;
        row->setContentsMargins(0, 0, 0, 0);
        row->setSpacing(2);
        column->addLayout(row, 1);
        auto* menuButton = new QToolButton(panel);
        auto* menu = new QMenu(menuButton);
        auto append = [&](const char* name) {
            if (auto* command = app.commandManager().getCommandByName(name);
                command && command->allowedByMaturity()) {
                if (!keywords.contains(name)) {
                    keywords.insert(name, {});
                }
                command->addTo(menu);
            }
        };
        for (const auto& button : pins) {
            addButton(*row, button);
            append(button.command);
        }
        if (!menu->isEmpty() && more.size() > 0) {
            menu->addSeparator();
        }
        for (const char* name : more) {
            append(name);
        }
        if (menu->isEmpty()) {
            delete panel;
            return;
        }

        if (ribbon.count() > 0) {
            auto* divider = new QFrame(ribbon.parentWidget());
            divider->setObjectName(QStringLiteral("PersonalCadGroupDivider"));
            divider->setFixedWidth(1);
            ribbon.addWidget(divider);
        }
        menuButton->setObjectName(QStringLiteral("PersonalCadGroup_%1").arg(QString::fromLatin1(title.en)));
        menuButton->setText(text(title));
        menuButton->setAccessibleName(menuButton->text());
        menuButton->setProperty("personalCadRole", "group");
        menuButton->setAutoRaise(true);
        menuButton->setMenu(menu);
        menuButton->setPopupMode(QToolButton::InstantPopup);
        menuButton->setSizePolicy(QSizePolicy::MinimumExpanding, QSizePolicy::Fixed);
        column->addWidget(menuButton);
        ribbon.addWidget(panel);
    }

    void rebuildGroups(int tab);

    MainWindow& mainWindow;
    Application& app;
    QWidget* host;
    QToolButton* workspace;
    QTabBar* tabs;
    QScrollArea* scroll;
    QHash<QByteArray, QString> keywords;
    QTimer update;
    fastsignals::scoped_connection inEdit;
    fastsignals::scoped_connection resetEdit;
    fastsignals::scoped_connection activeView;
    fastsignals::scoped_connection commandsChanged;
    std::string shownWorkbench;
    std::string shownLanguage;
    int shownTab = -1;
    int shownCommandRevision = -1;
    int lastDesignPage = Solid;
    int sketchPage = Sketch;
    bool sketchWasEditing = false;
    bool solidPart = false;
};

void DesignToolbar::rebuildGroups(int tab)
{
    keywords.clear();
    if (auto* old = scroll->takeWidget()) {
        old->deleteLater();
    }
    auto* page = new QWidget;
    page->setObjectName(QStringLiteral("PersonalCadCommandPage"));
    auto* row = new QHBoxLayout(page);
    row->setContentsMargins(0, 0, 0, 0);
    row->setSpacing(0);
    const bool part = solidPart || !app.commandManager().getCommandByName("PartDesign_Pad");
    const char* newSketch = part ? "Sketcher_NewSketch" : "PartDesign_NewSketch";

    if (tab == Solid) {
        if (part) {
            group(*row, {"Create", "创建"},
                  {{newSketch, {"Create Sketch", "创建草图"}},
                   {"PersonalCAD_Extrude", {"Extrude", "拉伸"}},
                   {"Part_Revolve", {"Revolve", "旋转"}},
                   {"PartDesign_ExternalThread", {"External Thread", "外螺纹"}}},
                  {"Part_Primitives", "Part_Sweep", "Part_Loft", "Part_Mirror",
                   "PartDesign_Hole", "PartDesign_LinearPattern"});
        }
        else {
            group(*row, {"Create", "创建"},
                  {{newSketch, {"Create Sketch", "创建草图"}},
                   {"PersonalCAD_Extrude", {"Extrude", "拉伸"}},
                   {"PartDesign_Revolution", {"Revolve", "旋转"}},
                   {"PartDesign_Hole", {"Hole", "孔"}},
                   {"PartDesign_ExternalThread", {"External Thread", "外螺纹"}},
                   {"PartDesign_LinearPattern", {"Pattern", "阵列"}}},
                  {"PartDesign_Body", "PartDesign_Pocket", "PartDesign_Groove",
                   "PartDesign_AdditivePipe", "PartDesign_AdditiveLoft",
                   "PartDesign_SubtractivePipe", "PartDesign_SubtractiveLoft",
                   "PartDesign_CircularPattern", "PartDesign_PathPattern",
                   "PartDesign_Mirrored", "PartDesign_CompPrimitiveAdditive",
                   "PartDesign_CompPrimitiveSubtractive"});
        }
        group(*row, {"Modify", "修改"},
              {{"PersonalCAD_PressPull", {"Press Pull", "推拉"}},
               {part ? "Part_Fillet" : "PartDesign_Fillet", {"Fillet", "圆角"}},
               {part ? "Part_Thickness" : "PartDesign_Thickness", {"Shell", "抽壳"}},
               {part ? "Part_Boolean" : "PartDesign_Boolean", {"Combine", "组合"}},
               {"Std_Transform", {"Move", "移动"}, "Std_TransformManip"}},
              {part ? "Part_Chamfer" : "PartDesign_Chamfer", "PartDesign_Draft",
               "Part_Slice", "Part_Scale", "Std_Alignment", "Part_ColorPerFace",
               "Part_RefineShape"});
        group(*row, {"Assemble", "装配"},
              {{"PersonalCAD_Joint", {"Joint", "联接"}, "Link"}},
              {"Assembly_CreateAssembly", "Assembly_InsertLink", "Assembly_InsertNewPart",
               "Assembly_CreateJointRigidGroup", "Assembly_ToggleGrounded"});
    }
    else if (tab == Surface) {
        group(*row, {"Create", "创建"},
              {{newSketch, {"Create Sketch", "创建草图"}},
               {"Part_Extrude", {"Extrude", "拉伸"}},
               {"Part_Revolve", {"Revolve", "旋转"}},
               {"Surface_Filling", {"Patch", "填补"}},
               {"Part_Offset", {"Offset", "偏移"}}},
              {"Part_Sweep", "Part_Loft", "Surface_GeomFillSurface", "Surface_Sections",
               "Part_RuledSurface", "Surface_BlendCurve"});
        group(*row, {"Modify", "修改"},
              {{"Surface_ExtendFace", {"Extend", "延伸"}},
               {"Std_Transform", {"Move", "移动"}, "Std_TransformManip"}},
              {"Part_ReverseShape", "Part_Scale", "Part_Slice", "Part_ColorPerFace"});
    }
    else if (tab == Assembly) {
        group(*row, {"Insert", "插入"},
              {{"Assembly_InsertLink", {"Insert Component", "插入零件"}}}, {"Std_Import"});
        group(*row, {"Create", "创建"},
              {{"Assembly_CreateAssembly", {"New Assembly", "新建装配"}},
               {"Assembly_InsertNewPart", {"New Component", "新建零件"}}});
        group(*row, {"Relationships", "关系"},
              {{"PersonalCAD_Joint", {"Joint", "联接"}, "Link"},
               {"Assembly_CreateJointRigidGroup", {"Rigid Group", "刚性组"}},
               {"Assembly_ToggleGrounded", {"Ground", "固定"}}},
              {"Assembly_CreateJointFixed", "Assembly_CreateJointRevolute",
               "Assembly_CreateJointCylindrical", "Assembly_CreateJointSlider",
               "Assembly_CreateJointBall", "Assembly_CreateJointDistance",
               "Assembly_CreateJointParallel", "Assembly_CreateJointPerpendicular",
               "Assembly_CreateJointAngle"});
        group(*row, {"Modify", "修改"}, {{"Std_Transform", {"Move", "移动"}, "Std_TransformManip"}}, {"Std_Alignment"});
    }
    else {
        group(*row, {"Create", "创建"},
              {{"Sketcher_CreateLine", {"Line", "直线"}},
               {"Sketcher_CreateRectangle", {"Rectangle", "矩形"}},
               {"Sketcher_CreateCircle", {"Circle", "圆"}},
               {"Sketcher_CreateBSplineByInterpolation", {"Spline", "样条曲线"}},
               {"Sketcher_Symmetry", {"Mirror", "镜像"}},
               {"Sketcher_Dimension", {"Dimension", "尺寸"}}},
              {"Sketcher_CreatePolyline", "Sketcher_CompCreateArc",
               "Sketcher_CreateRectangle_Center", "Sketcher_CompCreateConic",
               "Sketcher_CreatePoint", "Sketcher_CreateSlot", "Sketcher_CreateText",
               "Sketcher_Projection", "Sketcher_Intersection"});
        group(*row, {"Modify", "修改"},
              {{"Sketcher_CreateFillet", {"Fillet", "圆角"}},
               {"Sketcher_Trimming", {"Trim", "修剪"}},
               {"Sketcher_Offset", {"Offset", "偏移"}}},
              {"Sketcher_CreateChamfer", "Sketcher_Extend", "Sketcher_Split",
               "Sketcher_Translate", "Sketcher_Rotate", "Sketcher_Scale",
               "Sketcher_ToggleConstruction"});
        group(*row, {"Constraints", "约束"},
              {{"Sketcher_ConstrainHorVer", {"Horizontal/Vertical", "水平／垂直"}},
               {"Sketcher_ConstrainCoincidentUnified", {"Coincident", "重合"}},
               {"Sketcher_ConstrainTangent", {"Tangent", "相切"}}},
              {"Sketcher_ConstrainEqual", "Sketcher_ConstrainParallel",
               "Sketcher_ConstrainPerpendicular", "Sketcher_ConstrainBlock",
               "Sketcher_ConstrainSymmetric"});
    }

    if (tab != Sketch) {
        group(*row, {"Construct", "构造"},
              {{"PartDesign_Plane", {"Plane", "基准面"}}},
              {"PartDesign_Line", "PartDesign_Point", "PartDesign_CoordinateSystem"});
    }
    if (tab == Assembly) {
        group(*row, {"Inspect", "检查"},
              {{"Std_Measure", {"Measure", "测量"}}, {"Assembly_CreateBom", {"BOM", "物料清单"}}},
              {"Std_ToggleClipPlane", "Part_CheckGeometry"});
    }
    else {
        group(*row, {"Inspect", "检查"},
              {{"Std_Measure", {"Measure", "测量"}},
               {"Std_ToggleClipPlane", {"Section", "剖面"}}},
              {"Part_CheckGeometry", "Std_ViewFitAll", "Std_ViewFitSelection"});
    }
    if (tab == Sketch) {
        group(*row, {"Insert", "插入"}, {{"Sketcher_Projection", {"Project", "投影"}}}, {"Std_Import"});
    }
    else if (tab != Assembly) {
        group(*row, {"Insert", "插入"}, {{"Std_Import", {"Import", "导入"}}});
    }
    group(*row, {"Select", "选择"},
          {{"Std_BoxSelection", {"Window", "框选"}},
           {"PersonalCAD_FreeformSelection", {"Freeform", "自由框选"}},
           {"PersonalCAD_PaintSelection", {"Paint", "涂抹选择"}}},
          {"Std_BoxElementSelection", "Std_SelectAll", "Std_SelectVisibleObjects"});
    if (tab == Sketch) {
        addButton(*row, {"Sketcher_LeaveSketch", {"Finish Sketch", "完成草图"}});
    }
    row->addStretch(1);

    // Keep every panel reachable on small screens without enlarging the main window.
    const int height = page->sizeHint().height();
    const int scrollbar = scroll->style()->pixelMetric(QStyle::PM_ScrollBarExtent, nullptr, scroll);
    scroll->setFixedHeight(height + scrollbar);
    scroll->setWidget(page);
    scroll->setAccessibleName(text(tabLabels[tab]));
}

}  // namespace

bool usesPersonalCadDesignToolbar(std::string_view workbench)
{
    return designTab(workbench) >= 0;
}

QHash<QByteArray, QString> personalCadDesignCommandKeywords(MainWindow& window)
{
    auto* toolbar = window.findChild<QToolBar*>(QStringLiteral("PersonalCadDesignToolbar"));
    if (auto* design = dynamic_cast<DesignToolbar*>(toolbar)) {
        return design->commandKeywords();
    }
    return {};
}

void installPersonalCadDesignToolbar(MainWindow& window, Application& app)
{
    new DesignToolbar(window, app);
}

}  // namespace Gui
